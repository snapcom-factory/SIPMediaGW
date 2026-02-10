#!/usr/bin/env python

import json
import uuid
from ipaddress import ip_address
from typing import Dict, List, Optional, Tuple

from manageInstance import ManageInstance


class OpenstackProvider(ManageInstance):
    def __init__(self, profile):
        self.profile = profile
        self.conn = None
        self.instName = None
        self.instType = None
        self.ami = None
        self.network_ref = None
        self.network_id = None
        self.network_name = None
        self.fip_network_ref = None
        self.fip_network_id = None
        self.fip_network_name = None
        self.secuGrp = None
        self.keypair = None
        self.userData = ""
        self.flavor_cpu = {}
        self.flavor_cache = {}

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            uuid.UUID(value)
            return True
        except Exception:
            return False

    def _connect(self, profile_cfg: Optional[Dict]):
        import openstack

        if not profile_cfg:
            return openstack.connect()

        cloud = profile_cfg.get("cloud")
        region = profile_cfg.get("region_name") or profile_cfg.get("region")
        interface = profile_cfg.get("interface")
        verify = profile_cfg.get("verify")
        cacert = profile_cfg.get("cacert")
        app_name = profile_cfg.get("app_name")
        app_version = profile_cfg.get("app_version")

        if cloud:
            kwargs = {"cloud": cloud}
            if region:
                kwargs["region_name"] = region
            if interface:
                kwargs["interface"] = interface
            if verify is not None:
                kwargs["verify"] = verify
            if cacert:
                kwargs["cacert"] = cacert
            if app_name:
                kwargs["app_name"] = app_name
            if app_version:
                kwargs["app_version"] = app_version
            return openstack.connect(**kwargs)

        kwargs = {}
        auth = profile_cfg.get("auth", {})
        if isinstance(auth, dict):
            kwargs.update(auth)

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
            if key in profile_cfg:
                kwargs[key] = profile_cfg[key]

        if region:
            kwargs["region_name"] = region
        if interface:
            kwargs["interface"] = interface
        if verify is not None:
            kwargs["verify"] = verify
        if cacert:
            kwargs["cacert"] = cacert
        if app_name:
            kwargs["app_name"] = app_name
        if app_version:
            kwargs["app_version"] = app_version

        return openstack.connect(**kwargs) if kwargs else openstack.connect()

    def _resolve_network(self, ref: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not ref:
            return None, None

        net = None
        subnet = None
        if self._is_uuid(ref):
            try:
                net = self.conn.network.get_network(ref)
            except Exception:
                net = None
            if not net:
                try:
                    subnet = self.conn.network.get_subnet(ref)
                except Exception:
                    subnet = None
        else:
            net = self.conn.network.find_network(ref)
            if not net:
                subnet = self.conn.network.find_subnet(ref)

        if subnet and not net:
            try:
                net = self.conn.network.get_network(subnet.network_id)
            except Exception:
                net = None

        if not net:
            raise RuntimeError(f"Network or subnet not found: {ref}")
        return net.id, net.name

    def _resolve_security_groups(self) -> List[str]:
        if not self.secuGrp:
            return []

        groups: List[str] = []
        if isinstance(self.secuGrp, dict):
            for key in ["admin", "app"]:
                if self.secuGrp.get(key):
                    groups.append(self.secuGrp[key])
        elif isinstance(self.secuGrp, list):
            groups = [g for g in self.secuGrp if g]
        else:
            groups = [self.secuGrp]

        resolved: List[str] = []
        for group in groups:
            sg = self.conn.network.find_security_group(group)
            resolved.append(sg.name if sg else group)
        return resolved

  
    def configureInstance(self, configFile, initData):
        with open(configFile) as f:
            instConfig = json.load(f)

        profile_cfg = instConfig.get("profile", {}).get(self.profile, {})
        self.conn = self._connect(profile_cfg)

        self.instName = instConfig.get("name")
        self.instType = instConfig.get("instance_type_by_cpu_num", {})
        self.ami = instConfig.get("instance_image")
        self.network_ref = instConfig.get("network") or instConfig.get("subnet")
        self.fip_network_ref = instConfig.get("floating_ip_network")
        self.secuGrp = instConfig.get("security_group")
        self.keypair = instConfig.get("keypair")
        self.flavor_cpu = {v: int(k) for k, v in self.instType.items()}

        self.network_id, self.network_name = self._resolve_network(self.network_ref)
        if self.fip_network_ref:
            self.fip_network_id, self.fip_network_name = self._resolve_network(self.fip_network_ref)

    def _get_server_ips(self, server) -> Tuple[Optional[str], Optional[str]]:
        priv_ip = None
        pub_ip = None
        addresses = server.addresses or {}
        for net_name, addrs in addresses.items():
            for addr in addrs:
                ip_addr = addr.get("addr") or addr.get("address")
                ip_type = addr.get("OS-EXT-IPS:type") or addr.get("type")
                if ip_type == "floating":
                    pub_ip = ip_addr
                elif ip_type == "fixed":
                    if self.network_name is None or net_name == self.network_name:
                        priv_ip = ip_addr
        return priv_ip, pub_ip

    def _get_cpu_count(self, server) -> Optional[int]:
        metadata = getattr(server, "metadata", {}) or {}
        if "cpu_count" in metadata:
            try:
                return int(metadata["cpu_count"])
            except Exception:
                pass

        flavor = getattr(server, "flavor", {}) or {}
        flavor_id = flavor.get("id")
        if not flavor_id:
            return None

        if flavor_id not in self.flavor_cache:
            flv = self.conn.compute.get_flavor(flavor_id)
            self.flavor_cache[flavor_id] = getattr(flv, "vcpus", None)

        vcpus = self.flavor_cache.get(flavor_id)
        return int(vcpus) if vcpus is not None else None

    def enumerateInstances(self):
        if not self.conn:
            return []

        app_sg = None
        if isinstance(self.secuGrp, dict):
            app_sg = self.secuGrp.get("app")

        instances = []
        for server in self.conn.compute.servers(details=True):
            status = (server.status or "").upper()
            if status != "ACTIVE":
                continue

            if app_sg:
                sg_names = set()
                for sg in server.security_groups or []:
                    if isinstance(sg, dict):
                        sg_names.add(sg.get("name"))
                    else:
                        sg_names.add(sg)
                if app_sg not in sg_names:
                    continue

            priv_ip, pub_ip = self._get_server_ips(server)
            if self.network_name and not priv_ip:
                continue

            cpu_cnt = self._get_cpu_count(server) or 0
            start_time = getattr(server, "created_at", None) or getattr(server, "created", None)

            instances.append(
                {
                    "start": start_time,
                    "addr": {"priv": priv_ip, "pub": pub_ip},
                    "cpu_count": int(cpu_cnt),
                }
            )

        return instances

    def _build_name(self, name: Optional[str]) -> str:
        if name:
            return f"{name}.{self.instName}"
        return self.instName or "sipmediagw"

    def createInstance(self, numCPU, name=None, ip=None):
        if not self.conn:
            raise RuntimeError("Provider is not configured. Call configureInstance first.")

        flavor_ref = self.instType.get(str(numCPU))
        if not flavor_ref:
            raise RuntimeError(f"No flavor configured for CPU count: {numCPU}")

        flavor = self.conn.compute.find_flavor(flavor_ref)
        if not flavor:
            try:
                flavor = self.conn.compute.get_flavor(flavor_ref)
            except Exception:
                flavor = None
        if not flavor:
            raise RuntimeError(f"Flavor not found: {flavor_ref}")

        image = self.conn.compute.find_image(self.ami)
        if not image:
            try:
                image = self.conn.compute.get_image(self.ami)
            except Exception:
                image = None
        if not image:
            raise RuntimeError(f"Image not found: {self.ami}")

        if not self.network_id:
            raise RuntimeError("Network is not configured")

        networks = [{"uuid": self.network_id}]
        sec_groups = self._resolve_security_groups()
        server_name = self._build_name(name)

        create_kwargs = {
            "name": server_name,
            "image_id": image.id,
            "flavor_id": flavor.id,
            "networks": networks,
            "metadata": {"cpu_count": str(numCPU), "gw_prefix": self.instName or ""},
        }
        if sec_groups:
            create_kwargs["security_groups"] = [{"name": sg} for sg in sec_groups]
        if self.keypair:
            create_kwargs["key_name"] = self.keypair
        if self.userData:
            create_kwargs["user_data"] = self.userData

        server = self.conn.compute.create_server(**create_kwargs)
        server = self.conn.compute.wait_for_server(server)
        server = self.conn.compute.get_server(server.id)

        pub_ip = None
        if ip:
            pub_ip = ip
        elif self.fip_network_id:
            fip = self.conn.network.create_ip(floating_network_id=self.fip_network_id)
            pub_ip = fip.floating_ip_address

        if pub_ip:
            self.conn.compute.add_floating_ip_to_server(server, pub_ip)

        priv_ip, actual_pub = self._get_server_ips(server)
        if actual_pub:
            pub_ip = actual_pub

        print(
            f"Created Instance: {server.id}, {priv_ip}, {pub_ip}, {numCPU}VCPUs",
            flush=True,
        )

        return {"id": server.id, "ip": pub_ip}

    def _find_server_by_ip(self, ip: str):
        for server in self.conn.compute.servers(details=True):
            addresses = server.addresses or {}
            for addrs in addresses.values():
                for addr in addrs:
                    if addr.get("addr") == ip or addr.get("address") == ip:
                        return server
        return None

    def destroyInstances(self, ipList):
        if not self.conn:
            return

        for ip in ipList:
            server = self._find_server_by_ip(ip)
            pub_ip = None
            priv_ip = None

            try:
                is_private = ip_address(ip).is_private
            except Exception:
                is_private = False

            if server:
                priv_ip, pub_ip = self._get_server_ips(server)
                if is_private:
                    priv_ip = ip
                else:
                    pub_ip = ip

                if pub_ip:
                    try:
                        self.conn.compute.remove_floating_ip_from_server(server, pub_ip)
                    except Exception:
                        pass
                    fip = self.conn.network.find_ip(pub_ip)
                    if fip:
                        self.conn.network.delete_ip(fip)

                self.conn.compute.delete_server(server, ignore_missing=True)
            else:
                if not is_private:
                    fip = self.conn.network.find_ip(ip)
                    if fip:
                        self.conn.network.delete_ip(fip)

            print(f"Deleted Instance: {server.id if server else None}, {priv_ip}, {pub_ip}", flush=True)
