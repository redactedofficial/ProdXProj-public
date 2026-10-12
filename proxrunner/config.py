import json
import re
from pathlib import Path, PurePosixPath


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Expected a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..", "") for p in value.split("/")):
        raise ValueError("Unsafe relative path")
    return value


def commands(value):
    if not isinstance(value, list):
        raise ValueError("Commands must be a list of argument arrays")
    for cmd in value:
        if not isinstance(cmd, list) or not cmd or any(not isinstance(a, str) or not a or "\0" in a for a in cmd):
            raise ValueError("Each command must be a nonempty argument array")


def validate(cfg):
    if not isinstance(cfg, dict) or cfg.get("schema") != 1:
        raise ValueError("Project configuration requires schema 1")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", cfg.get("name", "")):
        raise ValueError("Project name must contain letters, digits, underscores or hyphens")
    if not re.fullmatch(r"\d{1,8}([.-]\d{1,8}){0,7}", cfg.get("version", "")):
        raise ValueError("Version must be numeric, for example 1-1-1-2")
    allowed = {"schema", "name", "version", "project_id", "branch", "forked_from", "fork_origin", "build", "activate",
               "health", "quiesce", "resume", "parameters", "env_files", "data_paths", "exclude", "timeout", "backup_keep", "targets", "recovery", "service", "test", "emulation"}
    if set(cfg) - allowed:
        raise ValueError("Unknown project configuration fields")
    if "branch" in cfg and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", cfg["branch"]):
        raise ValueError("Invalid branch name")
    import uuid
    for field in ("project_id", "forked_from", "fork_origin"):
        if cfg.get(field):
            uuid.UUID(cfg[field])
    for group in ("build", "activate", "health", "quiesce", "resume", "test"):
        commands(cfg.get(group, []))
    if cfg.get("service"):
        from .services import validate_service
        validate_service(cfg["service"])
    if "emulation" in cfg:
        from .emulation import validate_options
        validate_options(cfg["emulation"])
    recovery = cfg.get("recovery", {})
    if not isinstance(recovery, dict) or set(recovery) - {"rebuild", "reinstall", "nuke"}:
        raise ValueError("Recovery must map rebuild/reinstall/nuke to trusted local adapter commands")
    for value in recovery.values():
        commands(value)
    for group in ("env_files", "data_paths", "exclude"):
        if not isinstance(cfg.get(group, []), list):
            raise ValueError("Path fields must be lists")
        for path in cfg.get(group, []):
            relative(path)
    if not isinstance(cfg.get("parameters", {}), dict):
        raise ValueError("Parameters must be an environment mapping")
    for name, value in cfg.get("parameters", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(value, str) or "\0" in value:
            raise ValueError("Invalid environment parameter")
        if name.startswith("PROX_"):
            raise ValueError("PROX_ parameters are reserved")
    for setting, default in (("timeout", 300), ("backup_keep", 5)):
        val = cfg.get(setting, default)
        if type(val) is not int or not 1 <= val <= 86400:
            raise ValueError("Timeout and backup retention must be positive bounded integers")
    if not isinstance(cfg.get("targets", {}), dict):
        raise ValueError("Targets must be an object")
    for name, target in cfg.get("targets", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(target, dict):
            raise ValueError("Invalid target name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", target.get("host", "")):
            raise ValueError("Invalid SSH host or IP")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", target.get("user", "")):
            raise ValueError("Invalid SSH user")
        root = target.get("root", "")
        if not isinstance(root, str) or not root.startswith("/") or ".." in root.split("/") or root in ("/", "/etc", "/usr", "/var", "/home", "/opt"):
            raise ValueError("Target root must be a dedicated absolute deployment directory")
        if type(target.get("port", 22)) is not int or not 1 <= target.get("port", 22) <= 65535:
            raise ValueError("Invalid SSH port")
        for field in ("key_file", "known_hosts"):
            if field in target:
                relative(target[field])
    return cfg


def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def template(name):
    return {"schema": 1, "name": name, "version": "1-1-1-2", "build": [],
            "activate": [], "health": [], "quiesce": [], "resume": [],
            "parameters": {}, "env_files": [".env"], "data_paths": ["data"],
            "exclude": [], "timeout": 300, "backup_keep": 5, "targets": {}}
