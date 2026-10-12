# Verification

Run:

```text
python -m unittest discover -s tests -v
python -m compileall -q proxrunner tests
```

Automated checks cover default exclusions, sorted package controls, encryption round-trip, wrong passwords/tampering/truncation, credential export opt-in, full snapshots, compile success/failure, output preservation, provenance/fork topology, validation, hostile archives, monitor authentication/scopes, heartbeat expiry, recovery-plan confirmation/replay, LXC API parameters, backup-gated destruction, worker backups/restoration/retention, data-conflict preservation and backup failure before activation.

On Windows, the Linux-only flock module is stubbed for worker filesystem tests; native locking still requires Linux integration. Native symlink rollback testing may be skipped where Windows symlink privilege is absent; an emulated-pointer test also exercises the actual rollback algorithm. The adapter tests use mocked provider APIs. Local HTTP tests exercise the actual monitor server on loopback, including encrypted artifact upload/download.

## Live integration checklist

Before production use, validate in disposable infrastructure with representative data:

- Trusted SSH deployment to Linux, permissions, required Python/runtimes and no credential attachment leakage.
- First deployment, subsequent deployment, activation failure and failed health check, including actual current-symlink rollback.
- Quiesce/resume behavior and application/database restore from a completed snapshot.
- Reporter and backup cron jobs after reboot; monitor's offline detection when reports stop.
- HTTPS certificate validation, private CA, token rotation and denied role access.
- Proxmox ACLs, installed templates, LXC creation/start, static networking and verified guest SSH bootstrap.
- Docker installation in the selected guest/kernel, Compose activation, lifecycle API and protected volume data.
- Stop-mode vzdump to independent storage, restore to an unused VMID and application validation.
- Guest deletion only after the exact-resource plan and independent backup; excluded storage must block it.

These live integrations require separately configured test infrastructure. This source distribution includes no production host, provider credentials, SSH keys or TLS certificates. Local automated checks do not establish that deployment to real infrastructure works.

## Publication checks

Run `python scripts/check_github_publication.py .` before preparing a commit or distribution. The checker reports blocked paths and privacy-pattern findings without matched values. Review the complete proposed file list and contents as well; automated patterns cannot identify every kind of private information. Check outgoing Git history separately.

Encrypted package tests generate temporary passphrases at runtime. Other test credentials and example deployment addresses are synthetic fixtures, not live access details.

## Version 0.2.1 remediation checks

Legacy fingerprint export/storage migration, journaled service recovery and editor operation tracking have maintained regression tests. Live Linux interruption/reboot and production provider checks remain separate requirements. The extension build uses a pinned vsce dependency and npm integrity lock. Historical releases retain their original contents.
