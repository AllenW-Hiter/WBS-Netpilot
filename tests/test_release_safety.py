"""Release review regressions; no real network, backups, or privileged commands."""
import contextlib
import io
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import netpilot
from netpilot_lib.common import UserError
from netpilot_lib.network import Device


class ProfileDeletionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.devices = [Device("eth0", "ethernet", "connected", "", True),
                        Device("veth0", "ethernet", "connected", "", False)]
        self.profile = {"uuid": "selected", "name": "User connection", "path": "/profile/selected",
                        "type": "802-3-ethernet", "interface": "", "autoconnect": True,
                        "filename": "/etc/NetworkManager/system-connections/user.nmconnection"}
        self.nm = mock.Mock()
        self.nm.settings_with_secrets.return_value = ({"connection": {"id": "User connection"}}, [])
        self.nm.backup_profile.return_value = "private-connection-backup"
        self.stack.enter_context(mock.patch.object(netpilot, "Transactions"))
        self.stack.enter_context(mock.patch.object(netpilot, "eligible_profiles", return_value=[self.profile]))
        self.stack.enter_context(mock.patch.object(netpilot, "inventory", return_value=self.devices))
        self.stack.enter_context(mock.patch.object(netpilot, "candidates", return_value=self.devices[:1]))
        self.stack.enter_context(mock.patch.object(netpilot, "choose", return_value="批量删除非活动配置"))
        self.stack.enter_context(mock.patch.object(netpilot, "ask", side_effect=["1", "DELETE"]))
        self.backup = self.stack.enter_context(mock.patch.object(netpilot, "backup_system"))
        self.stack.enter_context(mock.patch.object(netpilot, "audit"))

    def test_active_uuid_on_virtual_interface_is_rejected_before_backups(self):
        self.nm.active_uuid.side_effect = lambda name: "selected" if name == "veth0" else ""
        with self.assertRaisesRegex(UserError, "正在使用"):
            netpilot.profile_menu(self.nm, 180)
        self.nm.delete.assert_not_called()
        self.nm.backup_profile.assert_not_called()
        self.backup.assert_not_called()

    def test_virtual_activation_after_backup_prevents_deletion(self):
        self.nm.active_uuid.side_effect = ["", "", "", "selected"]
        with self.assertRaisesRegex(UserError, "刚被其他程序激活"):
            netpilot.profile_menu(self.nm, 180)
        self.nm.backup_profile.assert_called_once_with("selected")
        self.nm.delete.assert_not_called()

    def test_unreadable_activity_stops_before_any_deletion(self):
        self.nm.active_uuid.side_effect = UserError("cannot read activity")
        with self.assertRaises(UserError):
            netpilot.profile_menu(self.nm, 180)
        self.nm.delete.assert_not_called()
        self.nm.backup_profile.assert_not_called()

    def test_inactive_profile_is_backed_up_before_deletion(self):
        self.nm.active_uuid.return_value = ""
        netpilot.profile_menu(self.nm, 180)
        self.nm.delete.assert_called_once_with("selected")
        calls = self.nm.mock_calls
        self.assertLess(calls.index(mock.call.backup_profile("selected")), calls.index(mock.call.delete("selected")))
        self.assertEqual(self.nm.active_uuid.call_args_list,
                         [mock.call("eth0"), mock.call("veth0"), mock.call("eth0"), mock.call("veth0")])


class TransactionCliSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(mock.patch.object(netpilot, "require_root"))
        routes = self.stack.enter_context(mock.patch.object(netpilot, "RouteActions"))
        routes.return_value.path.return_value = self.directory / "absent-route-record"
        self.stack.enter_context(mock.patch.object(netpilot, "NM"))
        self.transactions = self.stack.enter_context(mock.patch.object(netpilot, "Transactions"))
        self.manager = self.transactions.return_value

    def test_confirm_partial_save_is_a_cli_error(self):
        self.manager.commit.return_value = {"status": "confirmed-not-saved"}
        with self.assertRaisesRegex(UserError, "未全部完成"):
            netpilot.main(["confirm", "a1b2c3d4e5f6"])

    def test_confirm_committed_returns_success(self):
        self.manager.commit.return_value = {"status": "committed"}
        self.assertEqual(netpilot.main(["confirm", "a1b2c3d4e5f6"]), 0)

    def test_partial_rollback_is_a_cli_error(self):
        self.manager.rollback.return_value = {"status": "rollback-needs-attention"}
        with self.assertRaises(UserError):
            netpilot.main(["rollback", "a1b2c3d4e5f6"])

    def test_completed_rollback_returns_success(self):
        self.manager.rollback.return_value = {"status": "rolled-back"}
        self.assertEqual(netpilot.main(["rollback", "a1b2c3d4e5f6"]), 0)

    def test_external_partial_commit_in_wait_loop_is_failure(self):
        record = {"token": "a1b2c3d4e5f6", "created": time.time(), "timeout": 180,
                  "status": "confirmed-not-saved"}
        path = self.directory / "transaction.json"
        path.write_text(json.dumps(record))
        manager = mock.Mock()
        manager.path.return_value = path
        with self.assertRaisesRegex(UserError, "未全部完成"):
            netpilot.wait_confirmation(manager, record)

    def test_keep_with_partial_save_is_failure(self):
        record = {"token": "a1b2c3d4e5f6", "created": time.time(), "timeout": 180, "status": "pending"}
        path = self.directory / "transaction.json"
        path.write_text(json.dumps(record))
        manager = mock.Mock()
        manager.path.return_value = path
        manager.commit.return_value = {"status": "confirmed-not-saved"}
        stream = io.StringIO("KEEP\n")
        with mock.patch.object(netpilot.sys, "stdin", stream), \
             mock.patch.object(netpilot.select, "select", return_value=([stream], [], [])):
            with self.assertRaisesRegex(UserError, "未全部提交"):
                netpilot.wait_confirmation(manager, record)


if __name__ == "__main__":
    unittest.main()
