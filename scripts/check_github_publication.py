"""Read-only publication checks. Findings never include matched values."""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import sys


CONTENT_RULES = {
    "private key": r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
    "credential token": (
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{15,}"
    ),
    "credential URL": r"https?://[^\s/@:]+:[^\s/@]+@",
    "local user path": r"(?i)\b[A-Z]:[\\/]Users[\\/]|/(?:Users|home)/",
    "email address requires review": r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}",
    "AI instructions require review": (
        r"(?i)ignore (?:all |the )?(?:previous|prior) instructions"
        r"|you are (?:an? )?(?:AI assistant|ChatGPT|Codex|coding agent)"
        r"|<\|(?:system|assistant|developer)\|>"
    ),
    "literal credential requires review": (
        r"(?i)\b(?:password|passwd|api_key|api_token|access_token|client_secret)"
        r"\b[\"']?\s*[:=]\s*[\"'][^\"'\r\n]+[\"']"
    ),
}


def scan(root, policy):
    findings = []
    checked = 0
    blocked_dirs = {name.casefold() for name in policy["blocked_directories"]}
    prompt_exception = policy["public_prompt_exception"]
    rules = {label: re.compile(pattern) for label, pattern in CONTENT_RULES.items()}

    def report(path, rule, line=None):
        finding = {"file": path, "rule": rule}
        if line is not None:
            finding["line"] = line
        findings.append(finding)

    def walk_error(error):
        report(".", "directory could not be inspected")

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        current = Path(current)
        for name in list(dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if name.casefold() == ".git" and current == root:
                dirs.remove(name)  # Git metadata is checked separately before a push.
            elif path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                report(relative, "linked directory")
                dirs.remove(name)
            elif name.casefold() in blocked_dirs or name.casefold().endswith(".egg-info"):
                report(relative, "blocked directory")
                dirs.remove(name)
        for name in files:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                report(relative, "linked file")
                continue
            if relative != prompt_exception and any(
                fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
                for pattern in policy["blocked_filename_patterns"]
            ):
                report(relative, "blocked filename")
                continue
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    report(relative, "large file requires manual review")
                    continue
                content = path.read_text(encoding="utf-8")
                if "\0" in content:
                    report(relative, "binary file requires manual review")
                    continue
            except (OSError, UnicodeError):
                report(relative, "unreadable or binary file")
                continue
            checked += 1
            for label, expression in rules.items():
                # The approved generic prompt is still checked for credentials/privacy.
                if relative == prompt_exception and label == "AI instructions require review":
                    continue
                for match in expression.finditer(content):
                    report(relative, label, content.count("\n", 0, match.start()) + 1)
    return {"checked_text_files": checked, "findings": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="candidate publication folder")
    parser.add_argument(
        "--policy", type=Path,
        default=Path(__file__).resolve().parents[1] / "github-publication-blocklist.json",
    )
    args = parser.parse_args()
    root = args.folder.resolve()
    if not root.is_dir():
        parser.error("candidate folder must exist")
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    result = scan(root, policy)
    result["scope"] = "working folder only; Git history and manual review also required"
    print(json.dumps(result, indent=2))
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
