import contextlib
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

import netpilot
import netpilot_lib.nm as nm_module
import netpilot_lib.routes as routes_module
from netpilot_lib.common import UserError, atomic_write, load_json, save_json, ask
from netpilot_lib.nm import Transactions, encode_typed, decode_typed, settings_to_dbus
from netpilot_lib.proxy import bash_proxy_script
from netpilot_lib.network import build_settings
from test_validation import example


class Dictionary(dict):
    def __init__(self, value=(), signature="sv"):
        super().__init__(value); self.signature = signature
class Array(list):
    def __init__(self, value=(), signature="v"):
        super().__init__(value); self.signature = signature
class Struct(tuple):
    pass
class ByteArray(bytes):
    pass

FakeDBus = types.SimpleNamespace(Dictionary=Dictionary, Array=Array, Struct=Struct, ByteArray=ByteArray)
for _name in ("Byte", "Boolean", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"):
    setattr(FakeDBus, _name, type(_name, (int,), {}))
for _name in ("String", "ObjectPath", "Signature"):
    setattr(FakeDBus, _name, type(_name, (str,), {}))
FakeDBus.Double = type("Double", (float,), {})


class DBusTypingTests(unittest.TestCase):
    def test_typed_ipv4_addresses(self):
        settings = settings_to_dbus(build_settings(example()), FakeDBus)
        self.assertEqual(settings.signature, "sa{sv}")
        self.assertEqual(settings["ipv4"]["address-data"].signature, "a{sv}")
        self.assertEqual(type(settings["ipv4"]["address-data"][0]["prefix"]).__name__, "UInt32")

    def test_dns_native_order_uint32(self):
        import socket, struct
        settings = settings_to_dbus(build_settings(example()), FakeDBus)
        value = settings["ipv4"]["dns"][0]
        self.assertEqual(socket.inet_ntoa(struct.pack("=I", value)), "192.168.1.20")
        self.assertEqual(settings["ipv4"]["dns"].signature, "u")

    def test_ssid_bytearray(self):
        settings = settings_to_dbus(build_settings(example("dual_wifi_host.json"), "example-pass"), FakeDBus)
        self.assertEqual(type(settings["802-11-wireless"]["ssid"]).__name__, "ByteArray")

    def test_route_metric_int64(self):
        settings = settings_to_dbus({"ipv4": {"route-metric": 100}}, FakeDBus)
        self.assertEqual(type(settings["ipv4"]["route-metric"]).__name__, "Int64")

    def test_lossless_backup_roundtrip(self):
        settings = settings_to_dbus(build_settings(example("dual_wifi_host.json"), "example-pass"), FakeDBus)
        restored = decode_typed(json.loads(json.dumps(encode_typed(settings))), FakeDBus)
        self.assertEqual(restored, settings)
        self.assertEqual(type(restored["802-11-wireless"]["ssid"]).__name__, "ByteArray")
        self.assertEqual(type(restored["connection"]["autoconnect"]).__name__, "Boolean")

    def test_untrusted_serial_type_rejected(self):
        with self.assertRaises(UserError): decode_typed({"t": "eval", "v": "bad"}, FakeDBus)


class FakeNM:
    def __init__(self):
        self.items = {"old": {"uuid": "old", "name": "Original", "path": "/old", "saved": True}}
        self.active = "old"
        self.cp = False
        self.old_active = "old"
        self.log = []
        self.activate_fail = False
        self.persist_fail = False
        self.rollback_fail = False

    def active_uuid(self, interface): return self.active
    def profile(self, u): return self.items.get(u)
    def backup_profile(self, u): self.log.append(("backup", u)); return "private-backup-" + u
    def checkpoint(self, interfaces, timeout):
        self.cp = True; self.old_active = self.active; self.log.append(("checkpoint", tuple(interfaces)))
        return "/checkpoint/1"
    def checkpoint_exists(self, path): return self.cp
    def add_unsaved(self, settings):
        c = settings["connection"]; u = c["uuid"]
        self.items[u] = {"uuid": u, "name": c["id"], "path": "/" + u, "saved": False, "autoconnect": c["autoconnect"]}
        self.log.append(("add-unsaved", u))
        return "/" + u
    def activate(self, path, interface):
        if self.activate_fail: raise UserError("simulated activation failure")
        self.active = path[1:]; self.log.append(("activate", self.active))
    def rollback(self, path):
        if self.rollback_fail: return {"/device/1": 1}
        if self.cp: self.active = self.old_active
        self.cp = False; self.log.append(("rollback",))
        return {"/device/1": 0}
    def destroy_checkpoint(self, path):
        if not self.cp: raise UserError("checkpoint expired")
        self.cp = False; self.log.append(("destroy",))
    def persist(self, u, name, auto):
        if self.cp: raise AssertionError("persisted before confirmation")
        if self.persist_fail: raise UserError("simulated disk failure")
        self.items[u].update(saved=True, name=name, autoconnect=auto)
        self.log.append(("persist", u))
    def delete(self, u):
        self.items.pop(u, None); self.log.append(("delete", u))
    def disconnect(self, interface): self.active = ""


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.temp.name)
        self.patches = [patch.object(nm_module, "STATE", self.state),
                        patch.object(nm_module, "require_root"), patch.object(nm_module, "audit"),
                        patch.object(nm_module, "safe_directory", lambda path, **kw: path.mkdir(parents=True, exist_ok=True))]
        for p in self.patches: p.start()
        self.output = contextlib.redirect_stdout(io.StringIO()); self.output.__enter__()
        self.nm = FakeNM(); self.manager = Transactions(self.nm, 180)

    def tearDown(self):
        self.output.__exit__(None, None, None)
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def stage(self, u="new"):
        return self.manager.stage(build_settings(example(), uuid_value=u), "end1", "DEVICE-NETWORK", True, "client:end1", example())

    def test_trial_is_unsaved_and_no_autoconnect(self):
        self.stage()
        self.assertFalse(self.nm.items["new"]["saved"])
        self.assertFalse(self.nm.items["new"]["autoconnect"])
        self.assertTrue(self.nm.cp)

    def test_commit_saves_only_after_confirmation(self):
        record = self.stage(); final = self.manager.commit(record["token"])
        self.assertEqual(final["status"], "committed")
        self.assertTrue(self.nm.items["new"]["saved"])
        self.assertLess(self.nm.log.index(("destroy",)), self.nm.log.index(("persist", "new")))

    def test_original_profile_is_preserved(self):
        record = self.stage(); self.manager.commit(record["token"])
        self.assertIn("old", self.nm.items)
        self.assertNotIn(("delete", "old"), self.nm.log)

    def test_manual_rollback_restores_and_cleans_trial(self):
        record = self.stage(); final = self.manager.rollback(record["token"])
        self.assertEqual(final["status"], "rolled-back")
        self.assertEqual(self.nm.active, "old")
        self.assertNotIn("new", self.nm.items)

    def test_activation_failure_rolls_back(self):
        self.nm.activate_fail = True
        with self.assertRaises(UserError): self.stage()
        self.assertEqual(self.nm.active, "old")
        self.assertNotIn("new", self.nm.items)
        self.assertFalse(self.nm.cp)

    def test_expired_checkpoint_cannot_commit(self):
        record = self.stage(); self.nm.cp = False; self.nm.active = "old"
        with self.assertRaises(UserError): self.manager.commit(record["token"])
        self.assertFalse(self.nm.items["new"]["saved"])

    def test_external_connection_switch_cannot_commit_trial(self):
        record = self.stage(); self.nm.active = "old"
        with self.assertRaises(UserError): self.manager.commit(record["token"])

    def test_overlapping_tool_transaction_refused(self):
        self.stage()
        with self.assertRaises(UserError): self.stage("new2")
        self.assertNotIn("new2", self.nm.items)

    def test_disk_failure_not_reported_as_saved(self):
        record = self.stage(); self.nm.persist_fail = True
        with self.assertRaises(UserError): self.manager.commit(record["token"])
        self.assertEqual(load_json(self.manager.path(record["token"]))["status"], "confirmed-not-saved")
        self.assertIn("old", self.nm.items)

    def test_repeated_configuration_prunes_only_owned_old_profile(self):
        r1 = self.stage("first"); self.manager.commit(r1["token"])
        r2 = self.stage("second"); self.manager.commit(r2["token"])
        self.assertIn("old", self.nm.items)
        self.assertNotIn("first", self.nm.items)
        self.assertIn("second", self.nm.items)
        self.assertEqual(set(self.manager.registry()), {"second"})

    def test_committed_checkpoint_not_falsely_rollbackable(self):
        r = self.stage(); self.manager.commit(r["token"])
        with self.assertRaises(UserError): self.manager.rollback(r["token"])

    def test_rollback_failure_keeps_trial_for_investigation(self):
        r = self.stage(); self.nm.rollback_fail = True
        with self.assertRaises(UserError): self.manager.rollback(r["token"])
        self.assertIn("new", self.nm.items)
        self.assertEqual(load_json(self.manager.path(r["token"]))["status"], "rollback-needs-attention")

    def test_invalid_token_path_traversal(self):
        with self.assertRaises(UserError): self.manager.path("../../secret")

    def test_native_timeout_orphan_cleanup(self):
        r = self.stage(); self.nm.cp = False; self.nm.active = "old"
        self.manager.ensure_idle()
        self.assertNotIn("new", self.nm.items)
        self.assertEqual(load_json(self.manager.path(r["token"]))["status"], "rolled-back")

    def test_do_not_delete_still_active_orphan(self):
        r = self.stage(); self.nm.cp = False
        with self.assertRaises(UserError): self.manager.rollback(r["token"])
        self.assertIn("new", self.nm.items)

    def test_disconnect_is_reversible(self):
        r = self.manager.disconnect("end1")
        self.assertEqual(self.nm.active, "")
        self.manager.rollback(r["token"])
        self.assertEqual(self.nm.active, "old")

    def test_disconnect_reconnected_by_other_actor_cannot_commit(self):
        r = self.manager.disconnect("end1")
        self.nm.active = "old"
        with self.assertRaises(UserError): self.manager.commit(r["token"])
        self.assertTrue(self.nm.cp)

    def test_activated_profile_changed_by_other_actor_cannot_commit(self):
        r = self.manager.activate_existing("old", "end1")
        self.nm.active = "other"
        with self.assertRaises(UserError): self.manager.commit(r["token"])
        self.assertTrue(self.nm.cp)

    def test_missing_checkpoint_disconnect_not_falsely_restored(self):
        r = self.manager.disconnect("end1")
        self.nm.cp = False
        with self.assertRaises(UserError): self.manager.rollback(r["token"])
        self.assertEqual(load_json(self.manager.path(r["token"]))["status"], "rollback-needs-attention")

    def test_missing_checkpoint_external_activation_not_falsely_restored(self):
        r = self.manager.activate_existing("old", "end1")
        self.nm.cp = False
        self.nm.active = "external"
        with self.assertRaises(UserError): self.manager.rollback(r["token"])
        self.assertEqual(load_json(self.manager.path(r["token"]))["status"], "rollback-needs-attention")

    def test_pending_receipt_has_no_wifi_password(self):
        p = example("dual_wifi_host.json")
        r = self.manager.stage(build_settings(p, "dummy-secret-password", uuid_value="wifi"),
                               "end1", "test", True, "host:end1", p)
        self.assertNotIn("dummy-secret-password", self.manager.path(r["token"]).read_text())



class RouteSetupSafetyTests(unittest.TestCase):
    def run_failed_setup(self, fail_install):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            calls = []
            def fake_run(args, **kwargs):
                calls.append(list(args))
                if args[:2] == ["systemctl", "is-system-running"]:
                    return types.SimpleNamespace(stdout="running\n", stderr="", returncode=0)
                if args[0] == "systemd-run" and fail_install:
                    raise UserError("simulated timer install failure")
                if "route" in args and "add" in args:
                    raise KeyboardInterrupt()
                return types.SimpleNamespace(stdout="", stderr="", returncode=0)
            with patch.object(routes_module, "STATE", state), patch.object(routes_module, "require_root"), \
                 patch.object(routes_module, "safe_directory", lambda path, **kw: path.mkdir(parents=True, exist_ok=True)), \
                 patch.object(routes_module, "run", side_effect=fake_run), \
                 patch.object(routes_module, "boot_id", return_value="test-boot"), \
                 contextlib.redirect_stdout(io.StringIO()):
                manager = routes_module.RouteActions(180)
                error = UserError if fail_install else KeyboardInterrupt
                with self.assertRaises(error):
                    manager.begin({"interface": "end1", "gateway": "192.168.1.20", "metric": 100, "protocol": 242}, "add")
                records = list((state / "route-actions").glob("*.json"))
                self.assertEqual(len(records), 1)
                return load_json(records[0]), calls

    def test_interrupt_after_route_attempt_preserves_independent_timer(self):
        data, calls = self.run_failed_setup(False)
        self.assertEqual(data["status"], "pending")
        self.assertFalse(any(c[:2] == ["systemctl", "stop"] for c in calls))

    def test_timer_install_failure_never_attempts_route_change(self):
        data, calls = self.run_failed_setup(True)
        self.assertEqual(data["status"], "failed")
        self.assertFalse(any("route" in c and "add" in c for c in calls))


class ProxyShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "proxy.sh"
        self.config = {"schema": 1, "scheme": "http", "host": "192.168.1.20", "port": 7890,
                       "username": "user", "password": "dummy-pass", "no_proxy": "localhost,127.0.0.1"}
        self.path.write_text(bash_proxy_script(self.config, apt_helper=["/bin/true"]))

    def tearDown(self): self.temp.cleanup()

    def execute(self, script):
        env = dict(os.environ)
        for key in list(env):
            if key.lower().endswith("_proxy"): env.pop(key)
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script, "bash", str(self.path)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10)

    def test_generated_script_syntax(self):
        result = subprocess.run(["bash", "-n", str(self.path)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_does_not_enable(self):
        r = self.execute('source "$1"; [[ ! -v http_proxy ]]')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_enable_and_restore_original(self):
        r = self.execute('export http_proxy="http://old:1"; source "$1"; netpilot_proxy_on >/dev/null; '
                         '[[ "$http_proxy" == *"192.168.1.20:7890" ]] || exit 2; netpilot_proxy_off >/dev/null; '
                         '[[ "$http_proxy" == "http://old:1" && ! -v https_proxy ]]')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_repeat_on_does_not_overwrite_old_state(self):
        r = self.execute('export http_proxy="old"; source "$1"; netpilot_proxy_on >/dev/null; '
                         'netpilot_proxy_on >/dev/null; netpilot_proxy_off >/dev/null; [[ "$http_proxy" == old ]]')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_restore_nonexported_variable(self):
        r = self.execute('http_proxy="old"; source "$1"; netpilot_proxy_on >/dev/null; '
                         'netpilot_proxy_off >/dev/null; [[ "$http_proxy" == old ]] && ! export -p | grep -q "http_proxy="')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_readonly_variable_no_partial_change(self):
        r = self.execute('readonly https_proxy="fixed"; http_proxy="old"; source "$1"; '
                         'netpilot_proxy_on >/dev/null 2>&1; [[ $? == 1 && "$http_proxy" == old ]]')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_shell_metacharacters_remain_data(self):
        marker = pathlib.Path(self.temp.name) / "SHOULD_NOT_EXIST"
        self.config["no_proxy"] = "$(touch " + str(marker) + ") ' ; echo injected"
        self.path.write_text(bash_proxy_script(self.config, apt_helper=["/bin/true"]))
        r = self.execute('source "$1"; netpilot_proxy_on >/dev/null; netpilot_proxy_off >/dev/null')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists())

    def test_password_not_printed(self):
        r = self.execute('source "$1"; netpilot_proxy_on; netpilot_proxy_off')
        self.assertNotIn("dummy-pass", r.stdout + r.stderr)


class FileAndCliSafetyTests(unittest.TestCase):
    def test_atomic_write_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            directory = pathlib.Path(d); target = directory / "target"; target.write_text("original")
            link = directory / "link"; link.symlink_to(target)
            with self.assertRaises(UserError): atomic_write(link, "modified")
            self.assertEqual(target.read_text(), "original")

    def test_secret_input_whitespace_is_preserved(self):
        with patch("getpass.getpass", return_value=" pass word "):
            self.assertEqual(ask("password", secret=True), " pass word ")

    def test_dry_run_plan_does_not_call_network_or_root(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(netpilot, "NM", side_effect=AssertionError("NM called")), \
                patch.object(netpilot, "preflight", side_effect=AssertionError("preflight called")), \
                patch.object(netpilot, "backup_system", side_effect=AssertionError("backup called")):
            result = netpilot.main(["--dry-run", "--plan", str(pathlib.Path(__file__).resolve().parents[1] / "examples" / "client_static.json")])
            self.assertEqual(result, 0)

    def test_misplaced_dry_run_not_ignored(self):
        with patch.object(netpilot, "host_plan", side_effect=AssertionError("wizard called")):
            with self.assertRaises(UserError): netpilot.main(["host", "--dry-run"])

    def test_confirm_requires_one_token(self):
        with self.assertRaises(UserError): netpilot.main(["confirm", "abc", "extra"])

    def test_timeout_bounds(self):
        with self.assertRaises(UserError): netpilot.main(["--timeout", "1", "--demo"])

    def test_demo_no_nm(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(netpilot, "NM", side_effect=AssertionError("NM called")):
            self.assertEqual(netpilot.main(["--demo"]), 0)


if __name__ == "__main__": unittest.main()
