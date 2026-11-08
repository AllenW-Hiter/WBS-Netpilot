"""User-owned foreground SSH forwarding; no network configuration or credentials."""
from __future__ import annotations
import ipaddress
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess

from .common import (Cancelled, UserError, as_operator, ask, atomic_write, choose,
                     confirm, load_json, operator_account, safe_directory, save_json, timestamp)


def port_number(value, minimum=1024):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 65535:
        raise UserError("端口必须为 {}～65535 的整数。".format(minimum))
    return value


def validate_tunnel(data):
    if isinstance(data, dict) and data.get("schema") == 1:
        data = dict(schema=2, mode="reverse", destination=data.get("destination"), ssh_port=data.get("ssh_port"),
                    listen_port=data.get("remote_port"), target_port=data.get("local_port"), scheme=data.get("scheme"))
    keys = {"schema", "mode", "destination", "ssh_port", "listen_port", "target_port", "scheme"}
    if not isinstance(data, dict) or set(data) != keys or type(data["schema"]) is not int or data["schema"] != 2:
        raise UserError("SSH 隧道配置结构无效。")
    if data["mode"] not in ("forward", "dynamic", "reverse") or data["scheme"] not in ("http", "https", "socks5h"):
        raise UserError("SSH 隧道方向或代理协议无效。")
    port_number(data["listen_port"])
    if data["mode"] == "dynamic":
        if data["target_port"] is not None or data["scheme"] != "socks5h":
            raise UserError("动态 SSH 代理使用 SOCKS5h，不填写目标代理端口。")
    else:
        port_number(data["target_port"], minimum=1)
    port = data["ssh_port"]
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise UserError("SSH 端口必须为 1～65535，或留空沿用 SSH 配置。")
    dest = data["destination"]
    if not isinstance(dest, str) or len(dest) > 300:
        raise UserError("SSH 目标无效。")
    parts = dest.split("@")
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", parts[0])):
        raise UserError("SSH 用户名无效。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", parts[-1]):
        try:
            ipaddress.IPv6Address(parts[-1])
        except ValueError:
            raise UserError("SSH 目标只接受主机别名或 用户@主机，不接受命令或额外选项。")
    return dict(data)


def ssh_arguments(data):
    c = validate_tunnel(data)
    args = ["ssh", "-N", "-T"]
    for option in ("ExitOnForwardFailure=yes", "ServerAliveInterval=30", "ServerAliveCountMax=3",
                   "ControlMaster=no", "ControlPath=none", "ControlPersist=no", "ForkAfterAuthentication=no",
                   "SessionType=none", "RemoteCommand=none", "PermitLocalCommand=no", "Tunnel=no",
                   "ForwardAgent=no", "ForwardX11=no"):
        args += ["-o", option]
    if c["ssh_port"] is not None:
        args += ["-p", str(c["ssh_port"])]
    if c["mode"] == "dynamic":
        args += ["-D", "127.0.0.1:{}".format(c["listen_port"])]
    else:
        args += ["-R" if c["mode"] == "reverse" else "-L",
                 "127.0.0.1:{}:127.0.0.1:{}".format(c["listen_port"], c["target_port"])]
    return args + [c["destination"]]


def check_forwardings(output, data):
    c = validate_tunnel(data)
    key = {"forward": "localforward", "reverse": "remoteforward", "dynamic": "dynamicforward"}[c["mode"]]
    value = "[127.0.0.1]:{}".format(c["listen_port"])
    if c["mode"] != "dynamic":
        value += " [127.0.0.1]:{}".format(c["target_port"])
    actual = [(k, v) for line in output.splitlines() for k, _, v in [line.partition(" ")]
              if k in ("localforward", "remoteforward", "dynamicforward") and v != "none"]
    if actual != [(key, value)]:
        raise UserError("SSH 解析出的转发规则与计划不一致；请使用没有其他转发规则的主机别名。")


class SSHTunnel:
    def __init__(self):
        self.account = operator_account()
        self.directory = pathlib.Path(self.account.pw_dir) / ".config" / "netpilot"
        self.config_file = self.directory / "ssh-tunnel.json"

    def load(self):
        with as_operator():
            path = self.config_file if self.config_file.exists() else self.directory / "codex-ssh.json"
            if path.is_symlink() or (path.exists() and (path.stat().st_uid != self.account.pw_uid or path.stat().st_mode & 0o077)):
                raise UserError("SSH 隧道配置必须是当前用户的普通私有文件（chmod 600）。")
            if not path.is_file():
                raise UserError("尚未配置 SSH 隧道；请运行 ./network_setup.sh ssh-tunnel-config。")
            return validate_tunnel(load_json(path))

    @staticmethod
    def ask_port(prompt, default, minimum=1024):
        value = ask(prompt, str(default))
        if not value.isascii() or not value.isdigit():
            raise UserError("端口必须为整数。")
        return port_number(int(value), minimum=minimum)

    def configure(self):
        mode = choose("SSH 代理方向", ["forward", "dynamic", "reverse"], lambda x: {
            "forward": "本机借远端已有代理（本机监听 → SSH 服务器的回环代理端口）",
            "dynamic": "本机借 SSH 服务器直接出网（本机 SOCKS5 监听）",
            "reverse": "远端借本机已有代理（AutoDL：远端监听 → 本机回环代理端口）"}[x])
        destination = ask("SSH 主机别名或 用户@主机")
        ssh_port = ask("SSH 端口；留空沿用 SSH 配置")
        if ssh_port and (not ssh_port.isascii() or not ssh_port.isdigit()):
            raise UserError("SSH 端口必须为整数。")
        scheme = "socks5h" if mode == "dynamic" else choose("已有代理端口的协议", ["http", "socks5h", "https"])
        listen = self.ask_port("{}回环监听端口".format("远端" if mode == "reverse" else "本机"), 17890)
        target = None if mode == "dynamic" else self.ask_port("{}已有代理端口".format("本机" if mode == "reverse" else "远端"), 7890, minimum=1)
        c = validate_tunnel(dict(schema=2, mode=mode, destination=destination,
            ssh_port=int(ssh_port) if ssh_port else None, scheme=scheme, listen_port=listen, target_port=target))
        print("计划命令：" + shlex.join(ssh_arguments(c)))
        if not confirm("保存当前用户的隧道配置？保存不会启动连接。"):
            return
        with as_operator():
            safe_directory(self.directory)
            if os.path.lexists(self.config_file):
                self.load()
                atomic_write(self.directory / ("ssh-tunnel.before-" + timestamp()), self.config_file.read_text())
            save_json(self.config_file, c)
        print("已保存；运行 ./network_setup.sh ssh-tunnel 启动并保持前台隧道。")
        print("在{}配置 SSH 代理端点：{}://127.0.0.1:{}".format("远端" if mode == "reverse" else "本机", scheme, listen))

    def invoke(self, args, capture=False):
        def drop():
            os.initgroups(self.account.pw_name, self.account.pw_gid)
            os.setgid(self.account.pw_gid)
            os.setuid(self.account.pw_uid)
        try:
            return subprocess.run(list(args), check=False, text=True,
                env=dict(os.environ, HOME=self.account.pw_dir, USER=self.account.pw_name, LOGNAME=self.account.pw_name),
                stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None,
                timeout=20 if capture else None,
                preexec_fn=drop if os.geteuid() == 0 and self.account.pw_uid != 0 else None)
        except subprocess.TimeoutExpired:
            raise UserError("SSH 配置解析超时，未启动隧道。")
        except OSError:
            raise UserError("无法启动 SSH，请检查 openssh-client 和当前用户权限。")

    def start(self):
        c = self.load()
        if not shutil.which("ssh"):
            raise UserError("缺少 openssh-client。")
        args = ssh_arguments(c)
        result = self.invoke(["ssh", "-G"] + args[1:], capture=True)
        if result.returncode:
            raise UserError("无法解析 SSH 配置；请先检查 ssh -G 主机别名。")
        check_forwardings(result.stdout, c)
        if c["mode"] == "reverse":
            try:
                with socket.create_connection(("127.0.0.1", c["target_port"]), timeout=3):
                    pass
            except OSError:
                raise UserError("本机已有代理端口不可达；未启动隧道。")
        print("即将启动前台 SSH 隧道；核对 OpenSSH 主机指纹。Ctrl+C 停止。")
        print("{}的回环监听目标为 127.0.0.1:{}；代理开关需在该端手动开启。".format(
              "远端" if c["mode"] == "reverse" else "本机", c["listen_port"]))
        if c["mode"] == "reverse":
            print("请在远端核对实际监听范围；sshd 的 GatewayPorts 强制策略可能改写绑定。")
        try:
            result = self.invoke(args)
        except KeyboardInterrupt:
            raise Cancelled("已停止前台 SSH 隧道。代理终端另行执行 ssh_proxy_off。")
        if result.returncode:
            raise UserError("SSH 隧道退出（{}）；请检查认证、监听端口和 TCP 转发许可。".format(result.returncode))
        print("SSH 隧道已结束。")
