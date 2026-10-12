import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from proxrunner import crypto, recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = {"targets": {"prod": {"host": "192.0.2.10", "user": "deploy", "root": "/opt/app"}},
                    "recovery": {"nuke": [["trusted-provider", "delete"]]}, "backup_keep": 5, "timeout": 300}

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_has_no_backup_or_provider_side_effects(self):
        with patch.object(recovery, "invoke") as invoke, patch.object(recovery.subprocess, "run") as run:
            plan = recovery.recover(self.root, self.cfg, "prod", "nuke")
            self.assertEqual(plan["operation"], "nuke")
            invoke.assert_not_called()
            run.assert_not_called()

    def test_failed_preservation_blocks_destructive_adapter(self):
        with patch.object(recovery, "preserve", side_effect=ValueError("backup failed")), patch.object(recovery.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                recovery.recover(self.root, self.cfg, "prod", "nuke", True, "test-password")
            run.assert_not_called()

    def test_preserved_backup_is_encrypted_and_restoreable_before_adapter(self):
        content = b"important production data"
        def invoke(root, cfg, target, action, **fields):
            if action == "backup":
                return {"backup": "20261012T120000-test.zip"}
            with zipfile.ZipFile(fields["download"], "w") as archive:
                archive.writestr("shared/data.txt", content)
                archive.writestr("backup-manifest.json", json.dumps({"shared/data.txt": hashlib.sha256(content).hexdigest()}))
            return {"completed": True}
        with patch.object(recovery, "invoke", invoke), patch.object(recovery.subprocess, "run", return_value=Mock(returncode=0)) as run:
            result = recovery.recover(self.root, self.cfg, "prod", "nuke", True, "a-long-test-password")
            preserved = Path(result["backup"])
            self.assertNotIn(content, preserved.read_bytes())
            restored = self.root / "restored.zip"
            crypto.decrypt(preserved, restored, "a-long-test-password")
            with zipfile.ZipFile(restored) as archive:
                self.assertEqual(archive.read("shared/data.txt"), content)
            self.assertEqual(run.call_args.kwargs["env"]["PROX_RECOVERY_BACKUP"], str(preserved))

    def test_corrupt_snapshot_blocks_adapter(self):
        def invoke(root, cfg, target, action, **fields):
            if action == "backup":
                return {"backup": "test.zip"}
            with zipfile.ZipFile(fields["download"], "w") as archive:
                archive.writestr("shared/data.txt", b"corrupt")
                archive.writestr("backup-manifest.json", json.dumps({"shared/data.txt": "0" * 64}))
        with patch.object(recovery, "invoke", invoke), patch.object(recovery.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                recovery.recover(self.root, self.cfg, "prod", "nuke", True, "a-long-test-password")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
