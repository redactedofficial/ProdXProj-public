import json
import ssl
from urllib.parse import urlparse
from urllib.request import Request
from .network import urlopen
from pathlib import Path


def request(url, token, method, path, data=None, ca_file=None, timeout=1800):
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        raise ValueError("Monitor clients require HTTPS outside loopback")
    if not path.startswith(("/v1/", "/openapi.json")):
        raise ValueError("Unsupported API path")
    req = Request(url.rstrip("/") + path, data=json.dumps(data).encode() if data is not None else None,
                  method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(req, context=ssl.create_default_context(cafile=ca_file), timeout=timeout) as response:
        return json.loads(response.read(16 * 1024**2))


def upload(url, token, release, path, ca_file=None):
    # Use the normal client to enforce TLS policy before sending a large body.
    request(url, token, "GET", "/openapi.json", ca_file=ca_file)
    with open(path, "rb") as body:
        req = Request(url.rstrip("/") + "/v1/packages/" + release, method="POST", data=body,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream",
                               "Content-Length": str(Path(path).stat().st_size)})
        with urlopen(req, context=ssl.create_default_context(cafile=ca_file), timeout=1800) as response:
            return json.loads(response.read(65536))


def download(url, token, release, destination, ca_file=None):
    import uuid
    release = str(uuid.UUID(release))
    request(url, token, "GET", "/openapi.json", ca_file=ca_file)
    req = Request(url.rstrip("/") + "/v1/packages/" + release, headers={"Authorization": "Bearer " + token})
    path = Path(destination)
    created = False
    try:
        with urlopen(req, context=ssl.create_default_context(cafile=ca_file), timeout=1800) as response, open(path, "xb") as out:
            created = True
            path.chmod(0o600)
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 64 * 1024**3:
                    raise ValueError("Download exceeds limit")
                out.write(chunk)
        return {"downloaded": str(path.resolve())}
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
