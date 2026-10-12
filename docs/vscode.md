# VS Code integration

Build `build-artifacts/prodxproj-vscode-0.2.1.vsix` with:

```powershell
./.venv/Scripts/python.exe scripts/build_vscode_extension.py
node --test vscode-extension/test/core.test.js
```

In VS Code, select **Extensions: Install from VSIX…** and choose that file. Open the project folder and trust it if you intend to run its commands. Run **ProdXProj: Select Python Interpreter** and select the project's virtual-environment Python with `requirements.lock` installed, then **ProdXProj: Dashboard**. The CLI launcher is `prodx --project . --gui`.

Features and boundaries are documented in [the extension README](../vscode-extension/README.md). The source lives in `vscode-extension/`; the build bundles `proxrunner/` into the VSIX. Extension versioning is separate from previously built Python wheels and production images. Existing 0.1.2 production artifacts do not contain this integration.

Use the dashboard's preview before an executable action. Live editing only triggers static checks. Dashboard operation buttons explicitly run local Docker/tests or selected-target SSH setup/service controls; no remote infrastructure is configured by this repository itself.
