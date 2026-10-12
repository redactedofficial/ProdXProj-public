"""Optional central monitor: authenticated reports, inventory and management API."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .providers import identifier, manage

AUDIT_ROUTES = {"/openapi.json", "/v1/nodes", "/v1/events", "/v1/releases", "/v1/report", "/v1/manage", "/v1/plans", "/v1/packages/{id}", "/v1/plans/{id}/execute", "/unknown"}


def audit_route(path):
    import re
    path = path.split("?", 1)[0]
    if path in AUDIT_ROUTES:
        return path
    if re.fullmatch(r"/v1/packages/[a-fA-F0-9-]{36}", path):
        return "/v1/packages/{id}"
    if re.fullmatch(r"/v1/plans/[a-fA-F0-9-]{36}/execute", path):
        return "/v1/plans/{id}/execute"
    return "/unknown"

OPENAPI = {
    "openapi": "3.1.0", "info": {"title": "ProdXProj monitor", "version": "0.1.2"},
    "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearerAuth": []}],
    "paths": {path: {method: {"summary": title, "responses": {"200": {"description": "JSON result"},
                "401": {"description": "Authentication required"}, "400": {"description": "Invalid request"}}}}
              for path, method, title in [("/v1/nodes", "get", "View production inventory (viewer/admin)"),
                                         ("/v1/report", "post", "Report a node heartbeat (node token)"),
                                         ("/v1/manage", "post", "Manage Proxmox/Docker (admin)"),
                                         ("/v1/plans", "post", "Prepare destructive recovery plan (admin)"),
                                         ("/v1/plans/{id}/execute", "post", "Execute reviewed recovery plan (admin)")]}}

OPENAPI["components"]["schemas"] = {
    "Report": {"type": "object", "additionalProperties": False, "required": ["node_id", "health"],
               "properties": {**{key: {"type": "string", "maxLength": 2048} for key in
                              ("node_id", "hostname", "address", "release", "version", "project", "detail", "last_backup")},
                              "health": {"enum": ["healthy", "unhealthy", "unknown"]},
                              "uptime": {"type": "number", "minimum": 0}, "disk_free": {"type": "integer", "minimum": 0}}},
    "InfrastructureRequest": {"type": "object", "required": ["provider", "action"], "additionalProperties": False,
                              "properties": {"provider": {"enum": ["proxmox", "docker"]},
                                             "action": {"enum": ["nodes", "resources", "status", "start", "shutdown", "stop", "reboot",
                                                                   "backup", "create-lxc", "restore-lxc", "install-docker", "containers", "info", "restart"]},
                                             **{k: {"type": "string"} for k in ("node", "kind", "storage", "archive", "hostname", "ostemplate", "bridge", "ip", "gateway", "ssh_public_key", "container")},
                                             **{k: {"type": "integer", "minimum": 1} for k in ("vmid", "disk_gb", "cores", "memory_mb")},
                                             "docker": {"type": "boolean"}}},
    "RecoveryPlan": {"type": "object", "required": ["provider", "action", "node", "vmid"],
                     "properties": {"provider": {"const": "proxmox"}, "action": {"const": "destroy"},
                                    "node": {"type": "string"}, "vmid": {"type": "integer", "minimum": 100},
                                    "kind": {"enum": ["lxc", "qemu"]}, "storage": {"type": "string"}}},
    "PlanConfirmation": {"type": "object", "required": ["confirm"], "properties": {"confirm": {"type": "string"}}},
}
for path, schema in (("/v1/report", "Report"), ("/v1/manage", "InfrastructureRequest"),
                     ("/v1/plans", "RecoveryPlan"), ("/v1/plans/{id}/execute", "PlanConfirmation")):
    OPENAPI["paths"][path]["post"]["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + schema}}}}
OPENAPI["paths"]["/v1/report"]["post"]["parameters"] = [{"name": "X-Prox-Node", "in": "header", "required": True, "schema": {"type": "string"}}]
for path in ("/v1/events", "/v1/releases", "/v1/packages/{id}"):
    OPENAPI["paths"][path] = {"get": {"summary": "Authenticated read", "responses": {"200": {"description": "JSON metadata or encrypted artifact"}}}}
OPENAPI["paths"]["/v1/packages/{id}"]["get"]["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}]
OPENAPI["paths"]["/v1/packages/{id}"]["post"] = {"summary": "Store encrypted package (admin)",
    "parameters": OPENAPI["paths"]["/v1/packages/{id}"]["get"]["parameters"],
    "requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
    "responses": {"200": {"description": "Stored"}, "409": {"description": "Already exists"}}}
OPENAPI["paths"]["/v1/releases"]["post"] = {"summary": "Publish redacted topology summaries (admin)",
    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "array", "maxItems": 100, "items": {"type": "object"}}}}},
    "responses": {"200": {"description": "Published"}}}
OPENAPI["paths"]["/v1/plans/{id}/execute"]["post"]["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}]


class Store:
    def __init__(self, path, stale_after=180):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stale_after = stale_after
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, seen REAL, report TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, node TEXT, at REAL, health TEXT, release TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, expires REAL, payload TEXT, state TEXT, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, manifest TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL, actor TEXT, method TEXT, route TEXT, resource TEXT, outcome INTEGER, error TEXT)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def report(self, value):
        allowed = {"node_id", "hostname", "address", "release", "version", "health", "uptime", "disk_free", "project", "detail", "last_backup", "service_type", "service_name", "service_state", "startup"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Unknown report fields")
        identifier(value["node_id"])
        if value.get("health") not in ("healthy", "unhealthy", "unknown"):
            raise ValueError("Invalid health")
        for key, item in value.items():
            if key in ("uptime", "disk_free"):
                if type(item) not in (int, float) or not 0 <= item < 10**18:
                    raise ValueError("Invalid metric")
            elif not isinstance(item, str) or len(item) > 2048:
                raise ValueError("Invalid report value")
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?, ?)", (value["node_id"], now, json.dumps(value)))
            db.execute("INSERT INTO events (node, at, health, release) VALUES (?, ?, ?, ?)",
                       (value["node_id"], now, value["health"], value.get("release")))
            # Retain a bounded history. Node inventory persists independently.
            db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id)-100000 FROM events)")
        return {"accepted": True}

    def nodes(self):
        with self.connect() as db:
            return [{**json.loads(report), "last_seen": seen, "online": time.time()-seen <= self.stale_after}
                    for seen, report in db.execute("SELECT seen, report FROM nodes ORDER BY id")]

    def events(self):
        with self.connect() as db:
            heartbeat = [{"kind": "heartbeat", "node": n, "at": at, "health": h, "release": r}
                    for n, at, h, r in db.execute("SELECT node, at, health, release FROM events ORDER BY id DESC LIMIT 500")]
            administration = [{"kind": "audit", "at": at, "actor": actor, "method": method, "route": route,
                               "resource": json.loads(resource), "status": outcome, "error_type": error}
                              for at, actor, method, route, resource, outcome, error in db.execute("SELECT at, actor, method, route, resource, outcome, error FROM audit ORDER BY id DESC LIMIT 500")]
            return sorted(heartbeat + administration, key=lambda value: value["at"], reverse=True)[:500]

    def audit(self, actor, method, route, resource, outcome, error=None):
        import re
        actor = actor if re.fullmatch(r"admin|unauthenticated|viewer:\d{1,4}|node:[A-Za-z0-9_.-]{1,128}", actor) else "unknown"
        method = method if method in ("GET", "POST") else "OTHER"
        route = route if route in AUDIT_ROUTES else "/unknown"
        resource = {key: value for key, value in resource.items() if key in ("provider", "action", "node", "vmid", "kind", "container")
                    and (type(value) is int and 0 <= value < 10**10 or isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value))}
        outcome = outcome if type(outcome) is int and 100 <= outcome <= 599 else 500
        error = error if isinstance(error, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error) else None
        with self.connect() as db:
            db.execute("INSERT INTO audit (at, actor, method, route, resource, outcome, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (time.time(), actor, method, route, json.dumps(resource), outcome, error))
            db.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id)-10000 FROM audit)")

    def releases(self, values=None, offset=0):
        from .history import public_metadata
        if values is not None:
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError("Publish up to 100 releases per request")
            with self.connect() as db:
                for value in values:
                    if not isinstance(value, dict) or any(k in value for k in ("config", "credentials", "history")):
                        raise ValueError("Release metadata must be redacted")
                    for key in ("id", "project_id", "name", "version", "branch", "created", "creator", "machine", "changes"):
                        if key not in value:
                            raise ValueError("Incomplete release metadata")
                    uuid.UUID(value["id"])
                    db.execute("INSERT OR REPLACE INTO releases VALUES (?, ?)", (value["id"], json.dumps(public_metadata(value))))
            return {"published": len(values)}
        with self.connect() as db:
            result = []
            for release, raw in db.execute("SELECT id, manifest FROM releases ORDER BY id LIMIT 50 OFFSET ?", (offset,)).fetchall():
                original = json.loads(raw)
                value = public_metadata(original)
                if value != original:
                    db.execute("UPDATE releases SET manifest=? WHERE id=?", (json.dumps(value), release))
                result.append(value)
            return result

    def plan(self, data):
        # Plan preparation has no infrastructure side effects. Execution rechecks
        # current state and makes its own backup, so a plan cannot reuse stale proof.
        if data.get("provider") != "proxmox" or data.get("action") != "destroy":
            raise ValueError("Recovery plans currently support Proxmox guest destruction")
        identifier(data["node"])
        from .providers import bounded
        bounded(data["vmid"], 100, 999999999)
        if data.get("kind", "lxc") not in ("lxc", "qemu"):
            raise ValueError("Invalid guest kind")
        from .providers import Proxmox
        config = Proxmox().request("GET", f"/nodes/{identifier(data['node'])}/{data.get('kind', 'lxc')}/{data['vmid']}/config")
        data = dict(data)
        data["identity_hash"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        plan_id = str(uuid.uuid4())
        expires = time.time() + 600
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?, ?, ?, 'pending', NULL)", (plan_id, expires, json.dumps(data)))
        return {"plan_id": plan_id, "expires": expires, "resource": data,
                "steps": ["Check external data mounts", "Create stop-mode vzdump backup", "Confirm backup volume exists", "Stop guest", "Delete exact guest"],
                "confirm": f"destroy:{data['node']}:{data['vmid']}"}

    def execute_plan(self, plan_id, confirmation):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT expires, payload, state FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row or row[0] < time.time() or row[2] != "pending":
                raise ValueError("Plan missing, expired or already used")
            data = json.loads(row[1])
            if confirmation != f"destroy:{data['node']}:{data['vmid']}":
                raise ValueError("Confirmation does not match the exact guest")
            db.execute("UPDATE plans SET state='running' WHERE id=?", (plan_id,))
        try:
            from .providers import Proxmox
            config = Proxmox().request("GET", f"/nodes/{identifier(data['node'])}/{data.get('kind', 'lxc')}/{data['vmid']}/config")
            if hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() != data["identity_hash"]:
                raise ValueError("Guest configuration changed; prepare a new recovery plan")
            result = manage(data)
        except Exception:
            with self.connect() as db:
                db.execute("UPDATE plans SET state='failed' WHERE id=?", (plan_id,))
            raise
        with self.connect() as db:
            db.execute("UPDATE plans SET state='completed', result=? WHERE id=?", (json.dumps(result), plan_id))
        return result


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, admin, viewers, node_tokens):
        if len(admin) < 32 or any(len(t) < 32 for t in list(viewers) + list(node_tokens.values())):
            raise ValueError("Monitor tokens must be at least 32 characters")
        if len(set([admin] + list(viewers) + list(node_tokens.values()))) != 1 + len(viewers) + len(node_tokens):
            raise ValueError("Admin, viewer and node tokens must be distinct")
        super().__init__(address, Handler)
        self.store, self.admin, self.viewers, self.node_tokens = store, admin, viewers, node_tokens
        self.rate_lock, self.rate = threading.Lock(), {}
        self.limit = threading.BoundedSemaphore(16)
        self.tls_context = None

    def configure_tls(self, context):
        self.tls_context = context

    def process_request(self, request, client_address):
        # A bounded thread pool prevents slow authenticated callers exhausting memory.
        if not self.limit.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            if self.tls_context:
                request.settimeout(3)
                request = self.tls_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                request.do_handshake()
            super().process_request_thread(request, client_address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.limit.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "ProdXProj"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass

    def reply(self, status, value):
        self._outcome = status
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def role(self):
        token = self.headers.get("Authorization", "")
        if not token.startswith("Bearer "):
            return None
        token = token[7:]
        if hmac.compare_digest(token, self.server.admin):
            self._actor = "admin"
            return "admin"
        for index, value in enumerate(self.server.viewers):
            if hmac.compare_digest(token, value):
                self._actor = "viewer:" + str(index)
                return "viewer"
        node = self.headers.get("X-Prox-Node", "")
        expected = self.server.node_tokens.get(node)
        if expected and hmac.compare_digest(token, expected):
            self._actor = "node:" + node
            return "node"
        return None

    def dispatch(self, method):
        now = time.monotonic()
        with self.server.rate_lock:
            rates = self.server.rate
            ip = self.client_address[0]
            recent = [at for at in rates.get(ip, []) if now-at < 1]
            if len(recent) >= 10:
                return self.reply(429, {"error": "rate_limited"})
            if len(rates) > 1024:
                self.server.rate = {k: v for k, v in rates.items() if v and now-v[-1] < 60}
                rates = self.server.rate
            rates[ip] = recent + [now]
        role = self.role()
        if role is None:
            return self.reply(401, {"error": "authentication_required"})
        path = self.path.split("?", 1)[0]
        if method == "GET":
            if role not in ("admin", "viewer"):
                return self.reply(403, {"error": "forbidden"})
            if path == "/v1/nodes":
                return self.reply(200, self.server.store.nodes())
            if path == "/v1/events":
                return self.reply(200, self.server.store.events())
            if path == "/v1/releases":
                offset = int(parse_qs(urlsplit(self.path).query).get("offset", ["0"])[0])
                if not 0 <= offset <= 1000000:
                    raise ValueError("Invalid release pagination offset")
                return self.reply(200, self.server.store.releases(offset=offset))
            if path.startswith("/v1/packages/"):
                release = str(uuid.UUID(path.split("/")[-1]))
                package = self.server.store.path.parent / "packages" / (release + ".proxproj")
                if not package.is_file():
                    return self.reply(404, {"error": "not_found"})
                self.send_response(200)
                self._outcome = 200
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(package.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                import shutil
                with open(package, "rb") as inp:
                    shutil.copyfileobj(inp, self.wfile, 1024 * 1024)
                return
            if path == "/openapi.json":
                return self.reply(200, OPENAPI)
            return self.reply(404, {"error": "not_found"})
        if role != "admin" and not (role == "node" and path == "/v1/report"):
            return self.reply(403, {"error": "forbidden"})
        if self.headers.get("Transfer-Encoding"):
            return self.reply(400, {"error": "unsupported_encoding"})
        size = int(self.headers.get("Content-Length", "0"))
        if path.startswith("/v1/packages/"):
            if role != "admin" or not 52 <= size <= 64 * 1024**3:
                return self.reply(400, {"error": "invalid_package_upload"})
            release = str(uuid.UUID(path.split("/")[-1]))
            folder = self.server.store.path.parent / "packages"
            folder.mkdir(mode=0o700, exist_ok=True)
            final = folder / (release + ".proxproj")
            if final.exists():
                return self.reply(409, {"error": "package_exists"})
            temporary = folder / (release + ".partial-" + uuid.uuid4().hex)
            try:
                with open(temporary, "xb") as out:
                    temporary.chmod(0o600)
                    magic = self.rfile.read(8)
                    if magic != b"PROXENC1":
                        raise ValueError("Central storage accepts encrypted packages only")
                    out.write(magic)
                    remaining = size - 8
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Interrupted upload")
                        out.write(chunk)
                        remaining -= len(chunk)
                # Atomic hard-link with exclusive destination prevents replacement races.
                os.link(temporary, final)
                return self.reply(200, {"stored": release})
            finally:
                temporary.unlink(missing_ok=True)
        if not 0 < size <= 65536:
            return self.reply(413, {"error": "body_too_large"})
        data = json.loads(self.rfile.read(size))
        if path == "/v1/releases":
            if role != "admin":
                return self.reply(403, {"error": "forbidden"})
            return self.reply(200, self.server.store.releases(data))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        import re
        self._resource = {k: v for k, v in data.items() if k in ("provider", "action", "node", "vmid", "kind", "container")
                          and (type(v) is int or isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", v))}
        if path == "/v1/report":
            if role != "node" or data.get("node_id") != self.headers.get("X-Prox-Node"):
                return self.reply(403, {"error": "node_identity_mismatch"})
            result = self.server.store.report(data)
        elif path == "/v1/manage":
            if data.get("action") == "destroy":
                return self.reply(400, {"error": "recovery_plan_required"})
            result = manage(data)
        elif path == "/v1/plans":
            result = self.server.store.plan(data)
        elif path.startswith("/v1/plans/") and path.endswith("/execute"):
            result = self.server.store.execute_plan(path.split("/")[3], data.get("confirm"))
        else:
            return self.reply(404, {"error": "not_found"})
        return self.reply(200, result)

    def handle_method(self, method):
        self._actor, self._resource, self._outcome = "unauthenticated", {}, 500
        error_type = None
        try:
            self.dispatch(method)
        except Exception as error:
            error_type = type(error).__name__
            # Provider exceptions may include URLs, credentials, SSH output or API bodies.
            self.reply(400, {"error": "operation_failed", "message": "Check request fields, provider access and task state"})
        finally:
            # Store only fixed endpoint categories and validated identifiers, never
            # raw query strings, authorization values or exception messages.
            route = audit_route(self.path)
            self.server.store.audit(self._actor, method, route, self._resource, self._outcome, error_type)

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")


def serve(args):
    admin = os.environ[args.admin_token_env]
    viewers = json.loads(os.environ.get(args.viewer_tokens_env, "[]"))
    nodes = json.loads(Path(args.node_tokens).read_text(encoding="utf-8"))
    if args.bind not in ("127.0.0.1", "::1", "localhost") and not (args.cert and args.key):
        raise ValueError("A non-loopback monitor must use TLS")
    server = Server((args.bind, args.port), Store(args.database), admin, viewers, nodes)
    if args.cert or args.key:
        if not args.cert or not args.key:
            raise ValueError("Both TLS certificate and key are required")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
        server.configure_tls(context)
    try:
        server.serve_forever()
    finally:
        server.server_close()
