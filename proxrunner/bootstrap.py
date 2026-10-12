"""Client-side SSH prerequisite setup for an explicitly configured Linux target."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .providers import DOCKER_INSTALL
from .transport import invoke


def ssh_command(root, target, key_file=None):
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-p", str(target.get("port", 22))]
    identity = Path(key_file).resolve() if key_file else (Path(root) / target["key_file"]).resolve() if target.get("key_file") else None
    if identity:
        if not identity.is_file():
            raise ValueError("Select an existing SSH identity accepted by this server")
        command += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    if target.get("known_hosts"):
        command += ["-o", "UserKnownHostsFile=" + str((Path(root) / target["known_hosts"]).resolve())]
    return command + [target["user"] + "@" + target["host"]]


def script(target, packages, docker=False):
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:-]{0,127}", name) for name in packages):
        raise ValueError("OS package names must be literal package identifiers")
    extra = " ".join(shlex.quote(name) for name in packages)
    payload = '''set -eu
test "$(id -u)" = 0
if command -v apt-get >/dev/null 2>&1; then
 export DEBIAN_FRONTEND=noninteractive
 apt-get update -qq
 apt-get install -y python3 python3-pip cron util-linux ca-certificates EXTRA_PACKAGES
elif command -v dnf >/dev/null 2>&1; then
 dnf install -y python3 python3-pip cronie util-linux ca-certificates EXTRA_PACKAGES
elif command -v yum >/dev/null 2>&1; then
 yum install -y python3 python3-pip cronie util-linux ca-certificates EXTRA_PACKAGES
elif command -v apk >/dev/null 2>&1; then
 apk add python3 py3-pip dcron util-linux ca-certificates EXTRA_PACKAGES
elif command -v zypper >/dev/null 2>&1; then
 zypper --non-interactive install python3 python3-pip cron util-linux ca-certificates EXTRA_PACKAGES
else
 echo 'Unsupported package manager; install prerequisites manually' >&2; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,10)'
if command -v systemctl >/dev/null 2>&1; then
 systemctl enable --now cron 2>/dev/null || systemctl enable --now crond 2>/dev/null || exit 1
elif command -v rc-service >/dev/null 2>&1; then
 rc-update add dcron default
 rc-service dcron start
fi
'''.replace("EXTRA_PACKAGES", extra)
    if docker:
        payload += DOCKER_INSTALL
    quoted_root, quoted_user = shlex.quote(target["root"]), shlex.quote(target["user"])
    payload += "\nif [ ! -e " + quoted_root + " ]; then install -d -m 0700 -o " + quoted_user + " -g \"$(id -gn " + quoted_user + ")\" " + quoted_root + "; fi\n"
    return payload


def generate(root, cfg, target_name, key_file=None, packages=(), docker=False, allow_sudo=False,
             monitor_url=None, node_id=None, report_token=None, ca_file=None, execute=False):
    target = cfg["targets"][target_name]
    docker = docker or bool(cfg.get("service", {}).get("install_docker"))
    payload = script(target, list(packages), docker)
    if bool(monitor_url) != bool(node_id):
        raise ValueError("Monitor URL and node ID must be supplied together")
    if monitor_url and (not monitor_url.startswith("https://") or not report_token or len(report_token) < 32):
        raise ValueError("Remote reporting requires HTTPS and a unique 32+ character node token")
    plan = {"target": target_name, "host": target["host"], "user": target["user"], "root": target["root"],
            "prerequisites": ["Python 3.10+", "pip", "cron", "flock", "CA certificates", *packages],
            "docker": docker, "reporter": bool(monitor_url), "script": payload,
            "mode": "Preview only; no network operations"}
    if not execute:
        return plan
    if target["user"] != "root" and not allow_sudo:
        raise ValueError("Remote setup needs root access or explicit --allow-sudo with scoped passwordless sudo")
    command = ssh_command(root, target, key_file) + ["sh -s" if target["user"] == "root" else "sudo -n sh -s"]
    result = subprocess.run(command, input=payload.encode(), timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError("Remote setup failed; check trusted host key, SSH access, sudo, distro support and repositories")
    result = {"target": target_name, "prerequisites_installed": True, "docker_requested": docker}
    if monitor_url:
        settings = {"url": monitor_url, "node_id": node_id, "token": report_token, "address": target["host"]}
        if ca_file:
            settings["ca_file"] = ca_file
        result["reporter"] = invoke(root, cfg, target_name, "install-reporter", key_file=key_file,
                                    settings=settings, script=Path(__file__).with_name("reporter.py").read_text(encoding="utf-8"))
    result["server"] = invoke(root, cfg, target_name, "status", key_file=key_file)
    return result


def keygen(root, target_name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target_name):
        raise ValueError("Invalid target name")
    folder = Path(root) / "secrets/ssh" / target_name
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    identity = folder / "id_ed25519"
    if identity.exists() or identity.with_suffix(".pub").exists():
        raise ValueError("A key already exists for this target; it will not be overwritten")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(identity), "-N", "", "-C", "ProdXProj:" + target_name],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    identity.chmod(0o600)
    return {"private_key": str(identity), "public_key": str(identity) + ".pub",
            "note": "Enroll the public key on the server through a trusted access path before using it"}
