import json
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_remote import remote


class Crash(BaseException):
    pass


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.old, self.new = (str(uuid.uuid4()) for _ in range(2))
        self.old_path = self.root / "releases" / self.old
        self.new_path = self.root / "releases" / self.new
        self.old_path.mkdir(parents=True)
        self.new_path.mkdir()
        self.old_cfg = {"service": {"name": "old", "type": "systemd"}, "resume": [["old-resume"]]}
        self.new_cfg = {"service": {"name": "new", "type": "systemd"}, "resume": [["new-resume"]]}
        self.manifest = {"id": self.new, "version": "2", "config": self.new_cfg}
        remote.write_json(self.old_path / ".proxrelease.json", {"config": self.old_cfg})
        remote.write_json(self.new_path / ".proxrelease.json", self.manifest)
        remote.write_json(self.root / "active.json", {"release": self.old, "version": "1"})
        (self.root / "current").write_text(str(self.old_path))
        self.stack = ExitStack()
        original = Path.is_symlink
        self.stack.enter_context(patch.object(Path, "symlink_to", lambda p, t, **_: p.write_text(str(t))))
        self.stack.enter_context(patch.object(Path, "is_symlink", lambda p: (p.name == "current" or p.name.startswith("current-")) and p.exists() or original(p)))
        self.stack.enter_context(patch.object(remote.os, "readlink", lambda p: p.read_text()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def pending(self, phase="activated", legacy=False):
        (self.root / "current").write_text(str(self.new_path))
        remote.write_json(self.root / "active.json", {"release": self.new, "version": "2"})
        value = {"previous": {"release": self.old, "version": "1"}, "target": str(self.old_path)}
        if not legacy:
            value.update(phase=phase, incoming={"release": self.new, "config": self.new_cfg})
        remote.write_json(self.root / ".transition.json", value)

    def test_every_uncommitted_activation_phase_reconciles_service_and_hooks(self):
        for phase in ("prepared", "activation-starting", "activated", "resumed", "healthy"):
            with self.subTest(phase=phase):
                self.pending(phase)
                with patch.object(remote, "managed") as managed, patch.object(remote, "hooks") as hooks:
                    remote.restore_transition(self.root)
                self.assertEqual([call.args[2] for call in managed.call_args_list], ["stop", "disable", "apply"])
                self.assertEqual([call.args[3] for call in hooks.call_args_list], ["activate", "resume", "health"])
                self.assertEqual(remote.state(self.root)["release"], self.old)
                self.assertFalse((self.root / ".transition.json").exists())

    def test_failed_prior_runtime_recovery_retains_journal_and_can_retry(self):
        self.pending()
        with patch.object(remote, "managed", side_effect=[None, None, ValueError("failed")]), patch.object(remote, "hooks"):
            with self.assertRaisesRegex(ValueError, "journal retained"):
                remote.restore_transition(self.root)
        value = json.loads((self.root / ".transition.json").read_text())
        self.assertEqual(value["phase"], "recovery-required")
        self.assertEqual(value["recovery_step"], "previous-runtime")
        with patch.object(remote, "managed") as managed, patch.object(remote, "hooks"):
            remote.restore_transition(self.root)
        self.assertEqual([call.args[2] for call in managed.call_args_list], ["apply"])

    def test_failed_new_service_stop_keeps_new_pointer_and_journal(self):
        self.pending()
        with patch.object(remote, "managed", side_effect=ValueError("failed")), patch.object(remote, "hooks") as hooks:
            with self.assertRaises(ValueError):
                remote.restore_transition(self.root)
        self.assertEqual((self.root / "current").read_text(), str(self.new_path))
        self.assertTrue((self.root / ".transition.json").exists())
        hooks.assert_not_called()

    def test_legacy_journal_reconciles_incoming_service(self):
        self.pending(legacy=True)
        with patch.object(remote, "managed") as managed, patch.object(remote, "hooks"):
            remote.restore_transition(self.root)
        self.assertEqual([call.args[2] for call in managed.call_args_list], ["stop", "disable", "apply"])

    def test_abrupt_exception_after_service_start_leaves_recoverable_journal(self):
        with patch.object(remote, "managed", side_effect=Crash()), patch.object(remote, "hooks"):
            with self.assertRaises(Crash):
                remote.switch(self.root, self.new, self.manifest)
        self.assertEqual(json.loads((self.root / ".transition.json").read_text())["phase"], "activation-starting")
        with patch.object(remote, "managed"), patch.object(remote, "hooks"):
            remote.restore_transition(self.root)
        self.assertEqual(remote.state(self.root)["release"], self.old)

    def test_crash_after_pointer_restore_before_metadata_write_can_retry(self):
        self.pending()
        native_replace = remote.os.replace
        def replace(source, destination):
            native_replace(source, destination)
            if Path(destination).name == "current":
                raise Crash()
        with patch.object(remote.os, "replace", replace), patch.object(remote, "managed"), patch.object(remote, "hooks"):
            with self.assertRaises(Crash):
                remote.restore_transition(self.root)
        self.assertEqual(json.loads((self.root / ".transition.json").read_text())["recovery_step"], "incoming-stopped")
        with patch.object(remote, "managed") as managed, patch.object(remote, "hooks"):
            remote.restore_transition(self.root)
        self.assertEqual([call.args[2] for call in managed.call_args_list], ["apply"])

    def test_successful_activation_commits_without_pending_recovery(self):
        with patch.object(remote, "managed"), patch.object(remote, "hooks"):
            remote.switch(self.root, self.new, self.manifest)
        self.assertEqual(remote.state(self.root)["release"], self.new)
        self.assertFalse((self.root / ".transition.json").exists())
        with patch.object(remote, "managed") as managed:
            remote.restore_transition(self.root)
        managed.assert_not_called()
