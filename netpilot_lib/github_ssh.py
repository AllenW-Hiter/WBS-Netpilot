"""Interactive, operator-owned GitHub SSH keys; never upload private keys."""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid

from .common import (Cancelled, UserError, as_operator, ask, atomic_write, choose,
                     clean_text, confirm, operator_account, timestamp)

BEGIN = "# >>> NetPilot GitHub SSH >>>"
END = "# <<< NetPilot GitHub SSH <<<"
KEY_PAGE = "https://github.com/settings/ssh/new"
FINGERPRINT_PAGE = "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints"


def key_filename(value):
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value)
            or value.endswith(".pub")
            or value in {"config", "authorized_keys", "known_hosts", "known_hosts.old", "environment"}):
        raise UserError("密钥文件名只接受字母、数字、下划线、点和连字符；不能使用路径、公钥或 SSH 配置文件名。")
    return value


def without_managed_block(text):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if not starts and not ends:
        return text
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise UserError("~/.ssh/config 中的 NetPilot 标记不完整或重复；请人工整理，未修改文件。")
    return "".join(lines[:starts[0]] + lines[ends[0] + 1:])


class GitHubSSH:
    def __init__(self):
        self.account = operator_account()
        if self.account.pw_uid == 0:
            raise UserError("请以普通用户运行 GitHub SSH 向导；sudo 启动时会使用原调用用户。")
        self.directory = pathlib.Path(self.account.pw_dir) / ".ssh"

    def prepare_directory(self):
        with as_operator():
            if self.directory.is_symlink():
                raise UserError("拒绝使用符号链接 ~/.ssh。")
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.account.pw_uid:
                raise UserError("~/.ssh 必须是当前用户所有的目录。")
            self.directory.chmod(0o700)

    def check_file(self, path, private=False):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != self.account.pw_uid:
            raise UserError("文件必须是当前用户所有的普通文件，不能是符号链接：" + str(path))
        if private and info.st_mode & 0o077:
            raise UserError("私钥应仅本用户可读；请先 chmod 600 " + str(path))
        if info.st_size > 1024 * 1024:
            raise UserError("SSH 文件过大，拒绝读取：" + str(path))

    def invoke(self, args, capture=False, timeout=None):
        # Dropping effective uid alone leaves a root real uid in exec children.
        # Permanently drop all child privileges before OpenSSH reads user files.
        def drop():
            os.initgroups(self.account.pw_name, self.account.pw_gid)
            os.setgid(self.account.pw_gid)
            os.setuid(self.account.pw_uid)

        env = dict(os.environ)
        env.update(HOME=self.account.pw_dir, USER=self.account.pw_name,
                   LOGNAME=self.account.pw_name, LC_ALL="C", LANG="C")
        for key in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY"):
            env.pop(key, None)
        try:
            return subprocess.run(list(args), env=env, check=False, text=True,
                                  stdout=subprocess.PIPE if capture else None,
                                  stderr=subprocess.PIPE if capture else None,
                                  preexec_fn=drop if os.geteuid() == 0 else None,
                                  timeout=timeout)
        except KeyboardInterrupt:
            raise Cancelled("操作已取消；请检查已完成的本地步骤。")
        except subprocess.TimeoutExpired:
            raise UserError("SSH 操作超时；本地文件仍保留，未把超时当成成功。")
        except OSError:
            raise UserError("无法执行 OpenSSH 工具，请检查 openssh-client 和当前用户权限。")

    def public_key(self, private, verify_pair=False):
        public = pathlib.Path(str(private) + ".pub")
        with as_operator():
            self.check_file(private, private=True)
            self.check_file(public)
            try:
                text = public.read_text(encoding="utf-8").strip()
            except UnicodeError:
                raise UserError("公钥不是有效的 UTF-8 文本。")
        fields = text.split()
        if (len(text) > 16384 or "\n" in text or "\r" in text
                or clean_text(text) != text or len(fields) < 2
                or fields[0] not in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                                     "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")):
            raise UserError("公钥格式无效或类型不受支持。")
        fingerprint = self.invoke(["ssh-keygen", "-l", "-E", "sha256", "-f", str(public)],
                                  capture=True, timeout=15)
        if fingerprint.returncode:
            raise UserError("OpenSSH 无法读取该公钥；未写入 SSH 配置。")
        if verify_pair:
            print("验证已有私钥和公钥是否配对；如有口令，请在 OpenSSH 提示中输入。")
            derived = self.invoke(["ssh-keygen", "-y", "-f", str(private)], capture=True)
            if derived.returncode or derived.stdout.split()[:2] != fields[:2]:
                raise UserError("私钥无法读取或与 .pub 不匹配；未写入 SSH 配置。")
        return text, clean_text(fingerprint.stdout.strip())

    def generate(self, name, comment):
        name = key_filename(name)
        if not comment or len(comment) > 200 or clean_text(comment) != comment:
            raise UserError("邮箱/密钥备注应为 1～200 个可打印字符。")
        private = self.directory / name
        public = self.directory / (name + ".pub")
        with as_operator():
            if os.path.lexists(private) or os.path.lexists(public):
                raise UserError("密钥或公钥文件已存在；请选择其他文件名或使用已有密钥。")
            temporary = pathlib.Path(tempfile.mkdtemp(prefix=".netpilot-key-", dir=self.directory))
        try:
            print("由 ssh-keygen 提示输入密钥口令并确认；直接回车可不设置口令。")
            result = self.invoke(["ssh-keygen", "-t", "ed25519", "-C", comment,
                                  "-f", str(temporary / "key")])
            if result.returncode:
                raise UserError("密钥生成失败；未安装密钥或修改 SSH 配置。")
            key, fingerprint = self.public_key(temporary / "key")
            with as_operator():
                # link() is exclusive: a concurrent file creation cannot be overwritten.
                os.link(temporary / "key", private)
                try:
                    os.link(temporary / "key.pub", public)
                except OSError:
                    if private.lstat().st_ino == (temporary / "key").lstat().st_ino:
                        private.unlink()
                    raise
                private.chmod(0o600)
                public.chmod(0o600)
            return private, key, fingerprint
        except OSError:
            raise UserError("安装密钥失败；没有覆盖原文件，请检查 ~/.ssh 中的同名文件。")
        finally:
            with as_operator():
                shutil.rmtree(temporary)

    def install_config(self, private, port):
        if port not in (22, 443) or private.parent != self.directory:
            raise UserError("GitHub SSH 只支持 22/443 端口和本用户 ~/.ssh 中的密钥。")
        name = key_filename(private.name)
        hostname = "github.com" if port == 22 else "ssh.github.com"
        target = self.directory / "config"
        block = (BEGIN + "\nHost github.com\n  HostName " + hostname +
                 "\n  Port " + str(port) + "\n  User git\n  IdentityFile ~/.ssh/" + name +
                 "\n  IdentitiesOnly yes\nHost *\n" + END + "\n")
        with as_operator():
            self.check_file(private, private=True)
            original = ""
            exists = os.path.lexists(target)
            if exists:
                self.check_file(target)
                original = target.read_text(encoding="utf-8")
            updated = block + without_managed_block(original)
            if original == updated:
                return
            if exists:
                backup = self.directory / ("config.before-netpilot-" + timestamp() + "-" + uuid.uuid4().hex[:8])
                atomic_write(backup, original)
                print("原 SSH 配置备份：" + str(backup))
            atomic_write(target, updated)
        print("已为 github.com 写入当前用户 SSH 配置：" + str(target))

    def test(self, private, port):
        with as_operator():
            known_hosts = self.directory / "known_hosts"
            if os.path.lexists(known_hosts):
                self.check_file(known_hosts)
        hostname = "github.com" if port == 22 else "ssh.github.com"
        print("首次连接请核对 GitHub 官方主机指纹后再接受：" + FINGERPRINT_PAGE)
        result = self.invoke(["ssh", "-F", "/dev/null", "-T", "-p", str(port),
                              "-i", str(private), "-o", "IdentitiesOnly=yes",
                              "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=ask",
                              "-o", "ConnectTimeout=10", "git@" + hostname], capture=True)
        raw_output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        output = "\n".join(clean_text(line) for line in raw_output.splitlines())
        if output:
            print(output[:2000])
        # GitHub deliberately returns 1 even when authentication succeeds.
        if result.returncode in (0, 1) and re.search(
                r"(?m)^Hi [A-Za-z0-9_.-]+! You've successfully authenticated, but GitHub does not\s+provide shell access\.$", output):
            print("GitHub SSH 身份验证成功。")
        else:
            raise UserError("GitHub SSH 验证未成功；本地密钥及配置已保留，请检查公钥、口令和网络。")

    def configure(self):
        for tool in ("ssh-keygen", "ssh"):
            if not shutil.which(tool):
                raise UserError("缺少 " + tool + "；请安装 openssh-client 后重试。")
        print("GitHub SSH 密钥所属用户：{}；目录：{}".format(self.account.pw_name, self.directory))
        mode = choose("GitHub SSH 密钥", ["生成新的 Ed25519 密钥", "使用已有密钥"])
        name = key_filename(ask("~/.ssh 中的私钥文件名", "id_ed25519_github"))
        if mode == "生成新的 Ed25519 密钥":
            comment = ask("GitHub 邮箱或密钥备注")
            if not confirm("为当前用户生成 ~/.ssh/" + name + "？"):
                return
            self.prepare_directory()
            private, key, fingerprint = self.generate(name, comment)
        else:
            self.prepare_directory()
            private = self.directory / name
            try:
                key, fingerprint = self.public_key(private, verify_pair=True)
            except FileNotFoundError:
                raise UserError("找不到该私钥及配套 .pub 文件；请检查文件名。")
        print("密钥指纹：" + fingerprint)
        port = choose("GitHub SSH 连接端口", [22, 443],
                      lambda value: "22（默认）" if value == 22 else "443（22 端口受限时使用 ssh.github.com）")
        print("将为 github.com 指定所选密钥和端口；已有 SSH 配置先备份。")
        if confirm("写入 ~/.ssh/config？"):
            self.install_config(private, port)
        print("\n将以下整行公钥复制到 " + KEY_PAGE + "，选择 Authentication Key 后保存：")
        print(key)
        print("私钥路径：" + str(private))
        print("需要缓存密钥口令时，在自己的终端运行：ssh-add " + shlex.quote(str(private)))
        print("HTTP 应用代理开关不代理 Git SSH；验证将直连所选 SSH 端口。")
        if confirm("已将公钥添加到 GitHub，立即验证 SSH？"):
            self.test(private, port)
        else:
            print("本地密钥准备完成；尚未验证 GitHub 账户授权和网络连接。")
            hostname = "github.com" if port == 22 else "ssh.github.com"
            print("验证命令：" + shlex.join(["ssh", "-F", "/dev/null", "-T", "-p", str(port),
                                               "-i", str(private), "-o", "IdentitiesOnly=yes", "git@" + hostname]))
