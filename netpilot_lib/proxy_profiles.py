"""Wire/SSH proxy profiles, reversible optional effects, and read-only detection."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import urllib.parse
import uuid

from . import proxy as legacy
from .common import (PROXY_KEYS, Cancelled, UserError, as_operator, ask, atomic_write, choose,
                     confirm, https_test, load_json, operator_account, require_root, safe_directory,
                     save_json, timestamp, run)
from .proxy_effects import APTEffect, GitEffect, apt_rules, regular_file
from .proxy_shell import BASH_BEGIN, BASH_END, profile_script
from .ssh_tunnel import SSHTunnel

SOURCES = ("wire", "ssh")
HELPER = ["/usr/bin/python3", str(pathlib.Path(__file__).resolve().parents[1] / "netpilot.py")]


def source_name(source):
    if source not in SOURCES:
        raise UserError("代理来源必须为 wire 或 ssh。")
    return source


def validate_profile(data):
    if not isinstance(data, dict) or set(data) != {"schema", "source", "proxy", "apt", "git"}:
        raise UserError("代理来源配置结构无效。")
    if type(data["schema"]) is not int or data["schema"] != 2 or type(data["apt"]) is not bool or type(data["git"]) is not bool:
        raise UserError("代理配置版本或 APT/Git 选项无效。")
    source_name(data["source"])
    proxy = data["proxy"]
    if not isinstance(proxy, dict) or any(not isinstance(proxy.get(key, ""), str)
                                         for key in ("scheme", "host", "username", "password", "no_proxy")):
        raise UserError("代理端点格式无效。")
    legacy.validate_proxy_config(proxy)
    if data["source"] == "ssh" and proxy["host"] not in ("127.0.0.1", "localhost", "::1"):
        raise UserError("SSH 代理来源必须连接当前机器的回环监听端口。")
    return dict(data)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def masked_endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if not parsed.hostname or not parsed.scheme or parsed.port is None:
            return {"state": "set", "endpoint": "无法解析（值已隐藏）"}
        return {"state": "set", "scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port,
                "authentication": parsed.username is not None}
    except (ValueError, TypeError):
        return {"state": "set", "endpoint": "无法解析（值已隐藏）"}


def remove_block(text, begin, end):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end]
    if not starts and not ends and begin not in text and end not in text:
        return text
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise UserError("Bash 配置中的 NetPilot 标记不完整或重复，未修改。")
    return "".join(lines[:starts[0]] + lines[ends[0] + 1:])


class ProfileManager:
    def __init__(self):
        self.account = operator_account()
        self.directory = pathlib.Path(self.account.pw_dir) / ".config" / "netpilot"
        self.session_file = self.directory / "proxy-session.json"
        self.apt_effect = APTEffect(self.account)
        self.git_effect = GitEffect(self.account, self.directory)

    def path(self, source):
        return self.directory / (source_name(source) + "-proxy.json")

    def read_private(self, path, default=None):
        with as_operator():
            regular_file(path, self.account.pw_uid, private=True)
            return load_json(path, default)

    def load(self, source):
        source_name(source)
        data = self.read_private(self.path(source))
        if data is None:
            # Read-only compatibility: migration is saved only after explicit configuration.
            old = self.read_private(self.directory / ("proxy.json" if source == "wire" else "codex-proxy.json"))
            if old:
                if source == "ssh":
                    old = dict(schema=1, scheme=old["scheme"], host="127.0.0.1", port=old["remote_port"],
                               username="", password="", no_proxy="localhost,127.0.0.1,::1")
                data = dict(schema=2, source=source, proxy=old, apt=False, git=False)
        if data is None:
            raise UserError("该代理来源尚未配置：" + source)
        data = validate_profile(data)
        if data["source"] != source:
            raise UserError("代理配置的来源与文件不一致。")
        return data

    @contextlib.contextmanager
    def lock(self):
        with as_operator():
            safe_directory(self.directory)
            path = self.directory / "proxy-session.lock"
            regular_file(path, self.account.pw_uid, private=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save_session(self, data):
        with as_operator():
            save_json(self.session_file, data)

    def session(self):
        data = self.read_private(self.session_file)
        if data and (data.get("schema") != 1 or data.get("source") not in SOURCES or
                     data.get("status") not in ("applying", "active", "partial")):
            raise UserError("代理会话记录损坏，未猜测恢复范围。")
        return data

    def needs_root(self, action, source):
        source_name(source)
        if action == "on":
            return self.load(source)["apt"]
        data = self.session()
        return bool(data and data["source"] == source and data.get("apt_pending"))

    def enable(self, source):
        profile = self.load(source)
        if profile["apt"]:
            require_root()
        with self.lock():
            old = self.session()
            if old and old["source"] == source:
                if old["status"] != "active" or old["fingerprint"] != fingerprint(profile):
                    raise UserError("配置已改变或上次切换未完成；请先运行该来源的 _proxy_off。")
                if old.get("apt_pending"):
                    self.apt_effect.enable(source, profile["proxy"])
                if old.get("git_pending"):
                    with as_operator():
                        if (not self.git_effect.proxy_file.exists() or self.git_effect.proxy_file.read_text() != old["git_data"]["content"] or
                                old["git_data"]["block"] not in self.git_effect.global_file.read_text()):
                            raise UserError("Git 代理文件或标记已改变；请先检查或关闭。")
                return
            if not profile["apt"] and not profile["git"]:
                return
            if old:
                raise UserError("已有另一来源的 APT/Git 会话，请先关闭原来源。")
            data = dict(schema=1, source=source, status="applying", fingerprint=fingerprint(profile),
                        apt_pending=profile["apt"], git_pending=profile["git"],
                        git_data=self.git_effect.prepare(profile["proxy"]) if profile["git"] else None)
            self.save_session(data)  # Recovery intent exists before any persistent change.
            try:
                if profile["git"]:
                    self.git_effect.enable(data["git_data"])
                if profile["apt"]:
                    self.apt_effect.enable(source, profile["proxy"])
                data["status"] = "active"
                self.save_session(data)
            except BaseException:
                self._restore(data)
                raise

    def _restore(self, data):
        failures = []
        for key, restore in (("apt_pending", lambda: self.apt_effect.disable(data["source"])),
                             ("git_pending", lambda: self.git_effect.disable(data["git_data"]))):
            if not data.get(key):
                continue
            try:
                restore()
                data[key] = False
                self.save_session(data)
            except (UserError, OSError) as exc:
                failures.append(type(exc).__name__ + ": " + str(exc))
        if failures:
            data["status"] = "partial"
            self.save_session(data)
            raise UserError("代理未全部恢复；请重试原来源的 _proxy_off 或检查私有会话备份：" + str(self.session_file))
        with as_operator():
            self.session_file.unlink(missing_ok=True)

    def disable(self, source):
        source_name(source)
        with self.lock():
            data = self.session()
            if data and data["source"] == source:
                self._restore(data)

    def bash_loader(self, sources):
        lines = [BASH_BEGIN, 'if [[ -n "${_NETPILOT_CODEX_WRAPPER:-}" || "${_NETPILOT_PROXY_ACTIVE:-0}" == 1 ]]; then',
                 "  printf '请先关闭旧版代理或使用新的 Bash 终端，再加载新版开关。\\n' >&2", "else"]
        for source in sources:
            path = shlex.quote(str(self.directory / (source + "-proxy.sh")))
            lines += [source + "_proxy_on() {", "  . " + path + " || return", "  _netpilot_" + source + "_on", "}",
                      source + "_proxy_off() {", "  if ! declare -F _netpilot_" + source + "_off >/dev/null; then",
                      "    . " + path + " || return", "  fi", "  _netpilot_" + source + "_off", "}"]
        if "wire" in sources:
            lines += ["netpilot_proxy_on() { wire_proxy_on; }", "netpilot_proxy_off() { wire_proxy_off; }"]
        return "\n".join(lines + ["fi", BASH_END, ""])

    def install(self, profile):
        profile = validate_profile(profile)
        source = profile["source"]
        bashrc = pathlib.Path(self.account.pw_dir) / ".bashrc"
        script = self.directory / (source + "-proxy.sh")
        with self.lock(), as_operator():
            regular_file(bashrc, self.account.pw_uid)
            regular_file(self.path(source), self.account.pw_uid, private=True)
            regular_file(script, self.account.pw_uid, private=True)
            if script.exists() and not script.read_text().startswith("# Generated by NetPilot proxy profiles;"):
                raise UserError("同名代理脚本不是本工具创建的，拒绝覆盖。")
            text = bashrc.read_text() if bashrc.exists() else ""
            for begin, end in ((BASH_BEGIN, BASH_END), (legacy.BASH_BEGIN, legacy.BASH_END),
                               ("# >>> NetPilot Codex proxy >>>", "# <<< NetPilot Codex proxy <<<")):
                text = remove_block(text, begin, end)
            sources = [s for s in SOURCES if s == source or self.path(s).exists()]
            # Existing legacy wire configuration remains accessible through compatibility aliases.
            if source == "ssh" and "wire" not in sources and (self.directory / "proxy.json").exists():
                raise UserError("请先通过 wire-proxy 配置迁移已有网线代理，再安装 SSH 来源。")
            content = text.rstrip("\n") + "\n\n" + self.bash_loader(sources)
            changes = [(self.path(source), json.dumps(profile, ensure_ascii=False, indent=2) + "\n", 0o600),
                       (script, profile_script(source, profile["proxy"], HELPER), 0o600),
                       (bashrc, content, bashrc.stat().st_mode & 0o777 if bashrc.exists() else 0o644)]
            backup = self.directory / ("profiles-backup-" + timestamp() + "-" + uuid.uuid4().hex[:8])
            safe_directory(backup)
            before = {}
            for path, _, _ in changes:
                before[path] = (path.read_text(), path.stat().st_mode & 0o777) if path.exists() else None
                if before[path] is not None:
                    atomic_write(backup / path.name, before[path][0])
            written = []
            try:
                for path, value, mode in changes:
                    written.append(path)
                    atomic_write(path, value, mode)
            except BaseException:
                failures = []
                for path in reversed(written):
                    try:
                        if before[path] is None:
                            regular_file(path, self.account.pw_uid)
                            path.unlink(missing_ok=True)
                        else:
                            atomic_write(path, *before[path])
                    except (OSError, UserError):
                        failures.append(path)
                if failures:
                    raise UserError("代理开关安装失败且部分文件未恢复；私有备份：" + str(backup))
                raise
        print("已保存 {} 来源并安装 {}_proxy_on/off；尚未开启代理。备份：{}".format(source, source, backup))
        print("在 Bash 执行 source ~/.bashrc 后手动开启；新终端不自动设置代理。")

    def configure(self, source):
        source_name(source)
        print("配置{}：终端代理手动开启，APT 与 Git 全局代理分别选择。".format("网线主机代理" if source == "wire" else "SSH 回环代理"))
        scheme = choose("代理协议（与实际服务一致）", ["http", "socks5h", "https"])
        host = ask("代理服务器地址", "192.168.1.20") if source == "wire" else "127.0.0.1"
        port = ask("代理服务端口", "10808" if source == "wire" else "17890")
        if not port.isascii() or not port.isdigit():
            raise UserError("代理端口必须为整数。")
        username = ask("代理认证用户名；无需认证则回车")
        password = ask("代理认证密码", secret=True) if username else ""
        config = dict(schema=1, scheme=scheme, host=host, port=int(port), username=username, password=password,
                      no_proxy=ask("绕过代理的主机/IP", "localhost,127.0.0.1,::1," + host))
        legacy.validate_proxy_config(config)
        profile = validate_profile(dict(schema=2, source=source, proxy=config,
            apt=confirm("开启该来源时，是否联动系统 APT 代理（需要 sudo）？"),
            git=confirm("开启该来源时，是否联动当前用户的 Git 全局 HTTPS 代理？")))
        print("端点：{}://{}:{}；APT={}，Git={}".format(scheme, host, port, profile["apt"], profile["git"]))
        if confirm("备份后保存配置并安装手动开关？保存时不修改 APT/Git。"):
            self.install(profile)

    def test(self, source):
        profile = self.load(source)
        url = ask("通过所选代理测试的 HTTPS 网址", "https://example.com")
        result = https_test(url, legacy.validate_proxy_config(profile["proxy"]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["transport_ok"]:
            raise UserError("代理 HTTPS 传输失败，请检查监听、协议和出口。")
        print("HTTPS 传输完成；HTTP 状态、账号授权与具体应用请求仍需分别验证。")

    def status(self):
        report = {"scope": "检测本进程继承的终端变量及可读的 APT/Git 配置；配置存在不等于正在使用。",
                  "terminal": {}, "profiles": {}}
        for key in PROXY_KEYS:
            if key.lower() == "no_proxy":
                report["terminal"][key] = {"state": "set" if key in os.environ else "unset"}
            else:
                report["terminal"][key] = masked_endpoint(os.environ[key]) if key in os.environ else {"state": "unset"}
        matching = {}
        for source in SOURCES:
            try:
                data = self.load(source)
                url = legacy.validate_proxy_config(data["proxy"])
                report["profiles"][source] = dict(masked_endpoint(url), state="configured", apt=data["apt"], git=data["git"],
                                                  legacy=not self.path(source).exists())
                matching[source] = [key for key in PROXY_KEYS if key.lower() != "no_proxy" and os.environ.get(key) == url]
            except UserError:
                report["profiles"][source] = {"state": "missing-or-invalid"}
        values = {os.environ[key] for key in PROXY_KEYS if key.lower() != "no_proxy" and os.environ.get(key)}
        report["terminal_source_matches"] = {source: keys for source, keys in matching.items() if keys}
        report["terminal_variables_consistent"] = len(values) <= 1
        if legacy.APT_FILE.exists() and not os.access(legacy.APT_FILE, os.R_OK):
            report["apt"] = {"state": "permission-required", "hint": "sudo ./network_setup.sh proxy-status 可读取系统私有规则"}
        elif shutil.which("apt-config"):
            try:
                report["apt"] = {key: masked_endpoint(value) for key, value in apt_rules().items()}
            except UserError:
                report["apt"] = {"state": "unreadable"}
        else:
            report["apt"] = {"state": "tool-missing"}
        if shutil.which("git"):
            with as_operator():
                result = run(["git", "config", "--null", "--show-origin", "--get-regexp", legacy.GIT_PROXY_PATTERN],
                             check=False, sensitive=True, env=self.git_effect.env(allow_override=True))
            parts = result.stdout.rstrip("\0").split("\0") if result.stdout else []
            entries = []
            if result.returncode == 0 and len(parts) % 2 == 0:
                for origin, item in zip(parts[::2], parts[1::2]):
                    key, _, value = item.partition("\n")
                    kind = "http.proxy" if key == "http.proxy" else "scoped-or-command-proxy"
                    entries.append(dict(origin="".join(c for c in origin if c.isprintable()), kind=kind, value=masked_endpoint(value)))
            report["git"] = {"state": "rules-present" if result.returncode == 0 else "no-rules" if result.returncode == 1 else "unreadable",
                             "rule_count": len(entries), "rules": entries,
                             "scope": "含当前目录仓库/系统/全局规则；端点已脱敏，Git SSH 不受 HTTP 代理控制"}
        else:
            report["git"] = {"state": "tool-missing"}
        data = self.session()
        report["managed_session"] = {key: data[key] for key in ("source", "status", "apt_pending", "git_pending")} if data else None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def run_command(self, source, args):
        args = args[1:] if args and args[0] == "--" else args
        if not args:
            raise UserError("请在代理运行命令后提供程序参数列表。")
        env = dict(os.environ)
        env.update(legacy.proxy_environment(self.load(source)["proxy"]))
        env.update(HOME=self.account.pw_dir, USER=self.account.pw_name, LOGNAME=self.account.pw_name)
        def drop():
            os.initgroups(self.account.pw_name, self.account.pw_gid)
            os.setgid(self.account.pw_gid)
            os.setuid(self.account.pw_uid)
        try:
            return subprocess.call(list(args), env=env,
                preexec_fn=drop if os.geteuid() == 0 and self.account.pw_uid != 0 else None)
        except OSError:
            raise UserError("无法执行指定程序。")

    def remove(self, source):
        source_name(source)
        print("请先在启用的 Bash 终端执行对应的 _proxy_off；移除不会改变已有进程。")
        if not confirm("恢复该来源登记的 APT/Git 状态，再备份并移除该来源配置与加载入口？"):
            return
        self.disable(source)
        with self.lock(), as_operator():
            paths = [self.path(source), self.directory / (source + "-proxy.sh")]
            bashrc = pathlib.Path(self.account.pw_dir) / ".bashrc"
            for path in paths: regular_file(path, self.account.pw_uid, private=True)
            regular_file(bashrc, self.account.pw_uid)
            text = bashrc.read_text() if bashrc.exists() else ""
            text = remove_block(text, BASH_BEGIN, BASH_END)
            remaining = [s for s in SOURCES if s != source and self.path(s).exists()]
            if remaining: text = text.rstrip("\n") + "\n\n" + self.bash_loader(remaining)
            backup = self.directory / ("profiles-backup-" + timestamp() + "-" + uuid.uuid4().hex[:8])
            safe_directory(backup)
            for path in [bashrc, *paths]:
                if path.exists(): atomic_write(backup / path.name, path.read_text())
            # Remove loader before its files; failures are explicit and backup stays available.
            if bashrc.exists(): atomic_write(bashrc, text, bashrc.stat().st_mode & 0o777)
            try:
                for path in paths: path.unlink(missing_ok=True)
            except OSError:
                raise UserError("代理来源移除未全部完成；备份：" + str(backup))
        print("已移除 {} 来源；历史备份与其他来源保留：{}".format(source, backup))

    def menu(self):
        while True:
            try:
                action = choose("网络代理", ["检测当前代理情况", "配置网线主机代理（wire）", "配置 SSH 回环代理（ssh）",
                                "配置 SSH 隧道方向与端口", "启动前台 SSH 隧道", "测试代理 HTTPS 路径", "重装某来源的开关",
                                "移除某个代理来源", "显式清理旧版 APT/Git 代理"])
            except Cancelled:
                return
            if action == "检测当前代理情况": self.status()
            elif action == "配置网线主机代理（wire）": self.configure("wire")
            elif action == "配置 SSH 回环代理（ssh）": self.configure("ssh")
            elif action == "配置 SSH 隧道方向与端口": SSHTunnel().configure()
            elif action == "启动前台 SSH 隧道": SSHTunnel().start()
            elif action == "测试代理 HTTPS 路径": self.test(choose("选择来源", list(SOURCES)))
            elif action == "重装某来源的开关": self.install(self.load(choose("选择来源", list(SOURCES))))
            elif action == "移除某个代理来源":
                from .cli import main
                if main(["proxy-remove", choose("选择来源", list(SOURCES))]): raise UserError("代理移除未完成。")
            else:
                from .cli import main
                code = main(["proxy-cleanup"])
                if code: raise UserError("旧代理清理未完成。")
