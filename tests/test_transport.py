import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from proxrunner import archive, transport
from proxrunner.config import template


class TransportTests(unittest.TestCase):
    def test_runtime_upload_strips_credentials_targets_and_local_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = template("Example")
            cfg.update(project_id=str(uuid.uuid4()), data_paths=[])
            cfg["targets"] = {"prod": {"host": "192.0.2.10", "user": "deploy", "root": "/opt/app", "key_file": "secrets/key"}}
            (root / "secrets").mkdir()
            identity = root / "secrets" / "key"
            identity.write_text("synthetic-private-key")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("synthetic-git-metadata")
            (root / ".env").write_text("TOKEN=intentionally-shipped-runtime-value")
            (root / "main.py").write_text("pass")
            package = root / "release.proxproj"
            archive.pack(root, cfg, package, "a-long-test-password", include_all=True, include_keys=True)
            captured = {}
            def run(command, **options):
                stream = options["stdin"]
                captured["request"] = json.loads(stream.readline())
                captured["payload"] = stream.read()
                captured["command"] = command
                return SimpleNamespace(returncode=0, stdout=b'{"completed":true}')
            with archive.opened(package, "a-long-test-password") as (packed, manifest), patch.object(transport.subprocess, "run", run):
                transport.invoke(root, cfg, "prod", "deploy", packed, manifest, key_file=identity)
            runtime = captured["request"]["manifest"]
            self.assertNotIn("credentials", runtime)
            self.assertNotIn("targets", runtime["config"])
            self.assertIn("StrictHostKeyChecking=yes", captured["command"])
            with zipfile.ZipFile(io.BytesIO(captured["payload"])) as packed:
                self.assertEqual(packed.read("app/.env"), b"TOKEN=intentionally-shipped-runtime-value")
                self.assertIn("app/scripts/PRODX-HOOKS.json", packed.namelist())
                self.assertFalse(any("credentials" in p or ".git" in p for p in packed.namelist()))


if __name__ == "__main__":
    unittest.main()
