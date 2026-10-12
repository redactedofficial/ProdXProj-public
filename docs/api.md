# Central monitor API

The central monitor stores and serves production details. Production servers send reports to it; authenticated viewing clients query it. TLS is required when binding outside loopback. Deploy behind a trusted reverse proxy or bind directly with a certificate whose identity matches the static IP/hostname clients use.

## Start the monitor

Store unique report tokens in `secrets/monitor/nodes.json`, for example `{"web01":"<32+-character-random-token>"}`. Supply a separate administrator token and optional viewer-token array through `PROX_MONITOR_ADMIN_TOKEN` and `PROX_MONITOR_VIEWER_TOKENS`.

```text
prodx monitor --bind 0.0.0.0 --port 8443 --node-tokens secrets/monitor/nodes.json --cert secrets/monitor/tls.crt --key secrets/monitor/tls.key
```

The database defaults to `secrets/monitor/state.db`, with encrypted package storage next to it. For a service deployment, set an explicit working directory, run as a dedicated account, protect the environment file, enable automatic service restart, and back up the database/package vault. Use certificate validation; there is no `insecure` switch.

## Authentication

Every endpoint requires `Authorization: Bearer TOKEN`.

The generated [OpenAPI JSON](openapi.json) describes request shapes for tool integration. Regenerate it with `python scripts/export_api.py` after API changes.

| Role | Access |
| --- | --- |
| Viewer | Production inventory, events, redacted release metadata, encrypted package downloads and schema |
| Node | Only `/v1/report`, for the node ID enrolled with its unique token |
| Administrator | Inventory, package publication, infrastructure operations and recovery plans |

Node requests also send `X-Prox-Node: web01`; the report's `node_id` must match it. Tokens cannot be shared between roles or nodes. Tokens are at least 32 characters. Rotate tokens by updating configuration and restarting the monitor/reinstalling affected reporters. TLS protects bearer tokens in transit. The initial implementation uses static service tokens, not browser sessions, OAuth or MFA.

## Endpoints

| Method / path | Request and result |
| --- | --- |
| `GET /openapi.json` | API discovery schema |
| `GET /v1/nodes` | Current reports, server-side receipt times and computed online/offline state |
| `GET /v1/events` | Latest 500 report events |
| `POST /v1/report` | Node-scoped JSON heartbeat |
| `GET /v1/releases` | Custom topology summaries, 50 per page; use `?offset=50` for subsequent pages |
| `POST /v1/releases` | Admin publication of up to 100 redacted release records; 64 KiB body limit |
| `POST /v1/packages/{release-uuid}` | Admin encrypted artifact upload; binary body with Content-Length; existing files rejected |
| `GET /v1/packages/{release-uuid}` | Viewer/admin encrypted artifact download |
| `POST /v1/manage` | Admin Proxmox/Docker inventory, lifecycle and provisioning request |
| `POST /v1/plans` | Admin preparation of a Proxmox destruction/recovery plan |
| `POST /v1/plans/{plan-id}/execute` | Exact-resource confirmation and single-use plan execution |

Normal JSON request bodies are limited to 64 KiB. Artifact transfers are limited to 64 GiB and must be encrypted `.proxproj` envelopes. Responses use HTTP 401 for missing/wrong tokens, 403 for forbidden roles, 404 for missing resources, 409 for duplicate packages and 429 for rate limiting. Invalid/provider-failed operations return a redacted HTTP 400 error. No command output, API tokens, `.env` contents or private keys are returned in errors.

Published topology summaries retain provenance, parents, settings fingerprints, change counts and up to 200 paths per change category. Full file inventories and unabridged build/change details remain inside the encrypted package. Fetch/unpack that package to restore complete history details; `sync-history` retrieves summaries for the graph without unlocking artifacts.

Management operations are synchronous. Proxmox operations wait for asynchronous provider tasks and check their exit state. After a timeout or connection loss, inspect the guest/provider task state before retrying: creation may already have completed.

## CLI and other tools

```text
prodx api --url https://monitor.example:8443 /v1/nodes
prodx api --url https://monitor.example:8443 /v1/events
prodx api --url https://monitor.example:8443 --method POST --body-file request.json /v1/manage
prodx publish release.proxproj --url https://monitor.example:8443
prodx fetch <release-uuid> recovered.proxproj --url https://monitor.example:8443
prodx --project ./workspace sync-history --url https://monitor.example:8443
prodx --project ./workspace map --output topology.html
```

Set `PROX_MONITOR_TOKEN` in the caller's environment. The CLI prints JSON for scripting. Local Python integrations can call `proxrunner.archive.pack/opened`, `proxrunner.transport.invoke` and `proxrunner.api_client.request`. Other languages can use the HTTPS API directly:

```sh
curl --fail --cacert monitor-ca.crt \
  -H "Authorization: Bearer $PROX_MONITOR_TOKEN" \
  https://monitor.example:8443/v1/nodes
```

Example node report:

```json
{
  "node_id": "web01",
  "hostname": "production-web01",
  "address": "192.0.2.10",
  "project": "ExampleApp",
  "release": "e330a7eb-8485-46de-9d37-5482ec95eaa4",
  "version": "1-1-1-2",
  "health": "healthy",
  "uptime": 12345,
  "disk_free": 10737418240,
  "last_backup": "20261012T120000-abc.zip"
}
```

Health can be `healthy`, `unhealthy` or `unknown`. Node inventory becomes offline after 180 seconds without a report. Receipt time comes from the monitor. Reports are bounded and may contain only documented fields; `detail` is an optional short string. Never place credentials or unnecessary personal data in reports.
