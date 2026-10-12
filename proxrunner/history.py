"""Custom local release DAG. No Git repository or hosting service required."""
import hashlib
import html
import json
import sqlite3
import hmac
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(root):
    folder = Path(root) / ".prox"
    folder.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(folder / "history.db", timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, project TEXT, branch TEXT, created TEXT, manifest TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS heads (project TEXT, branch TEXT, id TEXT, PRIMARY KEY(project, branch))")
    try:
        with db:
            yield db
    finally:
        db.close()


def parent(root, project, branch):
    with connect(root) as db:
        row = db.execute("SELECT id FROM heads WHERE project=? AND branch=?", (project, branch)).fetchone()
        return row[0] if row else None


def get(root, release):
    with connect(root) as db:
        row = db.execute("SELECT manifest FROM releases WHERE id=?", (release,)).fetchone()
        return json.loads(row[0]) if row else None


def changes(previous, files):
    old = (previous or {}).get("files", {})
    return {"added": sorted(set(files) - set(old)), "removed": sorted(set(old) - set(files)),
            "modified": sorted(p for p in files.keys() & old.keys() if files[p] != old[p])}


def record(root, manifest):
    # Only a redacted configuration fingerprint is catalogued; keys/parameters/targets
    # and environment file CONTENTS live inside the encrypted artifact.
    value = {k: v for k, v in manifest.items() if k not in ("config", "credentials", "history")}
    if manifest.get("config") is not None:
        value["private_settings"] = private_fingerprint(root, manifest["config"].get("parameters", {}))
        value["files"] = protected_files(root, value.get("files", {}))
    with connect(root) as db:
        db.execute("INSERT OR IGNORE INTO releases VALUES (?, ?, ?, ?, ?)",
                   (value["id"], value["project_id"], value["branch"], value["created"], json.dumps(value)))
        existing = db.execute("SELECT manifest FROM releases WHERE id=?", (value["id"],)).fetchone()
        if manifest.get("config") is not None or (value.get("files") and not json.loads(existing[0]).get("files")):
            db.execute("UPDATE releases SET manifest=? WHERE id=?", (json.dumps(value), value["id"]))
        db.execute("INSERT OR REPLACE INTO heads VALUES (?, ?, ?)",
                   (value["project_id"], value["branch"], value["id"]))


def all_releases(root):
    with connect(root) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT manifest FROM releases ORDER BY created, id")]


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def private_fingerprint(root, value):
    vault = Path(root) / "secrets"
    vault.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = vault / "history-hmac.key"
    try:
        with open(path, "x", encoding="ascii") as out:
            path.chmod(0o600)
            out.write(os.urandom(32).hex())
    except FileExistsError:
        pass
    key = bytes.fromhex(path.read_text(encoding="ascii"))
    return hmac.new(key, json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def protected_files(root, files):
    result = {}
    for name, meta in files.items():
        value = dict(meta)
        if name.startswith(("environment/", "credentials/", "data/")):
            value["sha256"] = private_fingerprint(root, value["sha256"])
        result[name] = value
    return result


def public_metadata(manifest):
    value = {k: v for k, v in manifest.items() if k not in ("config", "credentials", "history", "private_settings")}
    value["settings_hashes"] = {k: v for k, v in value.get("settings_hashes", {}).items() if k != "parameters"}
    if value.get("settings_fingerprint_schema") != 2:
        value.pop("config_hash", None)
    value["files"] = {name: {**meta, "sha256": "restricted"} if name.startswith(("environment/", "credentials/", "data/")) else meta
                      for name, meta in value.get("files", {}).items()}
    value["publisher_identity"] = "self-reported; execution requires independent artifact trust"
    return value


def summary(manifest):
    value = {k: v for k, v in public_metadata(manifest).items() if k != "files"}
    value["files"] = {}
    value["file_count"] = manifest.get("file_count", len(manifest.get("files", {})))
    value["change_counts"] = manifest.get("change_counts", {k: len(v) for k, v in value.get("changes", {}).items()})
    value["changes"] = {k: v[:200] for k, v in value.get("changes", {}).items()}
    value["changes_truncated"] = any(n > 200 for n in value["change_counts"].values())
    value["build_commands"] = [[arg[:256] for arg in command[:12]] for command in value.get("build_commands", [])[:8]]
    while len(json.dumps(value).encode()) > 60 * 1024:
        largest = max(value["changes"], key=lambda k: len(value["changes"][k]), default=None)
        if largest and value["changes"][largest]:
            value["changes"][largest].pop()
            value["changes_truncated"] = True
        elif value["build_commands"]:
            value["build_commands"].pop()
        else:
            raise ValueError("Topology summary exceeds API limit")
    return value


def render(root, output):
    nodes = all_releases(root)
    positions = {n["id"]: i for i, n in enumerate(nodes)}
    branches, node_lanes, lane_heads, base_lanes = {}, {}, {}, {}
    lookup = {n["id"]: n for n in nodes}
    for node in nodes:
        base = node["project_id"] + node["branch"]
        par = node.get("parent")
        parent_node = lookup.get(par)
        parent_lane = node_lanes.get(par)
        same_branch = parent_node and parent_node["project_id"] + parent_node["branch"] == base
        if same_branch and lane_heads.get(parent_lane) == par:
            lane = parent_lane
        elif base not in base_lanes:
            lane = base
            base_lanes[base] = lane
        else:
            # Imported/offline copies can create multiple children without explicitly
            # naming a branch; preserve those splits as separate topology lanes.
            lane = base + ":split:" + node["id"]
        branches.setdefault(lane, len(branches))
        node_lanes[node["id"]] = lane
        lane_heads[lane] = node["id"]
    svg, rows = [], []
    for i, node in enumerate(nodes):
        x = 35 + branches[node_lanes[node["id"]]] * 45
        y = 40 + i * 110
        par = node.get("parent")
        if par in positions:
            p = nodes[positions[par]]
            px = 35 + branches[node_lanes[p["id"]]] * 45
            py = 40 + positions[par] * 110
            svg.append(f'<path d="M {px} {py} C {px} {y-45}, {x} {y-45}, {x} {y}"/>')
        svg.append(f'<circle cx="{x}" cy="{y}" r="7"/>')
        diff = node["changes"]
        label = html.escape(f'{node["name"]} {node["version"]} / {node["branch"]}')
        details = html.escape(json.dumps({"files": diff, "build": node.get("build_commands", []),
                                         "settings_changed": node.get("settings_changed"),
                                         "settings_changes": node.get("settings_changes"), "message": node.get("message"),
                                         "parent": par}, indent=2))
        rows.append(f'<article><details><summary><b>{label}</b><small>{html.escape(node["created"])} · '
                    f'{html.escape(node["creator"])} · {html.escape(node["machine"])} · '
                    f'+{len(diff["added"])} / ~{len(diff["modified"])} / −{len(diff["removed"])}</small>'
                    f'</summary><pre>{details}</pre></details></article>')
    width = 70 + max(1, len(branches)) * 45
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Production topology</title><style>body{{background:#101722;color:#e4edf8;font:16px system-ui;margin:40px}}
h1{{margin-bottom:8px}}p,small{{color:#a3b5c9}}main{{display:flex;align-items:flex-start}}svg{{flex-shrink:0}}
path{{fill:none;stroke:#456888;stroke-width:2}}circle{{fill:#65dfba}}article{{min-height:110px;box-sizing:border-box;padding:20px 0}}
small{{display:block;margin-top:8px}}summary{{cursor:pointer}}article{{height:110px;position:relative}}
pre{{position:absolute;z-index:2;white-space:pre-wrap;background:#1b293a;padding:16px;width:min(650px,70vw);max-height:350px;overflow:auto}}
</style><h1>Production topology</h1><p>Custom release history · click a version to inspect changes and build commands.</p>
<main><svg width="{width}" height="{len(nodes)*110+50}" aria-label="Release ancestry graph">{''.join(svg)}</svg>
<section>{''.join(rows) or '<p>No builds recorded yet.</p>'}</section></main></html>'''
    Path(output).write_text(page, encoding="utf-8")
