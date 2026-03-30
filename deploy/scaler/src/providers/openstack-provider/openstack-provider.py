#!/usr/bin/env python

import json
import time
from ipaddress import ip_address, ip_network
from concurrent.futures import ThreadPoolExecutor, as_completed

import openstack

from manageInstance import ManageInstance


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
        # Optional second private NIC for admin traffic.
        # Mapped to OpenStack Neutron network/subnet names from provider config.
        self.adminNetwork = None
        self.adminSubnet = None
        self.secuGrp = {}
        self.keyPair = None
        self.userData = ""
        self._flavorVcpuCache = {}

    def _connect(self, profileCfg):
        cfg = profileCfg if profileCfg else {}

        # OVH/OpenStack application credentials support.
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

    def configureInstance(self, configFile, initData):
        with open(configFile) as f:
            instConfig = json.load(f)

        profileCfg = instConfig.get("profile", {}).get(self.profile, {})
        self.conn = self._connect(profileCfg)

        self.instName = instConfig.get("name")
        self.instType = instConfig.get("instance_type_by_cpu_num", {})
        self.ami = instConfig.get("instance_image")
        self.network = instConfig.get("network")
        self.subnet = instConfig.get("subnet")
        iface1 = instConfig.get("interface_1", {}) or {}
        # Accept either "priv"/"pub" keys or explicit names "priv_subnet"/"pub_subnet".
        self.interface1PrivRef = iface1.get("priv") or iface1.get("priv_subnet")
        self.interface1PubRef = iface1.get("pub") or iface1.get("pub_subnet")
        self.adminNetwork = instConfig.get("admin_network")
        self.adminSubnet = instConfig.get("admin_subnet")
        self.secuGrp = instConfig.get("security_group", {})
        self.keyPair = instConfig.get("key_pair")
        self.userData = ""
        self._primarySubnetCidr = None

        # Best-effort: derive the subnet CIDR for the "primary" fixed IP.
        # This makes _getServerIps() behave like the Outscale provider which
        # relies on a single SubnetId.
        primaryRef = self.interface1PrivRef or self.interface1PubRef
        if primaryRef:
            try:
                sub = self.conn.network.find_subnet(primaryRef)
                if sub and getattr(sub, "cidr", None):
                    self._primarySubnetCidr = sub.cidr
            except Exception:
                pass

            # If we couldn't resolve a subnet CIDR (primaryRef may be a network name),
            # fall back to using self.network (may still be helpful for fixed IPs).
            if self._primarySubnetCidr is None and not self.network:
                try:
                    net = self.conn.network.find_network(primaryRef)
                    if net:
                        self.network = getattr(net, "name", None) or primaryRef
                except Exception:
                    pass

        if self._primarySubnetCidr is None and self.subnet:
            try:
                sub = self.conn.network.find_subnet(self.subnet)
                if sub and getattr(sub, "cidr", None):
                    self._primarySubnetCidr = sub.cidr
            except Exception:
                pass

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
            self.userData += "\n".join(actionScript).format(**initData[act])

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
                        except Exception:
                            # If CIDR parsing fails, ignore and fall back to net-name matching.
                            pass
                    elif not self.network or netName == self.network:
                        privIp = ipAddr
        return privIp, pubIp

    def _getServerVcpus(self, server):
        flavorId = None
        if getattr(server, "flavor", None):
            flavorId = server.flavor.get("id")
        if not flavorId:
            return 0

        if flavorId not in self._flavorVcpuCache:
            flv = self.conn.compute.get_flavor(flavorId)
            self._flavorVcpuCache[flavorId] = int(getattr(flv, "vcpus", 0) or 0)
        return self._flavorVcpuCache[flavorId]

    def enumerateInstances(self):
        if not self.conn:
            return []

        appSg = self.secuGrp.get("app") if isinstance(self.secuGrp, dict) else None
        instDict = []
        for server in self.conn.compute.servers(details=True):
            if (server.status or "").upper() != "ACTIVE":
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
        return instDict

    def _findServerByIp(self, targetIp):
        for server in self.conn.compute.servers(details=True):
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
        We use it to attach the floating IP to the private interface (like Outscale).
        """
        if not fixedIp or not server or not getattr(server, "id", None):
            return None

        try:
            ports_iter = self.conn.network.ports(device_id=server.id)
        except Exception:
            ports_iter = self.conn.network.ports()

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
        except Exception:
            return None

        return None

    def _resolveFloatingNetworkId(self):
        # Floating IP allocation expects a floating network id.
        # We resolve it from `interface_1.pub` if provided (subnet preferred, fallback to network),
        # otherwise keep legacy behavior (`subnet`/`network`).
        if self.interface1PubRef:
            try:
                sub = self.conn.network.find_subnet(self.interface1PubRef)
                return sub.network_id if sub else None
            except Exception:
                # Might be a network name instead of a subnet name.
                pass
            try:
                net = self.conn.network.find_network(self.interface1PubRef)
                return net.id if net else None
            except Exception:
                return None

        if self.subnet:
            try:
                sub = self.conn.network.find_subnet(self.subnet)
                return sub.network_id if sub else None
            except Exception:
                pass

        if self.network:
            try:
                net = self.conn.network.find_network(self.network)
                return net.id if net else None
            except Exception:
                pass

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
            # If it's a subnet name: use net-id + fixed-ip allocation.
            try:
                subnet = self.conn.network.find_subnet(primaryRef)
                if subnet:
                    return [{"net-id": subnet.network_id, "v4-fixed-ip": None}]
            except Exception:
                pass
            # If it's a network name: attach using uuid.
            try:
                net = self.conn.network.find_network(primaryRef)
                if net:
                    return [{"uuid": net.id}]
            except Exception:
                pass

        # Legacy behavior fallback.
        if self.subnet:
            subnet = self.conn.network.find_subnet(self.subnet)
            if subnet:
                return [{"net-id": subnet.network_id, "v4-fixed-ip": None}]

        if self.network:
            net = self.conn.network.find_network(self.network)
            if net:
                return [{"uuid": net.id}]

        return []

    def createInstance(self, numCPU, gigaRAM, name=None, ip=None):
        if not self.conn:
            raise RuntimeError("Provider not configured")

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

        # Optional admin NIC.
        if self.adminSubnet:
            admin_subnet = self.conn.network.find_subnet(self.adminSubnet)
            if admin_subnet:
                nics.append({"net-id": admin_subnet.network_id, "v4-fixed-ip": None})
            else:
                raise RuntimeError("Admin subnet not found: {}".format(self.adminSubnet))
        elif self.adminNetwork:
            admin_net = self.conn.network.find_network(self.adminNetwork)
            if admin_net:
                nics.append({"uuid": admin_net.id})
            else:
                raise RuntimeError("Admin network not found: {}".format(self.adminNetwork))

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
        server = self.conn.compute.wait_for_server(server)
        server = self.conn.compute.get_server(server.id)

        privIp, actualPubIp = self._getServerIps(server)
        pubIp = ip or actualPubIp

        # Allocate/associate floating IP to the port that matches privIp.
        if not pubIp:
            fipNetId = self._resolveFloatingNetworkId()
            if fipNetId:
                portId = self._resolvePortIdForFixedIp(server, privIp)
                if portId:
                    fip = self.conn.network.create_ip(
                        floating_network_id=fipNetId, port_id=portId
                    )
                    pubIp = fip.floating_ip_address
                else:
                    fip = self.conn.network.create_ip(floating_network_id=fipNetId)
                    pubIp = fip.floating_ip_address
                    if pubIp:
                        self.conn.compute.add_floating_ip_to_server(server, pubIp)
        elif privIp:
            # If the caller provided a floating IP, try to associate it to the private port.
            portId = self._resolvePortIdForFixedIp(server, privIp)
            if portId:
                try:
                    fip = self.conn.network.find_ip(pubIp)
                    if fip:
                        self.conn.network.update_ip(fip, port_id=portId)
                except Exception:
                    # Fallback to the older behavior.
                    try:
                        self.conn.compute.add_floating_ip_to_server(server, pubIp)
                    except Exception:
                        pass

        server = self.conn.compute.get_server(server.id)
        privIp, actualPubIp = self._getServerIps(server)
        if actualPubIp:
            pubIp = actualPubIp

        print(
            "Created Instance: {}, {}, {}, {}VCPUs, {}G".format(
                server.id, privIp, pubIp, numCPU, gigaRAM
            ),
            flush=True,
        )
        return {"id": server.id, "ip": pubIp}

    def _createServerOnly(self, numCPU, gigaRAM, name=None):
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

        # Optional admin NIC.
        if self.adminSubnet:
            admin_subnet = self.conn.network.find_subnet(self.adminSubnet)
            if admin_subnet:
                nics.append({"net-id": admin_subnet.network_id, "v4-fixed-ip": None})
            else:
                raise RuntimeError("Admin subnet not found: {}".format(self.adminSubnet))
        elif self.adminNetwork:
            admin_net = self.conn.network.find_network(self.adminNetwork)
            if admin_net:
                nics.append({"uuid": admin_net.id})
            else:
                raise RuntimeError("Admin network not found: {}".format(self.adminNetwork))

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

    def createInstancesParallel(self, count, numCPU, gigaRAM, name=None, timeoutSeconds=None):
        if not self.conn:
            raise RuntimeError("Provider not configured")
        if count <= 0:
            return []

        timeout = int(timeoutSeconds if timeoutSeconds is not None else 300)
        maxWorkers = max(1, int(count))
        createdIds = []
        results = []
        failures = 0

        # Dispatch creation requests in parallel.
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
                    print("Create request failed: {}".format(err), flush=True)

        if not createdIds:
            return []

        startTime = time.time()
        pending = set(createdIds)
        readyIds = []
        while pending and (time.time() - startTime) < timeout:
            doneNow = []
            for serverId in list(pending):
                try:
                    server = self.conn.compute.get_server(serverId)
                except Exception:
                    continue
                status = (getattr(server, "status", "") or "").upper()
                if status == "ACTIVE":
                    doneNow.append(serverId)
                    readyIds.append(serverId)
                elif status == "ERROR":
                    doneNow.append(serverId)
                    failures += 1
                    print("Server {} in ERROR state".format(serverId), flush=True)
                    # If the VM failed to boot, immediately clean it up so it
                    # doesn't keep consuming compute resources.
                    try:
                        self.conn.compute.delete_server(serverId, ignore_missing=True)
                        print(
                            "Server {} deleted after ERROR state".format(serverId),
                            flush=True,
                        )
                    except Exception as err:
                        print(
                            "Failed to delete server {} after ERROR: {}".format(serverId, err),
                            flush=True,
                        )
            for serverId in doneNow:
                pending.discard(serverId)
            if pending:
                time.sleep(5)

        # Cleanup timed-out servers.
        if pending:
            for serverId in list(pending):
                failures += 1
                try:
                    self.conn.compute.delete_server(serverId, ignore_missing=True)
                except Exception:
                    pass
                print("Server {} creation timed out after {}s".format(serverId, timeout), flush=True)

        # Attach floating IPs for ACTIVE instances.
        for serverId in readyIds:
            server = self.conn.compute.get_server(serverId)
            pubIp = None
            privIp, actualPubIp = self._getServerIps(server)
            pubIp = actualPubIp

            if not pubIp:
                fipNetId = self._resolveFloatingNetworkId()
                if fipNetId:
                    portId = self._resolvePortIdForFixedIp(server, privIp)
                    if portId:
                        fip = self.conn.network.create_ip(
                            floating_network_id=fipNetId, port_id=portId
                        )
                        pubIp = fip.floating_ip_address
                    else:
                        fip = self.conn.network.create_ip(floating_network_id=fipNetId)
                        pubIp = fip.floating_ip_address
                        if pubIp:
                            self.conn.compute.add_floating_ip_to_server(server, pubIp)

            server = self.conn.compute.get_server(serverId)
            privIp, actualPubIp = self._getServerIps(server)
            if actualPubIp:
                pubIp = actualPubIp

            print(
                "Created Instance: {}, {}, {}, {}VCPUs, {}G".format(
                    server.id, privIp, pubIp, numCPU, gigaRAM
                ),
                flush=True,
            )
            results.append({"id": server.id, "ip": pubIp})

        print(
            "OpenStack batch create done: requested={}, started={}, active={}, failed={}".format(
                count, len(createdIds), len(results), failures
            ),
            flush=True,
        )
        return results

    def destroyInstances(self, ipList):
        if not self.conn:
            return

        for ip in ipList:
            instanceId = None
            pubIp = None
            privIp = None
            server = self._findServerByIp(ip)

            if server:
                instanceId = server.id
                privIp, pubIp = self._getServerIps(server)

            try:
                isPrivate = ip_address(ip).is_private
            except Exception:
                isPrivate = False

            if isPrivate:
                privIp = ip
            else:
                pubIp = ip

            if server and pubIp:
                try:
                    self.conn.compute.remove_floating_ip_from_server(server, pubIp)
                except Exception:
                    pass

            if server:
                self.conn.compute.delete_server(server, ignore_missing=True)

            if pubIp:
                fip = self.conn.network.find_ip(pubIp)
                if fip:
                    self.conn.network.delete_ip(fip)

            print("Deleted Instance: {}, {}, {}".format(instanceId, privIp, pubIp), flush=True)
