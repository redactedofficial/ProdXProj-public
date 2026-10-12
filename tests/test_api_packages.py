import os
import secrets
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from proxrunner import api_client, archive
from proxrunner.config import template
from proxrunner.monitor import Server, Store


class PackageApiTests(unittest.TestCase):
    def test_encrypted_artifact_publish_fetch_and_open_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = template("Example")
            cfg.update(project_id=str(uuid.uuid4()), env_files=[], data_paths=[])
            (root / "code.txt").write_text("project contents")
            artifact = root / "original.proxproj"
            test_passphrase = secrets.token_urlsafe(32)
            manifest = archive.pack(root, cfg, artifact, password=test_passphrase)
            admin, viewer = "admin-" + "a" * 40, "viewer-" + "v" * 40
            server = Server(("127.0.0.1", 0), Store(root / "monitor" / "state.db"), admin, [viewer], {})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_port)
            try:
                from proxrunner.history import summary
                public = summary(manifest)
                api_client.request(url, admin, "POST", "/v1/releases", [public])
                api_client.upload(url, admin, manifest["id"], artifact)
                fetched = root / "fetched.proxproj"
                api_client.download(url, viewer, manifest["id"], fetched)
                self.assertEqual(artifact.read_bytes(), fetched.read_bytes())
                self.assertEqual(api_client.request(url, viewer, "GET", "/v1/releases")[0]["id"], manifest["id"])
                with archive.opened(fetched, test_passphrase) as (packed, _):
                    self.assertEqual(packed.read("app/code.txt"), b"project contents")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_non_loopback_plaintext_client_rejected(self):
        with self.assertRaises(ValueError):
            api_client.request("http://192.0.2.1:8443", "token", "GET", "/v1/nodes")


if __name__ == "__main__":
    unittest.main()
