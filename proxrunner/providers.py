"""Monitor-side, authenticated Proxmox and Docker adapters."""
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request
from .network import urlopen


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Invalid provider resource identifier")
    return quote(value, safe="")


class Proxmox:
    def __init__(self):
        self.url = os.environ["PROX_PROXMOX_URL"].rstrip("/")
        if not self.url.startswith("https://"):
            raise ValueError("Proxmox requires HTTPS")
        self.token = "PVEAPIToken=" + os.environ["PROX_PROXMOX_TOKEN_ID"] + "=" + os.environ["PROX_PROXMOX_TOKEN_SECRET"]
        self.context = ssl.create_default_context(cafile=os.environ.get("PROX_PROXMOX_CA"))

    def request(self, method, path, data=None):
        body = urlencode(data).encode() if data is not None else None
        request = Request(self.url + "/api2/json" + path, data=body, method=method,
                          headers={"Authorization": self.token, "Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(request, context=self.context, timeout=30) as response:
            return json.loads(response.read(8 * 1024**2))["data"]

    def wait(self, node, task, timeout=1800):
        deadline = time.monotonic() + timeout
        path = "/nodes/" + identifier(node) + "/tasks/" + quote(task, safe="") + "/status"
        while time.monotonic() < deadline:
            state = self.request("GET", path)
            if state.get("status") == "stopped":
                if state.get("exitstatus") != "OK":
                    raise ValueError("Proxmox task failed")
                return {"task": task, "completed": True}
            time.sleep(2)
        raise ValueError("Proxmox task timed out; inspect its status before retrying")

    def manage(self, data):
        action = data["action"]
        if action == "nodes":
            return self.request("GET", "/nodes")
        if action == "resources":
            return self.request("GET", "/cluster/resources", None)
        node = identifier(data["node"])
        if action == "create-lxc":
            return self.create_lxc(data)
        vmid = data["vmid"]
        if type(vmid) is not int or not 100 <= vmid <= 999999999:
            raise ValueError("Invalid VMID")
        kind = data.get("kind", "lxc")
        if kind not in ("lxc", "qemu"):
            raise ValueError("Kind must be lxc or qemu")
        path = f"/nodes/{node}/{kind}/{vmid}"
        if action == "install-docker":
            if kind != "lxc":
                raise ValueError("Automatic Docker bootstrap currently targets LXC guests")
            config = self.request("GET", path + "/config")
            network = dict(item.split("=", 1) for item in config.get("net0", "").split(",") if "=" in item)
            host = str(ipaddress.ip_interface(network["ip"]).ip)
            install_docker(host)
            return {"vmid": vmid, "docker_installed": True}
        if action == "status":
            return self.request("GET", path + "/status/current")
        if action in ("start", "shutdown", "stop", "reboot"):
            task = self.request("POST", path + "/status/" + action, {})
            return self.wait(data["node"], task)
        if action == "backup":
            storage = data.get("storage") or os.environ.get("PROX_PROXMOX_BACKUP_STORAGE")
            if not storage:
                raise ValueError("A backup storage is required")
            identifier(storage)
            task = self.request("POST", f"/nodes/{node}/vzdump", {"vmid": vmid, "storage": storage, "mode": "stop", "compress": "zstd"})
            result = self.wait(data["node"], task)
            result["storage"] = storage
            result["note"] = "Completed vzdump backup; bind/device mounts and external databases need separate backups"
            return result
        if action == "restore-lxc":
            archive = data["archive"]
            if not isinstance(archive, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:backup/[A-Za-z0-9_.-]+", archive):
                raise ValueError("Expected a Proxmox backup volume identifier")
            storage = data["storage"]
            identifier(storage)
            # Restores to an unused VMID. No force flag or overwrite is permitted.
            task = self.request("POST", f"/nodes/{node}/lxc", {"vmid": vmid, "ostemplate": archive,
                                                               "restore": 1, "storage": storage, "unprivileged": 1})
            return self.wait(data["node"], task)
        if action == "destroy":
            config = self.request("GET", path + "/config")
            # vzdump cannot preserve host bind mounts/device passthrough. Fail closed.
            if kind == "lxc" and any(k.startswith(("mp", "dev")) for k in config):
                raise ValueError("Destruction blocked: externally mounted data requires a separate recovery adapter")
            if kind == "qemu" and any(k.startswith(("hostpci", "usb")) for k in config):
                raise ValueError("Destruction blocked: passthrough devices require a separate recovery adapter")
            if any(isinstance(value, str) and ("backup=0" in value or
                   (re.fullmatch(r"(?:scsi|sata|ide|virtio)\d+", key) and value.startswith("/")))
                   for key, value in config.items()):
                raise ValueError("Destruction blocked: some storage is excluded from backup")
            storage = data.get("storage") or os.environ.get("PROX_PROXMOX_BACKUP_STORAGE")
            if not storage:
                raise ValueError("Independent backup storage must be configured before destruction")
            content_path = f"/nodes/{node}/storage/{identifier(storage)}/content?content=backup"
            previous_backups = {item["volid"] for item in self.request("GET", content_path)}
            result = self.manage({**data, "action": "backup", "storage": storage})
            # Locate the actual backup volume before allowing deletion.
            content = self.request("GET", f"/nodes/{node}/storage/{identifier(result['storage'])}/content?content=backup")
            backups = [item for item in content if str(item.get("vmid")) == str(vmid)
                       and item.get("volid") not in previous_backups and item.get("size", 0) > 0]
            if not backups:
                raise ValueError("Destruction blocked: no recoverable backup volume found")
            volume = max(backups, key=lambda item: item.get("ctime", 0))["volid"]
            self.manage({**data, "action": "stop"})
            task = self.request("DELETE", path)
            return {**self.wait(data["node"], task), "backup_volume": volume}
        raise ValueError("Unsupported Proxmox action")

    def create_lxc(self, data):
        node = identifier(data["node"])
        vmid = data["vmid"]
        if type(vmid) is not int or not 100 <= vmid <= 999999999:
            raise ValueError("Invalid VMID")
        for field in ("hostname", "storage", "bridge"):
            identifier(data[field])
        template = data["ostemplate"]
        if not isinstance(template, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:vztmpl/[A-Za-z0-9_.-]+", template):
            raise ValueError("Invalid LXC template volume")
        ip = data.get("ip", "dhcp")
        if ip != "dhcp":
            ipaddress.ip_interface(ip)
        if data.get("docker"):
            if ip == "dhcp":
                raise ValueError("Docker bootstrap requires a static IP")
            for setting in ("PROX_BOOTSTRAP_KEY_FILE", "PROX_BOOTSTRAP_KNOWN_HOSTS"):
                if not os.environ.get(setting):
                    raise ValueError("Docker bootstrap requires an SSH identity and trusted host-key file")
        params = {"vmid": vmid, "hostname": data["hostname"], "ostemplate": template,
                  "rootfs": data["storage"] + ":" + str(bounded(data.get("disk_gb", 16), 4, 4096)),
                  "cores": bounded(data.get("cores", 2), 1, 128),
                  "memory": bounded(data.get("memory_mb", 2048), 256, 1048576),
                  "unprivileged": 1, "onboot": 1,
                  "net0": f"name=eth0,bridge={data['bridge']},ip={ip},firewall=1"}
        if data.get("gateway"):
            params["net0"] += ",gw=" + str(ipaddress.ip_address(data["gateway"]))
        public_key = data.get("ssh_public_key", "")
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", public_key):
            raise ValueError("An Ed25519 public SSH key is required")
        params["ssh-public-keys"] = public_key
        if data.get("docker"):
            params["features"] = "nesting=1,keyctl=1"
        task = self.request("POST", f"/nodes/{node}/lxc", params)
        self.wait(data["node"], task)
        self.manage({"action": "start", "node": data["node"], "vmid": vmid})
        result = {"node": data["node"], "vmid": vmid, "created": True, "started": True}
        if data.get("docker"):
            try:
                install_docker(str(ipaddress.ip_interface(ip).ip))
                result["docker_installed"] = True
            except (ValueError, OSError, subprocess.TimeoutExpired):
                result.update(docker_installed=False, bootstrap="pending; verify guest readiness and trusted SSH, then use install-docker")
        return result


def bounded(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Provider parameter is outside its allowed range")
    return value


DOCKER_INSTALL = r'''set -eu
if command -v docker >/dev/null 2>&1; then docker info >/dev/null; exit 0; fi
. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) echo 'Unsupported Docker bootstrap OS' >&2; exit 1;; esac
test "$(id -u)" = 0
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl --proto '=https' --tlsv1.2 -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "${UBUNTU_CODENAME:-$VERSION_CODENAME}" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker info >/dev/null
'''


def install_docker(host):
    ipaddress.ip_address(host)
    identity = os.environ["PROX_BOOTSTRAP_KEY_FILE"]
    known = os.environ["PROX_BOOTSTRAP_KNOWN_HOSTS"]
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "UserKnownHostsFile=" + known, "-o", "ConnectTimeout=15",
                             "-o", "IdentitiesOnly=yes", "-i", identity, "root@" + host, "sh -s"],
                            input=DOCKER_INSTALL.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    if result.returncode:
        raise ValueError("Docker bootstrap failed; verify guest readiness and trusted SSH host key")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def request(self, method, path, data=None):
        address = os.environ.get("PROX_DOCKER_HOST", "unix:///var/run/docker.sock")
        if address.startswith("unix://"):
            connection = UnixConnection(address[7:])
        else:
            parsed = urlparse(address)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ValueError("Remote Docker requires HTTPS with client certificates")
            context = ssl.create_default_context(cafile=os.environ["PROX_DOCKER_CA"])
            context.load_cert_chain(os.environ["PROX_DOCKER_CERT"], os.environ["PROX_DOCKER_KEY"])
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 2376, context=context, timeout=30)
        try:
            connection.request(method, "/v1.45" + path, body=json.dumps(data).encode() if data is not None else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read(8 * 1024**2)
            if response.status >= 400:
                raise ValueError("Docker API operation failed")
            return json.loads(body) if body else {"completed": True}
        finally:
            connection.close()

    def manage(self, data):
        action = data["action"]
        if action == "containers":
            containers = self.request("GET", "/containers/json?all=true")
            return [{k: item.get(k) for k in ("Id", "Names", "Image", "State", "Status", "Ports")} for item in containers]
        if action == "info":
            info = self.request("GET", "/info")
            return {k: info.get(k) for k in ("ServerVersion", "Containers", "ContainersRunning", "OperatingSystem", "NCPU", "MemTotal")}
        container = identifier(data["container"])
        if action == "status":
            info = self.request("GET", f"/containers/{container}/json")
            state = {k: info["State"].get(k) for k in ("Status", "Running", "Paused", "Restarting", "OOMKilled", "Dead", "ExitCode", "StartedAt", "FinishedAt")}
            state["Health"] = info["State"].get("Health", {}).get("Status", "unknown")
            return {"Id": info["Id"], "Name": info["Name"], "State": state, "Mounts": info["Mounts"]}
        if action in ("start", "stop", "restart"):
            return self.request("POST", f"/containers/{container}/{action}?t=30")
        # Lifecycle deletion belongs to an explicit adapter preserving all volumes;
        # a container filesystem export alone cannot preserve mounted data.
        raise ValueError("Unsupported Docker action; persistent data recovery uses project backups or a VM/LXC backup")


def manage(data):
    provider = data.get("provider")
    if provider == "proxmox":
        allowed = {"provider", "action", "node", "vmid", "kind", "storage", "archive", "hostname", "ostemplate", "bridge",
                   "ip", "gateway", "cores", "memory_mb", "disk_gb", "ssh_public_key", "docker", "identity_hash"}
    elif provider == "docker":
        allowed = {"provider", "action", "container"}
    else:
        raise ValueError("Unknown provider")
    if set(data) - allowed:
        raise ValueError("Unknown provider request fields")
    if "docker" in data and type(data["docker"]) is not bool:
        raise ValueError("Docker bootstrap flag must be boolean")
    if provider == "proxmox":
        return Proxmox().manage(data)
    if provider == "docker":
        return Docker().manage(data)
    raise ValueError("Unknown provider")
