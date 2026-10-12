# Proxmox and Docker management

Infrastructure management runs on the central monitor. Provider credentials remain there; clients authenticate to ProdXProj. The adapters use verified HTTPS to Proxmox and a Unix socket or mutually authenticated TLS to Docker. A Docker socket grants substantial host privileges, so give access only to the dedicated monitor account where intended.

## Proxmox

Configure `PROX_PROXMOX_URL`, `PROX_PROXMOX_TOKEN_ID`, `PROX_PROXMOX_TOKEN_SECRET`, and optional CA/backup storage. Scope the API token to the intended node/VM pool/storage. Operations generally need VM audit/allocation/power/backup and storage allocation permissions; exact ACLs depend on the selected action and your Proxmox version.

Send requests to `POST /v1/manage`. Supported actions are `nodes`, `resources`, `status`, `start`, `shutdown`, `stop`, `reboot`, `backup`, `create-lxc`, `restore-lxc`, and `install-docker`. Destruction uses the separate plan workflow. `kind` defaults to `lxc`; status/power/backup/destruction also support `qemu`.

### Create and start an LXC

Use [`examples/create-lxc.json`](../examples/create-lxc.json), changing the node, VMID, installed template volume, storage, bridge, IP/gateway and your actual public key.

```json
{
  "provider": "proxmox",
  "action": "create-lxc",
  "node": "pve01",
  "vmid": 201,
  "hostname": "production-app01",
  "ostemplate": "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst",
  "storage": "local-lvm",
  "bridge": "vmbr0",
  "ip": "192.0.2.20/24",
  "gateway": "192.0.2.1",
  "cores": 2,
  "memory_mb": 2048,
  "disk_gb": 16,
  "ssh_public_key": "ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY",
  "docker": false
}
```

ProdXProj requests an unprivileged guest, enables on-boot startup, waits for creation to complete, and starts it. It never force-overwrites an existing VMID. The template must already be installed on the configured storage. Networking/firewall and template SSH policy must allow intended access.

For Docker, set `docker: true`. This enables nesting/keyctl for the unprivileged LXC. The current bootstrap uses Debian/Ubuntu's Docker apt repository and verifies `docker info`. It requires a static guest IP and monitor-side `PROX_BOOTSTRAP_KEY_FILE` plus `PROX_BOOTSTRAP_KNOWN_HOSTS`. Enroll the guest host key through a trusted channel; the manager never disables host-key checking. If the guest is not ready or its SSH identity is not enrolled yet, the response records bootstrap as pending. Use `install-docker` for the same node/VMID after fixing readiness/trust.

Example retry:

```json
{"provider":"proxmox","action":"install-docker","node":"pve01","vmid":201}
```

Docker is installed only when missing. Existing installations are checked instead of automatically replaced. Automatic bootstrap currently supports Debian and Ubuntu guests with systemd and root-key SSH access. Other Linux distributions need a reviewed project/provisioning adapter. Supported `.proxproj` deployment is broader: any Linux server with compatible Python, SSH and project runtimes.

### Recovery and destruction

1. Request a plan at `/v1/plans` with `{"provider":"proxmox","action":"destroy","node":"pve01","vmid":201,"kind":"lxc","storage":"backups"}`.
2. Review the exact guest, 10-minute expiry and steps. The plan binds the current guest configuration fingerprint.
3. POST `{"confirm":"destroy:pve01:201"}` to `/v1/plans/{plan-id}/execute`.

Execution rechecks guest configuration, blocks externally mounted/unbacked-up storage, runs stop-mode vzdump, verifies task success and backup-volume presence, stops the guest, and deletes that exact guest. Plans are single-use. The result includes the backup volume. Configure backup storage on separate infrastructure: same-host backups do not survive destruction of the hypervisor/storage.

Restore an LXC to an unused VMID with `restore-lxc`, supplying `node`, `vmid`, `archive` (e.g. `backups:backup/vzdump-lxc-201-...tar.zst`) and destination `storage`. Start/check the restored guest, then deploy the desired project release. QEMU restore, bare-metal OS reinstall and deleting an entire Proxmox host are not automated by this adapter. Those operations require your provider's provisioning/recovery integration.

Guest backup completion is not proof that an application database can be restored. Test application recovery separately. Bind/device mounts, excluded disks, passthrough devices and external databases need independent recovery procedures; the manager blocks recognized unsupported storage when destroying a guest.

## Docker Engine

Supported actions at `/v1/manage` are `info`, `containers`, `status`, `start`, `stop` and `restart`. Container actions require `container` (ID or name). Inventory excludes environment values and other configuration secrets.

```json
{"provider":"docker","action":"containers"}
```

```json
{"provider":"docker","action":"restart","container":"production-app"}
```

The adapter currently uses Engine API v1.45. Set `PROX_DOCKER_HOST=unix:///var/run/docker.sock` on the monitor host, or use an HTTPS Engine endpoint with a trusted CA and client certificate/key. Unauthenticated Docker TCP endpoints are rejected.

Create/update application containers using explicit project activation hooks such as Docker Compose commands. Store their persistent data under the project's shared directory or use a backup integration. Direct Docker container/volume deletion is intentionally unavailable until a volume-preserving recovery adapter is configured; filesystem exports alone omit mounted volume data. For Docker inside managed LXC, Proxmox guest backups provide the corresponding infrastructure recovery path.

## Primary references

- [Proxmox LXC API implementation](https://github.com/proxmox/pve-container/blob/master/src/PVE/API2/LXC.pm)
- [Proxmox container status API](https://github.com/proxmox/pve-container/blob/master/src/PVE/API2/LXC/Status.pm)
- [Proxmox vzdump API](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/VZDump.pm)
- [Docker Engine API reference](https://docs.docker.com/reference/api/engine/)
- [Docker Debian installation](https://docs.docker.com/engine/install/debian/)
- [Docker Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/)
