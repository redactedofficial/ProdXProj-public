"""Generated standalone Linux service manager; standard library only."""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKER = "# Managed by ProdXProj"


def service_name(cfg):
    name = cfg["service"].get("name", cfg["name"])
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("Invalid managed service name")
    return "prodx-" + name


def unit_quote(value, exec_arg=False):
    if any(ord(c) < 32 for c in value):
        raise ValueError("Control characters cannot enter systemd settings")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_arg:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def render_unit(cfg, root):
    svc = cfg["service"]
    user = "User=" + svc["user"] + "\n" if svc.get("scope", "user") == "system" else ""
    target = "multi-user.target" if svc.get("scope", "user") == "system" else "default.target"
    return (MARKER + "\n[Unit]\nDescription=ProdXProj " + service_name(cfg) + "\nAfter=network-online.target\n"
            "[Service]\nType=exec\n" + user + "WorkingDirectory=" + unit_quote(str(root) + "/current") + "\n"
            "EnvironmentFile=" + unit_quote(str(root) + "/service/environment") + "\nExecStart="
            + " ".join(unit_quote(arg, True) for arg in svc["exec_start"]) + "\nRestart=" + svc.get("restart", "on-failure")
            + "\nRestartSec=5\nNoNewPrivileges=true\nUMask=0077\n[Install]\nWantedBy=" + target + "\n")


def run(argv, cfg, root, capture=False, check=True, input_data=None):
    env = os.environ.copy()
    env.update(cfg.get("parameters", {}))
    env.update(PROX_RELEASE_ROOT=str(Path(root) / "current"), PROX_DEPLOY_ROOT=str(root), PROX_DATA_ROOT=str(Path(root) / "shared"))
    result = subprocess.run(argv, cwd=Path(root) / "current", env=env, timeout=cfg.get("timeout", 300),
                            input=input_data, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check and result.returncode:
        raise ValueError("Managed service command failed")
    return result


def systemctl(cfg, *args):
    scope = cfg["service"].get("scope", "user")
    if scope == "user":
        return ["systemctl", "--user", *args]
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    if prefix and not cfg["service"].get("allow_sudo"):
        raise ValueError("System services require explicit allow_sudo or root")
    return prefix + ["systemctl", *args]


def environment(cfg, root):
    values = {**cfg.get("parameters", {}), "PROX_RELEASE_ROOT": str(root / "current"),
              "PROX_DEPLOY_ROOT": str(root), "PROX_DATA_ROOT": str(root / "shared")}
    lines = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or any(ord(c) < 32 for c in value):
            raise ValueError("Managed service environment requires single-line values")
        lines.append(name + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
    path = root / "service" / "environment"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def compose_command(cfg, root, override=True):
    source = (root / "current" / cfg["service"].get("compose_file", "compose.yaml")).resolve()
    if not source.is_relative_to((root / "current").resolve()) or not source.is_file():
        raise ValueError("Compose file is missing or outside the release")
    command = ["docker", "compose", "-p", service_name(cfg), "--project-directory", str(root / "current"), "-f", str(source)]
    generated = root / "service" / "compose.override.json"
    if override and generated.exists():
        command += ["-f", str(generated)]
    return command


def apply(cfg, root):
    root = Path(root).resolve()
    svc = cfg["service"]
    if svc["type"] == "systemd" and svc.get("scope", "user") == "system":
        import pwd
        uid = pwd.getpwnam(svc["user"]).pw_uid
        if uid != root.stat().st_uid or uid != os.geteuid():
            raise ValueError("System service user must own the protected deployment root and match the SSH account")
    folder = root / "service"
    folder.mkdir(mode=0o700, exist_ok=True)
    environment(cfg, root)
    if svc["type"] == "systemd":
        name = service_name(cfg) + ".service"
        system = svc.get("scope", "user") == "system"
        target = Path("/etc/systemd/system") / name if system else Path.home() / ".config/systemd/user" / name
        if target.exists():
            old = target.read_text(encoding="utf-8")
            if not old.startswith(MARKER):
                raise ValueError("Refusing to replace a service not owned by ProdXProj")
            backup = folder / (name + ".before-" + str(time.time_ns()))
            backup.write_text(old, encoding="utf-8")
            backup.chmod(0o600)
        source = folder / name
        source.write_text(render_unit(cfg, root), encoding="utf-8")
        source.chmod(0o600)
        if system:
            prefix = systemctl(cfg)[:-1]
            run(prefix + ["install", "-m", "0644", str(source), str(target)], cfg, root)
        else:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            target.chmod(0o600)
            if svc.get("run_on_start", True):
                linger = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"], cfg, root, True, False)
                if linger.stdout.strip() != b"yes":
                    raise ValueError("User boot startup requires an already enabled loginctl linger setting")
        run(systemctl(cfg, "daemon-reload"), cfg, root)
        run(systemctl(cfg, "enable" if svc.get("run_on_start", True) else "disable", name), cfg, root)
        run(systemctl(cfg, "restart", name), cfg, root)
    else:
        # Installation is handled by remote-gen; deployment never runs an unreviewed installer.
        run(["docker", "info"], cfg, root)
        model = json.loads(run(compose_command(cfg, root, False) + ["config", "--format", "json"], cfg, root, True).stdout)
        override = {"services": {name: {"restart": "unless-stopped" if svc.get("run_on_start", True) else "no"}
                                 for name in model.get("services", {})}}
        if not override["services"]:
            raise ValueError("Compose file defines no services")
        path = folder / "compose.override.json"
        path.write_text(json.dumps(override), encoding="utf-8")
        path.chmod(0o600)
        run(compose_command(cfg, root) + ["config", "--quiet"], cfg, root)
        run(compose_command(cfg, root) + ["up", "-d", "--build", "--wait", "--wait-timeout", "120"], cfg, root)
    return status(cfg, root)


def status(cfg, root):
    root = Path(root)
    svc = cfg["service"]
    if svc["type"] == "systemd":
        name = service_name(cfg) + ".service"
        active = run(systemctl(cfg, "is-active", name), cfg, root, True, False).stdout.decode().strip()
        enabled = run(systemctl(cfg, "is-enabled", name), cfg, root, True, False).stdout.decode().strip()
        return {"type": "systemd", "name": name, "active": active, "enabled": enabled, "healthy": active == "active"}
    result = run(compose_command(cfg, root) + ["ps", "--all", "--format", "json"], cfg, root, True)
    text = result.stdout.decode().strip()
    containers = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    expected = json.loads((root / "service/compose.override.json").read_text())["services"]
    running = {c.get("Service") for c in containers if c.get("State") == "running" and c.get("Health", "") in ("", "healthy")}
    return {"type": "docker-compose", "name": service_name(cfg), "healthy": bool(expected) and set(expected) <= running,
            "containers": [{k: c.get(k) for k in ("Name", "Service", "State", "Health")} for c in containers]}


def manage(cfg, root, action):
    if action == "status":
        return status(cfg, root)
    if action == "apply":
        return apply(cfg, root)
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise ValueError("Unsupported managed service action")
    if cfg["service"]["type"] == "systemd":
        run(systemctl(cfg, action, service_name(cfg) + ".service"), cfg, Path(root))
    else:
        if action in ("enable", "disable"):
            root = Path(root)
            restart = "unless-stopped" if action == "enable" else "no"
            generated = root / "service/compose.override.json"
            override = json.loads(generated.read_text())
            for value in override["services"].values():
                value["restart"] = restart
            generated.write_text(json.dumps(override), encoding="utf-8")
            identifiers = run(compose_command(cfg, root) + ["ps", "--all", "--quiet"], cfg, root, True).stdout.decode().split()
            if any(not re.fullmatch(r"[a-f0-9]{12,64}", cid) for cid in identifiers):
                raise ValueError("Invalid container identifier")
            if identifiers:
                run(["docker", "update", "--restart=" + restart, *identifiers], cfg, root)
            return status(cfg, root)
        run(compose_command(cfg, Path(root)) + [action], cfg, Path(root))
    return status(cfg, root)


if __name__ == "__main__":
    try:
        root = Path(sys.argv[2]).resolve()
        cfg = json.loads((root / "current/.proxrelease.json").read_text())["config"]
        print(json.dumps(manage(cfg, root, sys.argv[1])))
    except Exception:
        print(json.dumps({"error": "managed_service_failed"}))
        sys.exit(1)
