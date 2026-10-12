"""Worker filesystem tests. Linux locking is stubbed only when testing on Windows."""
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    with patch.dict(sys.modules, {"fcntl": types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)}):
        remote = importlib.import_module("proxrunner.remote")
else:
    from proxrunner import remote


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "deployment"
        self.root.mkdir()
        self.release = str(uuid.uuid4())
        self.stage = self.root / "releases" / self.release
        self.stage.mkdir(parents=True)
        (self.root / "shared").mkdir()
        (self.stage / "main.txt").write_text("original application")
        (self.root / "shared" / "important.txt").write_text("important production data")
        self.cfg = {"backup_keep": 2, "activate": [], "health": [], "parameters": {}}
        self.manifest = {"id": self.release, "version": "1", "config": self.cfg, "files": {}}
        remote.write_json(self.stage / ".proxrelease.json", self.manifest)
        remote.write_json(self.root / "active.json", {"release": self.release, "version": "1"})

    def tearDown(self):
        self.temp.cleanup()

    def request(self, action, **kwargs):
        return remote.execute({"root": str(self.root), "action": action, **kwargs})

    def test_backup_restore_integrity_and_retention(self):
        # Explicit POSIX names avoid Windows ZipInfo.from_file behavior.
        first = self.request("backup", keep=2)["backup"]
        restored = self.request("restore", backup=first, destination="recovery-copy")["restored_to"]
        self.assertEqual((Path(restored) / "shared" / "important.txt").read_text(), "important production data")
        self.assertEqual((Path(restored) / "releases" / self.release / "main.txt").read_text(), "original application")
        self.request("backup", keep=2)
        self.request("backup", keep=2)
        self.assertEqual(len(list((self.root / "backups").glob("*.zip"))), 2)
        with self.assertRaises(ValueError):
            self.request("restore", backup=first, destination="../escape")

    def test_data_conflict_is_rejected_without_overwriting_production(self):
        content = b"replacement data"
        import hashlib
        manifest = {"id": str(uuid.uuid4()), "version": "2", "config": self.cfg,
                    "files": {"data/important.txt": {"sha256": hashlib.sha256(content).hexdigest(),
                              "size": len(content), "mode": 0o600, "mtime_ns": 0}}}
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as packed:
            packed.writestr("data/important.txt", content)
        payload.seek(0)
        with self.assertRaises(ValueError):
            remote.execute({"root": str(self.root), "action": "deploy", "manifest": manifest}, payload)
        self.assertEqual((self.root / "shared" / "important.txt").read_text(), "important production data")
        self.assertEqual(remote.state(self.root)["release"], self.release)

    def test_backup_failure_prevents_activation(self):
        manifest = {**self.manifest, "id": str(uuid.uuid4()), "version": "2"}
        with patch.object(remote, "backup", side_effect=OSError("disk full")), patch.object(remote, "switch") as switch:
            with self.assertRaises(OSError):
                remote.execute({"root": str(self.root), "action": "deploy", "manifest": manifest}, None)
            switch.assert_not_called()

    def test_failed_activation_reinstates_previous_pointer_and_hooks(self):
        # Exercise pointer rollback on hosts where unprivileged symlinks are supported.
        try:
            (self.root / "current").symlink_to(self.stage, target_is_directory=True)
        except OSError:
            self.skipTest("Native symlink privilege unavailable on this Windows host")
        release = str(uuid.uuid4())
        new = self.root / "releases" / release
        new.mkdir()
        manifest = {**self.manifest, "id": release, "version": "2"}
        with patch.object(remote, "hooks", side_effect=[ValueError("activation failed"), None, None]) as hooks:
            with self.assertRaises(ValueError):
                remote.switch(self.root, release, manifest)
            self.assertEqual(remote.state(self.root)["release"], self.release)
            self.assertEqual((self.root / "current").resolve(), self.stage.resolve())
            self.assertEqual(hooks.call_count, 3)

    def test_pointer_rollback_protocol_with_emulated_symlinks(self):
        # Run the rollback algorithm even where native Windows symlinks need privilege.
        release = str(uuid.uuid4())
        new = self.root / "releases" / release
        new.mkdir()
        manifest = {**self.manifest, "id": release, "version": "2"}
        current = self.root / "current"
        current.write_text(str(self.stage))
        native_check = Path.is_symlink
        def link(path, target, **_):
            path.write_text(str(target))
        def check(path):
            return (path.name == "current" or path.name.startswith("current-")) and path.exists() or native_check(path)
        with patch.object(Path, "symlink_to", link), patch.object(Path, "is_symlink", check), patch.object(remote.os, "readlink", lambda path: path.read_text()), \
             patch.object(remote, "hooks", side_effect=[None, ValueError("health failed"), None, None]) as hooks:
            with self.assertRaises(ValueError):
                remote.switch(self.root, release, manifest)
            self.assertEqual(remote.state(self.root)["release"], self.release)
            self.assertEqual(Path(current.read_text()).resolve(), self.stage.resolve())
            self.assertEqual(hooks.call_count, 4)


if __name__ == "__main__":
    unittest.main()
