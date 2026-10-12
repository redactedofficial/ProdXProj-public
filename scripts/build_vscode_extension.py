"""Prepare reviewed backend files and package a VSIX using Microsoft's vsce."""
import argparse
import json
import shutil
import subprocess
from pathlib import Path


def prepare(root):
    extension = root / "vscode-extension"
    destination = extension / "backend" / "proxrunner"
    destination.mkdir(parents=True, exist_ok=True)
    expected = set()
    for source in sorted((root / "proxrunner").glob("*.py")):
        if source.is_symlink():
            raise ValueError("Backend source must not be a symbolic link")
        expected.add(source.name)
        shutil.copyfile(source, destination / source.name)
    for old in destination.glob("*.py"):
        if old.name not in expected:
            old.unlink()
    shutil.copyfile(root / "requirements.lock", extension / "requirements.lock")
    return extension


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    extension = prepare(root)
    if not args.prepare_only:
        version = json.loads((extension / "package.json").read_text())["version"]
        output = root / "build-artifacts" / ("prodxproj-vscode-" + version + ".vsix")
        if output.exists():
            raise ValueError("Versioned VSIX already exists; use a new release version")
        output.parent.mkdir(exist_ok=True)
        # npm's Windows entrypoint is a batch script; call its trusted CLI JS via node.
        node = shutil.which("node")
        npm = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js" if node else None
        command = [node, str(npm)] if npm and npm.is_file() else [shutil.which("npm") or "npm"]
        subprocess.run(command + ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                       cwd=extension / "tools", check=True, shell=False)
        executable = extension / "tools/node_modules/@vscode/vsce/vsce"
        subprocess.run([node, str(executable), "package", "--allow-missing-repository", "--skip-license", "--out", str(output)],
                       cwd=extension, check=True, shell=False)
        print(output)
