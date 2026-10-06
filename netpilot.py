#!/usr/bin/env python3
"""NetPilot — Ubuntu/Debian NetworkManager interactive network assistant."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import platform
import select
import shutil
import signal
import sys
import tarfile
import time
import uuid

from netpilot_lib import __version__
from netpilot_lib.common import (STATE, Cancelled, UserError, as_operator, ask,
    atomic_write, audit, choose, clean_text, confirm, ensure_packages, file_lock,
    load_json, mask_secrets, operator_account, require_root, run, safe_directory,
    save_json, timestamp, validate_psk)
from netpilot_lib.network import (build_settings, candidates, client_plan, default_plan,
    choose_connection_name, diagnostics, host_plan, inventory, preflight, route_to, select_device, summary,
    uplink_plan, validate_plan, address_probe)
from netpilot_lib.nm import (CON_IFACE, NM, Transactions, decode_typed,
    settings_to_dbus)
from netpilot_lib.proxy import ProxyManager
from netpilot_lib.github_ssh import GitHubSSH
from netpilot_lib.routes import RouteActions

ROOT = pathlib.Path(__file__).resolve().parent


def check_environment():
    result = {"python": sys.version.split()[0], "python_executable": sys.executable,
              "euid": os.geteuid(), "architecture": platform.machine(), "network_changes": False,
              "commands": {c: shutil.which(c) or "missing" for c in
                           ("nmcli", "ip", "systemctl", "systemd-run", "iw", "rfkill", "dnsmasq", "curl", "arping")},
              "python_dbus": importlib.util.find_spec("dbus") is not None}
    if pathlib.Path("/etc/os-release").exists():
        result["os_release"] = pathlib.Path("/etc/os-release").read_text()
    if shutil.which("systemctl"):
        result["networkmanager_service"] = run(["systemctl", "is-active", "NetworkManager"], check=False).stdout.strip()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def backup_system():
    require_root()
    directory = STATE / "backups" / "system"
    safe_directory(directory, root_only=True)
    target = directory / ("network-" + timestamp() + "-" + uuid.uuid4().hex[:6] + ".tar.gz")
    # Back up persistent and runtime sources; no global restore/netplan apply is automatic.
    sources = [pathlib.Path(p) for p in ("/etc/NetworkManager", "/etc/netplan",
                                        "/run/NetworkManager/system-connections") if pathlib.Path(p).exists()]
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as archive:
                for path in sources:
                    archive.add(str(path), arcname=str(path).lstrip("/"), recursive=True)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print("系统配置备份（可能包含密码，仅 root 可读）：" + str(target))
    return target


def wait_confirmation(manager, record):
    token = record["token"]
    deadline = record["created"] + record["timeout"]
    print("\n请从另一终端验证 SSH、地址和所需联网路径。")
    print("仅看到本机连接激活不代表共享客户端已经可上网。")
    print("输入 KEEP 保存；输入其他内容立即撤销。也可在重连终端执行 confirm。")
    try:
        while time.time() < deadline:
            current = load_json(manager.path(token))
            if current.get("status") == "committed":
                print("已由另一个终端确认并提交。")
                return current
            if current.get("status") not in ("pending",):
                print("事务已结束：" + current.get("status", "unknown"))
                if current.get("status") in ("confirmed-not-saved", "rollback-needs-attention", "failed"):
                    raise UserError("事务未全部完成，请检查现状和备份：" + current["status"])
                return current
            ready, _, _ = select.select([sys.stdin], [], [], min(1.0, max(0.0, deadline - time.time())))
            if ready:
                line = sys.stdin.readline()
                if line == "":
                    print("终端输入已关闭。未自动确认；请重连后确认，或由系统超时回滚。")
                    return current
                if line.strip() == "KEEP":
                    result = manager.commit(token)
                    print("已提交。" if result.get("status") == "committed" else "结果：" + result.get("status", "unknown"))
                    if result.get("status") != "committed":
                        raise UserError("事务未全部提交，请检查现状和备份：" + result.get("status", "unknown"))
                    return result
                result = manager.rollback(token)
                print("已请求恢复，结果：" + result["status"])
                return result
        result = manager.rollback(token)
        print("确认超时；结果：" + result["status"])
        return result
    except KeyboardInterrupt:
        manager.rollback(token)
        raise Cancelled("已取消并请求回滚。")


def check_name(nm, plan):
    owned = load_json(STATE / "managed.json", {})
    while any(p["name"] == plan["name"] and p["uuid"] not in owned for p in nm.profiles()):
        print("已有非本工具的同名配置 {}；保留原配置，请自行输入新的连接名称。".format(clean_text(plan["name"])))
        plan["name"] = choose_connection_name()
    return plan


def apply_plan(plan, dry_run=False, timeout=180):
    plan = validate_plan(plan)
    if dry_run:
        print("\n只读计划预览；不安装依赖、不收集密码、不写系统配置、不执行联网变更。")
        print(summary(plan))
        print("试运行 → 确认 → 保存；下游出口跟随系统路由，未绑定指定上行。")
        return
    require_root()
    nm = NM()
    Transactions(nm, timeout).ensure_idle()
    plan = check_name(nm, plan)
    preflight(plan)
    address_probe(plan)
    print("\n即将试运行的计划：\n" + summary(plan))
    print("原始连接将保留；本工具不改其他连接的自动启动、Netplan renderer 或全局防火墙。")
    if plan["method"] == "shared":
        print("共享可能受 Docker/UFW/nftables/VPN 的转发策略影响；本机成功不代表下游成功。")
    if not confirm("确认按以上计划执行试运行？"):
        return
    password = ask("Wi-Fi/热点密码（不会回显或进入命令行参数）", secret=True) if plan["kind"] == "wifi" else ""
    if password:
        validate_psk(password)
    settings = settings_to_dbus(build_settings(plan, password))
    backup_system()
    manager = Transactions(nm, timeout)
    with file_lock(STATE / "operation.lock"):
        record = manager.stage(settings, plan["interface"], plan["name"], plan["autoconnect"],
                               plan["role"] + ":" + plan["interface"], plan)
    print(run(["nmcli", "device", "status"]).stdout)
    print(run(["ip", "-4", "route", "show", "dev", plan["interface"]]).stdout)
    final = wait_confirmation(manager, record)
    if final.get("status") == "committed":
        print("配置 UUID：" + final.get("new_uuid", ""))
        print("旧的非本工具配置没有删除。需要退役旧配置时，可在连接管理中按 UUID 关闭其自动连接。")
        if final.get("cleanup_warnings"):
            print("部分旧的本工具配置未清理，请到连接管理核对 UUID：", final["cleanup_warnings"])
        if plan.get("on_demand_gateway"):
            print("该从机默认不保存网关；使用主菜单“临时联网路由”按需开启。")
        if confirm("导出不含密码的当前计划及主从配置卡？"):
            export_plan(plan)


def export_plan(plan):
    account = operator_account()
    directory = pathlib.Path(account.pw_dir) / "netpilot-exports"
    stem = plan["role"] + "-" + plan["interface"] + "-" + timestamp()
    with as_operator():
        safe_directory(directory)
        file = directory / (stem + ".json")
        save_json(file, plan)
        card = ["# NetPilot 配置卡", "", "该卡不包含密码；仍包含网络地址和 SSID，分享前请检查。", "",
                "本机角色：`{}`  ".format(plan["role"]), "接口：`{}`  ".format(plan["interface"]),
                "连接名称：`{}`  ".format(plan["name"]), "地址：`{}`  ".format(plan.get("address") or "DHCP"),
                "持久网关：`{}`  ".format(plan.get("gateway") or "无"),
                "临时网关建议：`{}`  ".format(plan.get("on_demand_gateway") or "无"),
                "DHCP 范围：`{}`  ".format(plan.get("dhcp_range") or "未指定；共享时由 NM 自动决定"), ""]
        if plan["role"] == "host" and plan["method"] == "shared":
            address = plan["address"].split("/")[0]
            card += ["## 从机接入", "", "多设备优先选择 DHCP；使用固定 IP 必须避开地址池或由网关保留。", "",
                     "从机的网关/DNS 可填写主机下游地址 `{}`；从机不要设置为 shared。".format(address),
                     "共享跟随主机系统路由；未实现只为下游绑定出口，也不保证任意 VPN 转发。", ""]
        if plan.get("client_address_hint"):
            card += ["从机固定地址建议：`{}`。选择 DHCP 时实际地址由 DHCP 分配；本机操作不会配置从机。".format(plan["client_address_hint"]), ""]
        card += ["## 备注", ""] + ["- " + str(n) for n in plan.get("notes", [])]
        atomic_write(directory / (stem + ".md"), "\n".join(card) + "\n")
    print("已导出：" + str(file))


def eligible_profiles(nm):
    devices = {d.name: d for d in inventory()}
    result = []
    for profile in nm.profiles():
        if profile["type"] not in ("802-3-ethernet", "802-11-wireless"):
            continue
        bound = devices.get(profile["interface"])
        if bound and not bound.physical:
            continue
        result.append(profile)
    return result


def profile_label(profile):
    return "{} | UUID={} | {} | 接口={} | 自动={} | 来源={}".format(
        profile["name"], profile["uuid"], profile["type"], profile["interface"] or "未绑定",
        profile["autoconnect"], profile["filename"] or ("内存" if profile["unsaved"] else "未知"))


def profile_menu(nm, timeout):
    action = choose("连接配置管理（不会删除物理网卡）", ["查看详情", "激活已有连接", "安全断开指定网卡",
        "关闭自动连接", "开启自动连接", "备份配置", "批量删除非活动配置"])
    profiles = eligible_profiles(nm)
    manager = Transactions(nm, timeout)
    if action == "安全断开指定网卡":
        dev = select_device(title="选择要断开的网卡")
        if not confirm("将断开 {}，不会关闭其他无线网卡。继续？".format(dev.name)):
            return
        record = manager.disconnect(dev.name)
        wait_confirmation(manager, record)
        return
    if action == "批量删除非活动配置":
        for i, p in enumerate(profiles, 1):
            print("{}. {}".format(i, clean_text(profile_label(p))))
        value = ask("输入要删除的编号，以逗号分隔；0 返回")
        if value == "0":
            return
        try:
            indices = sorted(set(int(x.strip()) for x in value.split(",")))
        except ValueError:
            raise UserError("编号格式错误。")
        if not indices or any(i < 1 or i > len(profiles) for i in indices):
            raise UserError("编号超出范围。")
        selected = [profiles[i - 1] for i in indices]
        active = {nm.active_uuid(d.name) for d in inventory()}
        for p in selected:
            if p["uuid"] in active:
                raise UserError("{} 正在使用。请先安全切换/断开，再删除；本次未删除任何配置。".format(p["name"]))
            if p["filename"].startswith("/run/"):
                raise UserError("{} 来源于 /run（可能由 Netplan 生成）。请检查源 YAML，本版不猜测删除生成源。".format(p["name"]))
            settings, errors = nm.settings_with_secrets(p["path"])
            if errors or "802-1x" in settings:
                raise UserError("{} 的凭据无法完整备份或属于企业认证配置；本版拒绝自动删除。".format(p["name"]))
            sec = settings.get("802-11-wireless-security", {})
            if sec.get("key-mgmt") in ("wpa-psk", "sae") and not sec.get("psk"):
                raise UserError("{} 的密码可能只在用户密钥环中，无法完整备份；拒绝删除。".format(p["name"]))
        print("将备份后删除以下 UUID：")
        for p in selected:
            print(clean_text(profile_label(p)))
        if ask("输入 DELETE 确认") != "DELETE":
            return
        backups = [nm.backup_profile(p["uuid"]) for p in selected]
        backup_system()
        for p, backup in zip(selected, backups):
            # Recheck immediately before each deletion, in case an external app activated it.
            if p["uuid"] in {nm.active_uuid(d.name) for d in inventory()}:
                raise UserError("连接刚被其他程序激活，已停止后续删除；之前完成的删除可从备份恢复。")
            nm.delete(p["uuid"])
            print("已删除 {}；备份 {}".format(p["uuid"], backup))
            audit("profile-delete", {"uuid": p["uuid"], "backup": backup})
        return
    p = choose("选择连接配置", profiles, profile_label)
    if action == "查看详情":
        print(run(["nmcli", "connection", "show", "uuid", p["uuid"]]).stdout)
    elif action == "备份配置":
        print("私有备份：" + nm.backup_profile(p["uuid"]))
    elif action == "激活已有连接":
        kind = "wifi" if p["type"] == "802-11-wireless" else "ethernet"
        available = [d for d in candidates(kind) if not p["interface"] or d.name == p["interface"]]
        dev = choose("选择激活接口", available, lambda d: d.label())
        if confirm("试运行该连接，成功后仍需 KEEP 确认？"):
            record = manager.activate_existing(p["uuid"], dev.name)
            wait_confirmation(manager, record)
    else:
        value = action == "开启自动连接"
        if p["filename"].startswith("/run/"):
            raise UserError("该配置为运行时/生成配置；需要检查持久源，本版不直接改它的自动连接策略。")
        if confirm("{} {}？这不改变当前 IP，但会影响后续自动连接。".format(action, clean_text(p["name"]))):
            backup = nm.backup_profile(p["uuid"])
            nm.set_autoconnect(p["uuid"], value)
            print("已修改；原配置备份：" + backup)


def restore_profile(nm, timeout):
    directory = STATE / "backups" / "profiles"
    entries = []
    for file in sorted(directory.glob("*.json"), reverse=True):
        data = load_json(file)
        if data and data.get("schema") == 1 and data["metadata"]["type"] in ("802-3-ethernet", "802-11-wireless"):
            entries.append((file, data))
    file, data = choose("选择连接备份；恢复为新配置，不覆盖现有同 UUID 连接", entries,
                        lambda item: item[0].name + " | " + item[1]["metadata"]["name"])
    if data.get("secret_errors"):
        raise UserError("该备份记录了凭据缺失，不能保证自动恢复，请使用系统备份/原认证方式。")
    kind = "wifi" if data["metadata"]["type"] == "802-11-wireless" else "ethernet"
    dev = select_device(kind, "选择恢复到哪个物理接口")
    settings = decode_typed(data["settings"])
    con = settings["connection"]
    # Duplicate as a new connection: never overwrite an unrelated live UUID.
    new_uuid = str(uuid.uuid4())
    name = str(con["id"]) + " [恢复]"
    desired_auto = bool(con.get("autoconnect", True))
    con["id"] = nm.d.String(name + " [试运行]")
    con["uuid"] = nm.d.String(new_uuid)
    con["interface-name"] = nm.d.String(dev.name)
    con["autoconnect"] = nm.d.Boolean(False)
    print("将恢复：{} → {}，新 UUID {}".format(clean_text(data["metadata"]["name"]), dev.name, new_uuid))
    print("证书/私钥的外部引用不在该 JSON 中复制，相关文件必须仍存在。")
    if not confirm("试运行恢复？"):
        return
    backup_system()
    manager = Transactions(nm, timeout)
    record = manager.stage(settings, dev.name, name, desired_auto, "restore:" + dev.name,
                           {"restored_from": file.name})
    wait_confirmation(manager, record)


def disable_shared_keep_local(nm, timeout):
    registry = load_json(STATE / "managed.json", {})
    available = [(u, m) for u, m in registry.items() if m.get("plan", {}).get("role") == "host"
                 and m["plan"].get("kind") == "ethernet" and m["plan"].get("method") == "shared"
                 and nm.profile(u)]
    item, metadata = choose("选择本工具的有线共享；改为相同 IP 的本地管理连接", available,
                           lambda x: x[1]["plan"]["interface"] + " | " + x[1]["plan"]["address"])
    plan = dict(metadata["plan"])
    plan.update({"method": "manual", "dhcp_range": "", "uplink_hint": ""})
    plan["notes"] = ["有线地址保留；本连接不再启用 NetworkManager shared DHCP/NAT。已有第三方转发/代理不在本工具控制范围内。"]
    apply_plan(plan, timeout=timeout)


def status_display():
    print("NetPilot " + __version__ + " — 当前网络（只读）")
    for d in inventory():
        group = "可选物理网卡" if d.physical and d.managed and d.kind in ("wifi", "ethernet") else "系统/虚拟/未托管设备"
        print("[{}] {}".format(group, clean_text(d.label())))
    print("\n实际 IPv4 出口采样：", route_to())
    print("\n共享、临时默认路由、应用代理是独立状态；这不是“全机联网开关”。")


def transaction_command(action, token):
    require_root()
    if not token:
        raise UserError("缺少事务编号。")
    # Token is validated by both manager path functions, before any file read.
    routes = RouteActions()
    if routes.path(token).exists():
        manager = routes
    else:
        manager = Transactions(NM())
    result = manager.commit(token) if action == "confirm" else manager.rollback(token)
    print("事务 {}：{}".format(token, result["status"]))
    expected = {"committed"} if action == "confirm" else {"rolled-back", "expired-after-reboot"}
    if result.get("status") not in expected:
        raise UserError("事务未全部完成：{}；请检查现状和备份。".format(result.get("status", "unknown")))


def diagnostics_export(online=False):
    report = diagnostics(online)
    account = operator_account()
    target = pathlib.Path(account.pw_dir) / "netpilot-exports" / ("diagnostics-" + timestamp() + ".json")
    with as_operator():
        safe_directory(target.parent)
        save_json(target, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("诊断报告：" + str(target))


def demo():
    for name in ("host_shared.json", "client_static.json"):
        plan = load_json(ROOT / "examples" / name)
        print("\n=== {}：仅示例，不是当前机器检测结果 ===".format(name))
        print(summary(plan))
    print("没有运行 nmcli/ip、安装软件或修改系统配置。")


def menu(dry_run=False, timeout=180):
    if dry_run:
        role = choose("只读向导：收集参数并预览，不实际应用", ["host", "client", "uplink"])
        return apply_plan({"host": host_plan, "client": client_plan, "uplink": uplink_plan}[role](True), True, timeout)
    nm = NM()
    print("NetPilot {} | NetworkManager {}".format(__version__, nm.version))
    print("本版使用 IPv4 NAT 共享；不实现任意 VPN 的透明转发、强制 VPN 或独立出口策略。")
    pending = Transactions(nm, timeout).pending()
    if pending:
        print("已有事务记录：", [(v["token"], v["status"]) for v in pending])
    options = ["查看网卡与状态", "配置主机／共享网关", "配置从机／客户端", "配置本机上行",
               "管理已有连接", "临时联网路由", "应用代理", "停止有线共享但保留管理地址",
               "备份／恢复", "导入 JSON 计划", "诊断与导出报告", "开启 Wi-Fi 无线功能（全局）"]
    while True:
        try:
            option = choose("主菜单", options)
            if option == "查看网卡与状态":
                status_display()
            elif option in ("配置主机／共享网关", "配置从机／客户端", "配置本机上行"):
                wizard = {"配置主机／共享网关": host_plan, "配置从机／客户端": client_plan, "配置本机上行": uplink_plan}[option]
                apply_plan(wizard(), timeout=timeout)
            elif option == "管理已有连接":
                profile_menu(nm, timeout)
            elif option == "临时联网路由":
                routes = RouteActions(timeout)
                mode = choose("临时默认路由；不控制应用代理和其他上行", ["添加临时网关", "撤销本工具临时网关", "查看本工具路由记录"])
                if mode == "查看本工具路由记录":
                    print(json.dumps(routes.active(), ensure_ascii=False, indent=2))
                else:
                    record = routes.prepare_on(nm) if mode == "添加临时网关" else routes.prepare_off()
                    if record:
                        wait_confirmation(routes, record)
            elif option == "应用代理":
                ProxyManager().menu()
            elif option == "停止有线共享但保留管理地址":
                disable_shared_keep_local(nm, timeout)
            elif option == "备份／恢复":
                action = choose("备份与恢复", ["系统配置备份", "从私有连接备份恢复"])
                backup_system() if action == "系统配置备份" else restore_profile(nm, timeout)
            elif option == "导入 JSON 计划":
                path = pathlib.Path(ask("计划 JSON 路径")).expanduser()
                plan = validate_plan(load_json(path))
                # Remap interface explicitly, because the exporting machine is different.
                dev = select_device(plan["kind"], "确认/重新选择本机实际接口")
                plan["interface"] = dev.name
                if plan.get("uplink_hint"):
                    plan["uplink_hint"] = select_device(title="重新选择本机上行提示接口", exclude=dev.name).name
                apply_plan(plan, timeout=timeout)
            elif option == "诊断与导出报告":
                if confirm("安装可选诊断工具（缺失时才安装）？"):
                    ensure_packages(["curl", "ca-certificates", "iw", "rfkill", "iputils-ping", "iputils-arping"])
                diagnostics_export(confirm("同时向 example.com 发起 HTTPS 测试？"))
            else:
                print("这是全局 Wi-Fi 射频开关；开启后，已有自动连接的 Wi-Fi 配置可能开始连接。")
                print("此开关不属于设备连接检查点；仅明确确认后开启，不提供静默全局关闭操作。")
                if confirm("开启所有 Wi-Fi 设备的无线功能？"):
                    run(["nmcli", "radio", "wifi", "on"])
                    print("Wi-Fi 无线功能已开启。硬件 rfkill 开关仍需自行解除。")
        except Cancelled:
            if not sys.stdin.isatty():
                return
            # Top-level 0 exits; nested 0 returns to top. A plain choose exception
            # carries no level, so ask a single consistent choice rather than loop trap.
            if confirm("退出 NetPilot？"):
                return
        except UserError as exc:
            print("\n未继续：" + clean_text(str(exc)))
        except Exception as exc:
            print("\n操作失败（未把失败当成功）：" + clean_text(str(exc)))
            print("涉及试运行的连接仍受原检查点保护；请检查事务或使用 rollback。")


def parser():
    p = argparse.ArgumentParser(description="NetPilot：Ubuntu/Debian NetworkManager 主从机交互配置工具",
        epilog="建议入口 ./network_setup.sh；只读检查不会安装软件或修改网络。请先阅读 README.md。")
    p.add_argument("--version", action="version", version="NetPilot " + __version__)
    p.add_argument("--check", action="store_true", help="只读依赖/环境检查")
    p.add_argument("--status", action="store_true", help="只读网卡/路由概览")
    p.add_argument("--diagnose", action="store_true", help="只读诊断并导出本用户报告")
    p.add_argument("--online", action="store_true", help="诊断额外发起有时限的 HTTPS 测试")
    p.add_argument("--demo", action="store_true", help="离线展示主机/从机示例，不调用系统网络工具")
    p.add_argument("--dry-run", action="store_true", help="只读计划预览；不安装软件或修改网络")
    p.add_argument("--plan", type=pathlib.Path, help="导入 schema=1 JSON 计划；密码在实机应用时单独输入")
    p.add_argument("--self-test", action="store_true", help="运行离线单元测试（不修改系统网络）")
    p.add_argument("--timeout", type=int, default=180, help="试运行自动回滚秒数，120～900，默认 180")
    p.add_argument("command", nargs="?", default="menu", choices=["menu", "host", "client", "uplink", "confirm", "rollback", "route-on", "route-off", "proxy", "proxy-run", "proxy-apt-on", "proxy-apt-off", "proxy-cleanup", "github-ssh", "backup", "restore"])
    p.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.args and args.command not in ("confirm", "rollback", "proxy-run"):
        raise UserError("子命令后有未识别参数；--dry-run/--timeout 等全局选项必须放在子命令之前。")
    if args.command in ("confirm", "rollback") and len(args.args) != 1:
        raise UserError("confirm/rollback 必须且只能接一个事务编号。")
    if not 120 <= args.timeout <= 900:
        raise UserError("回滚时限必须为 120～900 秒。")
    if args.check:
        check_environment(); return 0
    if args.demo:
        demo(); return 0
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.status:
        status_display(); return 0
    if args.diagnose:
        diagnostics_export(args.online); return 0
    if args.command == "proxy-run":
        if args.dry_run:
            raise UserError("dry-run 不执行代理子命令；请移除 --dry-run 后明确运行。")
        return ProxyManager().run_command(args.args)
    if args.plan:
        plan = validate_plan(load_json(args.plan.expanduser()))
        apply_plan(plan, args.dry_run, args.timeout); return 0
    if args.dry_run and args.command not in ("host", "client", "uplink", "menu"):
        raise UserError("dry-run 仅用于配置计划/向导；其他只读操作请使用 --status/--check。")
    if args.command in ("host", "client", "uplink"):
        wizard = {"host": host_plan, "client": client_plan, "uplink": uplink_plan}[args.command]
        apply_plan(wizard(args.dry_run), args.dry_run, args.timeout)
    elif args.command in ("confirm", "rollback"):
        transaction_command(args.command, args.args[0] if args.args else "")
    elif args.command in ("route-on", "route-off"):
        manager = RouteActions(args.timeout)
        record = manager.prepare_on(NM()) if args.command == "route-on" else manager.prepare_off()
        if record:
            wait_confirmation(manager, record)
    elif args.command == "proxy":
        ProxyManager().menu()
    elif args.command == "github-ssh":
        GitHubSSH().configure()
    elif args.command in ("proxy-apt-on", "proxy-apt-off"):
        ProxyManager().set_apt(args.command == "proxy-apt-on")
    elif args.command == "proxy-cleanup":
        ProxyManager().cleanup_legacy(confirm_action=False)
    elif args.command == "backup":
        backup_system()
    elif args.command == "restore":
        restore_profile(NM(), args.timeout)
    else:
        menu(args.dry_run, args.timeout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Cancelled, KeyboardInterrupt):
        print("已退出；未确认的网络检查点仍由 NetworkManager 的时限保护。")
        sys.exit(0)
    except (UserError, ValueError, OSError) as exc:
        print("错误：" + clean_text(str(exc)), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("操作异常：" + clean_text(str(exc)), file=sys.stderr)
        print("未确认的配置不会自动变成持久配置；请检查 pending 事务和恢复通道。", file=sys.stderr)
        sys.exit(1)
