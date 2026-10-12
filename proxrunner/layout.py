"""Shared file selection and runtime layout for packaging and interface previews."""
import os
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".prox", ".venv", "__pycache__", "node_modules", "secrets", "credentials", ".ssh"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json"}
GENERATED = {"project.json", "shipping.json", "manifest.json", "credentials/ssh.json",
             "scripts/PRODX-HOOKS.json", "docs/PRODX-SHIPPING.md", "docs/PRODX-VARIABLES.md"}
RESERVED_SOURCE = {"proxproject.json", ".proxrelease.json", "scripts/PRODX-HOOKS.json",
                   "docs/PRODX-SHIPPING.md", "docs/PRODX-VARIABLES.md", "scripts/PRODX-runtime.py",
                   "scripts/PRODX-systemd.service.in", "docs/PRODX-DOCKER.md"}


@dataclass(frozen=True)
class Inclusion:
    env: bool = False
    keys: bool = False
    data: bool = False
    secrets: bool = False
    all: bool = False


def under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def sensitive(path):
    name = Path(path).name
    return (name.startswith(".env") and name != ".env.example") or name in SECRET_NAMES or name.endswith((".pem", ".key", ".p12", ".pfx"))


def selected_env(cfg, options):
    return cfg.get("env_files", []) if options.env else []


def skipped_dirs(options):
    skip = {".prox"} if options.all else SKIP_DIRS
    return skip - {"secrets", "credentials", ".ssh"} if options.secrets else skip


def destination_for(path, cfg, options):
    name = Path(path).name
    env_paths = selected_env(cfg, options)
    if name.endswith(".proxproj") or path in RESERVED_SOURCE or under(path, cfg.get("exclude", [])):
        return None
    if under(path, cfg.get("env_files", [])) and path not in env_paths and not options.all:
        return None
    if path in env_paths or (options.all and name.startswith(".env") and name != ".env.example"):
        return "environment/" + path
    if any(part in (".git", ".venv") for part in path.split("/")[:-1]):
        return "snapshot/" + path
    if any(part in ("secrets", "credentials", ".ssh") for part in path.split("/")[:-1]) or (sensitive(path) and path not in env_paths and not name.startswith(".env")):
        return "credentials/files/" + path if options.secrets or options.all else None
    if under(path, cfg.get("data_paths", [])):
        return "data/" + path if options.data else None
    if path.startswith(("docs/", "scripts/")):
        return path
    return None if sensitive(path) and path not in env_paths else "app/" + path


def iter_payload_files(root, cfg, options, output=None):
    root = Path(root).resolve()
    output = Path(output).resolve() if output else None
    env_paths, skip = selected_env(cfg, options), skipped_dirs(options)
    for directory, dirs, names in os.walk(root, followlinks=False):
        if options.all and any(Path(directory, d).is_symlink() for d in dirs):
            raise ValueError("Full snapshots currently require regular files and directories, without symlinks")
        dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink() and
                         (d not in skip or any(p.startswith(Path(directory, d).relative_to(root).as_posix() + "/") for p in env_paths)))
        for name in sorted(names):
            source = Path(directory, name)
            if options.all and source.is_symlink():
                raise ValueError("Full snapshots currently do not support symbolic links")
            if source.is_symlink() or not source.is_file() or source.resolve() == output:
                continue
            path = source.relative_to(root).as_posix()
            destination = destination_for(path, cfg, options)
            if destination:
                yield source, destination


def runtime_destination(path):
    if path.startswith(("app/", "data/")):
        return path
    if path.startswith(("docs/", "scripts/")):
        return "app/" + path
    if path.startswith("environment/"):
        return "app/" + path[len("environment/"):]
    return None
