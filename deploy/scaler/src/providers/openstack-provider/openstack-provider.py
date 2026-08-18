#!/usr/bin/env python

import json
import logging
import time
from collections import defaultdict
from ipaddress import ip_address, ip_network
from concurrent.futures import ThreadPoolExecutor, as_completed

import openstack

from manageInstance import ManageInstance

logger = logging.getLogger(__name__)

_MAX_PARALLEL_WORKERS = 10


def _isNotFoundError(exc):
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status == 404:
        return True
    if "notfound" in type(exc).__name__.lower():
        return True
    return "not found" in str(exc).lower()


class OpenstackProvider(ManageInstance):

    def __init__(self, profile):
        self.profile = profile
        self.conn = None
        self.instName = None
        self.instType = {}
        self.ami = None
        self.network = None
        self.subnet = None
        self._primarySubnetCidr = None
        # Interface selection for the "primary" NIC.
        # Values can be either Neutron network names or subnet names.
        # If `interface_1.priv` is set -> VM fixed IP comes from this.
        # If `interface_1.pub` is set -> floating IP is allocated from this.
        self.interface1PubRef = None
        self.interface1PrivRef = None
        self.secuGrp = {}
        self.keyPair = None
        self.userData = ""
        self._flavorVcpuCache = {}
        self.deleteVolumesOnDestroy = False

    def _connect(self, profileCfg):
        cfg = profileCfg if profileCfg else {}

        if cfg.get("client_id") and cfg.get("client_secret"):
            return openstack.connection.Connection(
                auth={
                    "auth_url": cfg.get("auth_url"),
                    "application_credential_id": cfg.get("client_id"),
                    "application_credential_secret": cfg.get("client_secret"),
                },
                auth_type="v3applicationcredential",
                region_name=cfg.get("region"),
                identity_interface=cfg.get("interface", "public"),
            )

        auth = {}
        for key in [
            "auth_url",
            "username",
            "password",
            "project_name",
            "project_id",
            "user_domain_name",
            "user_domain_id",
            "project_domain_name",
            "project_domain_id",
        ]:
            if cfg.get(key) is not None:
                auth[key] = cfg.get(key)

        kwargs = {
            "auth": auth if auth else None,
            "region_name": cfg.get("region"),
            "identity_interface": cfg.get("interface", "public"),
        }
        return openstack.connection.Connection(**{k: v for k, v in kwargs.items() if v is not None})

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def configureInstance(self, configFile, initData):
        with open(configFile) as f:
            instConfig = json.load(f)

        required = ["name", "instance_image", "instance_type_by_cpu_num"]
        missing = [k for k in required if k not in instConfig]
        if missing:
            raise ValueError("Missing required config keys: {}".format(missing))

        if self.conn is None:
            profileCfg = instConfig.get("profile", {}).get(self.profile, {})
            self.conn = self._connect(profileCfg)

        self.instName = instConfig["name"]
        self.instType = instConfig["instance_type_by_cpu_num"]
        self.ami = instConfig["instance_image"]
        self.network = instConfig.get("network")
        self.subnet = instConfig.get("subnet")
        iface1 = instConfig.get("interface_1", {}) or {}
        self.interface1PrivRef = iface1.get("priv") or iface1.get("priv_subnet")
        self.interface1PubRef = iface1.get("pub") or iface1.get("pub_subnet")
        self.secuGrp = instConfig.get("security_group", {})
        self.keyPair = instConfig.get("key_pair")
        self.deleteVolumesOnDestroy = bool(instConfig.get("delete_volumes_on_destroy", False))
        self.userData = ""
        self._primarySubnetCidr = None
        logger.info(
            "Volume deletion on destroy is %s",
            "ENABLED" if self.deleteVolumesOnDestroy else "DISABLED",
        )

        self._resolvePrimarySubnetCidr()

        scriptCfg = instConfig.get("user_data", {}).get("script", {})
        self.userData += "\n".join(scriptCfg.get("common", []))

        if "sip" in initData:
            sipCfg = instConfig.get("user_data", {})

            sipRegistrar = None
            if "sip_registrar" in sipCfg:
                sipRegistrar = sipCfg["sip_registrar"].get("priv") or sipCfg["sip_registrar"].get("pub")
            if "registrar" not in initData["sip"]:
                initData["sip"]["registrar"] = sipRegistrar

            outboundProxy = None
            if "outbound_proxy" in sipCfg:
                outboundProxy = sipCfg["outbound_proxy"].get("priv") or sipCfg["outbound_proxy"].get("pub")
            if "proxy" not in initData["sip"]:
                initData["sip"]["proxy"] = outboundProxy

            turnSrv = None
            if "turn_server" in sipCfg:
                turnSrv = sipCfg["turn_server"].get("priv") or sipCfg["turn_server"].get("pub")
            if "turn" not in initData["sip"]:
                initData["sip"]["turn"] = turnSrv

        for act in initData:
            actionScript = scriptCfg.get(act, [])
            if not actionScript:
                continue
            self.userData += "\n"
            try:
                self.userData += "\n".join(actionScript).format_map(
                    defaultdict(str, initData[act])
                )
            except (KeyError, ValueError, IndexError) as exc:
                logger.error(
                    "Failed to render user_data script for action '%s': %s", act, exc
                )
                raise

    def _resolvePrimarySubnetCidr(self):
        """Best-effort: derive the subnet CIDR for the primary fixed IP."""
        primaryRef = self.interface1PrivRef or self.interface1PubRef
        if primaryRef:
            try:
                sub = self.conn.network.find_subnet(primaryRef)
                if sub and getattr(sub, "cidr", None):
                    self._primarySubnetCidr = sub.cidr
                    return
            except Exception as exc:
                logger.warning("Cannot resolve subnet '%s': %s", primaryRef, exc)

            if not self.network:
                try:
                    net = self.conn.network.find_network(primaryRef)
                    if net:
                        self.network = getattr(net, "name", None) or primaryRef
                except Exception as exc:
                    logger.warning("Cannot resolve network '%s': %s", primaryRef, exc)

        if self._primarySubnetCidr is None and self.subnet:
            try:
                sub = self.conn.network.find_subnet(self.subnet)
                if sub and getattr(sub, "cidr", None):
                    self._primarySubnetCidr = sub.cidr
            except Exception as exc:
                logger.warning("Cannot resolve subnet '%s': %s", self.subnet, exc)

    def _getServerIps(self, server):
        privIp = None
        pubIp = None
        addresses = server.addresses or {}
        primarySubnetCidr = self._primarySubnetCidr

        for netName, addrs in addresses.items():
            for addr in addrs:
                ipAddr = addr.get("addr") or addr.get("address")
                ipType = addr.get("OS-EXT-IPS:type") or addr.get("type")
                if ipType == "floating":
                    pubIp = ipAddr
                elif ipType == "fixed":
                    if not ipAddr:
                        continue
                    if primarySubnetCidr:
                        try:
                            if ip_address(ipAddr) in ip_network(primarySubnetCidr, strict=False):
                                privIp = ipAddr
                        except (ValueError, TypeError) as exc:
                            logger.debug("CIDR match failed for %s in %s: %s", ipAddr, primarySubnetCidr, exc)
                    elif not self.network or netName == self.network:
                        privIp = ipAddr
        return privIp, pubIp

    def _getServerVcpus(self, server):
        flavorInfo = getattr(server, "flavor", None) or {}
        if not flavorInfo:
            return 0

        # Some OpenStack versions (microversion 2.47+) embed vcpus in the
        # server detail response, so we can skip the extra API call.
        if flavorInfo.get("vcpus"):
            return int(flavorInfo["vcpus"])

        flavorRef = flavorInfo.get("id") or flavorInfo.get("original_name")
        if not flavorRef:
            return 0

        if flavorRef not in self._flavorVcpuCache:
            try:
                flv = self.conn.compute.find_flavor(flavorRef)
                self._flavorVcpuCache[flavorRef] = int(getattr(flv, "vcpus", 0) or 0) if flv else 0
            except Exception as exc:
                logger.warning("Cannot resolve vCPUs for flavor '%s': %s", flavorRef, exc)
                self._flavorVcpuCache[flavorRef] = 0
        return self._flavorVcpuCache[flavorRef]

    def _isManagedServer(self, server):
        """Return True if the server was created by this scaler (name prefix match)."""
        if not self.instName:
            return True
        serverName = getattr(server, "name", None) or ""
        return serverName.startswith(self.instName)

    def enumerateInstances(self):
        if not self.conn:
            return []

        appSg = self.secuGrp.get("app") if isinstance(self.secuGrp, dict) else None
        instDict = []
        for server in self.conn.compute.servers(details=True):
            try:
                if (server.status or "").upper() != "ACTIVE":
                    continue

                if not self._isManagedServer(server):
                    continue

                if appSg:
                    sgNames = set()
                    for sg in server.security_groups or []:
                        if isinstance(sg, dict):
                            sgNames.add(sg.get("name"))
                        else:
                            sgNames.add(sg)
                    if appSg not in sgNames:
                        continue

                privIp, pubIp = self._getServerIps(server)
                startTime = getattr(server, "created_at", None) or getattr(server, "created", None)
                cpuCnt = self._getServerVcpus(server)

                instDict.append(
                    {
                        "start": startTime,
                        "addr": {"priv": privIp, "pub": pubIp},
                        "cpu_count": int(cpuCnt),
                    }
                )
            except Exception as exc:
                logger.warning("Skipping server %s during enumeration: %s", getattr(server, "id", "?"), exc)
        return instDict

    def _buildServersByIpIndex(self):
        """Load all servers once and build an IP -> server lookup dict (managed VMs only)."""
        index = {}
        for server in self.conn.compute.servers(details=True):
            if not self._isManagedServer(server):
                continue
            for addrs in (server.addresses or {}).values():
                for addr in addrs:
                    ipAddr = addr.get("addr") or addr.get("address")
                    if ipAddr:
                        index[ipAddr] = server
        return index

    def _findServerByIp(self, targetIp):
        for server in self.conn.compute.servers(details=True):
            if not self._isManagedServer(server):
                continue
            addresses = server.addresses or {}
            for addrs in addresses.values():
                for addr in addrs:
                    ipAddr = addr.get("addr") or addr.get("address")
                    if ipAddr == targetIp:
                        return server
        return None

    def _resolvePortIdForFixedIp(self, server, fixedIp):
        """
        Best-effort: find the Neutron port on `server` that owns `fixedIp`.
        """
        if not fixedIp or not server or not getattr(server, "id", None):
            return None

        try:
            ports_iter = self.conn.network.ports(device_id=server.id)
        except Exception as exc:
            logger.warning(
                "Cannot list ports filtered by device_id=%s, skipping port resolution: %s",
                server.id, exc,
            )
            return None

        try:
            for port in ports_iter:
                fixedIps = getattr(port, "fixed_ips", None) or []
                for fi in fixedIps:
                    if isinstance(fi, dict):
                        ipAddr = (
                            fi.get("ip_address")
                            or fi.get("ip")
                            or fi.get("address")
                        )
                    else:
                        ipAddr = fi
                    if ipAddr == fixedIp:
                        return port.id
        except Exception as exc:
            logger.warning("Error iterating ports for server %s: %s", server.id, exc)
            return None

        return None

    def _resolveFloatingNetworkId(self):
        if self.interface1PubRef:
            try:
                sub = self.conn.network.find_subnet(self.interface1PubRef)
                if sub:
                    return sub.network_id
            except Exception as exc:
                logger.debug("interface1PubRef '%s' is not a subnet: %s", self.interface1PubRef, exc)
            try:
                net = self.conn.network.find_network(self.interface1PubRef)
                if net:
                    return net.id
            except Exception as exc:
                logger.warning("Cannot resolve floating network from '%s': %s", self.interface1PubRef, exc)
                return None

        if self.subnet:
            try:
                sub = self.conn.network.find_subnet(self.subnet)
                if sub:
                    return sub.network_id
            except Exception as exc:
                logger.warning("Cannot resolve floating network from subnet '%s': %s", self.subnet, exc)

        if self.network:
            try:
                net = self.conn.network.find_network(self.network)
                if net:
                    return net.id
            except Exception as exc:
                logger.warning("Cannot resolve floating network from network '%s': %s", self.network, exc)

        return None

    def _buildPrimaryNic(self):
        """
        Build the NIC list for the primary interface.
        Preference:
          1) interface_1.priv (fixed IP)
          2) interface_1.pub (if priv is missing)
          3) legacy network/subnet
        Returns a list of one OpenStack networks entry for `create_server()`.
        """
        primaryRef = self.interface1PrivRef or self.interface1PubRef

        if primaryRef:
            try:
                subnet = self.conn.network.find_subnet(primaryRef)
                if subnet:
                    return [{"net-id": subnet.network_id, "v4-fixed-ip": None}]
            except Exception as exc:
                logger.debug("primaryRef '%s' is not a subnet: %s", primaryRef, exc)
            try:
                net = self.conn.network.find_network(primaryRef)
                if net:
                    return [{"uuid": net.id}]
            except Exception as exc:
                logger.warning("Cannot resolve primaryRef '%s' as network: %s", primaryRef, exc)

        if self.subnet:
            try:
                subnet = self.conn.network.find_subnet(self.subnet)
                if subnet:
                    return [{"net-id": subnet.network_id, "v4-fixed-ip": None}]
            except Exception as exc:
                logger.warning("Cannot resolve legacy subnet '%s': %s", self.subnet, exc)

        if self.network:
            try:
                net = self.conn.network.find_network(self.network)
                if net:
                    return [{"uuid": net.id}]
            except Exception as exc:
                logger.warning("Cannot resolve legacy network '%s': %s", self.network, exc)

        return []

    def _createServerOnly(self, numCPU, gigaRAM, name=None):
        """Create a server without waiting for ACTIVE or floating IP allocation."""
        flavorName = self.instType.get(str(numCPU), {}).get(str(gigaRAM))
        if not flavorName:
            raise RuntimeError("No instance type for {} vCPU / {} GiB".format(numCPU, gigaRAM))

        flavor = self.conn.compute.find_flavor(flavorName)
        image = self.conn.compute.find_image(self.ami)
        if not flavor:
            raise RuntimeError("Flavor not found: {}".format(flavorName))
        if not image:
            raise RuntimeError("Image not found: {}".format(self.ami))

        nics = self._buildPrimaryNic()
        if not nics:
            raise RuntimeError("No primary NIC could be built (check interface_1/ network/ subnet config)")

        secGroups = []
        if isinstance(self.secuGrp, dict):
            for key in ["admin", "app"]:
                sg = self.secuGrp.get(key)
                if sg:
                    secGroups.append({"name": sg})

        serverName = "{}.{}".format(self.instName, name) if name else self.instName
        server = self.conn.compute.create_server(
            name=serverName,
            image_id=image.id,
            flavor_id=flavor.id,
            networks=nics,
            security_groups=secGroups if secGroups else None,
            key_name=self.keyPair,
            user_data=self.userData,
            metadata={"cpu_count": str(numCPU)},
        )
        return server.id

    def _associateFloatingIp(self, server, privIp, requestedPubIp=None):
        """
        Associate or allocate a floating IP for `server`.
        If `requestedPubIp` is given, try to attach that specific IP.
        Returns the public IP string or None.
        """
        pubIp = requestedPubIp

        if not pubIp:
            fipNetId = self._resolveFloatingNetworkId()
            if not fipNetId:
                return None
            portId = self._resolvePortIdForFixedIp(server, privIp)
            if portId:
                fip = self.conn.network.create_ip(
                    floating_network_id=fipNetId, port_id=portId
                )
                return fip.floating_ip_address
            else:
                fip = self.conn.network.create_ip(floating_network_id=fipNetId)
                pubIp = fip.floating_ip_address
                if pubIp:
                    self.conn.compute.add_floating_ip_to_server(server, pubIp)
                return pubIp

        if privIp:
            portId = self._resolvePortIdForFixedIp(server, privIp)
            if portId:
                try:
                    fip = self.conn.network.find_ip(pubIp)
                    if fip:
                        self.conn.network.update_ip(fip, port_id=portId)
                except Exception as exc:
                    logger.warning("Failed to attach FIP %s via port update, falling back: %s", pubIp, exc)
                    try:
                        self.conn.compute.add_floating_ip_to_server(server, pubIp)
                    except Exception as exc2:
                        logger.error("Failed to attach FIP %s via legacy API: %s", pubIp, exc2)
        return pubIp

    def createInstance(self, numCPU, gigaRAM, name=None, ip=None):
        if not self.conn:
            raise RuntimeError("Provider not configured")

        serverId = self._createServerOnly(numCPU, gigaRAM, name)

        try:
            server = self.conn.compute.wait_for_server(
                self.conn.compute.get_server(serverId)
            )
        except Exception as exc:
            logger.error("Server %s failed to become ACTIVE, cleaning up: %s", serverId, exc)
            try:
                self.conn.compute.delete_server(serverId, ignore_missing=True)
            except Exception as cleanup_exc:
                logger.error("Failed to cleanup server %s: %s", serverId, cleanup_exc)
            raise

        server = self.conn.compute.get_server(server.id)
        privIp, actualPubIp = self._getServerIps(server)
        pubIp = ip or actualPubIp

        if not pubIp or (ip and ip != actualPubIp):
            try:
                pubIp = self._associateFloatingIp(server, privIp, ip)
            except Exception as exc:
                logger.error("Floating IP allocation/association failed for %s: %s", serverId, exc)

        server = self.conn.compute.get_server(server.id)
        privIp, actualPubIp = self._getServerIps(server)
        if actualPubIp:
            pubIp = actualPubIp

        logger.info(
            "Created Instance: %s, %s, %s, %sVCPUs, %sG",
            server.id, privIp, pubIp, numCPU, gigaRAM,
        )
        return {"id": server.id, "ip": pubIp}

    def _ensureFloatingIp(self, server, privIp):
        """
        Ensure `server` has a floating IP.
        Returns the floating IP address if present/allocated, otherwise None.
        """
        if not server:
            return None

        _, actualPubIp = self._getServerIps(server)
        if actualPubIp:
            return actualPubIp

        fipNetId = self._resolveFloatingNetworkId()
        if not fipNetId:
            return None

        try:
            portId = self._resolvePortIdForFixedIp(server, privIp)
            if portId:
                fip = self.conn.network.create_ip(floating_network_id=fipNetId, port_id=portId)
                return getattr(fip, "floating_ip_address", None)

            fip = self.conn.network.create_ip(floating_network_id=fipNetId)
            pubIp = getattr(fip, "floating_ip_address", None)
            if pubIp:
                self.conn.compute.add_floating_ip_to_server(server, pubIp)
            return pubIp
        except Exception as exc:
            logger.warning("Failed to ensure floating IP for server %s: %s", getattr(server, "id", "?"), exc)
            return None

    def createInstancesParallel(self, count, numCPU, gigaRAM, name=None, timeoutSeconds=None):
        if not self.conn:
            raise RuntimeError("Provider not configured")
        if count <= 0:
            return []

        timeout = int(timeoutSeconds if timeoutSeconds is not None else 300)
        maxWorkers = min(max(1, int(count)), _MAX_PARALLEL_WORKERS)
        createdIds = []
        results = []
        failures = 0

        with ThreadPoolExecutor(max_workers=maxWorkers) as executor:
            baseName = name
            futures = [
                executor.submit(
                    self._createServerOnly,
                    numCPU,
                    gigaRAM,
                    ("{}-{}".format(baseName, i) if baseName else None),
                )
                for i in range(count)
            ]
            for fut in as_completed(futures):
                try:
                    createdIds.append(fut.result())
                except Exception as err:
                    failures += 1
                    logger.error("Create request failed: %s", err)

        if not createdIds:
            return []

        startTime = time.time()
        pending = set(createdIds)
        while pending and (time.time() - startTime) < timeout:
            doneNow = []
            for serverId in list(pending):
                try:
                    server = self.conn.compute.get_server(serverId)
                except Exception as exc:
                    logger.debug("Cannot poll server %s: %s", serverId, exc)
                    continue
                status = (getattr(server, "status", "") or "").upper()
                if status == "ACTIVE":
                    doneNow.append(serverId)
                    privIp, actualPubIp = self._getServerIps(server)
                    pubIp = actualPubIp

                    if not pubIp:
                        pubIp = self._ensureFloatingIp(server, privIp)

                    try:
                        server = self.conn.compute.get_server(serverId)
                        _, actualPubIp2 = self._getServerIps(server)
                        if actualPubIp2:
                            pubIp = actualPubIp2
                    except Exception as exc:
                        logger.debug("FIP refresh failed for %s: %s", serverId, exc)

                    logger.info(
                        "Created Instance: %s, %s, %s, %sVCPUs, %sG",
                        serverId, privIp, pubIp, numCPU, gigaRAM,
                    )
                    results.append({"id": serverId, "ip": pubIp})
                elif status == "ERROR":
                    doneNow.append(serverId)
                    failures += 1
                    logger.error("Server %s in ERROR state", serverId)
                    try:
                        self.conn.compute.delete_server(serverId, ignore_missing=True)
                        logger.info("Server %s deleted after ERROR state", serverId)
                    except Exception as err:
                        logger.error("Failed to delete server %s after ERROR: %s", serverId, err)
            for serverId in doneNow:
                pending.discard(serverId)
            if pending:
                time.sleep(5)

        if pending:
            for serverId in list(pending):
                failures += 1
                try:
                    self.conn.compute.delete_server(serverId, ignore_missing=True)
                except Exception as exc:
                    logger.error("Failed to cleanup timed-out server %s: %s", serverId, exc)
                logger.warning("Server %s creation timed out after %ss", serverId, timeout)

        logger.info(
            "OpenStack batch create done: requested=%s, started=%s, active=%s, failed=%s",
            count, len(createdIds), len(results), failures,
        )
        return results

    def _getServerVolumeIds(self, server):
        """Return a list of volume IDs attached to the server."""
        volumeIds = []
        try:
            attached = getattr(server, "attached_volumes", None) or []
            for vol in attached:
                volId = vol.get("id") if isinstance(vol, dict) else getattr(vol, "id", None)
                if volId:
                    volumeIds.append(volId)
        except Exception as exc:
            logger.warning(
                "Cannot list attached volumes for server %s: %s",
                getattr(server, "id", "?"), exc,
            )
        if not volumeIds:
            try:
                for attachment in self.conn.compute.volume_attachments(server):
                    volId = getattr(attachment, "volume_id", None)
                    if volId:
                        volumeIds.append(volId)
            except Exception as exc:
                logger.warning(
                    "Cannot list volume attachments for server %s: %s",
                    getattr(server, "id", "?"), exc,
                )
        logger.debug("Server %s has volumes: %s", getattr(server, "id", "?"), volumeIds)
        return volumeIds

    def _deleteVolumes(self, volumeIds):
        """Delete a list of volumes, waiting for them to become available."""
        uniqueIds = list(dict.fromkeys(volumeIds))
        if not uniqueIds:
            logger.info("No attached volumes queued for deletion")
            return

        logger.info("Starting volume deletion for %s volume(s): %s", len(uniqueIds), uniqueIds)
        remaining = list(uniqueIds)
        deleted = []
        alreadyGone = []
        failed = []

        maxAttempts = 6
        for attempt in range(maxAttempts):
            if not remaining:
                break
            if attempt > 0:
                time.sleep(10)
            logger.info(
                "Volume deletion attempt %s/%s for %s pending volume(s)",
                attempt + 1, maxAttempts, len(remaining),
            )
            stillPending = []
            for volId in remaining:
                try:
                    vol = self.conn.block_storage.find_volume(volId, ignore_missing=True)
                    if not vol:
                        logger.info("Volume %s already absent (treated as deleted)", volId)
                        alreadyGone.append(volId)
                        continue
                    status = (getattr(vol, "status", "") or "").lower()
                    if status == "in-use":
                        logger.info(
                            "Volume %s still in-use on attempt %s/%s, will retry",
                            volId, attempt + 1, maxAttempts,
                        )
                        stillPending.append(volId)
                        continue
                    self.conn.block_storage.delete_volume(volId, ignore_missing=True)
                    logger.info("Delete request sent for volume %s (status=%s)", volId, status or "unknown")
                    deleted.append(volId)
                except Exception as exc:
                    if _isNotFoundError(exc):
                        logger.info("Volume %s already absent (treated as deleted)", volId)
                        alreadyGone.append(volId)
                        continue
                    if attempt < (maxAttempts - 1):
                        logger.warning(
                            "Volume %s delete failed on attempt %s/%s, retrying: %s",
                            volId, attempt + 1, maxAttempts, exc,
                        )
                        stillPending.append(volId)
                    else:
                        logger.error(
                            "Volume %s delete failed after %s attempts: %s",
                            volId, maxAttempts, exc,
                        )
                        failed.append(volId)
            remaining = stillPending

        if remaining:
            for volId in remaining:
                if volId not in failed:
                    failed.append(volId)

        logger.info(
            "Volume deletion summary: requested=%s, deleted=%s, already_deleted=%s, failed=%s",
            len(uniqueIds), len(deleted), len(alreadyGone), len(failed),
        )
        if failed:
            logger.warning("Volume deletion failed for IDs: %s", failed)

    def destroyInstances(self, ipList):
        if not self.conn:
            return

        serversByIp = self._buildServersByIpIndex()
        pendingVolumeIds = []
        logger.info(
            "Destroy requested for %s IP(s); managed servers indexed=%s; volume deletion=%s",
            len(ipList or []),
            len(serversByIp),
            "ENABLED" if self.deleteVolumesOnDestroy else "DISABLED",
        )

        for ip in ipList:
            if not ip:
                logger.warning("destroyInstances called with None/empty IP, skipping")
                continue

            instanceId = None
            pubIp = None
            privIp = None
            server = serversByIp.get(ip)

            if not server:
                logger.warning("No server found for IP %s, skipping", ip)
                continue

            instanceId = server.id
            privIp, pubIp = self._getServerIps(server)

            try:
                isPrivate = ip_address(ip).is_private
            except (ValueError, TypeError):
                isPrivate = False

            if isPrivate:
                privIp = ip
            else:
                pubIp = ip

            if self.deleteVolumesOnDestroy and server:
                volumeIds = self._getServerVolumeIds(server)
                pendingVolumeIds.extend(volumeIds)
                logger.info(
                    "Collected %s volume(s) for server %s before deletion: %s",
                    len(volumeIds), instanceId, volumeIds,
                )

            if server and pubIp:
                try:
                    self.conn.compute.remove_floating_ip_from_server(server, pubIp)
                except Exception as exc:
                    logger.debug("Failed to disassociate FIP %s from server %s: %s", pubIp, instanceId, exc)

            if server:
                try:
                    self.conn.compute.delete_server(server, ignore_missing=True)
                except Exception as exc:
                    logger.error("Failed to delete server %s: %s", instanceId, exc)

            if pubIp:
                try:
                    fip = self.conn.network.find_ip(pubIp)
                    if fip:
                        self.conn.network.delete_ip(fip)
                except Exception as exc:
                    logger.warning("Failed to release floating IP %s: %s", pubIp, exc)

            logger.info("Deleted Instance: %s, %s, %s", instanceId, privIp, pubIp)

        if self.deleteVolumesOnDestroy and pendingVolumeIds:
            self._deleteVolumes(pendingVolumeIds)
        elif self.deleteVolumesOnDestroy:
            logger.info("Volume deletion enabled but no attached volumes were collected")
        else:
            logger.info("Volume deletion skipped because delete_volumes_on_destroy is disabled")