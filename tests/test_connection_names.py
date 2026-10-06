import contextlib
import io
import json
import pathlib
import types
import unittest
from unittest.mock import patch

import netpilot
import netpilot_lib.nm as nm_module
import netpilot_lib.network as network
from netpilot_lib.common import Cancelled, UserError, validate_connection_name


class ConnectionNameTests(unittest.TestCase):
    def test_unicode_spaces_and_punctuation_are_preserved(self):
        name = "控制robot + 网络共享"
        self.assertEqual(validate_connection_name(name), name)

    def test_empty_invalid_and_oversized_names_are_rejected(self):
        for name in ("", "   ", "bad\nname", "bad\x1bname", "x" * 101, None):
            with self.subTest(name=name), self.assertRaises(UserError):
                validate_connection_name(name)

    def test_invalid_interactive_name_is_retried_without_default(self):
        with patch.object(network, "ask", side_effect=["", "x" * 101, "robot控制"]) as ask, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(network.choose_connection_name(), "robot控制")
        for call in ask.call_args_list:
            self.assertEqual(len(call.args), 1)

    def test_cancelling_name_entry_propagates(self):
        with patch.object(network, "ask", side_effect=Cancelled("cancel")), self.assertRaises(Cancelled):
            network.choose_connection_name()

    def test_plan_rejects_whitespace_only_name(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        plan = json.loads((root / "examples/client_static.json").read_text())
        plan["name"] = "   "
        with self.assertRaises(UserError):
            network.validate_plan(plan)

    def test_wired_uplink_uses_user_name(self):
        device = network.Device("eth0", "ethernet", "disconnected", "", True)
        with patch.object(network, "select_device", return_value=device), \
             patch.object(network, "choose", return_value="auto"), \
             patch.object(network, "ask", return_value="robot上行"), \
             patch.object(network, "confirm", return_value=True):
            plan = network.uplink_plan(True)
        self.assertEqual(plan["name"], "robot上行")
        self.assertEqual(network.build_settings(plan)["connection"]["id"], "robot上行 [试运行]")

    def test_host_trial_name_and_user_profile_are_preserved_until_commit(self):
        import tempfile
        from test_safety import FakeNM
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(nm_module, "STATE", pathlib.Path(directory)), \
             patch.object(nm_module, "require_root"), patch.object(nm_module, "audit"), \
             patch.object(nm_module, "safe_directory", lambda path, **kw: path.mkdir(parents=True, exist_ok=True)), \
             contextlib.redirect_stdout(io.StringIO()):
            backend = FakeNM()
            manager = nm_module.Transactions(backend, 180)
            root = pathlib.Path(__file__).resolve().parents[1]
            plan = json.loads((root / "examples/host_shared.json").read_text())
            plan["name"] = "robot主机共享"
            record = manager.stage(network.build_settings(plan, uuid_value="custom"), "end1",
                                   plan["name"], True, "host:end1", plan)
            self.assertEqual(backend.items["custom"]["name"], "robot主机共享 [试运行]")
            self.assertFalse(backend.items["custom"]["saved"])
            manager.commit(record["token"])
            self.assertEqual(backend.items["custom"]["name"], "robot主机共享")
            self.assertIn("old", backend.items)


class NameConflictTests(unittest.TestCase):
    def backend(self):
        return types.SimpleNamespace(profiles=lambda: [
            {"name": "robot", "uuid": "user-original"},
            {"name": "robot另一个名称", "uuid": "user-other"}])

    def test_conflict_requires_user_name_instead_of_automatic_suffix(self):
        with patch.object(netpilot, "load_json", return_value={}), \
             patch.object(network, "ask", side_effect=["robot另一个名称", "robot有线控制"]), \
             contextlib.redirect_stdout(io.StringIO()):
            plan = netpilot.check_name(self.backend(), {"name": "robot"})
        self.assertEqual(plan["name"], "robot有线控制")

    def test_unique_name_does_not_prompt(self):
        with patch.object(netpilot, "load_json", return_value={}), \
             patch.object(netpilot, "choose_connection_name") as choose:
            self.assertEqual(netpilot.check_name(self.backend(), {"name": "robot新名称"})["name"], "robot新名称")
        choose.assert_not_called()

    def test_owned_same_name_remains_available(self):
        with patch.object(netpilot, "load_json", return_value={"user-original": {"slot": "host:eth0"}}), \
             patch.object(netpilot, "choose_connection_name") as choose:
            self.assertEqual(netpilot.check_name(self.backend(), {"name": "robot"})["name"], "robot")
        choose.assert_not_called()

    def test_cancel_conflict_does_not_start_network_changes(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        plan = json.loads((root / "examples/host_shared.json").read_text())
        plan["name"] = "robot"
        with patch.object(netpilot, "require_root"), \
             patch.object(netpilot, "NM", return_value=self.backend()), \
             patch.object(netpilot, "Transactions"), \
             patch.object(netpilot, "load_json", return_value={}), \
             patch.object(network, "ask", side_effect=Cancelled("cancel")), \
             patch.object(netpilot, "preflight") as preflight, \
             patch.object(netpilot, "backup_system") as backup, \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Cancelled):
            netpilot.apply_plan(plan)
        preflight.assert_not_called()
        backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
