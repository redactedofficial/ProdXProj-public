# Project configuration and variables

Run `prodx --project PATH init Name` to create `proxproject.json`. The same settings are embedded as `project.json` in packages. `manifest.json` records build identity and provenance; `shipping.json` records how the release ships. Use the [example configuration](../examples/proxproject.example.json) as a starting point.

| Field | Meaning |
| --- | --- |
| `schema` | Format version; currently `1` |
| `name`, `version` | Artifact name and numeric version, e.g. `ProductionProjectName`, `1-1-1-2` |
| `project_id` | UUID assigned by `init`; shared across versions |
| `branch` | Current development line, initially `main` |
| `forked_from`, `fork_origin` | Parent release and originating project when branching/forking |
| `build` | Local compiler commands, each an argv array |
| `activate`, `health` | Remote activation and health-check argv arrays; both required to deploy |
| `quiesce`, `resume` | Commands that pause and resume writes for consistent backups |
| `parameters` | Runtime environment mapping; nonempty parameters require package encryption |
| `env_files` | Project-relative files included only with `--include-env` or `--include-all` |
| `data_paths` | Project-relative user-data paths, included only when requested |
| `exclude` | Exact relative paths or directory prefixes excluded from packages |
| `timeout` | Per-command timeout in seconds; default 300 |
| `backup_keep` | Number of complete server snapshots retained; default 5 |
| `targets` | Named server definitions containing host/IP, user, root, optional port, key file and known-hosts file |
| `recovery` | Optional `rebuild`, `reinstall`, `nuke` local adapter argv arrays; execution requires a verified off-server backup first |

Paths in configuration use `/` even on Windows. Targets must use a dedicated absolute Linux deployment root. Credentials belong under local `secrets/`. Real configuration containing sensitive parameters should be kept outside source control. `init` never overwrites an existing configuration.

Commands execute as argument arrays, without an implicit shell. For shell logic, write a reviewed project script under `scripts/` and invoke it explicitly, e.g. `["sh", "scripts/activate.sh"]`. A `.env` is shipped as a file; ProdXProj does not silently parse it or export its values. The application's runtime or activation script loads it.

## Runtime variables

| Variable | Location |
| --- | --- |
| `PROX_RELEASE_ROOT` | Current release's application directory |
| `PROX_DEPLOY_ROOT` | Dedicated deployment root |
| `PROX_DATA_ROOT` | Persistent shared data directory |

Project parameters cannot override `PROX_` variables. Build commands inherit the local environment, so build only trusted projects. Runtime parameters are passed to activation, health, quiesce and resume hooks.

Local recovery adapters receive `PROX_RECOVERY_BACKUP`, `PROX_RECOVERY_ACTION`, `PROX_TARGET_HOST`, `PROX_TARGET_USER` and `PROX_TARGET_ROOT`. Adapter scripts should use these environment variables rather than string-interpolated shell commands. The backup password is not automatically passed to adapter commands.

## Monitor and provider variables

| Variable | Purpose |
| --- | --- |
| `PROX_MONITOR_ADMIN_TOKEN` | Central monitor administrator token, 32+ characters |
| `PROX_MONITOR_VIEWER_TOKENS` | JSON array of distinct viewer tokens |
| `PROX_MONITOR_TOKEN` | Token used by client API, publish, fetch and sync commands |
| `PROX_REPORT_TOKEN` | Node-specific token used when installing a reporter |
| `PROX_PROXMOX_URL` | Proxmox HTTPS origin, typically `https://pve.example:8006` |
| `PROX_PROXMOX_TOKEN_ID`, `PROX_PROXMOX_TOKEN_SECRET` | Scoped Proxmox API token identity and secret |
| `PROX_PROXMOX_CA` | Optional trusted CA file for Proxmox TLS |
| `PROX_PROXMOX_BACKUP_STORAGE` | Default vzdump backup storage |
| `PROX_BOOTSTRAP_KEY_FILE`, `PROX_BOOTSTRAP_KNOWN_HOSTS` | Monitor-side SSH identity and verified host-key file for Docker bootstrap |
| `PROX_DOCKER_HOST` | `unix:///var/run/docker.sock` or an HTTPS Docker endpoint |
| `PROX_DOCKER_CA`, `PROX_DOCKER_CERT`, `PROX_DOCKER_KEY` | CA and client certificates for remote Docker |

Use environment variable names in CLI flags; do not pass secret values as command arguments. Package passwords are prompted, or read from a variable named by `--password-env`. Interactive package passwords require 12+ characters; unattended callers must enforce their own password strength.
