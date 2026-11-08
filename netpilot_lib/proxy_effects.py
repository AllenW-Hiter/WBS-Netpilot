"""Owned, reversible APT and user-global Git proxy changes for proxy profiles."""
from __future__ import annotations
import json
import os
import pathlib
import re
import shutil

from . import proxy as legacy
from .common import (STATE, UserError, as_operator, atomic_write, file_lock,
                     load_json, require_root, run, safe_directory, save_json)


def apt_rules():
    result = run(["apt-config", "dump"], sensitive=True)
    rules = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'([^\s]+)\s+"(.*)";', line)
        if match and re.search(r"::proxy", match[1], re.I):
            rules[match[1].lower()] = match[2]
    return rules


def regular_file(path, uid, private=False):
    if os.path.lexists(path):
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_uid != uid:
            raise UserError("拒绝操作非当前归属的普通文件：" + str(path))
        if private and info.st_mode & 0o077:
            raise UserError("代理状态/配置需要私有权限（chmod 600）：" + str(path))


class APTEffect:
    def __init__(self, account):
        self.account = account
        self.owner_file = STATE / "proxy-profile-owner.json"

    def owner(self):
        regular_file(self.owner_file, os.geteuid(), private=True)
        return load_json(self.owner_file)

    @staticmethod
    def restore_file(data):
        if data["before"] is None:
            legacy.APT_FILE.unlink(missing_ok=True)
        else:
            atomic_write(legacy.APT_FILE, data["before"], data["mode"])

    def enable(self, source, config):
        require_root()
        safe_directory(STATE, root_only=True)
        expected = legacy.apt_text(config)
        with file_lock(STATE / "proxy-profile.lock"):
            regular_file(legacy.APT_FILE, os.geteuid())
            owner = self.owner()
            if owner:
                if owner["uid"] != self.account.pw_uid or owner["source"] != source or owner["expected"] != expected:
                    raise UserError("系统 APT 已由另一用户/代理来源接管；请先由原来源关闭。")
                if owner["status"] != "active" or not legacy.APT_FILE.exists() or legacy.APT_FILE.read_text() != expected:
                    raise UserError("APT 归属记录与文件不一致；请先关闭或检查恢复记录。")
                legacy.ProxyManager.check_apt_proxy(legacy.validate_proxy_config(config))
                return
            before = legacy.APT_FILE.read_text() if legacy.APT_FILE.exists() else None
            if before is not None and not before.startswith(legacy.APT_MARKER):
                raise UserError("APT 目标文件不是 NetPilot 创建的，拒绝覆盖。")
            owner = dict(schema=1, uid=self.account.pw_uid, source=source, status="applying", expected=expected,
                         before=before, mode=legacy.APT_FILE.stat().st_mode & 0o777 if before is not None else 0o600,
                         rules_before=apt_rules())
            save_json(self.owner_file, owner)
            try:
                atomic_write(legacy.APT_FILE, expected)
                legacy.ProxyManager.check_apt_proxy(legacy.validate_proxy_config(config))
                owner["status"] = "active"
                save_json(self.owner_file, owner)
            except BaseException:
                try:
                    self.restore_file(owner)
                    if apt_rules() != owner["rules_before"]:
                        raise UserError("APT 原规则未完整恢复。")
                    self.owner_file.unlink()
                except (OSError, UserError):
                    raise UserError("APT 启用失败且恢复未完成；请检查 root 私有归属记录：" + str(self.owner_file))
                raise

    def disable(self, source):
        require_root()
        safe_directory(STATE, root_only=True)
        with file_lock(STATE / "proxy-profile.lock"):
            owner = self.owner()
            if not owner:
                return
            if owner["uid"] != self.account.pw_uid or owner["source"] != source:
                raise UserError("APT 当前归属不是本用户的该代理来源，拒绝撤销。")
            regular_file(legacy.APT_FILE, os.geteuid())
            actual = legacy.APT_FILE.read_text() if legacy.APT_FILE.exists() else None
            if actual not in (owner["expected"], owner["before"]):
                raise UserError("APT 文件被外部修改；未覆盖外部内容，原内容保留在私有归属记录。")
            self.restore_file(owner)
            if apt_rules() != owner["rules_before"]:
                raise UserError("APT 文件已恢复，但生效规则发生变化；保留归属记录供重试/检查。")
            self.owner_file.unlink()


GIT_BEGIN = "# >>> NetPilot managed proxy >>>"
GIT_END = "# <<< NetPilot managed proxy <<<"
GIT_MARKER = "# Managed by NetPilot proxy profile\n"


class GitEffect:
    def __init__(self, account, directory):
        self.account = account
        self.directory = directory
        self.global_file = pathlib.Path(account.pw_dir) / ".gitconfig"
        self.proxy_file = directory / "git-proxy-active.config"

    def env(self, allow_override=False):
        override = os.environ.get("GIT_CONFIG_GLOBAL")
        if not allow_override and override and pathlib.Path(override).absolute() != self.global_file.absolute():
            raise UserError("当前 GIT_CONFIG_GLOBAL 指向其他文件；未修改，请先确认 Git 配置范围。")
        env = {"HOME": self.account.pw_dir, "XDG_CONFIG_HOME": str(pathlib.Path(self.account.pw_dir) / ".config")}
        if not allow_override:
            env["GIT_CONFIG_COUNT"] = "0"
        return env

    def rules(self):
        result = run(["git", "config", "--global", "--includes", "--null", "--show-origin", "--name-only",
                      "--get-regexp", legacy.GIT_PROXY_PATTERN], check=False, env=self.env(), sensitive=True)
        if result.returncode not in (0, 1):
            raise UserError("读取 Git 全局代理规则失败。")
        return result.stdout

    def prepare(self, config):
        if not shutil.which("git"):
            raise UserError("选择了 Git 配置，但未安装 git。")
        with as_operator():
            regular_file(self.global_file, self.account.pw_uid)
            regular_file(self.proxy_file, self.account.pw_uid, private=True)
            if self.rules():
                raise UserError("已有 Git 全局/include 代理规则；请先检查或显式备份清理，未自动覆盖。")
            text = self.global_file.read_text() if self.global_file.exists() else ""
            if GIT_BEGIN in text or GIT_END in text or self.proxy_file.exists():
                raise UserError("已有 Git 代理标记或遗留文件；请先恢复原代理会话。")
            block = "\n" + GIT_BEGIN + "\n[include]\n\tpath = " + json.dumps(str(self.proxy_file)) + "\n" + GIT_END + "\n"
            content = GIT_MARKER + "[http]\n\tproxy = " + json.dumps(legacy.validate_proxy_config(config)) + "\n"
            return dict(before=text if self.global_file.exists() else None,
                        mode=self.global_file.stat().st_mode & 0o777 if self.global_file.exists() else 0o644,
                        block=block, content=content)

    def enable(self, data):
        with as_operator():
            regular_file(self.global_file, self.account.pw_uid)
            regular_file(self.proxy_file, self.account.pw_uid, private=True)
            text = self.global_file.read_text() if self.global_file.exists() else ""
            if text != (data["before"] or ""):
                raise UserError("Git 配置在准备后发生变化，停止写入。")
            atomic_write(self.proxy_file, data["content"])
            atomic_write(self.global_file, text + data["block"], data["mode"])
            result = run(["git", "config", "--global", "--includes", "--get-all", "http.proxy"], check=False,
                         env=self.env(), sensitive=True)
            expected = json.loads(data["content"].split("proxy = ", 1)[1].strip())
            if result.returncode or result.stdout.splitlines() != [expected]:
                raise UserError("Git 未确认生效的全局代理规则。")

    def disable(self, data):
        with as_operator():
            regular_file(self.global_file, self.account.pw_uid)
            regular_file(self.proxy_file, self.account.pw_uid, private=True)
            text = self.global_file.read_text() if self.global_file.exists() else ""
            if text.count(data["block"]) > 1:
                raise UserError("Git 代理标记重复，未猜测恢复范围。")
            if data["block"] not in text:
                if text != (data["before"] or ""):
                    raise UserError("Git 代理标记被外部修改；原配置保留在会话备份。")
            else:
                text = text.replace(data["block"], "", 1)
            if self.proxy_file.exists() and self.proxy_file.read_text() != data["content"]:
                raise UserError("Git 代理私有文件被外部修改，拒绝覆盖。")
            if data["before"] is None and not text:
                self.global_file.unlink(missing_ok=True)
            else:
                atomic_write(self.global_file, text, data["mode"])
            self.proxy_file.unlink(missing_ok=True)
