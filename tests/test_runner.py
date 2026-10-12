import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
import warnings
from pathlib import Path

from proxrunner import archive, history
from proxrunner.config import template, validate


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.test_passphrase = secrets.token_urlsafe(32)
        self.cfg = template("ProductionProjectName")
        self.cfg["project_id"] = str(uuid.uuid4())
        (self.root / "main.py").write_text("print('hello')\n")
        (self.root / ".env").write_text("TOKEN=synthetic-test-value\n")
        (self.root / "data").mkdir()
        (self.root / "data" / "user.txt").write_text("synthetic user data")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "private").write_text("synthetic-key-for-tests")
        self.cfg["targets"] = {"prod": {"host": "192.0.2.10", "user": "deploy", "root": "/opt/example-app",
                                         "key_file": "secrets/private"}}

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, name="release.proxproj", **options):
        return archive.pack(self.root, self.cfg, self.root / name, **options)

    def test_default_excludes_secrets_env_and_user_data_with_sorted_documents(self):
        self.pack()
        with archive.opened(self.root / "release.proxproj") as (packed, manifest):
            self.assertIn("app/main.py", manifest["files"])
            self.assertIn("docs/PRODX-SHIPPING.md", manifest["files"])
            self.assertIn("project.json", packed.namelist())
            self.assertIn("shipping.json", packed.namelist())
            self.assertNotIn("environment/.env", packed.namelist())
            self.assertFalse(any(p.startswith("data/") for p in packed.namelist()))
            self.assertEqual(manifest["credentials"], {})
        self.assertNotIn("synthetic", (self.root / ".prox" / "history.db").read_bytes().decode(errors="ignore"))

    def test_encrypted_everything_roundtrip_and_credential_export_opt_in(self):
        self.pack(password=self.test_passphrase, include_env=True, include_keys=True, include_data=True)
        raw = (self.root / "release.proxproj").read_bytes()
        self.assertNotIn(b"synthetic", raw)
        with archive.opened(self.root / "release.proxproj", self.test_passphrase) as (packed, manifest):
            self.assertEqual(manifest["credentials"]["prod"], "synthetic-key-for-tests")
            destination = self.root / "checkout"
            archive.extract(packed, manifest, destination, include_data=True)
            self.assertEqual((destination / "main.py").read_text(), "print('hello')\n")
            self.assertTrue((destination / ".env").exists())
            self.assertTrue((destination / "data" / "user.txt").exists())
            self.assertFalse((destination / "secrets" / "credentials").exists())
            self.assertEqual(history.parent(destination, self.cfg["project_id"], "main"), manifest["id"])
            full = self.root / "checkout-secrets"
            archive.extract(packed, manifest, full, True, True)
            self.assertTrue((full / "secrets" / "credentials" / "ssh.json").exists())

    def test_wrong_password_tamper_and_truncation_rejected(self):
        self.pack(password=self.test_passphrase)
        path = self.root / "release.proxproj"
        with self.assertRaises(ValueError):
            with archive.opened(path, secrets.token_urlsafe(32)):
                pass
        value = bytearray(path.read_bytes())
        value[50] ^= 1
        path.write_bytes(value)
        with self.assertRaises(ValueError):
            with archive.opened(path, self.test_passphrase):
                pass
        path.write_bytes(value[:40])
        with self.assertRaises(ValueError):
            with archive.opened(path, self.test_passphrase):
                pass

    def test_secret_inclusion_requires_encryption(self):
        for option in ("include_keys", "include_env", "include_data", "include_secrets", "include_all"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.pack(**{option: True})

    def test_explicit_environment_file_in_vault_does_not_include_other_credentials(self):
        (self.root / "secrets" / "runtime.conf").write_text("RUNTIME=selected")
        self.cfg["env_files"] = ["secrets/runtime.conf"]
        self.pack(password=self.test_passphrase, include_env=True)
        with archive.opened(self.root / "release.proxproj", self.test_passphrase) as (packed, _):
            self.assertEqual(packed.read("environment/secrets/runtime.conf"), b"RUNTIME=selected")
            self.assertFalse(any("/private" in path for path in packed.namelist()))

    def test_full_snapshot_includes_dependencies_and_secret_section(self):
        dependency = self.root / "node_modules" / "library"
        dependency.mkdir(parents=True)
        (dependency / "index.js").write_text("// dependency")
        self.pack(password=self.test_passphrase, include_all=True, include_data=True, include_keys=True)
        with archive.opened(self.root / "release.proxproj", self.test_passphrase) as (_, manifest):
            self.assertIn("app/node_modules/library/index.js", manifest["files"])
            self.assertIn("credentials/files/secrets/private", manifest["files"])
            self.assertIn("environment/.env", manifest["files"])

    def test_modified_removed_added_and_fork_ancestry(self):
        first = self.pack("v1.proxproj")
        (self.root / "main.py").write_text("print('changed')\n")
        (self.root / "new.txt").write_text("new")
        self.cfg["version"] = "1-1-1-3"
        second = self.pack("v2.proxproj")
        self.assertEqual(second["parent"], first["id"])
        self.assertIn("app/main.py", second["changes"]["modified"])
        self.assertIn("app/new.txt", second["changes"]["added"])
        self.cfg.update(project_id=str(uuid.uuid4()), forked_from=first["id"], fork_origin=first["project_id"])
        (self.root / "new.txt").unlink()
        fork = self.pack("fork.proxproj", branch="experiment")
        self.assertEqual(fork["parent"], first["id"])
        self.assertEqual(fork["fork_origin"], first["project_id"])
        history.render(self.root, self.root / "map.html")
        self.assertIn("experiment", (self.root / "map.html").read_text(encoding="utf-8"))

    def test_unsafe_archive_paths_duplicates_and_untracked_payload_rejected(self):
        for names in (("../escape",), ("app/a", "app/a"), ("app/extra",)):
            path = self.root / "bad.zip"
            with warnings.catch_warnings(), zipfile.ZipFile(path, "w") as packed:
                warnings.simplefilter("ignore", UserWarning)
                packed.writestr("manifest.json", json.dumps({"schema": 1, "config": self.cfg, "files": {}}))
                for name in names:
                    packed.writestr(name, b"bad")
            with zipfile.ZipFile(path) as packed, self.assertRaises(ValueError):
                archive.verify(packed)

    def test_existing_output_is_not_overwritten(self):
        self.pack()
        original = (self.root / "release.proxproj").read_bytes()
        with self.assertRaises(ValueError):
            self.pack()
        self.assertEqual(original, (self.root / "release.proxproj").read_bytes())

    def test_compile_runs_actual_argv_build_and_failure_creates_no_artifact(self):
        self.cfg["build"] = [[sys.executable, "-c", "from pathlib import Path;Path('built.txt').write_text('built')"]]
        manifest = self.pack(compile_project=True)
        self.assertTrue(manifest["compiled"])
        self.assertIn("app/built.txt", manifest["files"])
        self.cfg["build"] = [[sys.executable, "-c", "raise SystemExit(3)"]]
        with self.assertRaises(ValueError):
            self.pack("failed.proxproj", compile_project=True)
        self.assertFalse((self.root / "failed.proxproj").exists())

    def test_validation_rejects_unknown_fields_and_ssh_injection(self):
        self.cfg["unknown"] = True
        with self.assertRaises(ValueError):
            validate(self.cfg)
        del self.cfg["unknown"]
        self.cfg["targets"]["prod"]["host"] = "host;touch /tmp/pwn"
        with self.assertRaises(ValueError):
            validate(self.cfg)


if __name__ == "__main__":
    unittest.main()
