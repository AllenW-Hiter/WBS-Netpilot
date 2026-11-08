"""AutoDL setup orchestration with explicit ownership of one SSH connection."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

from . import autodl_remote
from .common import (Cancelled, UserError, ask, choose, clean_text, confirm, file_lock,
                     load_json, operator_account, safe_directory, save_json)
from .proxy_effects import regular_file
from .ssh_auth import PasswordBridge
from .ssh_tunnel import port_number, validate_tunnel

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_KEYS = {"schema", "destination", "ssh_port", "local_port", "remote_port", "scheme", "apt", "git", "install_deps", "accept_new_host", "test_url"}


def parse_ssh_command(command):
    if not isinstance(command, str) or len(command) > 2048 or any(ord(c) < 32 for c in command):
        raise UserError("SSH 指令无效；请粘贴 ssh -p 端口 用户@主机。")
    try:
        tokens = shlex.split(command)
    except ValueError:
        raise UserError("SSH 指令的引号不完整。")
    if not tokens or tokens.pop(0) != "ssh":
        raise UserError("指令必须以 ssh 开头。")
    options, destination = {}, None
    while tokens:
        token = tokens.pop(0)
        flag = token[:2]
        if flag in ("-p", "-l"):
            value = token[2:] if len(token) > 2 else tokens.pop(0) if tokens else ""
            if not value or flag in options:
                raise UserError("SSH 端口/用户名参数缺失或重复。")
            options[flag] = value
        elif token.startswith("-") or destination is not None:
            raise UserError("只接受 ssh、-p、-l 和登录目标，不接受远程命令或其他 SSH 选项。")
        else:
            destination = token
    if not destination or ("-l" in options and "@" in destination):
        raise UserError("请提供唯一的 SSH 用户和主机。")
    if "-l" in options:
        destination = options["-l"] + "@" + destination
    if "@" not in destination:
        raise UserError("请使用 用户@主机，或通过 -l 指定用户。")
    value = options.get("-p", "22")
    if not value.isascii() or not value.isdigit():
        raise UserError("SSH 端口必须为整数。")
    data = validate_tunnel(dict(schema=2, mode="reverse", destination=destination, ssh_port=int(value),
                                listen_port=17890, target_port=10818, scheme="http"))
    return {"destination": data["destination"], "ssh_port": data["ssh_port"]}


def validate_plan(data):
    if not isinstance(data, dict) or set(data) != PLAN_KEYS or type(data["schema"]) is not int or data["schema"] != 1:
        raise UserError("AutoDL 配置结构无效。")
    for key in ("apt", "git", "install_deps", "accept_new_host"):
        if type(data[key]) is not bool:
            raise UserError("AutoDL 选项必须为布尔值。")
    if "@" not in str(data["destination"]):
        raise UserError("AutoDL 配置缺少 SSH 用户。")
    validate_tunnel(dict(schema=2, mode="reverse", destination=data["destination"], ssh_port=data["ssh_port"],
                         listen_port=data["remote_port"], target_port=data["local_port"], scheme=data["scheme"]))
    if data["ssh_port"] is None:
        raise UserError("AutoDL 配置需要明确的 SSH 端口。")
    url = data["test_url"]
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise UserError("测试地址无效。")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UserError("测试地址应为不含认证、查询参数或片段的 HTTPS URL。")
    return dict(data)


def bundle():
    files = [ROOT / "netpilot.py", ROOT / "network_setup.sh", *sorted((ROOT / "netpilot_lib").glob("*.py")), *sorted((ROOT / "examples").glob("*.json"))]
    stream, hashes = io.BytesIO(), {}
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise UserError("拒绝打包非普通运行文件。")
            name, content = path.relative_to(ROOT).as_posix(), path.read_bytes()
            hashes[name] = hashlib.sha256(content).hexdigest()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    content = stream.getvalue()
    return dict(archive=base64.b64encode(content).decode(), archive_sha256=hashlib.sha256(content).hexdigest(), files=hashes)


def password_input(from_stdin=False):
    if from_stdin:
        value = sys.stdin.readline(4098)
        if len(value) > 4097:
            raise UserError("SSH 密码过长。")
        return value.removesuffix("\n").removesuffix("\r")
    if not sys.stdin.isatty():
        raise UserError("隐藏密码输入需要终端；自动化调用可使用 --password-stdin。")
    return getpass.getpass("SSH 登录密码（不保存）：")


def execute(args, *, input_text=None, env=None, timeout=45, background=False):
    process = subprocess.Popen(list(args), stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if background else subprocess.PIPE,
        stderr=subprocess.DEVNULL if background else subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout or "", stderr or "")


class AutoDL:
    def __init__(self):
        self.account = operator_account()
        if os.geteuid() == 0 and self.account.pw_uid != 0:
            raise UserError("AutoDL 自动配置无需 sudo，请以原用户直接运行。")
        self.directory = pathlib.Path(self.account.pw_dir) / ".config" / "netpilot"
        self.path = self.directory / "autodl.json"

    def load(self):
        regular_file(self.path, os.geteuid(), private=True)
        data = load_json(self.path)
        if data is None:
            return None
        if isinstance(data, dict) and "forwarded" not in data:
            data = dict(data, forwarded=data.get("phase") in ("installing", "active"))
        if not isinstance(data, dict) or set(data) != {"schema", "plan", "phase", "control", "result", "owner_id", "forwarded"} or type(data["schema"]) is not int or data["schema"] != 1 or type(data["forwarded"]) is not bool or data["phase"] not in ("connecting", "forwarding", "installing", "active", "partial", "stopped"):
            raise UserError("AutoDL 会话记录无效，未猜测连接归属。")
        if not isinstance(data["owner_id"], str) or len(data["owner_id"]) != 32 or any(c not in "0123456789abcdef" for c in data["owner_id"]):
            raise UserError("AutoDL 归属编号无效。")
        validate_plan(data["plan"])
        return data

    def save(self, data):
        safe_directory(self.directory)
        save_json(self.path, data)

    def control_path(self, data):
        root = pathlib.Path(data["control"])
        if root.parent != pathlib.Path(tempfile.gettempdir()) or not root.name.startswith("netpilot-autodl-"):
            raise UserError("SSH 控制目录不属于本工具。")
        if not os.path.lexists(root):
            return root / "control.sock"
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise UserError("SSH 控制目录归属或权限异常。")
        marker = root / "owner.json"
        regular_file(marker, os.geteuid(), private=True)
        if load_json(marker) != data["plan"]:
            raise UserError("SSH 控制连接的归属记录不匹配。")
        path = root / "control.sock"
        if path.is_symlink() or (path.exists() and (not stat.S_ISSOCK(path.stat().st_mode) or path.stat().st_uid != os.geteuid())):
            raise UserError("SSH 控制套接字类型或归属异常。")
        return path

    def ssh(self, data):
        plan = data["plan"]
        return ["ssh", "-F", "/dev/null", "-S", str(self.control_path(data)), "-o", "ControlMaster=no",
                "-o", "BatchMode=yes", "-o", "ProxyCommand=false", "-o", "ConnectTimeout=10",
                "-p", str(plan["ssh_port"])]

    def control(self, data, operation, extra=()):
        return execute(self.ssh(data) + ["-O", operation, *extra, data["plan"]["destination"]], timeout=15)

    def alive(self, data):
        return bool(data and data["phase"] != "stopped" and self.control(data, "check").returncode == 0)

    def cleanup_control(self, data):
        root = self.control_path(data).parent
        if root.exists():
            for name in ("control.sock", "owner.json"):
                (root / name).unlink(missing_ok=True)
            root.rmdir()

    def remote(self, data, action):
        payload = dict(bundle(), plan=data["plan"], action=action, owner_id=data["owner_id"])
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        p = data["plan"]
        script = autodl_remote.bootstrap(encoded, scheme=p["scheme"], port=p["remote_port"],
            install_deps=p["install_deps"], git_selected=p["git"], action=action)
        result = execute(self.ssh(data) + [p["destination"], "bash", "-s"], input_text=script, timeout=420)
        lines = [line[len(autodl_remote.RESULT):] for line in result.stdout.splitlines() if line.startswith(autodl_remote.RESULT)]
        try:
            response = json.loads(lines[-1]) if lines else {}
        except ValueError:
            response = {}
        if result.returncode or not response.get("ok"):
            detail = response.get("error", "远端配置失败或连接中断。")
            raise UserError(clean_text(detail)[:400])
        return response["result"]

    def check_remote_port(self, data):
        script = '''# NETPILOT_PORT_PROBE
set -eu
for table in /proc/net/tcp /proc/net/tcp6; do
  [ -r "$table" ] || continue
  while read -r slot local_address peer state rest; do
    if [ "${local_address##*:}" = "@PORT@" ] && [ "$state" = 0A ]; then exit 37; fi
  done < "$table"
done
'''.replace("@PORT@", "{:04X}".format(data["plan"]["remote_port"]))
        result = execute(self.ssh(data) + [data["plan"]["destination"], "bash", "-s"], input_text=script, timeout=15)
        if result.returncode:
            raise UserError("远端端口已占用。请选择其他端口，不会接管已有监听。" if result.returncode == 37 else "无法检查远端监听端口，尚未转发。")

    def install_remote(self, data):
        data["phase"] = "installing"
        self.save(data)
        print("隧道已建立；正在校验 HTTPS、准备运行文件并安装远端手动开关。", flush=True)
        data["result"] = self.remote(data, "setup")
        data["phase"] = "active"
        self.save(data)

    @staticmethod
    def announce(data):
        print("AutoDL 配置完成，HTTPS 状态：" + clean_text(data["result"]["http_status"]))
        print("远端 Bash 执行：source ~/.bashrc，然后 ssh_proxy_on；用完执行 ssh_proxy_off。")
        print("远端管理/密钥向导入口：~/.config/netpilot/autodl-cli git-keys")
        print("本机隧道已后台保持；autodl-stop 停止，断线后 autodl-start 重新输入密码连接。")

    def setup(self, plan, secret):
        plan = validate_plan(plan)
        if not secret:
            raise UserError("SSH 密码不能为空。")
        bridge = PasswordBridge(secret)
        if not shutil.which("ssh"):
            raise UserError("缺少 openssh-client。")
        safe_directory(self.directory)
        with file_lock(self.directory / "autodl.lock"):
            old = self.load()
            if old and self.alive(old):
                raise UserError("已有本工具的 AutoDL 连接；请先停止，或查看 autodl-status。")
            if old and old["phase"] != "stopped" and (old["plan"]["apt"] or old["plan"]["git"]) and old["plan"] != plan:
                raise UserError("旧服务器的持久代理恢复尚未确认，请先重连旧配置并关闭，再更换目标。")
            try:
                with socket.create_connection(("127.0.0.1", plan["local_port"]), timeout=3):
                    pass
            except OSError:
                raise UserError("本机代理端口不可达，尚未连接远端。")
            if old:
                self.cleanup_control(old)
            root = pathlib.Path(tempfile.mkdtemp(prefix="netpilot-autodl-"))
            save_json(root / "owner.json", plan)
            owner_id = old["owner_id"] if old and old["plan"] == plan and old["phase"] != "stopped" else uuid.uuid4().hex
            data = dict(schema=1, plan=plan, phase="connecting", control=str(root), result=None, owner_id=owner_id, forwarded=False)
            self.save(data)
            try:
                print("正在连接 AutoDL；首次连接请核对主机指纹。", flush=True)
                args = ["ssh", "-f", "-N", "-T", "-M", "-S", str(root / "control.sock"), "-F", "/dev/null"]
                for option in ("ClearAllForwardings=yes", "ExitOnForwardFailure=yes", "ControlPersist=no", "ConnectTimeout=15", "ConnectionAttempts=1",
                               "ServerAliveInterval=30", "ServerAliveCountMax=3", "ForwardAgent=no", "ForwardX11=no",
                               "PermitLocalCommand=no", "RemoteCommand=none", "Tunnel=no", "NumberOfPasswordPrompts=1",
                               "PreferredAuthentications=password,keyboard-interactive", "PubkeyAuthentication=no",
                               "StrictHostKeyChecking=" + ("accept-new" if plan["accept_new_host"] else "ask")):
                    args += ["-o", option]
                args += ["-p", str(plan["ssh_port"]), plan["destination"]]
                with bridge:
                    connected = execute(args, env=bridge.environment(), timeout=180, background=True)
                secret = ""
                if connected.returncode or not self.alive(data):
                    raise UserError("SSH 握手或认证未成功；请核对控制台登录指令、主机指纹、密码及 VPN 路由。")
                data["phase"] = "forwarding"
                self.save(data)
                self.check_remote_port(data)
                forwarding = "127.0.0.1:{}:127.0.0.1:{}".format(plan["remote_port"], plan["local_port"])
                if self.control(data, "forward", ["-R", forwarding]).returncode:
                    raise UserError("反向监听失败，请检查远端端口占用及 SSH 转发权限；不会接管已有监听。")
                data["forwarded"] = True
                self.save(data)
                self.install_remote(data)
            except BaseException:
                data["phase"] = "partial"
                self.save(data)
                print("自动配置未全部完成；会话记录已保留，请查看 autodl-status 或执行 autodl-stop。", file=sys.stderr)
                raise
        self.announce(data)

    def start(self, from_stdin=False):
        data = self.load()
        if not data:
            raise UserError("尚无 AutoDL 配置，请先运行 autodl-setup。")
        if self.alive(data):
            if data["phase"] == "active":
                self.status()
                return
            if not data["forwarded"]:
                raise UserError("上次尚未建立转发，请先 autodl-stop，再重新配置。")
            with file_lock(self.directory / "autodl.lock"):
                data = self.load()
                try:
                    self.install_remote(data)
                except BaseException:
                    data["phase"] = "partial"
                    self.save(data)
                    raise
            self.announce(data)
            return
        self.setup(data["plan"], password_input(from_stdin))

    def stop(self):
        safe_directory(self.directory)
        with file_lock(self.directory / "autodl.lock"):
            data = self.load()
            if not data:
                raise UserError("没有本工具登记的 AutoDL 连接，不操作其他 SSH 会话。")
            if data["phase"] == "stopped":
                print("该 AutoDL 连接已经停止。")
                return
            connected = self.alive(data)
            if not connected and (data["plan"]["apt"] or data["plan"]["git"]):
                data["phase"] = "partial"
                self.save(data)
                raise UserError("连接已断开，无法确认远端 APT/Git 已恢复；请重新连接后停止，或在远端执行 ssh_proxy_off。")
            if connected:
                try:
                    self.remote(data, "restore")
                    if self.control(data, "exit").returncode:
                        raise UserError("无法确认 SSH 连接关闭，归属记录保留。")
                except BaseException:
                    data["phase"] = "partial"
                    self.save(data)
                    raise
            self.cleanup_control(data)
            data["phase"] = "stopped"
            self.save(data)
        print("已停止本工具的 AutoDL 连接。已有远端终端请执行 ssh_proxy_off 恢复自身变量。")

    def status(self):
        data = self.load()
        report = {"configured": data is not None}
        if data:
            report.update(plan=data["plan"], phase=data["phase"], ssh_connected=self.alive(data), result=data["result"])
            if not report["ssh_connected"] and data["phase"] != "stopped":
                report["hint"] = "连接已断开；远端持久代理规则不随断线自动撤销。可重连后关闭，或在远端执行 ssh_proxy_off。"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise UserError("AutoDL 参数无效；请查看 --help。密码使用隐藏输入或 --password-stdin，不接受明文密码参数。")


def dispatch(command, argv):
    parser = SafeParser(prog="./network_setup.sh " + command)
    if command in ("autodl-setup", "autodl-start"):
        parser.add_argument("--password-stdin", action="store_true", help="从标准输入读取一行密码，不保存")
    if command == "autodl-setup":
        parser.add_argument("--ssh", help="AutoDL 控制台的完整 SSH 登录指令")
        parser.add_argument("--local-port", type=int, default=10818)
        parser.add_argument("--remote-port", type=int, default=17890)
        parser.add_argument("--scheme", choices=("http", "https", "socks5h"), default="http")
        parser.add_argument("--apt", action="store_true", help="远端手动开启代理时联动 APT")
        parser.add_argument("--git", action="store_true", help="远端手动开启代理时联动 Git HTTPS")
        parser.add_argument("--install-deps", action="store_true", help="允许在缺少依赖时通过远端 root APT 安装")
        parser.add_argument("--accept-new-host", action="store_true", help="明确接受首次主机密钥；仍拒绝已改变的密钥")
        parser.add_argument("--test-url", default="https://example.com")
    args = parser.parse_args(argv)
    manager = AutoDL()
    if command == "autodl":
        while True:
            try:
                action = choose("AutoDL 自动配置", ["粘贴 SSH 指令自动配置", "重新连接已保存服务器", "查看连接状态", "停止本工具连接"])
            except Cancelled:
                return
            dispatch({"粘贴 SSH 指令自动配置": "autodl-setup", "重新连接已保存服务器": "autodl-start", "查看连接状态": "autodl-status", "停止本工具连接": "autodl-stop"}[action], [])
    elif command == "autodl-status":
        manager.status()
    elif command == "autodl-stop":
        manager.stop()
    elif command == "autodl-start":
        manager.start(args.password_stdin)
    else:
        interactive = args.ssh is None
        login = parse_ssh_command(args.ssh if args.ssh is not None else ask("粘贴 AutoDL 完整 SSH 登录指令（不要附带密码）"))
        if interactive:
            try:
                args.local_port = port_number(int(ask("本机已有代理端口", "10818")), minimum=1)
                args.remote_port = port_number(int(ask("远端回环监听端口", "17890")))
            except ValueError:
                raise UserError("代理端口必须为整数。")
            args.scheme = choose("本机代理协议", ["http", "socks5h", "https"])
            args.apt = confirm("远端手动开启时联动 APT？")
            args.git = confirm("远端手动开启时联动 Git HTTPS？")
            args.install_deps = confirm("允许在远端 root 环境安装缺失的 Python/curl/Git 依赖？")
        plan = validate_plan(dict(schema=1, **login, local_port=args.local_port, remote_port=args.remote_port,
            scheme=args.scheme, apt=args.apt, git=args.git, install_deps=args.install_deps,
            accept_new_host=args.accept_new_host, test_url=args.test_url))
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if interactive and not confirm("按以上配置连接、部署远端开关并保持后台隧道？"):
            return
        manager.setup(plan, password_input(args.password_stdin))
