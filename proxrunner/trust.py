"""Artifact execution trust is separate from imported/self-reported history."""
import json
from pathlib import Path


def remember_local_build(root, digest):
    folder = Path(root) / ".prox"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "trusted-artifacts.json"
    values = json.loads(path.read_text()) if path.exists() else []
    if digest not in values:
        values.append(digest)
    temporary = folder / "trusted-artifacts.tmp"
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def require(root, path, expected=None):
    from .archive import digest
    actual = digest(path)
    return require_digest(root, actual, expected)


def require_digest(root, actual, expected=None):
    if expected:
        if actual != expected.lower():
            raise ValueError("Artifact does not match the externally trusted SHA-256")
        return actual
    catalogue = Path(root) / ".prox/trusted-artifacts.json"
    if catalogue.exists() and actual in json.loads(catalogue.read_text()):
        return actual
    raise ValueError("Execution requires a local build or --trusted-sha256 from an independently trusted source")
