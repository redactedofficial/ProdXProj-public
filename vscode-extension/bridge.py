"""One bounded JSON request, using the reviewed backend bundled in this VSIX."""
import contextlib
import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))


def dispatch(value):
    from proxrunner import archive, cli, history
    from proxrunner.config import load
    from proxrunner.layout import Inclusion
    from proxrunner.planning import check_script, shipping_plan, variables
    root = Path(value["root"]).resolve(strict=True)
    action = value["action"]
    if action == "check":
        path = archive.inside(root, value["path"])
        return check_script(path, value.get("text"))
    cfg = load(root / "proxproject.json")
    target = value.get("target")
    if target is not None and target not in cfg.get("targets", {}):
        raise ValueError("Unknown target")
    if action == "plan":
        options = value.get("options", {})
        result = shipping_plan(root, cfg, Inclusion(bool(options.get("env")), bool(options.get("keys")),
                                                   bool(options.get("data")), bool(options.get("secrets"))), target)
        result["variables"] = variables(root, cfg, target, bool(value.get("reveal")))
        result["targets"] = list(cfg.get("targets", {}))
        result["history"] = [history.public_metadata(x) for x in history.all_releases(root)]
        return result
    commands = {
        "test": ["test-local"],
        "run": ["run-script", value.get("path", ""), "--execute"],
        "emulate-plan": ["emulate", "plan"], "emulate-up": ["emulate", "up"],
        "emulate-status": ["emulate", "status"], "emulate-down": ["emulate", "down"],
        "emulate-test": ["emulate", "test"],
        "remote-preview": ["remote-gen", "--target", target or ""],
        "remote-setup": ["remote-gen", "--target", target or "", "--execute"],
    }
    if action.startswith("service-") and action[8:] in ("status", "start", "stop", "restart", "enable", "disable"):
        commands[action] = ["service", action[8:], "--target", target or ""]
    if action not in commands:
        raise ValueError("Unsupported editor operation")
    arguments = commands[action]
    if action.startswith("emulate-") and "replicas" in value:
        count = value["replicas"]
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError("Replicas must be between 1 and 20")
        arguments += ["--replicas", str(count)]
    if action.startswith("emulate-") and value.get("options", {}).get("env"):
        arguments += ["--include-env"]
    with contextlib.redirect_stdout(io.StringIO()) as output:
        cli.execute(cli.parser().parse_args(["--project", str(root)] + arguments))
    return json.loads(output.getvalue())


def alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Denied access is conservatively alive.
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_record(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with open(temporary, 'x', encoding='utf-8') as out:
        temporary.chmod(0o600)
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def request(value):
    root = Path(value['root']).resolve(strict=True)
    action = value['action']
    readonly = {'plan', 'check', 'emulate-plan', 'emulate-status', 'remote-preview'}
    if action in readonly:
        return dispatch(value)
    allowed = {'operation-status', 'operation-reconcile', 'test', 'run', 'emulate-up', 'emulate-down', 'emulate-test',
               'remote-setup'} | {'service-' + x for x in ('status', 'start', 'stop', 'restart', 'enable', 'disable')}
    if action not in allowed:
        raise ValueError('Unsupported editor operation')
    folder = root / '.prox/editor-operations'
    if not folder.resolve().is_relative_to(root):
        raise ValueError('Operation folder escapes workspace')
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = folder / 'pending.json'
    if lock.is_symlink():
        raise ValueError('Unsafe operation lock')
    pending = json.loads(lock.read_text()) if lock.exists() else None
    if action == 'operation-status':
        records = [json.loads(p.read_text()) for p in sorted(folder.glob('*.json')) if p.name != 'pending.json' and not p.is_symlink()]
        if pending and not alive(pending['pid']):
            pending = {**pending, 'state': 'unknown', 'note': 'Inspect actual effects before acknowledging.'}
        return {'pending': pending, 'operations': records[-100:]}
    if action == 'operation-reconcile':
        if not pending or value.get('pendingId') != pending['operationId'] or value.get('inspected') is not True:
            raise ValueError('Matching interrupted operation acknowledgement required')
        if alive(pending['pid']):
            raise ValueError('Operation process is still running; acknowledgement blocked')
        pending.update(state='acknowledged', completed=datetime.now(timezone.utc).isoformat(),
                       note='Operator inspected effects; this does not cancel or prove remote completion.')
        write_record(folder / (pending['operationId'] + '.json'), pending)
        lock.unlink()
        return {'acknowledged': pending['operationId']}
    if pending:
        raise ValueError('Prior operation is running or needs reconciliation; conflicting retries blocked')
    operation_id = str(uuid.UUID(value.get('operationId', str(uuid.uuid4()))))
    record = {'operationId': operation_id, 'pid': os.getpid(), 'action': action, 'target': value.get('target'),
              'state': 'running', 'started': datetime.now(timezone.utc).isoformat()}
    with open(lock, 'x', encoding='utf-8') as out:
        lock.chmod(0o600)
        json.dump(record, out)
        out.flush()
        os.fsync(out.fileno())
    record_path = folder / (operation_id + '.json')
    try:
        write_record(record_path, record)
        result = dispatch(value)
        record.update(state='completed', completed=datetime.now(timezone.utc).isoformat())
        write_record(record_path, record)
        lock.unlink()
        return result
    except BaseException as error:
        record.update(state='unknown', error=type(error).__name__)
        write_record(record_path, record)
        write_record(lock, record)
        raise


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Editor request too large")
        print(json.dumps({"ok": True, "result": request(json.loads(raw))}))
    except Exception as error:
        # Exception text can contain project secrets; use a stable public error.
        print(json.dumps({"ok": False, "error": type(error).__name__,
                          "message": "Operation failed. Infrastructure completion may be unknown; inspect operation history and actual state before retrying. Also check project configuration and Python dependencies."}))
        sys.exit(1)
