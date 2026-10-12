import base64
import copy
import getpass
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from .layout import runtime_destination


def invoke(root, cfg, target_name, action, archive=None, manifest=None, key_file=None,
           timeout=1800, download=None, trusted_sha256=None, **fields):
    if action == "deploy":
        from .trust import require_digest
        require_digest(root, (manifest or {}).get("artifact_sha256"), trusted_sha256)
    target = cfg.get("targets", {}).get(target_name)
    if target is None:
        raise ValueError("Unknown deployment target")
    credentials = (manifest or {}).get("credentials", {})
    # Only runtime metadata/files cross the SSH connection. Deployment keys remain local.
    runtime = copy.deepcopy(manifest) if manifest else None
    if runtime:
        for field in ("credentials", "history"):
            runtime.pop(field, None)
        runtime["config"].pop("targets", None)
        runtime["files"] = {}
        for path, meta in manifest["files"].items():
            destination = runtime_destination(path)
            if destination is None:
                continue
            if destination in runtime["files"]:
                raise ValueError("Runtime file layout contains a collision")
            runtime["files"][destination] = meta
    request = {"root": target["root"], "action": action, **fields}
    if runtime:
        request["manifest"] = runtime
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
               "-p", str(target.get("port", 22))]
    if target.get("known_hosts"):
        command += ["-o", "UserKnownHostsFile=" + str((Path(root) / target["known_hosts"]).resolve())]
    # Embedded keys are materialized only in the local credential vault, then removed.
    vault = Path(root) / "secrets" / "ssh"
    vault.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prox-key-", dir=vault) as temporary:
        identity = Path(key_file).resolve() if key_file else None
        if not identity and target_name in credentials:
            identity = Path(temporary) / "id_ed25519"
            identity.write_text(credentials[target_name], encoding="utf-8")
            identity.chmod(0o600)
            if os.name == "nt":
                user = subprocess.check_output(["whoami"], text=True).strip()
                subprocess.run(["icacls", str(identity), "/inheritance:r", "/grant:r", user + ":F"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not identity and target.get("key_file"):
            identity = (Path(root) / target["key_file"]).resolve()
        if identity:
            if not identity.is_file():
                raise ValueError("SSH identity file does not exist")
            command += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
        code = base64.b64encode(Path(__file__).with_name("remote.py").read_bytes()).decode("ascii")
        bootstrap = "import base64;exec(compile(base64.b64decode('" + code + "'),'<prox-worker>','exec'))"
        command += [target["user"] + "@" + target["host"], "python3 -c " + shlex.quote(bootstrap)]
        with tempfile.TemporaryFile() as request_stream:
            request_stream.write(json.dumps(request).encode() + b"\n")
            if archive:
                # Build a credential-free runtime archive by streaming selected file entries.
                import zipfile
                with tempfile.TemporaryFile() as payload:
                    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as packed:
                        for path in manifest["files"]:
                            destination = runtime_destination(path)
                            if destination is None:
                                continue
                            with archive.open(path) as inp, packed.open(destination, "w", force_zip64=True) as out:
                                shutil.copyfileobj(inp, out, 1024 * 1024)
                    payload.seek(0)
                    shutil.copyfileobj(payload, request_stream, 1024 * 1024)
            request_stream.seek(0)
            if download is None:
                result = subprocess.run(command, stdin=request_stream, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=timeout)
            else:
                response_file = Path(temporary) / "response"
                with open(response_file, "wb") as response:
                    result = subprocess.run(command, stdin=request_stream, stdout=response,
                                            stderr=subprocess.DEVNULL, timeout=timeout)
        if result.returncode:
            raise ValueError("SSH operation failed: verify trusted host key, Python 3, access, permissions and hooks")
        if download is not None:
            import hashlib
            destination = Path(download)
            created = False
            try:
                with open(response_file, "rb") as inp:
                    info = json.loads(inp.readline(65536))
                    digest = hashlib.sha256()
                    count = 0
                    with open(destination, "xb") as out:
                        created = True
                        destination.chmod(0o600)
                        while chunk := inp.read(1024 * 1024):
                            count += len(chunk)
                            if count > 64 * 1024**3:
                                raise ValueError("Backup download exceeds limits")
                            digest.update(chunk)
                            out.write(chunk)
                    if count != info["size"] or digest.hexdigest() != info["sha256"]:
                        raise ValueError("Backup transfer integrity failed")
                    return info
            except Exception:
                if created:
                    destination.unlink(missing_ok=True)
                raise
        return json.loads(result.stdout)
