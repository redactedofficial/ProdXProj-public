import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proxrunner.config import template
from proxrunner.gui import launch
from scripts.build_vscode_extension import prepare


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extension = prepare(Path(__file__).resolve().parent.parent)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        cfg = template("EditorExample")
        cfg["parameters"] = {"DEMO_SETTING": "private-example-value"}
        (self.root / "proxproject.json").write_text(json.dumps(cfg))
        (self.root / "script.py").write_text("pass\n")

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, action, **extra):
        result = subprocess.run([sys.executable, "-I", str(self.extension / "bridge.py")],
                                input=json.dumps({"root": str(self.root), "action": action, **extra}),
                                capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout)

    def test_plan_masks_values_and_maps_files(self):
        result = self.request("plan")
        self.assertTrue(result["ok"])
        variable = next(x for x in result["result"]["variables"] if x["name"] == "DEMO_SETTING")
        self.assertEqual(variable["value"], "[set]")
        self.assertTrue(any(x["source"] == "script.py" for x in result["result"]["files"]))
        self.assertEqual(next(x for x in self.request("plan", reveal=True)["result"]["variables"]
                              if x["name"] == "DEMO_SETTING")["value"], "private-example-value")

    def test_unsaved_script_is_checked_without_execution(self):
        result = self.request("check", path="script.py", text="if broken syntax\n")
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["valid"])
        self.assertFalse(result["result"]["executed"])
        self.assertEqual((self.root / "script.py").read_text(), "pass\n")

    def test_unknown_operations_and_target_are_rejected(self):
        self.assertFalse(self.request("arbitrary-command", argv=["echo", "unsafe"])["ok"])
        self.assertFalse(self.request("plan", target="missing")["ok"])

    def test_check_rejects_files_outside_workspace(self):
        self.assertFalse(self.request("check", path="../outside.py", text="pass")["ok"])

    def test_failed_operation_is_durable_and_blocks_conflicting_retry(self):
        # No optional tests are configured, so this operation fails without running
        # project commands. Its conservative unknown state must survive bridge exit.
        self.assertFalse(self.request("test")["ok"])
        status = self.request("operation-status")["result"]
        self.assertEqual(status["pending"]["state"], "unknown")
        first = status["pending"]["operationId"]
        self.assertFalse(self.request("emulate-up")["ok"])
        self.assertEqual(self.request("operation-status")["result"]["pending"]["operationId"], first)
        self.assertFalse(self.request("operation-reconcile", pendingId=first, inspected=False)["ok"])
        self.assertTrue(self.request("operation-reconcile", pendingId=first, inspected=True)["ok"])
        self.assertIsNone(self.request("operation-status")["result"]["pending"])

    def test_gui_launch_uses_argv_and_encoded_project(self):
        with patch("proxrunner.gui.find_code", return_value="Code.exe"), patch("proxrunner.gui.subprocess.Popen") as process:
            launch(self.root)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(process.call_args_list[0].args[0], ["Code.exe", "--new-window", str(self.root.resolve())])
        self.assertTrue(process.call_args_list[1].args[0][2].startswith("vscode://prodxproj.prodxproj/open?project="))
        self.assertFalse(process.call_args.kwargs["shell"])
