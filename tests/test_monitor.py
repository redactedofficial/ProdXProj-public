import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from proxrunner.monitor import Server, Store
from proxrunner.providers import Proxmox

ADMIN = "admin-" + "a" * 40
VIEWER = "viewer-" + "v" * 40
NODE = "node-" + "n" * 40


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(("127.0.0.1", 0), Store(Path(self.temp.name) / "state.db"), ADMIN, [VIEWER], {"web01": NODE})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, token, data=None, node=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if node:
            headers["X-Prox-Node"] = node
        request = Request(self.url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
        try:
            with urlopen(request, timeout=3) as result:
                return json.loads(result.read())
        except HTTPError as error:
            error.close()
            raise

    def test_authenticated_report_inventory_and_offline_state(self):
        self.request("/v1/report", NODE, {"node_id": "web01", "health": "healthy", "release": "release-1"}, "web01")
        rows = self.request("/v1/nodes", VIEWER)
        self.assertEqual(rows[0]["release"], "release-1")
        self.assertTrue(rows[0]["online"])
        self.server.store.stale_after = -1
        self.assertFalse(self.request("/v1/nodes", VIEWER)[0]["online"])

    def test_roles_cannot_read_or_manage_other_scopes(self):
        for path, token, data, node, expected in (
            ("/v1/nodes", "bad", None, None, 401),
            ("/v1/nodes", NODE, None, "web01", 403),
            ("/v1/manage", VIEWER, {"provider": "docker", "action": "stop"}, None, 403),
            ("/v1/report", NODE, {"node_id": "other", "health": "healthy"}, "web01", 403),
            ("/v1/manage", ADMIN, {"provider": "proxmox", "action": "destroy"}, None, 400)):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                self.request(path, token, data, node)
            self.assertEqual(caught.exception.code, expected)

    def test_plan_requires_matching_confirmation_and_cannot_be_replayed(self):
        with patch("proxrunner.providers.Proxmox") as provider, patch("proxrunner.monitor.manage") as manage:
            provider.return_value.request.return_value = {"hostname": "test-guest"}
            manage.return_value = {"completed": True, "backup_volume": "backup:test"}
            plan = self.request("/v1/plans", ADMIN, {"provider": "proxmox", "action": "destroy", "node": "pve", "vmid": 101})
            endpoint = "/v1/plans/" + plan["plan_id"] + "/execute"
            with self.assertRaises(HTTPError):
                self.request(endpoint, ADMIN, {"confirm": "destroy:wrong:101"})
            self.assertTrue(self.request(endpoint, ADMIN, {"confirm": plan["confirm"]})["completed"])
            with self.assertRaises(HTTPError):
                self.request(endpoint, ADMIN, {"confirm": plan["confirm"]})
            self.assertEqual(manage.call_count, 1)

    def test_unknown_report_fields_rejected(self):
        with self.assertRaises(HTTPError):
            self.request("/v1/report", NODE, {"node_id": "web01", "health": "healthy", "secret": "value"}, "web01")


class ProviderTests(unittest.TestCase):
    def provider(self):
        p = object.__new__(Proxmox)
        return p

    def test_lxc_create_uses_unprivileged_and_waits_before_start(self):
        p = self.provider()
        with patch.object(p, "request", return_value="UPID:test") as request, patch.object(p, "wait", return_value={"completed": True}) as wait:
            result = p.manage({"action": "create-lxc", "node": "pve", "vmid": 101, "hostname": "web01", "storage": "local-lvm",
                               "bridge": "vmbr0", "ostemplate": "local:vztmpl/debian.tar.zst", "ssh_public_key": "ssh-ed25519 AAAATEST"})
            self.assertTrue(result["created"])
            create = request.call_args_list[0].args
            self.assertEqual(create[1], "/nodes/pve/lxc")
            self.assertEqual(create[2]["unprivileged"], 1)
            self.assertEqual(wait.call_count, 2)

    def test_destroy_external_mounts_blocks_before_backup_or_delete(self):
        p = self.provider()
        with patch.object(p, "request", return_value={"mp0": "/important/data,mp=/data"}) as request:
            with self.assertRaises(ValueError):
                p.manage({"action": "destroy", "node": "pve", "vmid": 101})
            self.assertEqual(request.call_count, 1)

    def test_failed_backup_task_blocks_destruction(self):
        p = self.provider()
        with patch.object(p, "request", side_effect=[{}, [], "UPID:backup"]) as request, patch.object(p, "wait", side_effect=ValueError("backup failed")):
            with self.assertRaises(ValueError):
                p.manage({"action": "destroy", "node": "pve", "vmid": 101, "storage": "backups"})
            self.assertFalse(any(call.args[0] == "DELETE" for call in request.call_args_list))


if __name__ == "__main__":
    unittest.main()
