"""Standalone Linux worker transmitted by the client over authenticated SSH.

Uses only Python's standard library. Not a daemon or externally reachable API.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LIMIT = 64 * 1024**3


def safe(root, name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or "\\" in name or ":" in name or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValueError("Unsafe path")
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Path escapes deployment root")
    return result


def write_json(path, data):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with open(temporary, "x", encoding="utf-8") as out:
        os.chmod(temporary, 0o600)
        json.dump(data, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def hooks(cfg, stage, root, phase):
    env = os.environ.copy()
    env.update(cfg.get("parameters", {}))
    env.update(PROX_RELEASE_ROOT=str(stage), PROX_DATA_ROOT=str(root / "shared"),
               PROX_DEPLOY_ROOT=str(root))
    for command in cfg.get(phase, []):
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("Invalid hook command")
        result = subprocess.run(command, cwd=stage, env=env, timeout=cfg.get("timeout", 300),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError(phase + " hook failed (output suppressed to protect secrets)")


def state(root):
    path = root / "active.json"
    return json.loads(path.read_text()) if path.exists() else None


def active_path(root):
    current = state(root)
    return safe(root / "releases", current["release"]) if current else None


def backup(root, keep=5):
    folder = root / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".zip"
    final = folder / name
    temporary = folder / (name + ".partial")
    files = [root / "active.json"] if (root / "active.json").exists() else []
    trees = [root / "shared", root / "service"]
    if active_path(root):
        trees.append(active_path(root))
    for tree in trees:
        if tree.exists():
            for directory, dirs, names in os.walk(tree, followlinks=False):
                if any(Path(directory, d).is_symlink() for d in dirs):
                    raise ValueError("Backups do not support symlink data directories")
                for item in names:
                    path = Path(directory, item)
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Backups require regular files")
                    files.append(path)
    if len(files) > 100000 or sum(p.stat().st_size for p in files) > LIMIT:
        raise ValueError("Backup exceeds limits")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            os.chmod(temporary, 0o600)
            hashes = {}
            for path in files:
                name_in_archive = path.relative_to(root).as_posix()
                member = zipfile.ZipInfo(name_in_archive)
                member.compress_type = zipfile.ZIP_DEFLATED
                member.external_attr = (stat.S_IMODE(path.stat().st_mode) | stat.S_IFREG) << 16
                digest = hashlib.sha256()
                with open(path, "rb") as inp, archive.open(member, "w", force_zip64=True) as out:
                    while chunk := inp.read(1024 * 1024):
                        digest.update(chunk)
                        out.write(chunk)
                hashes[name_in_archive] = digest.hexdigest()
            archive.writestr("backup-manifest.json", json.dumps(hashes))
        os.replace(temporary, final)
        # Retention is applied only after a complete snapshot exists.
        snapshots = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for old in snapshots[keep:]:
            old.unlink()
        return final.name
    finally:
        temporary.unlink(missing_ok=True)


def hash_stream(stream):
    h = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        h.update(chunk)
    return h.hexdigest()


def unpack(archive, manifest, destination):
    infos = archive.infolist()
    files = manifest["files"]
    if len(infos) > 100000 or sum(i.file_size for i in infos) > LIMIT:
        raise ValueError("Runtime payload exceeds limits")
    if len(infos) != len(files) or set(i.filename for i in infos) != set(files):
        raise ValueError("Runtime payload manifest mismatch")
    for info in infos:
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or not info.filename.startswith(("app/", "data/")):
            raise ValueError("Unexpected runtime file")
        target = safe(destination, info.filename)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with archive.open(info) as inp, open(target, "xb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
        meta = files[info.filename]
        with open(target, "rb") as inp:
            if target.stat().st_size != meta["size"] or hash_stream(inp) != meta["sha256"]:
                raise ValueError("Runtime payload integrity failure")
        target.chmod(0o700 if meta["mode"] & 0o111 else 0o600)
        os.utime(target, ns=(meta["mtime_ns"], meta["mtime_ns"]))


def managed(cfg, root, action):
    if not cfg.get("service"):
        return None
    script = root / "current/scripts/PRODX-runtime.py"
    if not script.is_file():
        raise ValueError("Managed runtime helper missing from release")
    result = subprocess.run([sys.executable, str(script), action, str(root)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=cfg.get("timeout", 300))
    if result.returncode:
        raise ValueError("Managed runtime operation failed")
    value = json.loads(result.stdout)
    if action == "apply" and not value.get("healthy"):
        raise ValueError("Managed service health check failed")
    return value


def restore_transition(root):
    journal = root / '.transition.json'
    if not journal.exists():
        return
    value = json.loads(journal.read_text())
    current = root / 'current'
    target = value.get('target')
    if target:
        target = Path(target).resolve()
        if not target.is_relative_to((root / 'releases').resolve()):
            raise ValueError('Unsafe transition recovery target')
        existing = (root / os.readlink(current)).resolve() if current.is_symlink() else None
        if existing != target:
            temporary = root / ('current-restore-' + uuid.uuid4().hex)
            try:
                temporary.symlink_to(target, target_is_directory=True)
                os.replace(temporary, current)
            finally:
                temporary.unlink(missing_ok=True)
    else:
        current.unlink(missing_ok=True)
    previous = value.get('previous')
    if previous is None:
        (root / 'active.json').unlink(missing_ok=True)
    elif state(root) != previous:
        write_json(root / 'active.json', previous)
    journal.unlink()


def switch(root, release, manifest):
    restore_transition(root)
    previous = state(root)
    path = safe(root / 'releases', release)
    cfg = manifest['config']
    current = root / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError('Existing current path must be a runner-managed symlink')
    old_target = str((root / os.readlink(current)).resolve()) if current.is_symlink() else None
    write_json(root / '.transition.json', {'previous': previous, 'target': old_target})
    temporary_link = root / ('current-' + uuid.uuid4().hex)
    activation_started = False
    try:
        temporary_link.symlink_to(path, target_is_directory=True)
        os.replace(temporary_link, current)
        write_json(root / 'active.json', {'release': release, 'version': manifest['version']})
        activation_started = True
        managed(cfg, root, 'apply')
        hooks(cfg, path, root, 'activate')
        if cfg.get('resume'):
            hooks(cfg, path, root, 'resume')
        hooks(cfg, path, root, 'health')
    except Exception:
        if activation_started and cfg.get('service'):
            try:
                managed(cfg, root, 'stop')
                managed(cfg, root, 'disable')
            except Exception:
                pass
        restore_transition(root)
        if activation_started and previous:
            old = safe(root / 'releases', previous['release'])
            old_manifest = json.loads((old / '.proxrelease.json').read_text())
            managed(old_manifest['config'], root, 'apply')
            hooks(old_manifest['config'], old, root, 'activate')
            hooks(old_manifest['config'], old, root, 'health')
        raise
    else:
        (root / '.transition.json').unlink(missing_ok=True)
    finally:
        temporary_link.unlink(missing_ok=True)


def restore(root, name, destination):
    if not re.fullmatch(r"[A-Za-z0-9-]+\.zip", name):
        raise ValueError("Invalid backup name")
    target = safe(root / "restores", destination)
    if target.exists():
        raise ValueError("Restore destination already exists")
    target.mkdir(parents=True, mode=0o700)
    try:
        with zipfile.ZipFile(root / "backups" / name) as archive:
            infos = archive.infolist()
            if len(infos) > 100001 or sum(i.file_size for i in infos) > LIMIT:
                raise ValueError("Backup exceeds limits")
            if archive.getinfo("backup-manifest.json").file_size > 32 * 1024**2:
                raise ValueError("Backup manifest too large")
            hashes = json.loads(archive.read("backup-manifest.json"))
            if len(infos) != len(hashes) + 1 or set(i.filename for i in infos) != set(hashes) | {"backup-manifest.json"}:
                raise ValueError("Backup manifest mismatch")
            for name_in_archive, expected in hashes.items():
                info = archive.getinfo(name_in_archive)
                if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Unsafe backup member")
                path = safe(target, name_in_archive)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(info) as inp, open(path, "xb") as out:
                    shutil.copyfileobj(inp, out)
                with open(path, "rb") as inp:
                    if hash_stream(inp) != expected:
                        raise ValueError("Backup integrity check failed")
                path.chmod(0o700 if (info.external_attr >> 16) & 0o111 else 0o600)
        return str(target)
    except Exception:
        shutil.rmtree(target)
        raise


def execute(request, payload=None):
    root = Path(request["root"])
    if not root.is_absolute() or root == Path(root.anchor) or root in map(Path, ("/etc", "/usr", "/var", "/home", "/opt")) or ".." in root.parts:
        raise ValueError("A dedicated absolute deployment root is required")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with open(root / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore_transition(root)
        for folder in ("releases", "shared", "backups"):
            path = safe(root, folder)
            path.mkdir(mode=0o700, exist_ok=True)
        action = request["action"]
        if action == "status":
            return {"active": state(root), "backups": sorted(p.name for p in (root / "backups").glob("*.zip")),
                    "releases": sorted(p.name for p in (root / "releases").iterdir())}
        if action == "service":
            path = active_path(root)
            if not path:
                raise ValueError("No active project release")
            cfg = json.loads((path / ".proxrelease.json").read_text())["config"]
            operation = request["operation"]
            if operation not in ("status", "start", "stop", "restart", "enable", "disable"):
                raise ValueError("Invalid managed service operation")
            return {"service": managed(cfg, root, operation)}
        if action == "install-reporter":
            settings = request["settings"]
            settings["root"] = str(root)
            if not settings["url"].startswith("https://") or len(settings["token"]) < 32:
                raise ValueError("HTTPS and a strong reporting token are required")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", settings["node_id"]):
                raise ValueError("Invalid node identity")
            folder = root / "reporter"
            folder.mkdir(mode=0o700, exist_ok=True)
            script = folder / "reporter.py"
            script.write_text(request["script"], encoding="utf-8")
            script.chmod(0o600)
            write_json(folder / "settings.json", settings)
            # Persistent user cron job. No daemon needs to run on a viewing client.
            import shlex
            marker = "# ProdXProj:" + settings["node_id"]
            command = " ".join(shlex.quote(x) for x in (sys.executable, str(script), str(folder / "settings.json"), "--once"))
            previous = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
            if previous.returncode not in (0, 1):
                raise ValueError("Unable to inspect existing crontab")
            lines = [line for line in previous.stdout.splitlines() if not line.endswith(marker)]
            # flock skips overlapping checks when an endpoint or health hook is slow.
            lines.append("* * * * * flock -n " + shlex.quote(str(folder / ".report.lock")) + " " + command + " >/dev/null 2>&1 " + marker)
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"reporter_installed": True, "node_id": settings["node_id"], "schedule": "every minute"}
        if action == "install-backups":
            every = request["every_minutes"]
            if type(every) is not int or not 1 <= every <= 59:
                raise ValueError("Invalid backup schedule")
            folder = root / "backup-runner"
            folder.mkdir(mode=0o700, exist_ok=True)
            script = folder / "worker.py"
            script.write_text(request["script"], encoding="utf-8")
            script.chmod(0o600)
            write_json(folder / "request.json", {"root": str(root), "action": "backup", "keep": request.get("keep", 5)})
            import shlex
            marker = "# ProdXProj-backup:" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
            command = " ".join(shlex.quote(x) for x in (sys.executable, str(script)))
            command += " < " + shlex.quote(str(folder / "request.json"))
            previous = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
            if previous.returncode not in (0, 1):
                raise ValueError("Unable to inspect existing crontab")
            lines = [line for line in previous.stdout.splitlines() if not line.endswith(marker)]
            lines.append(f"*/{every} * * * * " + command + " >/dev/null 2>&1 " + marker)
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"backup_schedule_installed": True, "every_minutes": every}
        if action == "restore":
            return {"restored_to": restore(root, request["backup"], request["destination"])}
        if action == "fetch-backup":
            name = request["backup"]
            if not re.fullmatch(r"[A-Za-z0-9-]+\.zip", name):
                raise ValueError("Invalid snapshot name")
            path = safe(root / "backups", name)
            return {"stream_file": str(path), "snapshot": name}
        if action == "backup":
            old_path = active_path(root)
            old_cfg = json.loads((old_path / ".proxrelease.json").read_text())["config"] if old_path else {}
            if old_path:
                hooks(old_cfg, old_path, root, "quiesce")
            try:
                return {"backup": backup(root, request.get("keep", 5))}
            finally:
                if old_path:
                    hooks(old_cfg, old_path, root, "resume")
        if action == "rollback":
            release = request["release"]
            path = safe(root / "releases", release)
            manifest = json.loads((path / ".proxrelease.json").read_text())
            backup(root, manifest["config"].get("backup_keep", 5))
            switch(root, release, manifest)
            return {"active": state(root)}
        if action != "deploy":
            raise ValueError("Unknown operation")
        manifest = request["manifest"]
        release = str(uuid.UUID(manifest["id"]))
        stage = safe(root / "releases", release)
        if stage.exists():
            raise ValueError("This release is already installed; use rollback to reactivate it")
        old_path = active_path(root)
        old_cfg = json.loads((old_path / ".proxrelease.json").read_text())["config"] if old_path else {}
        activated = False
        try:
            if old_path:
                hooks(old_cfg, old_path, root, "quiesce")
            snapshot = backup(root, manifest["config"].get("backup_keep", 5))
            with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as folder:
                with zipfile.ZipFile(payload) as archive:
                    unpack(archive, manifest, Path(folder))
                app = Path(folder) / "app"
                app.mkdir(exist_ok=True)
                # Seed user data only if a corresponding shared top-level entry is absent.
                # Existing production data is never overwritten by a deployment.
                data = Path(folder) / "data"
                if data.exists():
                    for entry in data.iterdir():
                        target = safe(root / "shared", entry.name)
                        if target.exists():
                            raise ValueError("Seed data conflicts with existing shared data")
                    for entry in data.iterdir():
                        shutil.move(str(entry), safe(root / "shared", entry.name))
                write_json(app / ".proxrelease.json", manifest)
                os.replace(app, stage)
            switch(root, release, manifest)
            activated = True
            return {"active": state(root), "backup": snapshot, "health_verified": bool(manifest["config"].get("health") or manifest["config"].get("service"))}
        finally:
            if old_path and not activated:
                hooks(old_cfg, old_path, root, "resume")


def main():
    os.umask(0o077)
    request = json.loads(sys.stdin.buffer.readline(32 * 1024**2))
    with tempfile.TemporaryFile() as payload:
        count = 0
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            count += len(chunk)
            if count > LIMIT:
                raise ValueError("Transfer exceeds limit")
            payload.write(chunk)
        payload.seek(0)
        result = execute(request, payload)
        if "stream_file" in result:
            path = Path(result.pop("stream_file"))
            with open(path, "rb") as inp:
                result.update(size=path.stat().st_size, sha256=hash_stream(inp))
                inp.seek(0)
                print(json.dumps(result), flush=True)
                shutil.copyfileobj(inp, sys.stdout.buffer, 1024 * 1024)
            return
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never serialize arbitrary exception strings from runtime hooks/configuration.
        print(json.dumps({"error": type(error).__name__, "message": "Operation failed; check configuration, permissions and hook commands"}))
        sys.exit(1)
