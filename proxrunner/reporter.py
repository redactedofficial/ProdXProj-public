"""Linux-side reporter. No inbound port; sends authenticated HTTPS heartbeats."""
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def report(settings):
    root = Path(settings["root"])
    value = {"node_id": settings["node_id"], "hostname": platform.node(),
             "address": settings.get("address", ""), "health": "unknown",
             "disk_free": shutil.disk_usage(root).free}
    backups = sorted((root / "backups").glob("*.zip"), key=lambda p: p.stat().st_mtime_ns)
    if backups:
        value["last_backup"] = backups[-1].name
    active = root / "active.json"
    if active.exists():
        current = json.loads(active.read_text())
        value.update(release=current["release"], version=current["version"])
        stage = (root / "releases" / current["release"]).resolve()
        if not stage.is_relative_to((root / "releases").resolve()):
            raise ValueError("Unsafe active release")
        manifest = json.loads((stage / ".proxrelease.json").read_text())
        cfg = manifest["config"]
        value["project"] = manifest["name"]
        env = os.environ.copy()
        env.update(cfg.get("parameters", {}))
        env.update(PROX_RELEASE_ROOT=str(stage), PROX_DEPLOY_ROOT=str(root), PROX_DATA_ROOT=str(root / "shared"))
        if cfg.get("health"):
            value["health"] = "healthy"
            for command in cfg["health"]:
                try:
                    result = subprocess.run(command, cwd=stage, env=env, timeout=min(cfg.get("timeout", 30), 30),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if result.returncode:
                        value["health"] = "unhealthy"
                except (OSError, subprocess.TimeoutExpired):
                    value["health"] = "unhealthy"
        if cfg.get("service"):
            try:
                check = subprocess.run([sys.executable, str(stage / "scripts/PRODX-runtime.py"), "status", str(root)],
                                       capture_output=True, timeout=30)
                observed = json.loads(check.stdout) if check.returncode == 0 else {}
                value["service_type"] = cfg["service"]["type"]
                value["service_name"] = observed.get("name", "unknown")
                value["service_state"] = observed.get("active", "running" if observed.get("healthy") else "unhealthy")
                value["startup"] = observed.get("enabled", "configured" if cfg["service"].get("run_on_start", True) else "disabled")
                if not observed.get("healthy"):
                    value["health"] = "unhealthy"
                elif value["health"] == "unknown":
                    value["health"] = "healthy"
            except Exception:
                value["health"] = "unhealthy"
    try:
        value["uptime"] = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        pass
    url = settings["url"].rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("Reporter requires HTTPS")
    request = Request(url + "/v1/report", data=json.dumps(value).encode(), method="POST",
                      headers={"Authorization": "Bearer " + settings["token"], "X-Prox-Node": settings["node_id"],
                               "Content-Type": "application/json"})
    context = ssl.create_default_context(cafile=settings.get("ca_file"))
    with build_opener(HTTPSHandler(context=context), NoRedirect()).open(request, timeout=15) as response:
        return json.loads(response.read(65536))


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    if "--once" in sys.argv:
        report(settings)
        return
    while True:
        try:
            report(settings)
        except Exception:
            # Heartbeat gaps become offline state on the central monitor; no secrets logged.
            pass
        time.sleep(max(30, settings.get("interval", 60)))


if __name__ == "__main__":
    main()
