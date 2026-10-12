"""Managed service contract and generated package artifacts."""
import re
from pathlib import Path

from .config import commands, relative


def validate_service(value):
    if not isinstance(value, dict) or value.get("type") not in ("systemd", "docker-compose"):
        raise ValueError("Service type must be systemd or docker-compose")
    allowed = {"type", "name", "scope", "user", "exec_start", "restart", "run_on_start", "allow_sudo", "compose_file", "install_docker"}
    if set(value) - allowed:
        raise ValueError("Unknown service settings")
    if "name" in value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", value["name"]):
        raise ValueError("Invalid service name")
    for flag in ("run_on_start", "allow_sudo", "install_docker"):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError("Service switches must be booleans")
    if value["type"] == "systemd":
        if value.get("scope", "user") not in ("user", "system"):
            raise ValueError("Invalid systemd scope")
        commands([value.get("exec_start", [])])
        for arg in value["exec_start"]:
            if any(ord(c) < 32 for c in arg):
                raise ValueError("Service arguments cannot contain control characters")
        if not value["exec_start"][0].startswith("/"):
            raise ValueError("Systemd executable must use an absolute Linux path")
        if value.get("restart", "on-failure") not in ("no", "on-failure", "always"):
            raise ValueError("Invalid service restart policy")
        if value.get("scope", "user") == "system" and not re.fullmatch(r"[a-z_][a-z0-9_-]*", value.get("user", "")):
            raise ValueError("System services require an explicit application user")
    else:
        relative(value.get("compose_file", "compose.yaml"))
    return value


def package_documents(cfg):
    if not cfg.get("service"):
        return {}
    from .service_runtime import render_unit
    result = {"scripts/PRODX-runtime.py": Path(__file__).with_name("service_runtime.py").read_text(encoding="utf-8")}
    if cfg["service"]["type"] == "systemd":
        result["scripts/PRODX-systemd.service.in"] = render_unit(cfg, "/PRODX_DEPLOY_ROOT")
    else:
        result["docs/PRODX-DOCKER.md"] = "# Managed Docker Compose\n\nThe runtime validates the compose file, generates a project-scoped restart-policy override, starts services, and checks running/health state.\nPersistent mounts must live under PROX_DATA_ROOT or use an independent backup.\n"
    return result
