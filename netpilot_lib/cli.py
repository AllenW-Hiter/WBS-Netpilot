"""Three feature areas with operation-scoped dependency and privilege dispatch."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from . import __version__, network_ops as net
from .common import (Cancelled, UserError, ask, choose, clean_text, confirm, ensure_packages,
                     load_json, operator_account)
from .github_ssh import GitHubSSH
from .proxy import ProxyManager
from .proxy_profiles import ProfileManager, source_name
from .ssh_tunnel import SSHTunnel
from .autodl import dispatch as autodl_dispatch

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALIASES = {"codex-ssh": "ssh-menu", "codex-tunnel": "ssh-tunnel", "codex-proxy": "ssh-proxy", "codex-proxy-test": "ssh-proxy-test"}
COMMANDS = ["menu", "network", "proxy", "git-keys", "host", "client", "uplink", "profiles", "confirm", "rollback", "route-on", "route-off",
            "backup", "restore", "network-import", "shared-stop", "wifi-on", "route-status", "github-ssh", "wire-proxy", "ssh-proxy", "proxy-status", "proxy-remove",
            "ssh-menu", "ssh-tunnel", "ssh-tunnel-config", "wire-proxy-test", "ssh-proxy-test", "proxy-session-on", "proxy-session-off",
            "proxy-run", "wire-proxy-run", "ssh-proxy-run", "proxy-apt-on", "proxy-apt-off", "proxy-cleanup",
            "autodl", "autodl-setup", "autodl-start", "autodl-status", "autodl-stop", *ALIASES]


@dataclass(frozen=True)
class Operation:
    handler: Callable
    root: bool = False
    network: bool = False


def elevate(argv):
    if not shutil.which("sudo"):
        raise UserError("此操作需要管理员权限；请准备 sudo 或在管理员终端运行该具体命令。")
    result = subprocess.run(["sudo", "--", "/usr/bin/env", "NETPILOT_SSH_CONNECTION=" + os.environ.get("SSH_CONNECTION", ""),
                             "/usr/bin/python3", str(ROOT / "netpilot.py"), *argv], check=False)
    return result.returncode


def network_dependencies():
    release = pathlib.Path("/etc/os-release")
    if sys.platform != "linux" or not release.exists() or not any(x in release.read_text().lower() for x in ("ubuntu", "debian")):
        raise UserError("自动准备网络配置依赖仅支持 Ubuntu/Debian。")
    if not shutil.which("nmcli"):
        print("安装 NetworkManager 可能启动服务；通过 SSH 操作时请先保留本机/串口恢复通道。")
    ensure_packages(["python3-dbus", "network-manager", "iproute2", "dbus", "systemd"])
    if importlib.util.find_spec("dbus") is None:
        raise UserError("系统 Python 无法导入 dbus，请检查 python3-dbus。")


def network_import(timeout):
    plan = net.validate_plan(load_json(pathlib.Path(ask("计划 JSON 路径")).expanduser()))
    plan["interface"] = net.select_device(plan["kind"], "选择本机实际接口").name
    if plan.get("uplink_hint"):
        plan["uplink_hint"] = net.select_device(title="选择本机上行提示接口", exclude=plan["interface"]).name
    net.apply_plan(plan, timeout=timeout)


def route_action(on, timeout):
    manager = net.RouteActions(timeout)
    record = manager.prepare_on(net.NM()) if on else manager.prepare_off()
    if record:
        net.wait_confirmation(manager, record)


def wifi_on():
    print("开启的是全局 Wi-Fi 射频；原有自动连接可能开始连接，此操作不属于连接检查点。")
    if confirm("明确开启所有 Wi-Fi 设备的无线功能？"):
        net.run(["nmcli", "radio", "wifi", "on"])


def check_legacy_proxy_idle():
    manager = ProfileManager()
    if manager.session() or manager.apt_effect.owner():
        raise UserError("请先关闭已有的 APT/Git 联动会话，再操作旧版代理，避免破坏恢复基线。")


def legacy_apt(enabled):
    check_legacy_proxy_idle()
    ProxyManager().set_apt(enabled)


def cleanup_legacy():
    check_legacy_proxy_idle()
    ProxyManager().cleanup_legacy()


def network_menu(timeout=180):
    actions = [("检测当前网络", "status"), ("配置主机/共享网关", "host"), ("配置从机/静态 IP", "client"),
               ("配置本机上行", "uplink"), ("管理连接", "profiles"), ("添加临时网关", "route-on"),
               ("撤销本工具临时网关", "route-off"), ("停止共享并保留管理地址", "shared-stop"),
               ("查看本工具临时路由记录", "route-status"),
               ("系统备份", "backup"), ("单连接恢复", "restore"), ("导入网络计划", "network-import"),
               ("诊断并导出报告", "diagnose"), ("明确开启全局 Wi-Fi", "wifi-on")]
    while True:
        try:
            _, command = choose("网络设置与检测", actions, lambda x: x[0])
        except Cancelled:
            return
        argv = ["--timeout", str(timeout)]
        argv += ["--status"] if command == "status" else ["--diagnose"] if command == "diagnose" else [command]
        try:
            if main(argv):
                print("操作未完成，请检查上方错误。")
        except Cancelled:
            pass
        except (UserError, OSError) as exc:
            print("未完成：" + clean_text(exc))


def ssh_menu():
    while True:
        try:
            action = choose("SSH 代理", ["配置 SSH 隧道方向和端口", "启动前台隧道", "配置当前机器的 SSH 终端代理", "检测当前代理情况"])
        except Cancelled:
            return
        if action == "配置 SSH 隧道方向和端口": SSHTunnel().configure()
        elif action == "启动前台隧道": SSHTunnel().start()
        elif action == "配置当前机器的 SSH 终端代理": ProfileManager().configure("ssh")
        else: ProfileManager().status()


def git_menu():
    while True:
        try:
            action = choose("Git SSH 密钥辅助", ["配置 GitHub SSH 密钥并辅助授权", "查看当前用户 SSH 公钥文件"])
        except Cancelled:
            return
        if action == "配置 GitHub SSH 密钥并辅助授权":
            GitHubSSH().configure()
        else:
            directory = pathlib.Path(operator_account().pw_dir) / ".ssh"
            print("当前用户公钥文件（私钥不读取）：")
            for path in sorted(directory.glob("*.pub")):
                if path.is_file() and not path.is_symlink():
                    print(str(path))


def menu(timeout=180):
    print("NetPilot " + __version__)
    while True:
        try:
            action = choose("主菜单", ["网络设置与检测", "网络代理与检测", "Git SSH 密钥辅助", "AutoDL 自动配置"])
        except Cancelled:
            return
        try:
            if action == "网络设置与检测": network_menu(timeout)
            elif action == "网络代理与检测": ProfileManager().menu()
            elif action == "Git SSH 密钥辅助": git_menu()
            else: autodl_dispatch("autodl", [])
        except Cancelled:
            pass
        except (UserError, OSError, ValueError) as exc:
            print("未完成：" + clean_text(exc))


def parser():
    p = argparse.ArgumentParser(description="NetPilot：网络设置、网络代理、Git SSH 密钥辅助",
        epilog="主菜单无需 sudo/NetworkManager；依赖与权限按所选操作检查。全局参数放在子命令前。")
    p.add_argument("--version", action="version", version="NetPilot " + __version__)
    modes = p.add_mutually_exclusive_group()
    for option, help_text in (("--check", "只读环境检查"), ("--status", "只读网络概览"), ("--diagnose", "诊断并导出报告"),
                              ("--proxy-status", "检测终端、APT、Git 当前代理"), ("--demo", "离线网络示例")):
        modes.add_argument(option, action="store_true", help=help_text)
    p.add_argument("--online", action="store_true", help="诊断时显式测试 HTTPS")
    p.add_argument("--dry-run", action="store_true", help="只读网络计划预览")
    p.add_argument("--plan", type=pathlib.Path, help="网络 JSON 计划")
    p.add_argument("--timeout", type=int, default=180, help="网络试运行超时：120～900 秒")
    p.add_argument("command", nargs="?", default="menu", choices=COMMANDS)
    p.add_argument("args", nargs=argparse.REMAINDER)
    return p


def operation(args, command):
    timeout = args.timeout
    if command.startswith("autodl"):
        return Operation(lambda: autodl_dispatch(command, args.args))
    if command in ("proxy-session-on", "proxy-session-off", "proxy-remove"):
        source = source_name(args.args[0])
        manager = ProfileManager()
        on = command.endswith("-on")
        if on and manager.load(source)["git"]:
            manager.git_effect.env()  # Validate Git scope before sudo drops environment overrides.
        handler = (lambda: manager.remove(source)) if command == "proxy-remove" else (lambda: manager.enable(source) if on else manager.disable(source))
        return Operation(handler, manager.needs_root("on" if on else "off", source))
    entries = {
        "menu": Operation(lambda: menu(timeout)), "network": Operation(lambda: network_menu(timeout)),
        "proxy": Operation(lambda: ProfileManager().menu()), "git-keys": Operation(git_menu),
        "github-ssh": Operation(lambda: GitHubSSH().configure()), "ssh-menu": Operation(ssh_menu),
        "ssh-tunnel": Operation(lambda: SSHTunnel().start()), "ssh-tunnel-config": Operation(lambda: SSHTunnel().configure()),
        "wire-proxy": Operation(lambda: ProfileManager().configure("wire")), "ssh-proxy": Operation(lambda: ProfileManager().configure("ssh")),
        "proxy-status": Operation(lambda: ProfileManager().status()),
        "wire-proxy-test": Operation(lambda: ProfileManager().test("wire")), "ssh-proxy-test": Operation(lambda: ProfileManager().test("ssh")),
        "proxy-apt-on": Operation(lambda: legacy_apt(True), True),
        "proxy-apt-off": Operation(lambda: legacy_apt(False), True),
        "proxy-cleanup": Operation(cleanup_legacy, True),
        "backup": Operation(net.backup_system, True), "restore": Operation(lambda: net.restore_profile(net.NM(), timeout), True, True),
        "profiles": Operation(lambda: net.profile_menu(net.NM(), timeout), True, True),
        "network-import": Operation(lambda: network_import(timeout), True, True),
        "shared-stop": Operation(lambda: net.disable_shared_keep_local(net.NM(), timeout), True, True),
        "wifi-on": Operation(wifi_on, True, True),
        "route-on": Operation(lambda: route_action(True, timeout), True, True),
        "route-off": Operation(lambda: route_action(False, timeout), True),
        "route-status": Operation(lambda: print(json.dumps(net.RouteActions(timeout).active(), ensure_ascii=False, indent=2)), True),
    }
    if command in ("host", "client", "uplink"):
        return Operation(lambda: net.apply_plan({"host": net.host_plan, "client": net.client_plan, "uplink": net.uplink_plan}[command](), timeout=timeout), True, True)
    if command in ("confirm", "rollback"):
        return Operation(lambda: net.transaction_command(command, args.args[0]), True)
    if command in ("proxy-run", "wire-proxy-run", "ssh-proxy-run"):
        return Operation(lambda: ProfileManager().run_command("ssh" if command == "ssh-proxy-run" else "wire", args.args))
    return entries[command]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    command = ALIASES.get(args.command, args.command)
    remainder = {"confirm", "rollback", "proxy-run", "wire-proxy-run", "ssh-proxy-run", "proxy-session-on", "proxy-session-off", "proxy-remove",
                 "autodl", "autodl-setup", "autodl-start", "autodl-status", "autodl-stop"}
    if args.args and command not in remainder:
        raise UserError("子命令后有未识别参数；全局选项必须放在子命令前。")
    if command in ("confirm", "rollback", "proxy-session-on", "proxy-session-off", "proxy-remove") and len(args.args) != 1:
        raise UserError("该命令必须且只能接一个事务编号或代理来源。")
    if not 120 <= args.timeout <= 900:
        raise UserError("回滚时限必须为 120～900 秒。")
    if args.plan and command not in ("menu", "host", "client", "uplink"):
        raise UserError("该命令不能同时应用网络 JSON 计划。")
    if args.dry_run and command not in ("menu", "network", "host", "client", "uplink"):
        raise UserError("dry-run 仅用于网络配置计划/向导。")
    if args.check: net.check_environment(); return 0
    if args.demo: net.demo(); return 0
    if args.status: net.status_display(); return 0
    if args.diagnose: net.diagnostics_export(args.online); return 0
    if args.proxy_status: ProfileManager().status(); return 0
    if args.plan or args.dry_run:
        if args.plan:
            plan = net.validate_plan(load_json(args.plan.expanduser()))
        else:
            role = command if command in ("host", "client", "uplink") else choose("只读网络向导", ["host", "client", "uplink"])
            plan = {"host": net.host_plan, "client": net.client_plan, "uplink": net.uplink_plan}[role](True)
        if not args.dry_run and os.geteuid() != 0:
            return elevate(argv)
        if not args.dry_run:
            network_dependencies()
        net.apply_plan(plan, args.dry_run, args.timeout)
        return 0
    op = operation(args, command)
    if op.root and os.geteuid() != 0:
        return elevate(argv)
    if op.network:
        network_dependencies()
    result = op.handler()
    return result if type(result) is int else 0


def entrypoint():
    try:
        return main()
    except (Cancelled, KeyboardInterrupt):
        print("已取消当前操作。待确认的网络事务请按其编号检查；SSH 隧道与终端代理分别控制。")
        return 0
    except Exception as exc:
        print("操作未完成：" + clean_text(exc), file=sys.stderr)
        return 1
