# ProdXProj

ProdXProj is a client-side production project compiler and runner, with an optional central monitoring and infrastructure management server.

- Build any project through configured commands and produce `ProductionProjectNameVer1-1-1-2.proxproj`.
- Keep application files, `docs/`, `scripts/`, settings, shipping instructions, optional environment files, data and credentials in one organized package.
- Encrypt sensitive packages, unpack projects, deploy to Linux over SSH, retain backups and roll back releases.
- Track a custom release topology: parent versions, branches, forks, file/settings changes, timestamps, creator, machine and build instructions.
- Run monitoring on a server. Production servers send authenticated HTTPS reports; authenticated clients view inventory and health.
- Manage Proxmox guests and Docker through an authenticated API. Create/start unprivileged LXC guests and optionally bootstrap Docker on Debian/Ubuntu.

## Install

Python 3.10+ is required on the client; Python 3.10+ and SSH are required on deployment targets.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -e .
.\.venv\Scripts\prodx.exe --help
```

On Linux/macOS, use `.venv/bin/python` and `.venv/bin/prodx`. `python -m proxrunner` and `prox` are equivalent entry points.

```powershell
prodx --project C:\projects\MyApp init ProductionProjectName
# Edit C:\projects\MyApp\proxproject.json to define builds, targets and hooks.
prodx --project C:\projects\MyApp compile --encrypt --include-env --include-keys --message "First production release"
prodx --project C:\projects\MyApp map --output topology.html
prodx deploy C:\projects\MyApp\dist\ProductionProjectNameVer1-1-1-2.proxproj --target prod --dry-run
```

The compiler executes project-specific build commands; it does not invent build tools or make platform-specific binaries portable. Deployment requires explicit activation and health-check commands.

## Documentation

Start with [the documentation index](docs/README.md), then [configuration and variables](docs/configuration.md), [package format](docs/package-format.md), [shipping and recovery](docs/shipping.md), [monitor API](docs/api.md), [Proxmox and Docker](docs/infrastructure.md), and [security](docs/security.md).

## Verify

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Live SSH, Proxmox and Docker operations need configured infrastructure. This checkout has no production target or credentials, so those integrations have not been exercised against a real server.

## Source publication

This distribution includes source, tests, public documentation, sanitized examples,
and verification tools. Local credentials, private engineering notes, AI-agent
configuration, release databases, build outputs, and prior Git history are excluded.
Create a fresh project configuration with `prodx init`; workspace-specific
`proxproject.json` settings are not distributed.

Run `python scripts/check_github_publication.py .` to check publication candidates.
See [verification](docs/verification.md) for the scan's scope and test limitations.
The [generic sorting prompt](GITHUB_SORTING_PROMPT.md) is the sole approved prompt
file; it describes the publication workflow without private workspace content.

## Maintainer and license

See [about the maintainer](ABOUTME.md). ProdXProj is available under the
[MIT license](LICENSE); dependencies retain their own licenses as described in
[third-party notices](THIRD_PARTY_NOTICES.md).
