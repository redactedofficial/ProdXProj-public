"""Client-driven generic recovery with mandatory off-server snapshot preservation."""
import json
import os
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from . import crypto
from .transport import invoke


def preserve(root, cfg, target, password, key_file=None):
    folder = Path(root) / ".prox" / "recovery"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    snapshot = invoke(root, cfg, target, "backup", key_file=key_file, keep=cfg.get("backup_keep", 5))["backup"]
    with tempfile.TemporaryDirectory(prefix="prox-recovery-") as temporary:
        plain = Path(temporary) / "snapshot.zip"
        invoke(root, cfg, target, "fetch-backup", key_file=key_file, backup=snapshot, download=plain)
        # Verify all stored content before authorizing the destructive adapter.
        from .archive import digest, stream_digest, MAX_BYTES, MAX_FILES
        with zipfile.ZipFile(plain) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1 or sum(i.file_size for i in infos) > MAX_BYTES:
                raise ValueError("Recovery snapshot exceeds limits")
            if archive.getinfo("backup-manifest.json").file_size > 32 * 1024**2:
                raise ValueError("Backup manifest exceeds limits")
            hashes = json.loads(archive.read("backup-manifest.json"))
            if len(infos) != len(hashes) + 1 or set(i.filename for i in infos) != set(hashes) | {"backup-manifest.json"}:
                raise ValueError("Recovery snapshot manifest mismatch")
            for name, expected in hashes.items():
                with archive.open(name) as inp:
                    if stream_digest(inp) != expected:
                        raise ValueError("Recovery snapshot integrity failure")
        destination = folder / (str(uuid.uuid4()) + ".proxbak")
        crypto.encrypt(plain, destination, password)
        destination.chmod(0o600)
        # Test the completed encrypted copy, not only the plaintext source.
        check = Path(temporary) / "verified.zip"
        crypto.decrypt(destination, check, password)
        if digest(check) != digest(plain):
            raise ValueError("Encrypted snapshot verification failed")
        return destination


def recover(root, cfg, target_name, action, execute=False, password=None, key_file=None):
    target = cfg["targets"][target_name]
    commands = cfg.get("recovery", {}).get(action)
    if not commands:
        raise ValueError("Configure a trusted local recovery adapter for this operation first")
    plan = {"operation": action, "target": target_name, "host": target["host"], "root": target["root"],
            "steps": ["Create consistent server snapshot", "Download and verify file hashes", "Encrypt and verify an off-server copy", "Run configured recovery adapter"],
            "commands": commands}
    if not execute:
        return plan
    backup = preserve(root, cfg, target_name, password, key_file)
    env = os.environ.copy()
    env.update(PROX_RECOVERY_BACKUP=str(backup), PROX_TARGET_HOST=target["host"],
               PROX_TARGET_USER=target["user"], PROX_TARGET_ROOT=target["root"], PROX_RECOVERY_ACTION=action)
    for command in commands:
        result = subprocess.run(command, cwd=root, env=env, timeout=cfg.get("timeout", 300),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError("Recovery adapter failed; the verified encrypted backup remains in .prox/recovery")
    return {"completed": True, "operation": action, "backup": str(backup),
            "note": "Adapter completion does not imply application health; redeploy/check the target as appropriate"}
