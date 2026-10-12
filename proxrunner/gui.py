"""Open the installed ProdXProj VS Code integration."""
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode


def find_code():
    # Prefer the executable: Windows .cmd launchers require a command shell.
    if os.name == "nt":
        for base, suffix in ((os.environ.get("LOCALAPPDATA"), "Programs/Microsoft VS Code/Code.exe"),
                             (os.environ.get("ProgramFiles"), "Microsoft VS Code/Code.exe")):
            if base and (Path(base) / suffix).is_file():
                return str(Path(base) / suffix)
    command = shutil.which("code")
    if command and Path(command).suffix.lower() not in (".cmd", ".bat"):
        return command
    raise ValueError("VS Code was not found. Install VS Code and the ProdXProj VSIX; then open ProdXProj: Dashboard.")


def launch(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Select a project directory")
    executable = find_code()
    subprocess.Popen([executable, "--new-window", str(root)], shell=False,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen([executable, "--open-url", "vscode://prodxproj.prodxproj/open?" + urlencode({"project": str(root)})],
                     shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
