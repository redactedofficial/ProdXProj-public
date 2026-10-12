import copy
import getpass
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import crypto, history
from .config import relative, validate
from .layout import Inclusion, SKIP_DIRS, SECRET_NAMES, iter_payload_files, sensitive, under

MAX_FILES = 100000
MAX_BYTES = 64 * 1024**3



def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(crypto.BLOCK):
            value.update(chunk)
    return value.hexdigest()


def stream_digest(stream):
    value = hashlib.sha256()
    while chunk := stream.read(crypto.BLOCK):
        value.update(chunk)
    return value.hexdigest()


def inside(root, value):
    relative(value)
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the project")
    return path


def run_commands(commands, cwd, timeout):
    for command in commands:
        result = subprocess.run(command, cwd=cwd, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError("Build command failed; run the configured command locally for diagnostics")


def pack(root, cfg, output, password=None, include_env=False, include_keys=False,
         include_data=False, compile_project=False, branch="main", parent_id=None,
         include_all=False, include_secrets=False, message=""):
    root, output = Path(root).resolve(), Path(output).resolve()
    cfg = copy.deepcopy(validate(cfg))
    if password is not None:
        crypto.creation_password(password)
    if not isinstance(message, str) or len(message) > 2048:
        raise ValueError("Release note must be at most 2048 characters")
    if not cfg.get("project_id"):
        raise ValueError("Run prox init to assign a project identity")
    if any((include_env, include_keys, include_data, include_all, include_secrets, cfg.get("parameters"), cfg.get("emulation", {}).get("parameters"))) and not password:
        raise ValueError("Keys, environment files, parameters and user data require encryption")
    if output.exists():
        raise ValueError("Output already exists; use a new version or destination")
    parent_id = parent_id or history.parent(root, cfg["project_id"], branch) or cfg.get("forked_from")
    previous = history.get(root, parent_id) if parent_id else None
    if compile_project:
        run_commands(cfg.get("build", []), root, cfg.get("timeout", 300))
    env_paths = cfg.get("env_files", []) if include_env else []
    data_paths = cfg.get("data_paths", [])
    for name in env_paths + (data_paths if include_data else []):
        if include_all and not inside(root, name).exists():
            continue
        if not inside(root, name).exists():
            raise ValueError("An explicitly included environment or data path does not exist")
        if name in env_paths and (not inside(root, name).is_file() or (root / name).is_symlink()):
            raise ValueError("Selected environment paths must be regular files")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prox-") as folder:
        zipped = Path(folder) / "package.zip"
        files, total = {}, 0
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            options = Inclusion(include_env, include_keys, include_data, include_secrets, include_all)
            for source, destination in iter_payload_files(root, cfg, options, output):
                info = source.stat()
                total += info.st_size
                if total > MAX_BYTES or len(files) >= MAX_FILES:
                    raise ValueError("Package exceeds the file or size limit")
                date = time.localtime(max(315532800, min(info.st_mtime, 4354819199)))[:6]
                member = zipfile.ZipInfo(destination, date_time=date)
                member.compress_type = zipfile.ZIP_DEFLATED
                member.external_attr = (stat.S_IMODE(info.st_mode) | stat.S_IFREG) << 16
                hash_value = hashlib.sha256()
                written = 0
                with open(source, "rb") as inp, archive.open(member, "w", force_zip64=True) as out:
                    while chunk := inp.read(crypto.BLOCK):
                        written += len(chunk)
                        if written + total - info.st_size > MAX_BYTES:
                            raise ValueError("Package exceeds size limit")
                        hash_value.update(chunk)
                        out.write(chunk)
                total += written - info.st_size
                files[destination] = {"sha256": hash_value.hexdigest(), "size": written,
                                      "mtime_ns": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode) & 0o777}
            credentials = {}
            if include_keys:
                for name, target in cfg.get("targets", {}).items():
                    if "key_file" in target:
                        key = inside(root, target["key_file"])
                        if key.stat().st_size > 1024 * 1024:
                            raise ValueError("SSH key file too large")
                        credentials[name] = key.read_text(encoding="utf-8")
            # Identity file paths stay in metadata; key contents never enter the runtime payload.
            manifest = {"schema": 1, "id": str(uuid.uuid4()), "project_id": cfg["project_id"],
                        "name": cfg["name"], "version": cfg["version"], "branch": branch,
                        "parent": parent_id, "fork_origin": cfg.get("fork_origin"),
                        "created": datetime.now(timezone.utc).isoformat(), "creator": getpass.getuser(),
                        "message": message,
                        "source_path": str(root),
                        "machine": platform.node(), "platform": platform.platform(),
                        "build_commands": cfg.get("build", []) if compile_project else [],
                        "compiled": compile_project, "files": files, "config": cfg,
                        "credentials": credentials, "changes": history.changes(previous, history.protected_files(root, files)),
                        "config_hash": history.fingerprint({k: v for k, v in cfg.items() if k != "parameters"})}
            manifest["private_settings"] = history.private_fingerprint(root, cfg.get("parameters", {}))
            manifest["settings_fingerprint_schema"] = 2
            manifest["settings_hashes"] = {k: history.fingerprint(v) for k, v in cfg.items() if k != "parameters"}
            manifest["settings_changes"] = history.changes(
                {"files": previous.get("settings_hashes", {})} if previous else None,
                manifest["settings_hashes"])
            manifest["history"] = [history.public_metadata(node) for node in history.all_releases(root)]
            shipping = {"schema": 1, "project": cfg["name"], "version": cfg["version"], "release": manifest["id"],
                        "targets": list(cfg.get("targets", {})), "activation": cfg.get("activate", []),
                        "health": cfg.get("health", []), "backup_keep": cfg.get("backup_keep", 5),
                        "data_policy": "Seed absent shared paths; reject conflicts", "environment_included": include_env or include_all,
                        "embedded_keys": bool(credentials), "full_snapshot": include_all,
                        "explicit_exclusions": cfg.get("exclude", []),
                        "always_excluded": [".prox/ (history exported as metadata)", "Existing .proxproj artifacts", "Symlinks (full snapshots reject them)"],
                        "prerequisites": ["Linux", "SSH", "Python 3.10+", "project runtimes"],
                        "folders": {"app": "Application files", "docs": "Documentation", "scripts": "Project scripts",
                                    "environment": "Selected runtime environment files", "data": "Selected user data",
                                    "credentials": "Deployment credentials; never uploaded as runtime files"}}
            documents = {"project.json": json.dumps(cfg, indent=2), "shipping.json": json.dumps(shipping, indent=2),
                         "credentials/ssh.json": json.dumps(credentials),
                         "scripts/PRODX-HOOKS.json": json.dumps({k: cfg.get(k, []) for k in ("build", "activate", "health", "quiesce", "resume")}, indent=2),
                         "docs/PRODX-SHIPPING.md": "# " + cfg["name"] + " shipping\n\n"
                         + "Deploy this package with `prodx deploy FILE --target TARGET`.\n\n"
                         + "Inspect `shipping.json` for commands and policy; `project.json` contains production settings.\n"
                         + "The client unlocks credentials; the server receives application files and selected environment/data only.\n"
                         + "Each deployment backs up the active release and shared data before activation and health checks.\n"
                         + "A failed activation restores the previous application; database/schema changes require their own recovery hooks.\n",
                         "docs/PRODX-VARIABLES.md": "# Runtime variables\n\n"
                         + "\n".join("- `" + k + "`: configured environment parameter (value kept in project.json)." for k in cfg.get("parameters", {}))
                         + "\n\n- `PROX_RELEASE_ROOT`: active application directory.\n- `PROX_DATA_ROOT`: persistent shared data directory.\n"
                         + "- `PROX_DEPLOY_ROOT`: dedicated deployment root.\n\n.env files are shipped as files, not automatically parsed.\n"}
            from .services import package_documents
            documents.update(package_documents(cfg))
            shipping["service"] = cfg.get("service")
            documents["shipping.json"] = json.dumps(shipping, indent=2)
            for path, content in documents.items():
                if path in files:
                    raise ValueError("Project uses a reserved ProdXProj package document filename")
                data = content.encode()
                archive.writestr(path, data)
                files[path] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                               "mtime_ns": 0, "mode": 0o600, "generated": True}
            # Credentials have their own sorted section, separate from project metadata.
            manifest.pop("credentials")
            manifest["changes"] = history.changes(previous, history.protected_files(root, files))
            manifest["settings_changed"] = not previous or previous.get("config_hash") != manifest["config_hash"] or previous.get("private_settings") != manifest["private_settings"]
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        staged = Path(folder) / "final"
        if password:
            crypto.encrypt(zipped, staged, password)
        else:
            shutil.copyfile(zipped, staged)
        # Exclusive creation prevents concurrent pack commands overwriting a release.
        created = False
        try:
            with open(output, "xb") as out, open(staged, "rb") as inp:
                created = True
                os.chmod(output, 0o600)
                shutil.copyfileobj(inp, out, crypto.BLOCK)
        except Exception:
            if created:
                output.unlink(missing_ok=True)
            raise
        manifest["artifact_sha256"] = digest(output)
        history.record(root, manifest)
        from .trust import remember_local_build
        remember_local_build(root, manifest["artifact_sha256"])
        return manifest


@contextmanager
def opened(path, password=None):
    with tempfile.TemporaryDirectory(prefix="prox-open-") as folder:
        path = Path(path)
        if path.stat().st_size > MAX_BYTES + 1024**2:
            raise ValueError("Package exceeds transfer size limit")
        snapshot = Path(folder) / "source.proxproj"
        with open(path, "rb") as inp, open(snapshot, "xb") as out:
            shutil.copyfileobj(inp, out, crypto.BLOCK)
        artifact_digest = digest(snapshot)
        path = snapshot
        with open(path, "rb") as stream:
            encrypted = stream.read(8) == crypto.MAGIC
        if encrypted:
            plaintext = Path(folder) / "package.zip"
            crypto.decrypt(path, plaintext, password)
            path = plaintext
        with zipfile.ZipFile(path) as archive:
            manifest = verify(archive)
            manifest["artifact_sha256"] = artifact_digest
            manifest["credentials"] = json.loads(archive.read("credentials/ssh.json"))
            if not encrypted and (manifest.get("credentials") or manifest["config"].get("parameters") or manifest["config"].get("emulation", {}).get("parameters") or
                                  any(p.startswith(("data/", "environment/", "credentials/files/")) or sensitive(p) for p in manifest["files"])):
                raise ValueError("Sensitive payload must be encrypted")
            yield archive, manifest


def verify(archive):
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1 or sum(i.file_size for i in infos) > MAX_BYTES:
        raise ValueError("Archive exceeds extraction limits")
    paths = set()
    for info in infos:
        relative(info.filename)
        if info.filename in paths or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError("Archive contains duplicate, directory or symlink entries")
        paths.add(info.filename)
    if "manifest.json" not in paths or archive.getinfo("manifest.json").file_size > 32 * 1024**2:
        raise ValueError("Missing or oversized manifest")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported package format")
    if not {"id", "project_id", "name", "version", "config", "files"} <= set(manifest):
        raise ValueError("Incomplete project manifest")
    validate(manifest["config"])
    for field in ("id", "project_id"):
        uuid.UUID(manifest[field])
    if manifest["name"] != manifest["config"]["name"] or manifest["version"] != manifest["config"]["version"]:
        raise ValueError("Manifest identity does not match project configuration")
    if json.loads(archive.read("project.json")) != manifest["config"]:
        raise ValueError("Critical project settings do not match manifest")
    if not isinstance(manifest.get("files"), dict) or paths != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Archive does not match its manifest")
    for path, meta in manifest["files"].items():
        if not isinstance(meta, dict) or type(meta.get("size")) is not int or not 0 <= meta["size"] <= MAX_BYTES:
            raise ValueError("Invalid file metadata")
        if type(meta.get("mode")) is not int or not 0 <= meta["mode"] <= 0o777:
            raise ValueError("Invalid file permissions")
        if type(meta.get("mtime_ns")) is not int or not 0 <= meta["mtime_ns"] < 2**63:
            raise ValueError("Invalid modification timestamp")
        if not path.startswith(("app/", "data/", "docs/", "scripts/", "environment/", "credentials/", "snapshot/")) and path not in ("project.json", "shipping.json"):
            raise ValueError("Unexpected package path")
        with archive.open(path) as stream:
            value = stream_digest(stream)
        if value != meta["sha256"] or archive.getinfo(path).file_size != meta["size"]:
            raise ValueError("Package integrity check failed")
    return manifest


def extract(archive, manifest, destination, include_data=False, include_credentials=False):
    root = Path(destination).resolve()
    if root.exists():
        raise ValueError("Unpack destination must not already exist")
    root.mkdir(parents=True, mode=0o700)
    try:
        for path, meta in manifest["files"].items():
            if path.startswith("credentials/") and not include_credentials:
                continue
            if path.startswith("data/") and not include_data:
                continue
            if path.startswith("credentials/"):
                destination_path = "secrets/" + path
            elif path.startswith(("app/", "data/", "environment/", "snapshot/")):
                destination_path = path.split("/", 1)[1]
            elif path in ("project.json", "shipping.json"):
                destination_path = ".prox/package/" + path
            else:
                destination_path = path
            target = inside(root, destination_path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(path) as inp, open(target, "xb") as out:
                shutil.copyfileobj(inp, out, crypto.BLOCK)
            target.chmod(0o700 if meta["mode"] & 0o111 else 0o600)
            os.utime(target, ns=(meta["mtime_ns"], meta["mtime_ns"]))
        # Credentials remain inside the original encrypted package, even after unpacking.
        config = copy.deepcopy(manifest["config"])
        (root / "proxproject.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (root / "proxproject.json").chmod(0o600)
        for ancestor in manifest.get("history", []):
            history.record(root, ancestor)
        history.record(root, manifest)
    except Exception:
        shutil.rmtree(root)
        raise
