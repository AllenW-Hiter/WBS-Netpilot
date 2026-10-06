"""GitHub SSH wizard: isolated user files, mocked online operations."""
import contextlib
import io
import os
import pathlib
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import types
import time
import unittest
from unittest import mock

import netpilot
from netpilot_lib import common, github_ssh, proxy
from netpilot_lib.common import Cancelled, UserError

PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPrrUDvmhrhaHB8SoPq4pz5iNvrztVpDvwatTdKWpfBV fixture@example.com"
AUTHENTICATED = "Hi fixture-user! You've successfully authenticated, but GitHub does not provide shell access.\n"


class GitHubSSHTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = pathlib.Path(self.temporary.name)
        self.account = types.SimpleNamespace(pw_dir=str(self.home), pw_uid=os.getuid(),
                                             pw_gid=os.getgid(), pw_name="fixture-user")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(github_ssh, "operator_account", return_value=self.account))
        self.stack.enter_context(mock.patch.object(common, "operator_account", return_value=self.account))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.child = self.stack.enter_context(mock.patch.object(github_ssh.subprocess, "run", side_effect=self.fake_run))
        self.manager = github_ssh.GitHubSSH()
        self.manager.prepare_directory()
        self.key = self.manager.directory / "id_ed25519_github"
        self.config = self.manager.directory / "config"
        self.child_exit = 0
        self.ssh_exit = 1
        self.ssh_output = AUTHENTICATED
        self.derived = PUBLIC

    def write_pair(self, path=None):
        path = path or self.key
        path.write_text("fixture private data; never display")
        path.chmod(0o600)
        pathlib.Path(str(path) + ".pub").write_text(PUBLIC + "\n")

    def fake_run(self, args, **kwargs):
        if args[:3] == ["ssh-keygen", "-t", "ed25519"]:
            if not self.child_exit:
                self.write_pair(pathlib.Path(args[args.index("-f") + 1]))
            return subprocess.CompletedProcess(args, self.child_exit, "", "")
        if args[:2] == ["ssh-keygen", "-l"]:
            return subprocess.CompletedProcess(args, self.child_exit, "256 SHA256:fixture fixture@example.com (ED25519)\n", "")
        if args[:2] == ["ssh-keygen", "-y"]:
            return subprocess.CompletedProcess(args, self.child_exit, self.derived, "")
        if args[0] == "ssh":
            return subprocess.CompletedProcess(args, self.ssh_exit, self.ssh_output, "")
        raise AssertionError("Unexpected command")

    def test_new_key_is_user_owned_private_and_password_never_in_arguments(self):
        private, public, fingerprint = self.manager.generate("id_ed25519_github", "fixture@example.com")
        self.assertEqual(private, self.key)
        self.assertEqual(public, PUBLIC)
        self.assertIn("SHA256:fixture", fingerprint)
        self.assertEqual(private.stat().st_uid, self.account.pw_uid)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.manager.directory.glob(".netpilot-key-*")))
        generation = self.child.call_args_list[0]
        self.assertNotIn("-N", generation.args[0])
        self.assertNotIn("-P", generation.args[0])
        self.assertNotIn("fixture private data", self.output.getvalue())
        self.assertFalse(generation.kwargs.get("shell", False))

    def test_existing_private_or_public_is_never_overwritten(self):
        for name in ("id_ed25519_github", "id_ed25519_github.pub"):
            with self.subTest(name=name):
                path = self.manager.directory / name
                path.write_text("original")
                with self.assertRaisesRegex(UserError, "已存在"):
                    self.manager.generate("id_ed25519_github", "fixture@example.com")
                self.assertEqual(path.read_text(), "original")
                path.unlink()
        self.child.assert_not_called()

    def test_broken_symlink_is_not_overwritten(self):
        self.key.symlink_to(self.home / "missing")
        with self.assertRaises(UserError):
            self.manager.generate(self.key.name, "fixture@example.com")
        self.assertTrue(self.key.is_symlink())
        self.child.assert_not_called()

    def test_concurrent_public_creation_preserves_foreign_file_and_removes_own_private(self):
        original = self.fake_run

        def race(args, **kwargs):
            result = original(args, **kwargs)
            if args[:3] == ["ssh-keygen", "-t", "ed25519"]:
                pathlib.Path(str(self.key) + ".pub").write_text("concurrent file")
            return result

        self.child.side_effect = race
        with self.assertRaisesRegex(UserError, "安装密钥失败"):
            self.manager.generate(self.key.name, "fixture@example.com")
        self.assertFalse(self.key.exists())
        self.assertEqual(pathlib.Path(str(self.key) + ".pub").read_text(), "concurrent file")

    def test_failed_keygen_leaves_no_key_or_config_and_removes_temporary_files(self):
        self.child_exit = 7
        with self.assertRaisesRegex(UserError, "生成失败"):
            self.manager.generate(self.key.name, "fixture@example.com")
        self.assertFalse(self.key.exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_invalid_filename_and_comment_never_start_keygen(self):
        for name in ("../key", "/tmp/key", "-key", "a\nHost evil", "config", "known_hosts", "key.pub", "$(touch x)"):
            with self.subTest(name=name), self.assertRaises(UserError):
                self.manager.generate(name, "fixture@example.com")
        for comment in ("", "x\nsecret", "\x1b[2J", "x" * 201):
            with self.subTest(comment=comment), self.assertRaises(UserError):
                self.manager.generate(self.key.name, comment)
        self.child.assert_not_called()

    def test_existing_encrypted_key_uses_native_interactive_prompt_and_checks_pair(self):
        self.write_pair()
        public, _ = self.manager.public_key(self.key, verify_pair=True)
        self.assertEqual(public, PUBLIC)
        args, kwargs = self.child.call_args
        self.assertEqual(args[0], ["ssh-keygen", "-y", "-f", str(self.key)])
        self.assertNotIn("input", kwargs)
        self.assertNotIn("stdin", kwargs)

    def test_mismatching_key_pair_is_rejected(self):
        self.write_pair()
        self.derived = "ssh-ed25519 other-key"
        with self.assertRaisesRegex(UserError, "不匹配"):
            self.manager.public_key(self.key, verify_pair=True)
        self.assertFalse(self.config.exists())

    def test_unreadable_public_key_is_rejected(self):
        self.write_pair()
        self.child_exit = 1
        with self.assertRaisesRegex(UserError, "无法读取"):
            self.manager.public_key(self.key)

    def test_non_private_or_symlink_private_key_is_rejected(self):
        self.write_pair()
        self.key.chmod(0o644)
        with self.assertRaisesRegex(UserError, "chmod 600"):
            self.manager.public_key(self.key)
        self.key.unlink()
        self.key.symlink_to(pathlib.Path(str(self.key) + ".pub"))
        with self.assertRaisesRegex(UserError, "符号链接"):
            self.manager.public_key(self.key)
        self.child.assert_not_called()

    def test_multiline_public_key_is_never_printed_or_used(self):
        self.write_pair()
        pathlib.Path(str(self.key) + ".pub").write_text(PUBLIC + "\nPRIVATE DATA")
        with self.assertRaises(UserError):
            self.manager.public_key(self.key)
        self.assertNotIn("PRIVATE DATA", self.output.getvalue())
        self.child.assert_not_called()

    def test_symlink_ssh_directory_is_rejected(self):
        self.manager.directory.rmdir()
        self.manager.directory.symlink_to(self.home)
        with self.assertRaisesRegex(UserError, "符号链接"):
            self.manager.prepare_directory()

    def test_config_443_backed_up_and_other_hosts_preserved(self):
        self.write_pair()
        original = "ServerAliveInterval 30\nHost other.example\n  User tester\n"
        self.config.write_text(original)
        self.manager.install_config(self.key, 443)
        contents = self.config.read_text()
        self.assertIn("HostName ssh.github.com\n  Port 443", contents)
        self.assertTrue(contents.endswith(original))
        self.assertIn("Host *\n" + github_ssh.END, contents)
        backups = list(self.manager.directory.glob("config.before-netpilot-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_reinstall_config_is_idempotent_and_update_keeps_single_block(self):
        self.write_pair()
        self.manager.install_config(self.key, 22)
        self.manager.install_config(self.key, 22)
        self.assertFalse(list(self.manager.directory.glob("config.before-netpilot-*")))
        self.manager.install_config(self.key, 443)
        contents = self.config.read_text()
        self.assertEqual(contents.count(github_ssh.BEGIN), 1)
        self.assertNotIn("Port 22", contents)

    def test_foreign_config_symlink_and_broken_markers_stop_before_backup_or_write(self):
        self.write_pair()
        target = self.home / "foreign"
        target.write_text("original")
        self.config.symlink_to(target)
        with self.assertRaises(UserError):
            self.manager.install_config(self.key, 22)
        self.assertEqual(target.read_text(), "original")
        self.config.unlink()
        broken = github_ssh.BEGIN + "\nHost github.com\n"
        self.config.write_text(broken)
        with self.assertRaisesRegex(UserError, "标记"):
            self.manager.install_config(self.key, 22)
        self.assertEqual(self.config.read_text(), broken)
        self.assertFalse(list(self.manager.directory.glob("config.before-netpilot-*")))

    def test_failed_config_write_is_not_reported_as_success_and_backup_is_kept(self):
        self.write_pair()
        self.config.write_text("original\n")
        original_write = common.atomic_write

        def fail_target(path, text):
            if path == self.config:
                raise OSError("fixture write failure")
            original_write(path, text)

        with mock.patch.object(github_ssh, "atomic_write", side_effect=fail_target):
            with self.assertRaises(OSError):
                self.manager.install_config(self.key, 22)
        self.assertEqual(self.config.read_text(), "original\n")
        self.assertEqual(len(list(self.manager.directory.glob("config.before-netpilot-*"))), 1)
        self.assertNotIn("已为 github.com 写入", self.output.getvalue())

    def test_ssh_exit_one_with_github_greeting_is_success_and_host_key_prompt_stays_enabled(self):
        self.manager.test(self.key, 443)
        self.assertIn("身份验证成功", self.output.getvalue())
        args = self.child.call_args.args[0]
        self.assertIn("git@ssh.github.com", args)
        self.assertIn("StrictHostKeyChecking=ask", args)
        self.assertIn("IdentityAgent=none", args)
        self.assertEqual(args[args.index("-F") + 1], "/dev/null")

    def test_ssh_permission_denied_or_plain_exit_one_is_failure(self):
        for returncode, output in ((1, ""), (255, "Permission denied (publickey)."), (255, AUTHENTICATED)):
            self.ssh_exit, self.ssh_output = returncode, output
            with self.subTest(returncode=returncode, output=output), self.assertRaisesRegex(UserError, "验证未成功"):
                self.manager.test(self.key, 22)
        self.assertNotIn("身份验证成功", self.output.getvalue())

    def test_ssh_greeting_on_stderr_with_host_warning_and_wrapped_line_is_success(self):
        self.child.side_effect = lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", "Warning: Permanently added host.\n" + AUTHENTICATED.replace("not provide", "not\nprovide"))
        self.manager.test(self.key, 22)
        self.assertIn("身份验证成功", self.output.getvalue())

    def test_invalid_utf8_public_key_is_actionable_failure(self):
        self.write_pair()
        pathlib.Path(str(self.key) + ".pub").write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(UserError, "UTF-8"):
            self.manager.public_key(self.key)
        self.child.assert_not_called()

    def test_ssh_symlink_known_hosts_is_rejected_without_connection(self):
        (self.manager.directory / "known_hosts").symlink_to(self.home / "missing")
        with self.assertRaises(UserError):
            self.manager.test(self.key, 22)
        self.child.assert_not_called()

    def test_missing_tool_explained_without_attempting_apt_or_writing_files(self):
        self.manager.directory.rmdir()
        with mock.patch.object(github_ssh.shutil, "which", return_value=None):
            with self.assertRaisesRegex(UserError, "openssh-client"):
                self.manager.configure()
        self.assertFalse(self.manager.directory.exists())
        self.child.assert_not_called()

    def test_cancelled_generation_does_not_create_directory_or_key(self):
        self.manager.directory.rmdir()
        with mock.patch.object(github_ssh.shutil, "which", return_value="fixture-tool"), \
                mock.patch.object(github_ssh, "choose", return_value="生成新的 Ed25519 密钥"), \
                mock.patch.object(github_ssh, "ask", side_effect=[self.key.name, "fixture@example.com"]), \
                mock.patch.object(github_ssh, "confirm", return_value=False):
            self.manager.configure()
        self.assertFalse(self.manager.directory.exists())
        self.child.assert_not_called()

    def test_complete_wizard_prints_public_key_and_manual_upload_without_online_test(self):
        with mock.patch.object(github_ssh.shutil, "which", return_value="fixture-tool"), \
                mock.patch.object(github_ssh, "choose", side_effect=["生成新的 Ed25519 密钥", 22]), \
                mock.patch.object(github_ssh, "ask", side_effect=[self.key.name, "fixture@example.com"]), \
                mock.patch.object(github_ssh, "confirm", side_effect=[True, True, False]):
            self.manager.configure()
        self.assertTrue(self.key.exists())
        self.assertTrue(self.config.exists())
        output = self.output.getvalue()
        self.assertIn(github_ssh.KEY_PAGE, output)
        self.assertIn(PUBLIC, output)
        self.assertIn("尚未验证", output)
        self.assertNotIn("fixture private data", output)
        self.assertFalse(any(call.args[0][0] == "ssh" for call in self.child.call_args_list))

    def test_reuse_wizard_without_config_write_can_verify_the_selected_key(self):
        self.write_pair()
        with mock.patch.object(github_ssh.shutil, "which", return_value="fixture-tool"), \
                mock.patch.object(github_ssh, "choose", side_effect=["使用已有密钥", 443]), \
                mock.patch.object(github_ssh, "ask", return_value=self.key.name), \
                mock.patch.object(github_ssh, "confirm", side_effect=[False, True]):
            self.manager.configure()
        self.assertFalse(self.config.exists())
        self.assertIn("身份验证成功", self.output.getvalue())
        self.assertFalse(any(call.args[0][:2] == ["ssh-keygen", "-t"] for call in self.child.call_args_list))

    def test_sudo_child_permanently_drops_uid_gid_and_groups_and_sets_user_home(self):
        with mock.patch.object(github_ssh.os, "geteuid", return_value=0), \
                mock.patch.object(github_ssh.os, "initgroups") as groups, \
                mock.patch.object(github_ssh.os, "setgid") as gid, \
                mock.patch.object(github_ssh.os, "setuid") as uid:
            self.manager.invoke(["ssh", "-T", "git@github.com"])
            kwargs = self.child.call_args.kwargs
            kwargs["preexec_fn"]()
            groups.assert_called_once_with(self.account.pw_name, self.account.pw_gid)
            gid.assert_called_once_with(self.account.pw_gid)
            uid.assert_called_once_with(self.account.pw_uid)
            self.assertEqual(kwargs["env"]["HOME"], str(self.home))
            self.assertEqual(kwargs["env"]["USER"], self.account.pw_name)

    def test_root_without_operator_is_rejected(self):
        root = types.SimpleNamespace(pw_uid=0, pw_dir="/root", pw_name="root")
        with mock.patch.object(github_ssh, "operator_account", return_value=root):
            with self.assertRaisesRegex(UserError, "普通用户"):
                github_ssh.GitHubSSH()

    def test_command_failure_and_timeout_are_not_success(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("ssh", 10)):
            self.child.side_effect = error
            with self.subTest(error=error), self.assertRaises(UserError):
                self.manager.test(self.key, 22)
        self.assertNotIn("身份验证成功", self.output.getvalue())

    def test_new_menu_item_does_not_require_proxy_config_or_renumber_existing_entries(self):
        with mock.patch.object(proxy, "choose", side_effect=["配置当前用户 GitHub SSH 密钥", Cancelled()]) as chooser, \
                mock.patch.object(proxy, "GitHubSSH") as wizard:
            with self.assertRaises(Cancelled):
                proxy.ProxyManager().menu()
        wizard.return_value.configure.assert_called_once_with()
        items = chooser.call_args_list[0].args[1]
        self.assertEqual(items[2:5], ["安装/更新终端代理开关函数", "清理旧 APT / Git 代理配置", "移除本用户代理配置"])
        self.assertEqual(items[5], "配置当前用户 GitHub SSH 密钥")


class GitHubSSHCommandTests(unittest.TestCase):
    def test_direct_command_dispatches_without_network_manager(self):
        with mock.patch.object(netpilot, "GitHubSSH") as wizard, \
                mock.patch.object(netpilot, "NM", side_effect=AssertionError("No NetworkManager")):
            self.assertEqual(netpilot.main(["github-ssh"]), 0)
        wizard.return_value.configure.assert_called_once_with()

    def test_dry_run_never_opens_key_wizard(self):
        with mock.patch.object(netpilot, "GitHubSSH") as wizard:
            with self.assertRaises(UserError):
                netpilot.main(["--dry-run", "github-ssh"])
        wizard.assert_not_called()

    def test_extra_arguments_never_open_key_wizard(self):
        with mock.patch.object(netpilot, "GitHubSSH") as wizard:
            with self.assertRaises(UserError):
                netpilot.main(["github-ssh", "unexpected"])
        wizard.assert_not_called()


@unittest.skipUnless(shutil.which("ssh") and shutil.which("ssh-keygen"), "OpenSSH client not installed")
class OfflineOpenSSHTests(unittest.TestCase):
    def test_encrypted_key_generation_and_reuse_prompt_without_password_arguments_or_echo(self):
        with tempfile.TemporaryDirectory() as temporary:
            account = types.SimpleNamespace(pw_dir=temporary, pw_uid=os.getuid(),
                                             pw_gid=os.getgid(), pw_name="fixture-user")
            # The child owns a temporary controlling terminal; all files stay in /tmp.
            pid, terminal = pty.fork()
            if pid == 0:
                try:
                    with mock.patch.object(github_ssh, "operator_account", return_value=account), \
                            mock.patch.object(common, "operator_account", return_value=account):
                        manager = github_ssh.GitHubSSH()
                        manager.prepare_directory()
                        key, public, _ = manager.generate("fixture-key", "fixture@example.com")
                        self.assertEqual(manager.public_key(key, verify_pair=True)[0], public)
                        print("ENCRYPTED_KEY_VERIFIED", flush=True)
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            output = b""
            prompts = 0
            finished = False
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([terminal], [], [], 0.2)
                    if ready:
                        try:
                            chunk = os.read(terminal, 8192)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output += chunk
                        count = (output.count(b"Enter passphrase (empty for no passphrase):")
                                 + output.count(b"Enter same passphrase again:")
                                 + output.count(b"Enter passphrase:")
                                 + output.count(b"Enter passphrase for "))
                        while prompts < count:
                            os.write(terminal, b"fixture-passphrase-only-in-tty\n")
                            prompts += 1
                    if b"ENCRYPTED_KEY_VERIFIED" in output:
                        finished = True
                        break
            finally:
                if not finished:
                    os.killpg(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
                os.close(terminal)
            self.assertTrue(finished, output.decode("utf-8", errors="replace"))
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertEqual(prompts, 3)
            self.assertNotIn(b"fixture-passphrase-only-in-tty", output)

    def test_real_key_pair_and_config_without_network_or_real_home_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            account = types.SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(),
                                             pw_gid=os.getgid(), pw_name="fixture-user")
            with mock.patch.object(github_ssh, "operator_account", return_value=account), \
                    mock.patch.object(common, "operator_account", return_value=account), \
                    contextlib.redirect_stdout(io.StringIO()):
                manager = github_ssh.GitHubSSH()
                manager.prepare_directory()
                private = manager.directory / "fixture-key"
                # Empty test-only passphrase; no credential is passed in argv.
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                               check=True, capture_output=True, timeout=10)
                public, fingerprint = manager.public_key(private, verify_pair=True)
                self.assertTrue(public.startswith("ssh-ed25519 "))
                self.assertIn("SHA256:", fingerprint)
                original = "ServerAliveInterval 30\nHost unrelated.example\n  User original-user\n"
                (manager.directory / "config").write_text(original)
                manager.install_config(private, 443)
                for host, hostname, port, user in (("github.com", "ssh.github.com", "443", "git"),
                                                   ("unrelated.example", "unrelated.example", "22", "original-user")):
                    result = subprocess.run(["ssh", "-G", "-F", str(manager.directory / "config"), host],
                                            capture_output=True, text=True, check=True, timeout=10)
                    settings = dict(line.split(" ", 1) for line in result.stdout.splitlines())
                    self.assertEqual(settings["hostname"], hostname)
                    self.assertEqual(settings["port"], port)
                    self.assertEqual(settings["user"], user)
                    self.assertEqual(settings["serveraliveinterval"], "30")


if __name__ == "__main__":
    unittest.main()
