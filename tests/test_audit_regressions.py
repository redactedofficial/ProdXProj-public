import json
import tempfile
import unittest
import uuid
import sys
import types
from pathlib import Path
from unittest.mock import patch

from proxrunner import history
from proxrunner.config import template
from proxrunner.monitor import Store
from proxrunner import service_runtime


class LegacyFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = template("FingerprintExample")
        self.cfg["project_id"] = str(uuid.uuid4())
        self.cfg["emulation"] = {"parameters": {"DEMO_PIN": "1234"}}
        self.value = {"id": str(uuid.uuid4()), "project_id": self.cfg["project_id"], "name": self.cfg["name"],
                      "version": "1", "branch": "main", "created": "2026-10-12", "creator": "test", "machine": "test",
                      "changes": {}, "files": {}, "settings_fingerprint_schema": 2,
                      "config_hash": history.fingerprint(self.cfg),
                      "settings_hashes": {"emulation": history.fingerprint(self.cfg["emulation"]), "parameters": "unsafe"}}

    def tearDown(self):
        self.temp.cleanup()

    def assert_safe(self, value):
        self.assertNotIn("config_hash", value)
        self.assertNotIn("emulation", value["settings_hashes"])
        self.assertNotIn("parameters", value["settings_hashes"])

    def test_legacy_export_and_summary_drop_unsafe_fingerprints(self):
        self.assert_safe(history.public_metadata(self.value))
        self.assert_safe(history.summary(self.value))

    def test_import_and_existing_database_rows_are_migrated(self):
        history.record(self.root, self.value)
        self.assert_safe(history.get(self.root, self.value["id"]))
        with history.connect(self.root) as db:
            db.execute("UPDATE releases SET manifest=?", (json.dumps(self.value),))
        self.assert_safe(history.all_releases(self.root)[0])
        with history.connect(self.root) as db:
            self.assert_safe(json.loads(db.execute("SELECT manifest FROM releases").fetchone()[0]))

    def test_monitor_import_and_old_rows_cannot_reexport_legacy_hashes(self):
        store = Store(self.root / "monitor.db")
        store.releases([self.value])
        self.assert_safe(store.releases()[0])
        with store.connect() as db:
            db.execute("UPDATE releases SET manifest=?", (json.dumps(self.value),))
        self.assert_safe(store.releases()[0])

    def test_configured_import_recomputes_schema_three_without_private_parameters(self):
        history.record(self.root, {**self.value, "config": self.cfg})
        value = history.public_metadata(history.get(self.root, self.value["id"]))
        self.assertEqual(value["settings_fingerprint_schema"], 3)
        self.assertEqual(value["config_hash"], history.fingerprint(history.safe_configuration(self.cfg)))
        self.assertNotEqual(value["settings_hashes"]["emulation"], self.value["settings_hashes"]["emulation"])

    def test_distinct_system_service_user_is_rejected_before_writing_runtime_files(self):
        cfg = {"service": {"type": "systemd", "scope": "system", "user": "other"}}
        account = types.SimpleNamespace(pw_uid=self.root.stat().st_uid + 1)
        with patch.dict(sys.modules, {"pwd": types.SimpleNamespace(getpwnam=lambda _: account)}), patch.object(service_runtime, "run") as run:
            with self.assertRaisesRegex(ValueError, "must own"):
                service_runtime.apply(cfg, self.root)
        run.assert_not_called()
        self.assertFalse((self.root / "service").exists())
