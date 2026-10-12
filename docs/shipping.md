# Shipping, backups and recovery

## Create a project

```text
prodx --project ./MyApp init ProductionProjectName
```

Edit `MyApp/proxproject.json` with trusted build commands, server targets and activation/health hooks. Define build outputs and exclusions for your project's language and target architecture. ProdXProj uses existing compilers and runtimes; it does not make a Windows binary run on Linux.

```text
prodx --project ./MyApp compile --encrypt --include-env --include-keys --message "Release candidate"
prodx --project ./MyApp pack --include-all --message "Full recovery snapshot"
prodx inspect ./MyApp/dist/ProductionProjectNameVer1-1-1-2.proxproj
```

`compile` executes configured build commands before packaging; `pack` snapshots files directly. Use a new version/output path for each release: existing artifacts are never overwritten. Builds suppress command output to avoid leaking secrets. Diagnose a failed command by running the configured command in a controlled local environment.

## Topology and forks

```text
prodx --project ./MyApp branch experiment
prodx --project ./MyApp compile --version 1-1-1-3 --message "Try new renderer"
prodx --project ./MyApp fork DifferentProject --from-release <release-uuid>
prodx --project ./MyApp map --output production-topology.html
prodx compare old.proxproj new.proxproj --text
```

`branch`/`fork` change the workspace's identity/branch settings; they do not reset source files to an old version. To edit a previous snapshot, unpack that package into a new folder first. Its imported graph preserves ancestry.

## Deploy

Enroll SSH host fingerprints through a trusted channel before deployment. The account needs write access to its dedicated root and permission to invoke the project's service commands. Avoid root for ordinary deployments.

```text
prodx deploy release.proxproj --target prod --dry-run
prodx deploy release.proxproj --target prod
prodx --project ./MyApp status --target prod
```

The client unlocks/verifies the package, strips deployment credentials, sends a standard-library worker plus runtime files over SSH, and checks the result. The worker locks the project, optionally quiesces the active release, creates a backup, stages the new release, updates `current`, runs activation, and checks health. Failure reactivates and checks the previous release where one exists. The first deployment has no prior application to restart.

Shared user data is separate from releases. Selected seed data is installed only where no corresponding shared entry exists; a conflict aborts deployment. Configure the application to use `PROX_DATA_ROOT`. For existing application data stored outside this root, move it intentionally or define your own backup/migration integration before relying on the runner.

## Independent server jobs

```text
prodx --project ./MyApp install-backups --target prod --every-minutes 30
prodx --project ./MyApp install-reporter --target prod --url https://monitor.example:8443 --node-id web01
```

These commands install per-user cron jobs while preserving unrelated crontab entries. The server requires cron; the reporter also requires `flock`. No client needs to remain online. The reporter reads the active release's health commands, reports every minute, and includes the latest completed backup name. Set `PROX_REPORT_TOKEN` before installation. A private CA must already exist on the target and can be referenced with `--ca-file`.

## Back up and restore

```text
prodx --project ./MyApp backup --target prod
prodx --project ./MyApp rollback --target prod <installed-release-uuid>
prodx --project ./MyApp restore-backup --target prod <snapshot.zip> recovery-copy
```

`status` lists installed releases and available snapshots. Backups contain active application files, release configuration, shared data and active state, with hashes. New snapshots are completed before retention removes older ones. A failed backup prevents deployment activation. A restore verifies hashes into a new directory under `restores/`, leaving the live application/data intact for review and controlled recovery.

Snapshots on the same machine are protected files, not off-machine disaster recovery. Arrange backup storage replication, an encrypted off-site backup system, or Proxmox vzdump storage before destroying the machine. Quiesce/resume hooks must pause writers and create database dumps where required. Raw filesystem copies of a live database are not automatically transaction-consistent.

Rollback restores application selection and hooks; it does not undo user-data writes, database/schema migrations or external-service changes. Failed activation may leave the newly staged release and newly seeded paths available for diagnosis. Full-machine reinstall/destruction is provider-specific; see [infrastructure recovery](infrastructure.md).

## Custom machine recovery

For other Linux providers, configure trusted local `recovery` adapter commands in `proxproject.json`. These can invoke your cloud CLI, bare-metal provisioning script, or existing recovery tooling. ProdXProj does not invent destructive commands for an unknown provider.

```json
{
  "recovery": {
    "rebuild": [["python", "scripts/provider_recovery.py", "rebuild"]],
    "reinstall": [["python", "scripts/provider_recovery.py", "reinstall"]],
    "nuke": [["python", "scripts/provider_recovery.py", "nuke"]]
  }
}
```

```text
prodx --project ./MyApp recover reinstall --target prod
prodx --project ./MyApp recover reinstall --target prod --execute
prodx open-backup .prox/recovery/<backup-uuid>.proxbak recovered.zip
```

The first command prints a reviewable JSON plan without touching infrastructure. Execution creates a server snapshot, downloads and verifies every stored file hash, encrypts it as `.prox/recovery/<uuid>.proxbak` on the client, authenticates the saved copy, and only then runs the adapter. Losing the backup password prevents recovery. A failed preservation step prevents adapter execution. A failed adapter leaves the backup available. This backup covers the active project and configured shared data; external disks/databases need their own backup hooks before using destructive adapters.

The client is needed while it requests recovery and preserves this copy. Production monitoring and periodic server backups remain independent of viewing clients. Restoring the OS, external storage, services and data is the provider adapter's responsibility; successful adapter exit does not imply successful application health.
