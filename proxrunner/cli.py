import argparse
import copy
import getpass
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from . import archive, crypto, history
from .config import load, template, validate


def password_for(path=None, encrypt=False, password_env=None):
    encrypted = encrypt
    if path:
        with open(path, "rb") as stream:
            encrypted = stream.read(8) == crypto.MAGIC
    if not encrypted:
        return None
    if password_env:
        value = os.environ.get(password_env)
        if not value:
            raise ValueError("Password environment variable is empty")
        return crypto.creation_password(value) if encrypt else value
    value = getpass.getpass("Package password: ")
    if encrypt and value != getpass.getpass("Repeat password: "):
        raise ValueError("Passwords do not match")
    return crypto.creation_password(value) if encrypt else value


def save_config(path, cfg):
    validate(cfg)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def parser():
    p = argparse.ArgumentParser(prog="prodx", description="ProdXProj: production packages, release topology and central monitoring")
    p.add_argument("--project", default=".", help="Project workspace")
    p.add_argument("--gui", action="store_true", help="Open the native desktop project explorer and shipping emulator")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("test-local", help="Run optional project-specific tests in the local workspace")
    emulate = sub.add_parser("emulate", help="Generate and run isolated local Docker replicas")
    emulate.add_argument("operation", choices=("plan", "prepare", "up", "status", "down", "test"), default="plan", nargs="?")
    emulate.add_argument("--replicas", type=int)
    emulate.add_argument("--include-env", action="store_true")
    plan = sub.add_parser("plan", help="Read-only file, variable, script and deployment visualization data")
    plan.add_argument("--target")
    plan.add_argument("--include-env", action="store_true")
    plan.add_argument("--include-keys", action="store_true")
    plan.add_argument("--include-data", action="store_true")
    plan.add_argument("--include-secrets", action="store_true")
    plan.add_argument("--include-all", action="store_true")
    plan.add_argument("--version")
    check = sub.add_parser("check-script", help="Parse a local script without executing it")
    check.add_argument("path")
    run_script = sub.add_parser("run-script", help="Explicitly run a saved local script after syntax checks")
    run_script.add_argument("path")
    run_script.add_argument("--execute", action="store_true")
    setup = sub.add_parser("remote-gen", help="Preview or run SSH-based production prerequisites and health setup")
    setup.add_argument("--target", required=True)
    setup.add_argument("--key-file")
    setup.add_argument("--package", action="append", default=[], help="Additional OS package name; repeatable")
    setup.add_argument("--install-docker", action="store_true")
    setup.add_argument("--allow-sudo", action="store_true")
    setup.add_argument("--url", help="Central monitor HTTPS URL")
    setup.add_argument("--node-id")
    setup.add_argument("--token-env", default="PROX_REPORT_TOKEN")
    setup.add_argument("--ca-file")
    setup.add_argument("--execute", action="store_true")
    keys = sub.add_parser("keygen", help="Generate a new target-scoped Ed25519 key without overwriting existing keys")
    keys.add_argument("--target", required=True)
    service = sub.add_parser("service", help="Manage/check the active project's systemd or Docker Compose service")
    service.add_argument("operation", choices=("status", "start", "stop", "restart", "enable", "disable"))
    service.add_argument("--target", required=True)
    service.add_argument("--key-file")
    init = sub.add_parser("init", help="Configure any existing project folder")
    init.add_argument("name")
    for name in ("pack", "compile"):
        command = sub.add_parser(name, help="Create a versioned package" + (" after configured build commands" if name == "compile" else ""))
        command.add_argument("--output")
        command.add_argument("--version")
        command.add_argument("--encrypt", action="store_true")
        command.add_argument("--password-env")
        command.add_argument("--include-keys", action="store_true")
        command.add_argument("--include-env", action="store_true")
        command.add_argument("--include-data", action="store_true")
        command.add_argument("--include-secrets", action="store_true", help="Include local credential files in encrypted package only")
        command.add_argument("--include-all", action="store_true", help="Encrypted full snapshot, including dependencies, environment, selected data and credentials")
        command.add_argument("--parent", help="Parent release UUID")
        command.add_argument("--message", default="", help="Why this release was made")
    for name in ("inspect", "unpack", "import-history", "deploy"):
        command = sub.add_parser(name)
        command.add_argument("package")
        command.add_argument("--password-env")
        if name == "unpack":
            command.add_argument("destination")
            command.add_argument("--include-data", action="store_true")
            command.add_argument("--include-credentials", action="store_true", help="Explicitly export plaintext credentials under destination/secrets/")
        if name == "deploy":
            command.add_argument("--target", required=True)
            command.add_argument("--key-file")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--trusted-sha256", help="Expected artifact digest obtained through an independently trusted channel")
    sub.add_parser("history", help="Show local version graph metadata")
    compare = sub.add_parser("compare", help="Compare two release packages")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--password-env")
    compare.add_argument("--text", action="store_true", help="Include text diffs for ordinary application files (up to 1 MiB each)")
    recover = sub.add_parser("recover", help="Review/run configured recovery adapters after preserving an off-server backup")
    recover.add_argument("operation", choices=("rebuild", "reinstall", "nuke"))
    recover.add_argument("--target", required=True)
    recover.add_argument("--execute", action="store_true")
    recover.add_argument("--password-env")
    recover.add_argument("--key-file")
    recovery_open = sub.add_parser("open-backup", help="Unlock an encrypted recovery snapshot to a new ZIP file")
    recovery_open.add_argument("backup")
    recovery_open.add_argument("destination")
    recovery_open.add_argument("--password-env")
    monitor = sub.add_parser("monitor", help="Run central monitoring and management API")
    monitor.add_argument("--bind", default="127.0.0.1")
    monitor.add_argument("--port", type=int, default=8443)
    monitor.add_argument("--database", default="secrets/monitor/state.db")
    monitor.add_argument("--node-tokens", required=True, help="JSON object mapping node IDs to unique report tokens")
    monitor.add_argument("--admin-token-env", default="PROX_MONITOR_ADMIN_TOKEN")
    monitor.add_argument("--viewer-tokens-env", default="PROX_MONITOR_VIEWER_TOKENS")
    monitor.add_argument("--cert")
    monitor.add_argument("--key")
    api = sub.add_parser("api", help="Authenticated JSON interface to central monitor")
    api.add_argument("--url", required=True)
    api.add_argument("--token-env", default="PROX_MONITOR_TOKEN")
    api.add_argument("--ca-file")
    api.add_argument("--method", choices=("GET", "POST"), default="GET")
    api.add_argument("--body-file", help="JSON request file; never put credentials in command arguments")
    api.add_argument("path")
    for name in ("publish", "fetch", "sync-history"):
        command = sub.add_parser(name, help="Share encrypted packages and custom history through the monitor")
        command.add_argument("--url", required=True)
        command.add_argument("--token-env", default="PROX_MONITOR_TOKEN")
        command.add_argument("--ca-file")
        if name == "publish":
            command.add_argument("package")
            command.add_argument("--password-env")
        if name == "fetch":
            command.add_argument("release")
            command.add_argument("destination")
    reporter = sub.add_parser("install-reporter", help="Install a server-side cron heartbeat")
    reporter.add_argument("--target", required=True)
    reporter.add_argument("--url", required=True)
    reporter.add_argument("--node-id", required=True)
    reporter.add_argument("--token-env", default="PROX_REPORT_TOKEN")
    reporter.add_argument("--ca-file", help="CA certificate path already present on the remote server")
    reporter.add_argument("--key-file")
    backups = sub.add_parser("install-backups", help="Install independent server-side backup scheduling")
    backups.add_argument("--target", required=True)
    backups.add_argument("--every-minutes", type=int, default=30)
    backups.add_argument("--key-file")
    topology = sub.add_parser("map", help="Write a standalone topology view")
    topology.add_argument("--output", default="production-topology.html")
    for name in ("branch", "fork"):
        command = sub.add_parser(name)
        command.add_argument("name")
        command.add_argument("--from-release", help="Release UUID to split from")
    for name in ("status", "backup", "backup-loop", "rollback", "restore-backup"):
        command = sub.add_parser(name)
        command.add_argument("--target", required=True)
        command.add_argument("--package", help="Use encrypted package configuration and embedded SSH key")
        command.add_argument("--password-env")
        command.add_argument("--key-file")
        if name == "backup-loop":
            command.add_argument("--interval", type=int, default=3600, help="Seconds between backups; client must stay running")
        if name == "rollback":
            command.add_argument("release", help="Previously installed release UUID")
        if name == "restore-backup":
            command.add_argument("backup", help="Snapshot filename from status")
            command.add_argument("destination", help="New relative directory under server restores/")
    return p


def output(value):
    print(json.dumps(value, indent=2))


def execute(args):
    root = Path(args.project).resolve()
    config_path = root / "proxproject.json"
    if args.gui:
        from .gui import launch
        return launch(root)
    if args.command == "test-local":
        cfg = load(config_path)
        if not cfg.get("test"):
            raise ValueError("Configure optional test commands in proxproject.json first")
        archive.run_commands(cfg["test"], root, cfg.get("timeout", 300))
        output({"local_tests_passed": True, "commands": len(cfg["test"])})
        return
    if args.command == "emulate":
        from .emulation import operate
        output(operate(root, load(config_path), args.operation, args.replicas, args.include_env))
        return
    if args.command in ("plan", "check-script", "run-script", "remote-gen", "keygen", "service"):
        if args.command == "keygen":
            from .bootstrap import keygen
            output(keygen(root, args.target))
        elif args.command in ("check-script", "run-script"):
            from .planning import check_script
            import shutil
            import subprocess
            path = archive.inside(root, args.path)
            result = check_script(path)
            if args.command == "run-script" and args.execute:
                if result["valid"] is False:
                    raise ValueError("Fix script syntax before running it")
                runtimes = {".py": [sys.executable], ".sh": ["sh"], ".js": ["node"], ".ps1": ["pwsh" if shutil.which("pwsh") else "powershell", "-NoProfile", "-File"]}
                if path.suffix not in runtimes:
                    raise ValueError("Select a Python, shell, JavaScript or PowerShell script")
                cfg = load(config_path) if config_path.exists() else {}
                env = os.environ.copy()
                env.update(cfg.get("parameters", {}))
                process = subprocess.run(runtimes[path.suffix] + [str(path)], cwd=root, env=env,
                                         timeout=cfg.get("timeout", 300), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result.update(executed=True, returncode=process.returncode, console="suppressed to protect credentials")
            output(result)
        else:
            cfg = load(config_path)
            if args.command == "plan":
                from .layout import Inclusion
                from .planning import shipping_plan, variables
                if args.version:
                    cfg["version"] = args.version
                options = Inclusion(args.include_env or args.include_all, args.include_keys or args.include_all,
                                    args.include_data or args.include_all, args.include_secrets, args.include_all)
                result = shipping_plan(root, cfg, options, args.target)
                result["variables"] = variables(root, cfg, args.target)
                output(result)
            elif args.command == "remote-gen":
                from .bootstrap import generate
                output(generate(root, cfg, args.target, args.key_file, args.package, args.install_docker, args.allow_sudo,
                                args.url, args.node_id, os.environ.get(args.token_env), args.ca_file, args.execute))
            else:
                from .transport import invoke
                output(invoke(root, cfg, args.target, "service", key_file=args.key_file, operation=args.operation))
        return
    if args.command == "open-backup":
        destination = Path(args.destination)
        # Decrypt into a protected temporary location; publish only after authentication.
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory(prefix="prox-backup-") as temporary:
            plain = Path(temporary) / "snapshot.zip"
            crypto.decrypt(args.backup, plain, password_for(args.backup, password_env=args.password_env))
            with open(destination, "xb") as out, open(plain, "rb") as inp:
                destination.chmod(0o600)
                shutil.copyfileobj(inp, out, 1024 * 1024)
        output({"snapshot": str(destination.resolve())})
        return
    if args.command == "recover":
        from .recovery import recover
        cfg = load(config_path)
        password = password_for(encrypt=args.execute, password_env=args.password_env)
        output(recover(root, cfg, args.target, args.operation, args.execute, password, args.key_file))
        return
    if args.command == "monitor":
        from .monitor import serve
        serve(args)
        return
    if args.command == "api":
        from .api_client import request
        body = json.loads(Path(args.body_file).read_text()) if args.body_file else None
        output(request(args.url, os.environ[args.token_env], args.method, args.path, body, args.ca_file))
        return
    if args.command == "compare":
        import difflib
        with archive.opened(args.before, password_for(args.before, password_env=args.password_env)) as (before_zip, before), \
             archive.opened(args.after, password_for(args.after, password_env=args.password_env)) as (after_zip, after):
            diff = history.changes(before, after["files"])
            result = {"before": before["id"], "after": after["id"], "files": diff,
                      "settings": history.changes({"files": before.get("settings_hashes", {})}, after.get("settings_hashes", {}))}
            if args.text:
                result["text"] = {}
                for path in diff["modified"]:
                    if not path.startswith(("app/", "docs/", "scripts/")) or archive.sensitive(path) or path == "scripts/PRODX-HOOKS.json":
                        continue
                    if max(before["files"][path]["size"], after["files"][path]["size"]) > 1024**2:
                        continue
                    try:
                        old = before_zip.read(path).decode("utf-8").splitlines(keepends=True)
                        new = after_zip.read(path).decode("utf-8").splitlines(keepends=True)
                    except UnicodeDecodeError:
                        continue
                    result["text"][path] = "".join(difflib.unified_diff(old, new, fromfile="before/"+path, tofile="after/"+path))
            output(result)
        return
    if args.command in ("publish", "fetch", "sync-history"):
        from .api_client import request, upload, download
        token = os.environ[args.token_env]
        if args.command == "fetch":
            output(download(args.url, token, args.release, args.destination, args.ca_file))
        elif args.command == "sync-history":
            values, offset = [], 0
            while True:
                page = request(args.url, token, "GET", "/v1/releases?offset=" + str(offset), ca_file=args.ca_file)
                values.extend(page)
                if len(page) < 50:
                    break
                offset += len(page)
            for value in sorted(values, key=lambda v: v["created"]):
                history.record(root, value)
            output({"synced": len(values)})
        else:
            with open(args.package, "rb") as inp:
                if inp.read(8) != crypto.MAGIC:
                    raise ValueError("Publishing requires an encrypted package")
            with archive.opened(args.package, password_for(args.package, password_env=args.password_env)) as (_, manifest):
                values = [history.summary(value) for value in manifest.get("history", []) + [manifest]]
                values[-1]["artifact_sha256"] = archive.digest(args.package)
                # Bound both count and JSON size to the API's request limit.
                for value in values:
                    request(args.url, token, "POST", "/v1/releases", [value], args.ca_file)
                output(upload(args.url, token, manifest["id"], args.package, args.ca_file))
        return
    if args.command == "install-reporter":
        from .transport import invoke
        cfg = load(config_path)
        settings = {"url": args.url, "node_id": args.node_id, "token": os.environ[args.token_env],
                    "address": cfg["targets"][args.target]["host"]}
        if args.ca_file:
            settings["ca_file"] = args.ca_file
        output(invoke(root, cfg, args.target, "install-reporter", key_file=args.key_file,
                      settings=settings, script=Path(__file__).with_name("reporter.py").read_text()))
        return
    if args.command == "install-backups":
        from .transport import invoke
        if not 1 <= args.every_minutes <= 59:
            raise ValueError("Server backup interval must be between 1 and 59 minutes")
        cfg = load(config_path)
        output(invoke(root, cfg, args.target, "install-backups", key_file=args.key_file,
                      every_minutes=args.every_minutes, keep=cfg.get("backup_keep", 5),
                      script=Path(__file__).with_name("remote.py").read_text()))
        return
    if args.command == "init":
        root.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            raise ValueError("Project configuration already exists")
        cfg = template(args.name)
        cfg.update(project_id=str(uuid.uuid4()), branch="main")
        save_config(config_path, cfg)
        output({"config": str(config_path), "project_id": cfg["project_id"]})
        return
    if args.command in ("history", "map"):
        if args.command == "history":
            output([history.public_metadata(node) for node in history.all_releases(root)])
        else:
            history.render(root, args.output)
            output({"map": str(Path(args.output).resolve())})
        return
    if args.command in ("inspect", "unpack", "import-history", "deploy"):
        if args.command == "deploy" and not args.dry_run:
            from .trust import require
            require(root, args.package, args.trusted_sha256)
        password = password_for(args.package, password_env=args.password_env)
        with archive.opened(args.package, password) as (packed, manifest):
            if args.command == "inspect":
                # Inspection gives provenance and summaries without exposing settings/keys.
                output(history.public_metadata(manifest))
            elif args.command == "unpack":
                archive.extract(packed, manifest, args.destination, args.include_data, args.include_credentials)
                output({"unpacked": str(Path(args.destination).resolve())})
            elif args.command == "import-history":
                for node in manifest.get("history", []):
                    history.record(root, node)
                history.record(root, manifest)
                output({"imported": manifest["id"]})
            else:
                cfg = manifest["config"]
                if args.target not in cfg.get("targets", {}):
                    raise ValueError("Package has no such target")
                if not cfg.get("service") and (not cfg.get("activate") or not cfg.get("health")):
                    raise ValueError("Deployment requires activation and health-check commands in project configuration")
                if args.dry_run:
                    target = cfg["targets"][args.target]
                    output({"release": manifest["id"], "version": manifest["version"],
                            "host": target["host"], "root": target["root"], "files": len(manifest["files"]),
                            "embedded_identity": args.target in manifest.get("credentials", {}),
                            "backup_before_deploy": True, "health_check": True})
                else:
                    from .transport import invoke
                    output(invoke(root, cfg, args.target, "deploy", packed, manifest, args.key_file, trusted_sha256=args.trusted_sha256))
        return
    cfg = load(config_path) if config_path.exists() else None
    if args.command in ("branch", "fork"):
        if cfg is None:
            raise ValueError("Run prox init first")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", args.name):
            raise ValueError("Invalid branch or fork name")
        source = args.from_release or history.parent(root, cfg["project_id"], cfg.get("branch", "main"))
        if source and not history.get(root, source):
            raise ValueError("Source release is not present in local history; import its package first")
        if not source:
            raise ValueError("Create or import a release before branching")
        if args.command == "fork":
            cfg.update(fork_origin=cfg["project_id"], project_id=str(uuid.uuid4()), name=args.name, branch="main")
        else:
            cfg["branch"] = args.name
        cfg["forked_from"] = source
        save_config(config_path, cfg)
        output({"project_id": cfg["project_id"], "branch": cfg["branch"], "parent": source})
    elif args.command in ("pack", "compile"):
        if cfg is None:
            raise ValueError("Run prox init first")
        if args.version:
            cfg["version"] = args.version
        password = password_for(encrypt=args.encrypt or args.include_keys or args.include_env or args.include_data or args.include_all or args.include_secrets or bool(cfg.get("parameters")) or bool(cfg.get("emulation", {}).get("parameters")),
                                password_env=args.password_env)
        destination = args.output or str(root / "dist" / (cfg["name"] + "Ver" + cfg["version"].replace(".", "-") + ".proxproj"))
        if args.parent and not history.get(root, args.parent):
            raise ValueError("Parent release not present in local history")
        manifest = archive.pack(root, cfg, destination, password, args.include_env or args.include_all, args.include_keys or args.include_all,
                                args.include_data or args.include_all, args.command == "compile", cfg.get("branch", "main"), args.parent,
                                args.include_all, args.include_secrets, args.message)
        output({"package": str(Path(destination).resolve()), "release": manifest["id"], "parent": manifest["parent"]})
    else:
        if args.command == "backup-loop" and args.interval < 60:
            raise ValueError("Backup interval must be at least 60 seconds")
        def operation(cfg, manifest=None):
            from .transport import invoke
            fields = {"keep": cfg.get("backup_keep", 5)}
            action = args.command
            if action == "rollback":
                fields["release"] = str(uuid.UUID(args.release))
            if action == "restore-backup":
                action = "restore"
                fields.update(backup=args.backup, destination=args.destination)
            if action == "backup-loop":
                while True:
                    output(invoke(root, cfg, args.target, "backup", manifest=manifest, key_file=args.key_file, **fields))
                    time.sleep(args.interval)
            else:
                output(invoke(root, cfg, args.target, action, manifest=manifest, key_file=args.key_file, **fields))
        if args.package:
            with archive.opened(args.package, password_for(args.package, password_env=args.password_env)) as (_, manifest):
                operation(manifest["config"], manifest)
        else:
            if cfg is None:
                raise ValueError("Run prox init or supply --package")
            operation(cfg)


def main():
    try:
        command_parser = parser()
        args = command_parser.parse_args()
        if args.gui and args.command:
            command_parser.error("Use --gui on its own, with optional --project")
        if not args.gui and not args.command:
            command_parser.error("Choose a command or use --gui")
        execute(args)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # Exception text from malformed files/subprocesses may contain secret material.
        if isinstance(error, ValueError):
            print("prox: " + str(error), file=sys.stderr)
        else:
            print("prox: operation failed (" + type(error).__name__ + "); check inputs and prerequisites", file=sys.stderr)
        sys.exit(1)
