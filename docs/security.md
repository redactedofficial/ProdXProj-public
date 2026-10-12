# Security and trust boundaries

## Credentials

The default package excludes credentials, environment files and user data. Explicit inclusion encrypts the package; base64 encoding is never treated as secret protection. Opt-in encrypted credential packaging does not permit committing credentials or exposing them in logs/runtime bundles.

Private deployment keys in a package are materialized locally under `secrets/ssh/` for SSH, then removed. On Windows the client restricts the temporary identity file ACL; on Unix it uses mode 600. Keys and other credential attachments are stripped from runtime upload. Parameter values and selected environment files are intentionally installed as protected runtime configuration. Unpacking credentials requires a separate flag and writes under the new workspace's `secrets/`.

The monitor stores encrypted project packages and never receives their unlock password during publication. Node report tokens are separate from administrator/viewer tokens. Provider secrets stay on the monitor. Runtime report settings intentionally contain a node token protected by directory/file permissions. Protect monitor token files, database, service environment, TLS keys and provider key files.

POSIX permission bits do not enforce Windows directory ACLs. Protect Windows project workspaces, the credential vault and temporary storage using your account/drive ACLs. Use encrypted disks when plaintext temporary package data is a concern. Package encryption is authenticated; package provenance is self-reported and unsigned.

## Executable projects

Build and activation commands are trusted executable instructions. ProdXProj validates their shape and uses argv arrays, but does not sandbox them. Unlocking/inspecting a package does not execute hooks. Compiling/deploying does. Review projects received from others before executing their commands. Keep secrets out of command-line arguments and scripts that print them.

SSH uses batch mode and strict host-key verification. HTTPS uses normal certificate validation, including private-CA files when provided. Do not enroll changed fingerprints without investigating. The monitor rejects non-loopback plaintext listeners and remote non-TLS Docker endpoints.

## Files and data

Archive paths must be relative, unique and contained. Symlink entries, traversal paths, unknown payload names, excessive counts/sizes and mismatched hashes are rejected. Unpack restores into a new destination only. Sensitive content in a plaintext package is rejected. Generated package controls are checked as part of inventory integrity.

Backups contain sensitive configuration/user data. Server backups are restricted ZIP files, not encrypted by the standard-library worker; use encrypted disks/storage or a separate encrypted off-site backup service. Their hashes support corruption checks, not signatures. Cron backup jobs and writes must use application quiesce/resume hooks for consistency. Same-server backups are insufficient for loss of that server.

The worker locks a dedicated deployment root with flock. It does not delete unrelated paths, bypass host verification or wipe the server. Application rollback cannot roll back external effects. Restore-to-new-directory avoids replacing live data. Proxmox destruction requires a resource-specific expiring plan and a completed backup; recognized excluded/external storage blocks deletion.

## Monitor API

The API separates viewer, node and administrator scopes, bounds connections/body sizes, rate-limits callers and suppresses sensitive provider errors. It uses static bearer tokens, not end-user MFA, role administration, tamper-proof audit storage or a multi-tenant authorization model. Put Internet-facing deployments behind an appropriate TLS reverse proxy/network policy. Protect the admin token as an infrastructure credential; Docker access and bootstrap root SSH are powerful privileges.

Administrative operations are exposed only for configured providers. The initial implementation does not accept arbitrary shell-command execution over the monitor API. Recovery plans bind guest configuration and cannot be replayed. No production infrastructure is configured in this checkout.

## Dependency notices

Release fingerprint schema 3 excludes production and local-emulation parameters from public configuration hashes. Older aggregate/emulation hashes are discarded when imported, read from local/monitor databases or exported because schema 2 had incompatible meanings. A trusted configuration can regenerate safe fingerprints. This cannot retract metadata already shared elsewhere; rotate credentials if an older exposed hash could reveal them.

Pending deployment journals retain activation/recovery phases until incoming services are stopped and the previous service, activation/resume and health hooks succeed. Failed reconciliation retains the journal and blocks subsequent worker actions. Hooks must tolerate retries after interruption; arbitrary external side effects remain outside automatic rollback. System-scoped service users must match the deployment owner/SSH account because release files are protected; provisioning a different service identity is not silently enabled by relaxing permissions.

The editor tracks long-running operations instead of terminating the immediate bridge at a fixed deadline. Failed/interrupted operations retain a workspace-local lock until their actual effects are inspected and explicitly acknowledged. That acknowledgement does not cancel remote work. These locks coordinate editor operations; other clients and external tools can still affect the same infrastructure.

Cryptography is supplied by the Python Cryptographic Authority under Apache-2.0 OR BSD-3-Clause. The installed CFFI dependency uses MIT-0, and pycparser uses BSD-3-Clause. Existing project assets/licenses remain part of packages; ProdXProj does not assign a new license to third-party project content. See [Cryptography's authenticated-encryption documentation](https://cryptography.io/en/latest/hazmat/primitives/aead/) for the underlying library and tag semantics.
