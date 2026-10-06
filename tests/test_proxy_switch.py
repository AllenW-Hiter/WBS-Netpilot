"""Unified proxy switches: temporary files, fake sudo/APT, isolated real Git config."""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import netpilot
from netpilot_lib import common, proxy
from netpilot_lib.common import UserError


CONFIG = {"schema": 1, "scheme": "http", "host": "192.168.1.20", "port": 10808,
          "username": "tester", "password": "fixture-secret", "no_proxy": "localhost,127.0.0.1"}


class UnifiedShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.script = self.directory / "proxy.sh"
        self.active = self.directory / "apt-active"
        self.calls = self.directory / "helper-calls"
        self.helper = self.directory / "fake-helper.py"
        self.helper.write_text(
            "import os, pathlib, sys\n"
            "action = sys.argv[1]\n"
            "with pathlib.Path(" + repr(str(self.calls)) + ").open('a') as f: f.write(action + '\\n')\n"
            "if os.environ.get('NETPILOT_TEST_FAIL_' + action.upper().replace('-', '_')): sys.exit(7)\n"
            "active = pathlib.Path(" + repr(str(self.active)) + ")\n"
            "if action == 'proxy-apt-on': active.write_text('on')\n"
            "elif active.exists(): active.unlink()\n")
        self.script.write_text(proxy.bash_proxy_script(CONFIG, [sys.executable, str(self.helper)]))

    def execute(self, commands, extra_env=None):
        env = {key: value for key, value in os.environ.items() if key not in common.PROXY_KEYS}
        env.update(extra_env or {})
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", commands, "bash", str(self.script)],
                              env=env, text=True, capture_output=True, timeout=10)

    def test_on_off_controls_apt_and_exported_child_environment(self):
        result = self.execute('source "$1"; netpilot_proxy_on >/dev/null || exit; '
                              '[[ "$https_proxy" == "$http_proxy" ]] || exit 2; '
                              "/usr/bin/python3 -c 'import os; assert os.environ[\"https_proxy\"].endswith(\"192.168.1.20:10808\")' || exit 3; "
                              'netpilot_proxy_off >/dev/null; [[ ! -v http_proxy && ! -v HTTPS_PROXY ]]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["proxy-apt-on", "proxy-apt-off"])
        self.assertFalse(self.active.exists())

    def test_source_never_calls_privileged_helper(self):
        result = self.execute('source "$1"; declare -F netpilot_proxy_on >/dev/null; [[ ! -v http_proxy ]]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.calls.exists())

    def test_failed_apt_on_leaves_original_environment_and_no_active_state(self):
        result = self.execute('export http_proxy=old; source "$1"; netpilot_proxy_on >/dev/null 2>&1; '
                              '[[ $? == 1 && "$http_proxy" == old && ! -v _NETPILOT_PROXY_ACTIVE ]]',
                              {"NETPILOT_TEST_FAIL_PROXY_APT_ON": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.active.exists())

    def test_failed_apt_off_preserves_proxy_and_recovery_state(self):
        result = self.execute('source "$1"; netpilot_proxy_on >/dev/null || exit; '
                              'export NETPILOT_TEST_FAIL_PROXY_APT_OFF=1; netpilot_proxy_off >/dev/null 2>&1; '
                              '[[ $? == 1 && "$_NETPILOT_PROXY_ACTIVE" == 1 && -v https_proxy ]]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.active.exists())

    def test_off_in_new_terminal_still_closes_system_apt(self):
        self.active.write_text("on")
        result = self.execute('source "$1"; netpilot_proxy_off >/dev/null')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.active.exists())

    def test_readonly_proxy_variable_prevents_any_apt_change(self):
        result = self.execute('readonly https_proxy=fixed; source "$1"; netpilot_proxy_on >/dev/null 2>&1; [[ $? == 1 ]]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.calls.exists())

    def test_readonly_variable_on_off_does_not_partially_disable_apt(self):
        result = self.execute('source "$1"; netpilot_proxy_on >/dev/null || exit; readonly https_proxy; '
                              'netpilot_proxy_off >/dev/null 2>&1; [[ $? == 1 && "$_NETPILOT_PROXY_ACTIVE" == 1 ]]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.active.exists())
        self.assertEqual(self.calls.read_text().splitlines(), ["proxy-apt-on"])

    def test_reload_and_repeated_on_still_restore_original_variables(self):
        result = self.execute('export http_proxy=original; source "$1"; netpilot_proxy_on >/dev/null; '
                              'source "$1"; netpilot_proxy_on >/dev/null; netpilot_proxy_off >/dev/null; '
                              '[[ "$http_proxy" == original && ! -v https_proxy ]]')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_credentials_never_appear_in_helper_arguments_or_output(self):
        result = self.execute('source "$1"; netpilot_proxy_on; netpilot_proxy_off')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(CONFIG["password"], result.stdout + result.stderr + self.calls.read_text())


class ProxyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "operator"
        self.home.mkdir()
        self.account = types.SimpleNamespace(pw_dir=str(self.home), pw_uid=os.getuid(),
                                             pw_gid=os.getgid(), pw_name="test-operator")
        self.apt = self.root / "etc-apt" / "apt.conf.d" / "99-netpilot-proxy"
        self.apt.parent.mkdir(parents=True)
        self.main = self.apt.parent.parent / "apt.conf"
        self.state = self.root / "state"
        self.git = self.home / ".gitconfig"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(proxy, "operator_account", return_value=self.account))
        self.stack.enter_context(mock.patch.object(common, "operator_account", return_value=self.account))
        self.stack.enter_context(mock.patch.object(proxy, "require_root"))
        self.stack.enter_context(mock.patch.object(proxy, "APT_FILE", self.apt))
        self.stack.enter_context(mock.patch.object(proxy, "APT_MAIN", self.main))
        self.stack.enter_context(mock.patch.object(proxy, "STATE", self.state))
        self.stack.enter_context(mock.patch.object(proxy, "safe_directory", side_effect=lambda path, root_only=False: common.safe_directory(path)))
        self.stack.enter_context(mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.git),
                                                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "0"}))
        self.manager = proxy.ProxyManager()
        self.manager.directory.mkdir(parents=True)
        common.save_json(self.manager.config_file, CONFIG)
        self.run_calls = []
        self.forced_dump = None
        self.stack.enter_context(mock.patch.object(proxy, "run", side_effect=self.fake_run))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def fake_run(self, args, **kwargs):
        self.run_calls.append(list(args))
        if args[:2] == ["apt-config", "dump"]:
            if self.forced_dump is not None:
                return subprocess.CompletedProcess(args, 0, self.forced_dump, "")
            items = {}
            for path in sorted(self.apt.parent.iterdir()) + [self.main]:
                if not path.is_file() or (path != self.main and path.suffix not in ("", ".conf")):
                    continue
                for line in path.read_text().splitlines():
                    match = re.fullmatch(r'([^\s]+)\s+"(.*)";', line)
                    if match:
                        items[match[1]] = match[2]
            return subprocess.CompletedProcess(args, 0, "\n".join('{} "{}";'.format(k, v) for k, v in items.items()), "")
        return common.run(args, **kwargs)

    def test_apt_on_and_off_write_private_owned_file(self):
        self.manager.set_apt(True)
        self.assertIn("192.168.1.20:10808", self.apt.read_text())
        self.assertEqual(self.apt.stat().st_mode & 0o777, 0o600)
        self.manager.set_apt(False)
        self.assertFalse(self.apt.exists())

    def test_apt_socks_and_https_protocols_remain_exact(self):
        for scheme in ("socks5h", "https"):
            config = dict(CONFIG, scheme=scheme)
            common.save_json(self.manager.config_file, config)
            self.manager.set_apt(True)
            self.assertIn('"{}://'.format(scheme), self.apt.read_text())
            self.manager.set_apt(False)

    def test_later_99proxy_is_detected_and_new_file_rolled_back(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://192.168.1.20:6984";\n')
        with self.assertRaises(UserError):
            self.manager.set_apt(True)
        self.assertFalse(self.apt.exists())
        self.assertIn("6984", legacy.read_text())

    def test_main_config_override_restores_previous_owned_file(self):
        old = proxy.apt_text(dict(CONFIG, port=7890))
        common.atomic_write(self.apt, old)
        self.main.write_text('Acquire::https::Proxy "http://old:6984";\n')
        with self.assertRaises(UserError):
            self.manager.set_apt(True)
        self.assertEqual(self.apt.read_text(), old)

    def test_host_specific_and_binary_proxy_rules_fail_apt_switch(self):
        for key in ("Acquire::http::Proxy::example.com", "Binary::apt::Acquire::http::Proxy",
                    "Acquire::http::Proxy-Auto-Detect"):
            self.forced_dump = '{} "http://fixture:6984";'.format(key)
            with self.assertRaises(UserError):
                self.manager.set_apt(True)
            self.assertFalse(self.apt.exists())

    def test_apt_off_failure_restores_owned_file(self):
        self.manager.set_apt(True)
        previous = self.apt.read_text()
        self.forced_dump = 'Acquire::http::Proxy "http://fixture:6984";'
        with self.assertRaises(UserError):
            self.manager.set_apt(False)
        self.assertEqual(self.apt.read_text(), previous)

    def test_foreign_or_symlink_apt_file_is_not_overwritten(self):
        self.apt.write_text("foreign configuration")
        with self.assertRaises(UserError):
            self.manager.set_apt(True)
        self.assertEqual(self.apt.read_text(), "foreign configuration")
        self.apt.unlink()
        target = self.root / "target"
        target.write_text("original")
        self.apt.symlink_to(target)
        with self.assertRaises(UserError):
            self.manager.set_apt(False)
        self.assertEqual(target.read_text(), "original")

    def test_bashrc_contains_two_functions_and_no_secret(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("# user settings\n")
        self.manager.install_bash_loader()
        text = bashrc.read_text()
        self.assertIn("netpilot_proxy_on()", text)
        self.assertIn("netpilot_proxy_off()", text)
        self.assertIn("# user settings", text)
        self.assertNotIn(CONFIG["password"], text)
        self.assertEqual(self.manager.shell_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(subprocess.run(["bash", "-n", str(bashrc)], capture_output=True).returncode, 0)

    def test_install_updates_legacy_block_without_duplicate_functions(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("# user settings\n" + proxy.BASH_BEGIN + "\n. old-proxy.sh\n" + proxy.BASH_END + "\n")
        self.manager.install_bash_loader()
        self.manager.install_bash_loader()
        text = bashrc.read_text()
        self.assertEqual(text.count("netpilot_proxy_on()"), 1)
        self.assertEqual(text.count(proxy.BASH_BEGIN), 1)
        self.assertNotIn("old-proxy.sh", text)

    def test_bad_bashrc_block_is_rejected_without_modification(self):
        bashrc = self.home / ".bashrc"
        for text in (proxy.BASH_BEGIN + "\n", (proxy.BASH_BEGIN + "\n" + proxy.BASH_END + "\n") * 2):
            bashrc.write_text(text)
            with self.assertRaises(UserError):
                self.manager.install_bash_loader()
            self.assertEqual(bashrc.read_text(), text)

    def test_bashrc_switch_reloads_updated_config_and_preserves_original_state(self):
        with mock.patch.object(proxy, "APT_HELPER", ["/bin/true"]):
            self.manager.install_bash_loader()
        second = self.root / "updated.sh"
        second.write_text(proxy.bash_proxy_script(dict(CONFIG, port=7890), ["/bin/true"]))
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c",
            'export http_proxy=original; source "$1"; netpilot_proxy_on >/dev/null || exit; '
            'cp "$2" "$3"; netpilot_proxy_on >/dev/null || exit; '
            '[[ "$http_proxy" == *":7890" ]] || exit 3; netpilot_proxy_off >/dev/null; [[ "$http_proxy" == original ]]',
            "bash", str(self.home / ".bashrc"), str(second), str(self.manager.shell_file)],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loaded_off_function_survives_private_script_removal(self):
        with mock.patch.object(proxy, "APT_HELPER", ["/bin/true"]):
            self.manager.install_bash_loader()
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c",
            'export http_proxy=original; source "$1"; netpilot_proxy_on >/dev/null || exit; '
            'rm "$2"; netpilot_proxy_off >/dev/null; [[ "$http_proxy" == original ]]',
            "bash", str(self.home / ".bashrc"), str(self.manager.shell_file)],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bashrc_symlink_is_rejected(self):
        target = self.root / "target-bashrc"
        target.write_text("original")
        (self.home / ".bashrc").symlink_to(target)
        with self.assertRaises(UserError):
            self.manager.install_bash_loader()
        self.assertEqual(target.read_text(), "original")

    def test_cleanup_backs_up_99proxy_and_preserves_nonproxy_settings(self):
        legacy = self.apt.parent / "99proxy"
        old = 'Acquire::http::Proxy "http://192.168.1.20:6984";\nAcquire::https::Proxy "http://192.168.1.20:6984";\n'
        legacy.write_text(old)
        self.main.write_text('APT::Get::Assume-Yes "false";\nAcquire::http::Proxy "http://fixture:6984";\n')
        self.manager.cleanup_legacy(confirm_action=False)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.main.read_text(), 'APT::Get::Assume-Yes "false";\n')
        backups = list((self.state / "backups" / "proxy").glob("*/*99proxy"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), old)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.manager.set_apt(True)
        self.manager.set_apt(False)

    def test_cleanup_real_git_global_and_included_proxy_keys_only(self):
        included = self.home / "included.gitconfig"
        included.write_text('[http "https://github.com"]\n\tproxy = http://fixture:6984\n[user]\n\tname = Test User\n')
        self.git.write_text('[http]\n\tproxy = http://fixture:6984\n[remote "origin"]\n\tproxy = http://fixture:6984\n'
                            '[include]\n\tpath = {}\n[user]\n\temail = tester@example.invalid\n'.format(included))
        self.manager.cleanup_legacy(confirm_action=False)
        self.assertNotIn("proxy =", self.git.read_text() + included.read_text())
        self.assertIn("tester@example.invalid", self.git.read_text())
        self.assertIn("Test User", included.read_text())
        backups = list(self.manager.directory.glob("legacy-proxy-*/*"))
        self.assertEqual(len(backups), 2)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))

    def test_cleanup_nested_apt_syntax_stops_before_any_change(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire { http { Proxy "http://fixture:6984"; }; };\n')
        previous = legacy.read_text()
        with self.assertRaises(UserError):
            self.manager.cleanup_legacy(confirm_action=False)
        self.assertEqual(legacy.read_text(), previous)
        self.assertFalse((self.state / "backups").exists())

    def test_cleanup_works_without_git_installed(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        with mock.patch.object(proxy.shutil, "which", return_value=None):
            self.manager.cleanup_legacy(confirm_action=False)
        self.assertFalse(legacy.exists())
        self.assertFalse(any(args[0] == "git" for args in self.run_calls))

    def test_cleanup_refuses_symlink_before_any_mutation(self):
        original = self.root / "original"
        original.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        (self.apt.parent / "99proxy").symlink_to(original)
        with self.assertRaises(UserError):
            self.manager.cleanup_legacy(confirm_action=False)
        self.assertTrue((self.apt.parent / "99proxy").is_symlink())

    def test_declined_cleanup_keeps_original_files(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        with mock.patch.object(proxy, "confirm", return_value=False):
            self.manager.cleanup_legacy()
        self.assertTrue(legacy.exists())
        self.assertFalse((self.state / "backups").exists())

    def test_backup_failure_prevents_any_old_proxy_deletion(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        with mock.patch.object(proxy, "atomic_write", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                self.manager.cleanup_legacy(confirm_action=False)
        self.assertTrue(legacy.exists())

    def test_partial_cleanup_is_failure_with_recoverable_backups(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        self.git.write_text('[http]\n\tproxy = http://fixture:6984\n')
        original_run = self.fake_run
        def fail_unset(args, **kwargs):
            if "--unset-all" in args:
                raise UserError("simulated write failure")
            return original_run(args, **kwargs)
        with mock.patch.object(proxy, "run", side_effect=fail_unset):
            with self.assertRaisesRegex(UserError, "未全部完成"):
                self.manager.cleanup_legacy(confirm_action=False)
        self.assertNotIn("已清理", self.output.getvalue())
        self.assertTrue(list((self.state / "backups" / "proxy").glob("*/*")))
        self.assertTrue(list(self.manager.directory.glob("legacy-proxy-*/*")))

    def test_cleanup_does_not_print_proxy_credentials(self):
        (self.apt.parent / "99proxy").write_text('Acquire::http::Proxy "http://tester:fixture-secret@host:6984";\n')
        self.manager.cleanup_legacy(confirm_action=False)
        self.assertNotIn("fixture-secret", self.output.getvalue())
        self.assertTrue(all("fixture-secret" not in arg for args in self.run_calls for arg in args))

    def test_configure_cleans_old_proxy_and_installs_functions_without_enabling(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        with mock.patch.object(proxy, "choose", return_value="http"), \
             mock.patch.object(proxy, "ask", side_effect=["192.168.1.20", "10808", "", "localhost"]), \
             mock.patch.object(proxy, "confirm", side_effect=[True, False]):
            self.manager.configure()
        self.assertFalse(legacy.exists())
        self.assertFalse(self.apt.exists())
        self.assertEqual(self.manager.load()["port"], 10808)
        self.assertIn("netpilot_proxy_on()", (self.home / ".bashrc").read_text())

    def test_declined_configure_does_not_clean_or_replace_configuration(self):
        legacy = self.apt.parent / "99proxy"
        legacy.write_text('Acquire::http::Proxy "http://fixture:6984";\n')
        previous = self.manager.config_file.read_text()
        with mock.patch.object(proxy, "choose", return_value="http"), \
             mock.patch.object(proxy, "ask", side_effect=["192.168.1.20", "7890", "", "localhost"]), \
             mock.patch.object(proxy, "confirm", return_value=False):
            self.manager.configure()
        self.assertTrue(legacy.exists())
        self.assertEqual(self.manager.config_file.read_text(), previous)
        self.assertFalse((self.home / ".bashrc").exists())

    def test_configure_cleanup_failure_does_not_replace_private_configuration(self):
        (self.apt.parent / "99proxy").write_text('Acquire { http { Proxy "http://fixture:6984"; }; };\n')
        previous = self.manager.config_file.read_text()
        with mock.patch.object(proxy, "choose", return_value="http"), \
             mock.patch.object(proxy, "ask", side_effect=["192.168.1.20", "7890", "", "localhost"]), \
             mock.patch.object(proxy, "confirm", return_value=True):
            with self.assertRaises(UserError):
                self.manager.configure()
        self.assertEqual(self.manager.config_file.read_text(), previous)
        self.assertFalse((self.home / ".bashrc").exists())

    def test_dry_run_apt_helper_and_cleanup_never_mutate(self):
        for command in ("proxy-apt-on", "proxy-apt-off", "proxy-cleanup", "proxy-run"):
            with mock.patch.object(netpilot, "ProxyManager") as manager:
                with self.assertRaises(UserError):
                    netpilot.main(["--dry-run", command])
                manager.assert_not_called()

    def test_menu_contains_function_install_and_cleanup_without_apt_toggle(self):
        def choose(title, items):
            self.assertIn("安装/更新终端代理开关函数", items)
            self.assertIn("清理旧 APT / Git 代理配置", items)
            self.assertFalse(any("APT 覆盖项" in item for item in items))
            raise common.Cancelled("done")
        with mock.patch.object(proxy, "choose", side_effect=choose):
            with self.assertRaises(common.Cancelled):
                self.manager.menu()


if __name__ == "__main__":
    unittest.main()
