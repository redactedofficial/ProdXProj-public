# `.proxproj` package format

Example filename: `ProductionProjectNameVer1-1-1-2.proxproj`.

An ordinary package is a ZIP64 archive. An encrypted package is a streaming AES-256-GCM envelope containing that archive. ProdXProj validates member paths, duplicate names, file counts, sizes and SHA-256 hashes before unpacking or shipping. The current limits are 100,000 source files and 64 GiB uncompressed content; manifest size is limited to 32 MiB.

```text
project.json                      # critical project configuration
shipping.json                     # shipping contract and inclusion policy
manifest.json                     # provenance, file hashes, history and ancestry
app/                              # application/project files
docs/                             # project documentation
  PRODX-SHIPPING.md                # generated shipping instructions
  PRODX-VARIABLES.md               # generated variable documentation
scripts/                          # project scripts
  PRODX-HOOKS.json                 # generated hook description
environment/                      # optional runtime .env/settings files
data/                             # optional user-data seeds
credentials/
  ssh.json                        # optional embedded target private keys
  files/                          # optional other local credential files
snapshot/                         # optional .git/.venv snapshots, never deployed
```

`docs/` and `scripts/` are first-class sections. The generated `PRODX-*` filenames are reserved and regenerated on builds. The unpack command reconstructs the original project paths so another client can edit, fork and rebuild it. Package `project.json` and `shipping.json` are retained under `.prox/package/`; editable configuration is restored to `proxproject.json`.

## Inclusion options

- `--include-env`: selected environment files.
- `--include-keys`: private SSH identities referenced by targets.
- `--include-data`: configured user-data paths.
- `--include-secrets`: local `secrets/`, `credentials/`, `.ssh/` and recognized key files, retained as credential attachments.
- `--include-all`: full encrypted project snapshot, including dependencies, environment, configured data and credential attachments. Local `.git` and `.venv` content is snapshot-only.

Sensitive content always requires encryption. Credential attachments are excluded from runtime upload. `unpack --include-credentials` explicitly exports them under destination `secrets/`. Without that flag, credentials remain in the encrypted package. Existing `.proxproj` artifacts, ProdXProj's own `.prox/` state and explicit exclusions are omitted. Prior history is exported as metadata. Normal packages omit symbolic links; full snapshots reject them to avoid silently claiming completeness. A package is a project snapshot, not a full disk/OS image.

## Provenance

The manifest stores UUIDs, version, branch, parent, fork origin, UTC timestamp, creator, machine, platform, source folder, release note, whether compilation ran, and build command arrays. Files record hashes, sizes, modes and modification timestamps. Changes list added, removed and modified paths. Settings changes identify fields without exposing values in history. Graph metadata travels in the file.

Creator and machine values are self-reported provenance, not verified identities or signatures. Plain package hashes detect accidental corruption, not malicious replacement. Encrypted package authentication detects ciphertext tampering when the password is trusted.

## Encryption envelope v1

`PROXENC1` (8 bytes), random salt (16 bytes), random GCM nonce (12 bytes), ciphertext, authentication tag (16 bytes). Scrypt derives a 32-byte key with `N=32768, r=8, p=1`. The header is authenticated. Encryption/decryption stream in 1 MiB chunks. The entire plaintext package is authenticated before any extraction or remote operation.

The password is never embedded. Losing it makes embedded keys/settings/data unrecoverable. Temporary plaintext files exist while creating or unlocking packages and are removed when the operation exits normally; use protected/encrypted local storage where required.
