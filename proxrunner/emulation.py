"""Project-scoped local Docker replicas with an isolated, credential-free build context."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import commands
from .layout import Inclusion, iter_payload_files, runtime_destination


def validate_options(value):
    allowed = {"image", "command", "install", "replicas", "container_port", "parameters", "tests", "dockerfile"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown local Docker settings")
    for field in ("install", "tests"):
        commands(value.get(field, []))
    if value.get("command"):
        commands([value["command"]])
    if value.get("image") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}", value["image"]):
        raise ValueError("Invalid emulation image")
    if type(value.get("replicas", 1)) is not int or not 1 <= value.get("replicas", 1) <= 20:
        raise ValueError("Local replicas must be between 1 and 20")
    if value.get("container_port") is not None and (type(value["container_port"]) is not int or not 1 <= value["container_port"] <= 65535):
        raise ValueError("Invalid container port")
    for name, item in value.get("parameters", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.startswith("PROX_") or not isinstance(item, str) or "\0" in item:
            raise ValueError("Invalid local emulation parameter")
    return value


def recipe(root, cfg, replicas=None):
    root = Path(root).resolve()
    settings = dict(cfg.get("emulation", {}))
    if replicas is not None:
        settings["replicas"] = replicas
    validate_options(settings)
    dockerfile = settings.get("dockerfile") or ("Dockerfile" if (root / "Dockerfile").is_file() else None)
    notes = []
    if dockerfile:
        from .archive import inside
        if not inside(root, dockerfile).is_file():
            raise ValueError("Configured Dockerfile is missing")
        generated = None
        notes.append("Uses the existing reviewed Dockerfile; its build/run behavior is authoritative")
    else:
        command = settings.get("command")
        image, install = settings.get("image"), settings.get("install")
        if not image:
            if (root / "package.json").is_file():
                image = "node:24-slim"
                if install is None:
                    install = [["npm", "ci" if (root / "package-lock.json").exists() else "install"]]
                if not command and json.loads((root / "package.json").read_text()).get("scripts", {}).get("start"):
                    command = ["npm", "start"]
            else:
                image = "python:3.14-slim"
                if install is None:
                    install = [["python", "-m", "pip", "install", "-r", "requirements.txt"]] if (root / "requirements.txt").exists() else [["python", "-m", "pip", "install", "."]] if (root / "pyproject.toml").exists() else []
                if not command:
                    entry = next((p for p in ("app.py", "main.py") if (root / p).is_file()), None)
                    command = ["python", entry] if entry else None
            notes.append("Runtime inferred from project files; review image, installation commands and entry point before launch")
        install = install or []
        lines = ["FROM " + image, "WORKDIR /workspace", "COPY --chown=10001:10001 . /workspace"]
        lines += ["RUN " + json.dumps(cmd) for cmd in install]
        lines += ["RUN mkdir -p /prodx-data && chown 10001:10001 /prodx-data", "USER 10001:10001"]
        if command:
            lines.append("CMD " + json.dumps(command))
        generated = "\n".join(lines) + "\n"
        dockerfile = "PRODX.Dockerfile"
        if not command:
            notes.append("No runnable entry point detected: configure emulation.command before starting")
        settings["command"] = command
    return {"settings": settings, "dockerfile": dockerfile, "generated": generated, "notes": notes,
            "replicas": settings.get("replicas", 1), "project_name": "prodx-local-" + cfg["project_id"].replace("-", "")[:16]}


def prepare(root, cfg, replicas=None, include_env=False):
    root = Path(root).resolve()
    model = recipe(root, cfg, replicas)
    folder = root / ".prox/emulation" / model["project_name"]
    context = folder / "context"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    if context.exists():
        # Resolved and bounded before recursive cleanup of our generated context.
        if not context.resolve().is_relative_to((root / ".prox/emulation").resolve()) or context.is_symlink():
            raise ValueError("Unsafe generated Docker context")
        shutil.rmtree(context)
    context.mkdir(mode=0o700)
    mounts = []
    for source, package in iter_payload_files(root, cfg, Inclusion(env=include_env)):
        runtime = runtime_destination(package)
        if not runtime or runtime.startswith("data/"):
            continue
        relative = runtime[4:]
        if package.startswith("environment/"):
            destination = folder / "environment" / relative
            mounts.append({"type": "bind", "source": str(destination), "target": "/workspace/" + relative, "read_only": True})
        else:
            destination = context / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600 if package.startswith("environment/") else 0o644)
    if model["generated"]:
        (context / model["dockerfile"]).write_text(model["generated"], encoding="utf-8")
    # Exclude private material again at Docker's context boundary.
    dockerignore = context / ".dockerignore"
    existing = dockerignore.read_text(encoding="utf-8") if dockerignore.exists() else ""
    dockerignore.write_text(existing + "\n**/secrets\n**/credentials\n**/.ssh\n**/.git\n**/.prox\n.env\n.env.*\n*.proxproj\n", encoding="utf-8")
    services, volumes = {}, {}
    for number in range(1, model["replicas"] + 1):
        name, volume = "replica" + str(number), "data" + str(number)
        value = {"build": {"context": str(context), "dockerfile": model["dockerfile"]}, "restart": "no",
                 "environment": {**model["settings"].get("parameters", {}), "PROX_RELEASE_ROOT": "/workspace", "PROX_DATA_ROOT": "/prodx-data", "PROX_DEPLOY_ROOT": "/workspace"},
                 "volumes": [volume + ":/prodx-data", *mounts], "mem_limit": "512m", "cpus": 1, "pids_limit": 128,
                 "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"]}
        if model["settings"].get("command"):
            value["command"] = model["settings"]["command"]
        if model["settings"].get("container_port"):
            value["ports"] = ["127.0.0.1::" + str(model["settings"]["container_port"])]
        services[name], volumes[volume] = value, {}
    compose = folder / "compose.json"
    compose.write_text(json.dumps({"services": services, "volumes": volumes}, indent=2), encoding="utf-8")
    compose.chmod(0o600)
    return {"folder": str(folder), "compose_file": str(compose), "project_name": model["project_name"],
            "replicas": model["replicas"], "notes": model["notes"], "entry_point": model["settings"].get("command"),
            "scope": "Local replicas only; isolated data volumes; production keys/parameters are not copied"}


def operate(root, cfg, action, replicas=None, include_env=False):
    if action == "plan":
        model = recipe(root, cfg, replicas)
        model["settings"] = {k: v for k, v in model["settings"].items() if k != "parameters"}
        return model
    if action == "prepare":
        return prepare(root, cfg, replicas, include_env)
    model = recipe(root, cfg, replicas)
    folder = Path(root).resolve() / ".prox/emulation" / model["project_name"]
    if action == "up":
        prepared = prepare(root, cfg, replicas, include_env)
        if prepared["entry_point"] is None and model["generated"]:
            raise ValueError("Configure an emulation.command before starting replicas")
    compose = folder / "compose.json"
    if not compose.is_file():
        raise ValueError("Prepare or start a local Docker emulation first")
    command = ["docker", "compose", "-p", model["project_name"], "-f", str(compose)]
    if action == "up":
        subprocess.run(command + ["config", "--quiet"], check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(command + ["up", "-d", "--build", "--wait", "--wait-timeout", "120"], check=True, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif action == "down":
        subprocess.run(command + ["down"], check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"stopped": True, "data_volumes": "Preserved"}
    elif action == "test":
        tests = cfg.get("emulation", {}).get("tests", [])
        if not tests:
            raise ValueError("Configure emulation.tests to test each local replica")
        for name in json.loads(compose.read_text())["services"]:
            for test in tests:
                subprocess.run(command + ["exec", "-T", name, *test], check=True, timeout=cfg.get("timeout", 300), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"tests_passed": True, "replicas": len(json.loads(compose.read_text())["services"])}
    elif action != "status":
        raise ValueError("Unsupported emulation operation")
    result = subprocess.run(command + ["ps", "--all", "--format", "json"], check=True, timeout=30, capture_output=True)
    text = result.stdout.decode().strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    return {"project_name": model["project_name"], "containers": [{k: row.get(k) for k in ("Name", "Service", "State", "Health", "Publishers")} for row in rows]}
