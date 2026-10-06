"""Interface inventory, validated declarative plans, and network diagnostics."""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import pathlib
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .common import (UserError, ask, choose, clean_text, confirm, ensure_packages,
                     https_test, json_command, mask_secrets, overlapping_routes,
                     run, split_nmcli, validate_cidr, validate_dns, validate_gateway,
                     validate_connection_name, validate_interface, validate_pool, validate_psk, validate_ssid)


@dataclasses.dataclass
class Device:
    name: str
    kind: str
    state: str
    connection: str
    physical: bool
    phy: str = ""
    addresses: str = ""
    managed: bool = True

    def label(self):
        return "{} | {} | {} | {} | {}{}".format(
            self.name, self.kind, self.state, self.addresses or "无 IPv4",
            self.connection or "无活动连接", " | " + self.phy if self.phy else "")


def nm_field(field: str, device: str) -> str:
    result = run(["nmcli", "-g", field, "device", "show", device], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def inventory() -> List[Device]:
    result = run(["nmcli", "-t", "--escape", "yes", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    addresses = {v["ifname"]: ",".join("{}/{}".format(a["local"], a["prefixlen"])
                                    for a in v.get("addr_info", []) if a.get("family") == "inet")
                 for v in json_command(["ip", "-j", "-4", "address", "show"])}
    devices = []
    for line in result.stdout.splitlines():
        parts = split_nmcli(line)
        if len(parts) != 4:
            raise UserError("无法可靠解析网卡列表；未猜测列对应关系。")
        name, kind, state, connection = parts
        phy_path = pathlib.Path("/sys/class/net") / name / "phy80211"
        phy = phy_path.resolve().name if phy_path.exists() else ""
        physical = (pathlib.Path("/sys/class/net") / name / "device").exists() or bool(phy)
        devices.append(Device(name, kind, state, connection, physical, phy,
                              addresses.get(name, ""), nm_field("GENERAL.NM-MANAGED", name) == "yes"))
    return devices


def candidates(kind: Optional[str] = None, exclude: str = "") -> List[Device]:
    return [d for d in inventory() if d.physical and d.managed and d.kind in ("ethernet", "wifi")
            and (kind is None or d.kind == kind) and d.name != exclude]


def select_device(kind: Optional[str] = None, title: str = "选择网卡", exclude: str = "") -> Device:
    return choose(title, candidates(kind, exclude), lambda d: d.label())


def route_to(address: str = "1.1.1.1") -> Dict[str, Any]:
    ipaddress.IPv4Address(address)
    try:
        data = json_command(["ip", "-j", "-4", "route", "get", address])
        return data[0] if data else {}
    except UserError:
        return {}


def check_ap(device: Device, dry_run: bool = False) -> None:
    if not dry_run:
        ensure_packages(["iw", "rfkill", "wpasupplicant"])
    if nm_field("WIFI-PROPERTIES.AP", device.name) != "yes":
        raise UserError("该设备未报告 AP 能力，不能作为本工具的热点下行。")
    result = run(["nmcli", "radio", "wifi"])
    if result.stdout.strip() != "enabled":
        raise UserError("Wi-Fi 无线功能已关闭。请先在菜单单独开启，再配置；不会静默打开所有无线网卡。")
    if shutil.which("rfkill"):
        print(run(["rfkill", "list"], check=False).stdout)
    if shutil.which("iw"):
        print("当前无线监管域（不由脚本自动更改国家码）：")
        print(run(["iw", "reg", "get"], check=False).stdout)


def require_distinct_phys(up: Device, down: Device) -> None:
    if up.name == down.name:
        raise UserError("上行和下行不能是同一个接口。")
    if up.kind == down.kind == "wifi":
        if not up.phy or not down.phy:
            raise UserError("无法确定两张无线网卡的物理 phy 归属，拒绝猜测为独立硬件。")
        if up.phy == down.phy:
            raise UserError("两个接口属于同一物理无线设备；本版不支持单 phy 的 STA+AP 并发。")


def shared_pool_supported() -> bool:
    result = run(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"])
    for line in result.stdout.splitlines():
        fields = split_nmcli(line)
        if len(fields) == 2 and fields[1] in ("802-3-ethernet", "802-11-wireless", "ethernet", "wifi"):
            probe = run(["nmcli", "-g", "ipv4.shared-dhcp-range", "connection", "show", "uuid", fields[0]], check=False)
            return probe.returncode == 0
    # No profile available for a non-mutating property probe. Fail conservatively.
    return False


def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema") != 1:
        raise UserError("计划必须是 schema=1 的 JSON 对象。")
    allowed = {"schema", "role", "interface", "kind", "method", "address", "gateway", "dns",
               "on_demand_gateway", "autoconnect", "name", "ipv6", "wifi", "dhcp_range",
               "uplink_hint", "notes", "client_address_hint"}
    if set(plan) - allowed:
        raise UserError("计划包含未知字段：" + ", ".join(sorted(set(plan) - allowed)))
    result = dict(plan)
    if result.get("role") not in ("host", "client", "uplink"):
        raise UserError("role 必须为 host/client/uplink。")
    validate_interface(result.get("interface", ""))
    if result.get("kind") not in ("ethernet", "wifi"):
        raise UserError("只支持有线或 Wi-Fi 接口。")
    if result.get("method") not in ("manual", "auto", "shared"):
        raise UserError("不支持的 IPv4 方法。")
    if result.get("method") == "shared" and result["role"] != "host":
        raise UserError("shared 只能用于提供共享的主机下行，不能用于普通从机。")
    for field in ("address", "gateway", "on_demand_gateway", "dhcp_range", "uplink_hint"):
        result.setdefault(field, "")
        if not isinstance(result[field], str):
            raise UserError(field + " 必须是字符串。")
    result.setdefault("dns", [])
    if not isinstance(result["dns"], list) or not all(isinstance(v, str) for v in result["dns"]):
        raise UserError("dns 必须是字符串数组。")
    result["dns"] = validate_dns(" ".join(result["dns"]))
    if result["method"] in ("manual", "shared"):
        validate_cidr(result["address"])
    elif result["address"] or result["gateway"] or result["on_demand_gateway"]:
        raise UserError("DHCP 模式不能同时携带固定地址或手动网关。")
    if result["gateway"]:
        validate_gateway(result["address"], result["gateway"])
    if result["on_demand_gateway"]:
        if result["method"] != "manual" or result["role"] != "client" or result["gateway"]:
            raise UserError("临时网关只用于从机 manual 模式，不能同时保存持久网关。")
        validate_gateway(result["address"], result["on_demand_gateway"])
    if result["method"] == "shared" and (result["gateway"] or result["dns"]):
        raise UserError("本版共享下行不设置手动默认网关/DNS；DNS 转发由 NetworkManager 管理。")
    client_hint = result.get("client_address_hint", "")
    if not isinstance(client_hint, str):
        raise UserError("client_address_hint 必须是字符串。")
    if client_hint:
        if result["role"] != "host" or result["method"] not in ("manual", "shared"):
            raise UserError("从机地址建议只能用于主机固定地址/共享计划。")
        client = validate_cidr(client_hint)
        host = validate_cidr(result["address"])
        if client.network != host.network or client.ip == host.ip:
            raise UserError("主从机地址必须位于同一网段，且不能相同。")
    if result["dhcp_range"]:
        if result["method"] != "shared" or len(result["dhcp_range"].split(",")) != 2:
            raise UserError("DHCP 范围只能用于共享模式，格式为 起始IP,结束IP。")
        validate_pool(result["address"], *result["dhcp_range"].split(","),
                      reserved_addresses=[str(client.ip)] if client_hint else [])
    if not isinstance(result.get("autoconnect"), bool):
        raise UserError("autoconnect 必须为 true/false。")
    result.setdefault("ipv6", "auto" if result["role"] == "uplink" else "disabled")
    if result["ipv6"] not in ("disabled", "auto"):
        raise UserError("不支持的 IPv6 策略。")
    if result["role"] != "uplink" and result["ipv6"] != "disabled":
        raise UserError("本版下游/从机配置限定 IPv4；IPv6 必须显式 disabled。")
    validate_connection_name(result.get("name", ""))
    if result["kind"] == "wifi":
        wifi = result.get("wifi")
        if not isinstance(wifi, dict) or set(wifi) - {"mode", "ssid", "security", "band", "channel", "hidden"}:
            raise UserError("Wi-Fi 参数结构无效；密码不允许保存在可导入计划中。")
        validate_ssid(wifi.get("ssid", ""))
        if wifi.get("mode") not in ("ap", "infrastructure") or wifi.get("security") not in ("wpa2", "wpa3"):
            raise UserError("Wi-Fi 仅支持 infrastructure/AP 及 WPA2/WPA3 Personal。")
        if result["role"] == "host" and wifi["mode"] != "ap":
            raise UserError("主机 Wi-Fi 下行必须是 AP 模式。")
        if result["role"] != "host" and wifi["mode"] != "infrastructure":
            raise UserError("客户端和上行 Wi-Fi 必须是 infrastructure 模式。")
        if wifi.get("band", "") not in ("", "bg", "a"):
            raise UserError("仅支持自动、2.4 GHz 或 5 GHz 频段。")
        channel = wifi.get("channel", 0)
        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 177:
            raise UserError("无线信道格式不合法。")
        if channel and wifi.get("band") == "bg" and not 1 <= channel <= 14:
            raise UserError("2.4 GHz 信道必须在 1～14 范围内，且须当地监管允许。")
        if not isinstance(wifi.get("hidden", False), bool):
            raise UserError("hidden 必须为布尔值。")
    elif result.get("wifi"):
        raise UserError("有线配置不能携带 Wi-Fi 参数。")
    result.setdefault("notes", [])
    if not isinstance(result["notes"], list) or not all(isinstance(v, str) for v in result["notes"]):
        raise UserError("notes 必须是字符串数组。")
    return result


def build_settings(plan: Dict[str, Any], password: str = "", uuid_value: str = "") -> Dict[str, Any]:
    p = validate_plan(plan)
    con = {"id": p["name"] + " [试运行]", "uuid": uuid_value or str(uuid.uuid4()),
           "type": "802-3-ethernet" if p["kind"] == "ethernet" else "802-11-wireless",
           "interface-name": p["interface"], "autoconnect": False,
           "autoconnect-priority": 999 if p["role"] != "uplink" else 100,
           "permissions": []}
    ip4 = {"method": p["method"], "may-fail": False,
           "never-default": p["role"] == "host" or (p["method"] == "manual" and not p["gateway"])}
    if p["address"]:
        cidr = validate_cidr(p["address"])
        ip4["address-data"] = [{"address": str(cidr.ip), "prefix": cidr.network.prefixlen}]
    if p["gateway"]:
        ip4["gateway"] = p["gateway"]
    if p["dns"]:
        ip4["dns"] = p["dns"]
        ip4["ignore-auto-dns"] = True
    if p["dhcp_range"]:
        ip4["shared-dhcp-range"] = p["dhcp_range"]
    settings = {"connection": con, "ipv4": ip4, "ipv6": {"method": p["ipv6"]}}
    if p["kind"] == "ethernet":
        settings["802-3-ethernet"] = {}
    else:
        validate_psk(password)
        w = p["wifi"]
        settings["802-11-wireless"] = {"ssid": w["ssid"], "mode": w["mode"], "hidden": w.get("hidden", False)}
        if w.get("band"):
            settings["802-11-wireless"]["band"] = w["band"]
        if w.get("channel"):
            settings["802-11-wireless"]["channel"] = w["channel"]
        settings["802-11-wireless-security"] = {"key-mgmt": "sae" if w["security"] == "wpa3" else "wpa-psk",
                                                "proto": ["rsn"], "pairwise": ["ccmp"], "group": ["ccmp"],
                                                "psk": password, "psk-flags": 0}
        if w["security"] == "wpa3":
            settings["802-11-wireless-security"]["pmf"] = 3
    return settings


def wifi_questions(mode: str, dry_run: bool = False) -> Dict[str, Any]:
    ssid = validate_ssid(ask("热点 SSID" if mode == "ap" else "要连接的 Wi-Fi SSID", "NetPilot-AP" if mode == "ap" else None))
    security = choose("无线安全", ["wpa2", "wpa3"], lambda v: "WPA2 Personal（默认兼容）" if v == "wpa2" else "WPA3 Personal（能力不足会失败，不降级）")
    result = {"mode": mode, "ssid": ssid, "security": security, "hidden": False}
    if mode == "ap":
        result["band"] = choose("热点频段（信道最终受网卡和当地监管限制）", ["bg", "a"], lambda v: "2.4 GHz" if v == "bg" else "5 GHz")
        value = ask("信道；0 表示自动，不自动修改监管域", "0")
        if not value.isdigit():
            raise UserError("信道必须为整数。")
        result["channel"] = int(value)
    else:
        result["hidden"] = confirm("这是隐藏 SSID 吗？")
    return result


def default_plan(role: str, device: Device) -> Dict[str, Any]:
    return {"schema": 1, "role": role, "interface": device.name, "kind": device.kind,
            "method": "manual", "name": "NP-{}-{}".format(role.upper(), device.name),
            "address": "", "gateway": "", "on_demand_gateway": "", "dns": [],
            "dhcp_range": "", "uplink_hint": "", "autoconnect": True,
            "ipv6": "auto" if role == "uplink" else "disabled", "notes": []}


def choose_connection_name() -> str:
    while True:
        value = ask("请输入连接名称（1～100 个可打印字符）")
        try:
            return validate_connection_name(value)
        except UserError as exc:
            print(exc)


def choose_address(default: str, device: str) -> str:
    while True:
        try:
            value = ask("IPv4 地址/前缀", default)
            validate_cidr(value)
            conflicts = overlapping_routes(value, device,
                json_command(["ip", "-j", "-4", "address", "show"]),
                json_command(["ip", "-j", "-4", "route", "show", "table", "all"]))
            if conflicts:
                print("检测到与其他接口/路由重叠；请换网段：\n  " + "\n  ".join(conflicts))
                continue
            return value
        except UserError as exc:
            print(exc)


def validate_subnet(value: str) -> ipaddress.IPv4Network:
    if not isinstance(value, str):
        raise UserError("网段必须是 IPv4 字符串。")
    value = value.strip()
    if re.fullmatch(r"(?:[0-9]{1,3}\.){2}[0-9]{1,3}\.?", value):
        value = value.rstrip(".") + ".0/24"
    elif "/" not in value:
        value += "/24"
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        raise UserError("请输入网段网络地址，例如 192.168.1.0/24；也可输入 192.168.1，表示 /24 网段。")
    if not 8 <= network.prefixlen <= 30:
        raise UserError("本版局域网网段支持 /8～/30。")
    if network.network_address.is_loopback or network.network_address.is_multicast or network.network_address.is_unspecified:
        raise UserError("不能使用回环、组播或未指定网段。")
    return network


def subnet_address(network: ipaddress.IPv4Network, value: str) -> str:
    if not isinstance(value, str):
        raise UserError("请输入地址末段 .X 或完整 IPv4 地址。")
    value = value.strip()
    if re.fullmatch(r"\.?[0-9]{1,3}", value):
        suffix = int(value.lstrip("."))
        if suffix > 255:
            raise UserError("地址末段必须为 0～255，且不能形成网络或广播地址。")
        prefix = str(network.network_address).rsplit(".", 1)[0]
        value = prefix + "." + str(suffix)
    address = validate_cidr("{}/{}".format(value, network.prefixlen))
    if address.ip not in network:
        raise UserError("地址必须位于指定网段 {}。".format(network))
    return str(address)


def choose_pair_addresses(device: str) -> Tuple[str, str]:
    while True:
        value = ask("请输入网段（例如 192.168.1.0/24 或 192.168.1）")
        try:
            network = validate_subnet(value)
        except UserError as exc:
            print(exc)
            continue
        addresses = []
        for label in ("主机", "从机"):
            while True:
                value = ask(label + "地址末段 .X（例如 20 或 .20；也可输入完整 IPv4）")
                try:
                    address = subnet_address(network, value)
                    if address in addresses:
                        raise UserError("主从机地址不能相同，请重新输入从机地址。")
                    addresses.append(address)
                    break
                except UserError as exc:
                    print(exc)
        conflicts = overlapping_routes(addresses[0], device,
            json_command(["ip", "-j", "-4", "address", "show"]),
            json_command(["ip", "-j", "-4", "route", "show", "table", "all"]))
        if conflicts:
            print("检测到与其他接口/路由重叠；请重新输入网段：\n  " + "\n  ".join(conflicts))
            continue
        print("网段：{}；主机：{}；从机：{}".format(network, *addresses))
        return addresses[0], addresses[1]


def automatic_pool_usage(plan: Dict[str, Any]) -> None:
    client = str(validate_cidr(plan["client_address_hint"]).ip)
    print("自动 DHCP 地址池不能保证为固定地址 {} 预留位置。".format(client))
    selection = choose("地址使用方式", ["dhcp", "single-static"],
        lambda v: "下游全部使用 DHCP（从机实际地址由 DHCP 分配）" if v == "dhcp" else "专用直连、仅一台静态从机；接受不能混接 DHCP 客户端")
    plan["notes"].append("NM 自动 DHCP 池；" + ("下游全部 DHCP，填写的从机地址仅供参考，不是固定分配。" if selection == "dhcp" else "仅专用单台静态客户端；不要混接 DHCP 客户端。"))


def host_plan(dry_run: bool = False) -> Dict[str, Any]:
    modes = [("ethernet", "wifi", "有线上行 → Wi-Fi 热点"),
             ("wifi", "ethernet", "Wi-Fi 上行 → 有线共享（控制robot常用）"),
             ("wifi", "wifi", "Wi-Fi A 上行 → 独立 Wi-Fi B 热点"),
             ("ethernet", "ethernet", "有线 A 上行 → 有线 B 共享"),
             (None, "ethernet", "仅有线直连管理（不启用 DHCP/NAT）")]
    up_kind, down_kind, label = choose("主机模式", modes, lambda v: v[2])
    up = select_device(up_kind, "选择已连接的上行接口") if up_kind else None
    down = select_device(down_kind, "选择面向从机的下行接口", up.name if up else "")
    if up:
        require_distinct_phys(up, down)
        if not up.state.startswith("connected"):
            raise UserError("上行接口尚未连接。请先用“配置本机上行”建立连接，再运行共享向导。")
        actual = route_to()
        print("到 1.1.1.1 的当前出口：" + json.dumps(actual, ensure_ascii=False))
        print("本版 NAT 跟随主机系统路由，不对下游流量做独立策略路由。")
        if actual.get("dev") != up.name:
            if ask("当前出口不是所选接口（或不存在）。接受跟随系统出口请输入 FOLLOW，否则返回") != "FOLLOW":
                raise UserError("未继续；可先调整上行或 VPN，再重新检查。")
    if down.kind == "wifi":
        check_ap(down, dry_run)
    p = default_plan("host", down)
    if down.kind == "ethernet":
        p["name"] = choose_connection_name()
    p["method"] = "shared" if up else "manual"
    p["address"], p["client_address_hint"] = choose_pair_addresses(down.name)
    print("从机地址为配置建议；请在从机运行客户端向导，填写相同网段和主从机地址。")
    if up:
        p["uplink_hint"] = up.name
        p["notes"].append("上行接口仅为当前选择提示；共享出口跟随系统路由，未实现锁定出口。")
        if shared_pool_supported():
            cidr = validate_cidr(p["address"])
            if cidr.network.prefixlen == 24:
                prefix = str(cidr.network.network_address).rsplit(".", 1)[0]
                default_lo, default_hi = prefix + ".100", prefix + ".199"
            else:
                default_lo, default_hi = "", ""
            client_ip = str(validate_cidr(p["client_address_hint"]).ip)
            if default_lo:
                try:
                    validate_pool(p["address"], default_lo, default_hi, [client_ip])
                except UserError:
                    default_lo, default_hi = "", ""
            if confirm("设置自定义 DHCP 地址池，避免覆盖固定 IP？", True):
                while True:
                    start = ask("DHCP 起点", default_lo)
                    end = ask("DHCP 终点", default_hi)
                    try:
                        p["dhcp_range"] = validate_pool(p["address"], start, end, [client_ip])
                        break
                    except UserError as exc:
                        print(exc)
            else:
                automatic_pool_usage(p)
        else:
            print("当前 NetworkManager 无法确认支持 shared-dhcp-range。将使用自动地址池。")
            automatic_pool_usage(p)
    if down.kind == "wifi":
        p["wifi"] = wifi_questions("ap", dry_run)
    p["autoconnect"] = confirm("确认配置成功后，开机自动启用此下行？", True)
    return validate_plan(p)


def client_plan(dry_run: bool = False) -> Dict[str, Any]:
    dev = select_device(title="选择从机连接主机的接口")
    mode = choose("从机联网策略", ["dhcp", "permanent", "on-demand", "local"],
                  lambda v: {"dhcp": "DHCP 自动获取", "permanent": "固定 IP，持久默认网关",
                             "on-demand": "固定 IP，按需临时网关", "local": "固定 IP，仅本地管理"}[v])
    p = default_plan("client", dev)
    if dev.kind == "ethernet":
        p["name"] = choose_connection_name()
    if mode == "dhcp":
        p["method"] = "auto"
    else:
        host_address, p["address"] = choose_pair_addresses(dev.name)
        if dev.kind != "ethernet":
            p["name"] = "DEVICE-NETWORK"
        p["notes"].append("主机地址：{}。".format(host_address))
        if mode != "local":
            gw = validate_gateway(p["address"], str(validate_cidr(host_address).ip))
            p["gateway" if mode == "permanent" else "on_demand_gateway"] = gw
            p["dns"] = validate_dns(ask("DNS；空格分隔（主机共享必须已开启才能提供 DNS）", gw))
        p["notes"].append("静态地址须在 DHCP 地址池之外，或由网关明确保留；本机探测不能替代全网地址管理。")
    if dev.kind == "wifi":
        p["wifi"] = wifi_questions("infrastructure", dry_run)
    p["autoconnect"] = confirm("成功后开机自动启用？", True)
    return validate_plan(p)


def uplink_plan(dry_run: bool = False) -> Dict[str, Any]:
    dev = select_device(title="选择本机上行网卡")
    p = default_plan("uplink", dev)
    if dev.kind == "ethernet":
        p["name"] = choose_connection_name()
    p["method"] = choose("上行地址方式", ["auto", "manual"], lambda v: "DHCP" if v == "auto" else "固定 IPv4")
    if dev.kind == "wifi":
        print("可见热点（不显示密码）：")
        print(run(["nmcli", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", dev.name, "--rescan", "no"], check=False).stdout)
        p["wifi"] = wifi_questions("infrastructure", dry_run)
    if p["method"] == "manual":
        p["address"] = choose_address("", dev.name)
        p["gateway"] = validate_gateway(p["address"], ask("上行网关 IPv4"))
        p["dns"] = validate_dns(ask("DNS IPv4，空格分隔"))
        if not p["dns"]:
            raise UserError("固定上行至少填写一个可用 DNS 地址。")
    p["autoconnect"] = confirm("成功后开机自动连接这个上行？", True)
    return validate_plan(p)


def preflight(plan: Dict[str, Any], dry_run: bool = False) -> Device:
    p = validate_plan(plan)
    found = [d for d in inventory() if d.name == p["interface"]]
    if not found or found[0] not in candidates():
        raise UserError("目标接口不存在、不是可选物理网卡，或不由 NetworkManager 管理。")
    dev = found[0]
    if dev.kind != p["kind"]:
        raise UserError("导入计划的接口类型与实机不一致。")
    if p["address"]:
        conflicts = overlapping_routes(p["address"], dev.name,
            json_command(["ip", "-j", "-4", "address", "show"]),
            json_command(["ip", "-j", "-4", "route", "show", "table", "all"]))
        if conflicts:
            raise UserError("地址/路由冲突，已停止：\n" + "\n".join(conflicts))
    if p["kind"] == "wifi":
        if not dry_run:
            ensure_packages(["wpasupplicant"])
        if run(["nmcli", "radio", "wifi"]).stdout.strip() != "enabled":
            raise UserError("Wi-Fi 已关闭。请先在独立菜单开启；不会静默开启所有无线网卡。")
        if p["wifi"]["mode"] == "ap":
            check_ap(dev, dry_run)
    if p["uplink_hint"]:
        up = [d for d in inventory() if d.name == p["uplink_hint"]]
        if not up or not up[0].physical or not up[0].managed or up[0].kind not in ("wifi", "ethernet"):
            raise UserError("计划中的上行必须是 NetworkManager 管理的物理有线/Wi-Fi 接口。")
        if not up[0].state.startswith("connected"):
            raise UserError("计划中的上行尚未连接，请先建立上行再配置共享。")
        require_distinct_phys(up[0], dev)
        actual = route_to()
        if actual.get("dev") != up[0].name:
            print("注意：实际采样出口 {} 与上行提示 {} 不同；共享仍跟随系统路由。".format(actual.get("dev", "无"), up[0].name))
    if p["dhcp_range"] and not shared_pool_supported():
        raise UserError("当前 NetworkManager 不支持/无法确认自定义 DHCP 地址池；请在向导中重新选择安全的兼容方案。")
    if p["method"] == "shared" and not dry_run:
        ensure_packages(["dnsmasq-base"])
    ssh = os.environ.get("NETPILOT_SSH_CONNECTION") or os.environ.get("SSH_CONNECTION", "")
    if ssh:
        peer = ssh.split()[0]
        try:
            peer_route = route_to(peer)
        except ValueError:
            peer_route = {}
        print("警告：当前通过 SSH 操作；对端 {}，路由接口 {}。".format(clean_text(peer), peer_route.get("dev", "未知/IPv6")))
        if peer_route.get("dev") == dev.name:
            print("本次将修改承载当前 SSH 的网卡，连接很可能中断。请记下事务确认命令，并保留串口/本机通道。")
    return dev


def summary(plan: Dict[str, Any]) -> str:
    p = validate_plan(plan)
    return json.dumps(mask_secrets(p), ensure_ascii=False, indent=2)


def diagnostics(online: bool = False) -> Dict[str, Any]:
    commands = {
        "devices": ["nmcli", "device", "status"],
        "profiles_without_secrets": ["nmcli", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"],
        "ipv4_addresses": ["ip", "-j", "-4", "address", "show"],
        "ipv4_routes_all_tables": ["ip", "-j", "-4", "route", "show", "table", "all"],
        "routing_rules": ["ip", "-4", "rule", "show"],
        "ipv6_routes": ["ip", "-6", "route", "show"],
        "dns": ["resolvectl", "status"],
        "rfkill": ["rfkill", "list"],
        "wireless": ["iw", "dev"],
        "neighbors": ["ip", "neigh", "show"],
        "ipv4_forwarding": ["sysctl", "net.ipv4.ip_forward"],
        "firewall_ufw": ["ufw", "status", "verbose"],
        "firewall_forward": ["iptables", "-S", "FORWARD"],
        "firewall_docker_user": ["iptables", "-S", "DOCKER-USER"],
        "firewall_nft": ["nft", "list", "ruleset"],
        "listeners": ["ss", "-lntu"],
    }
    report = {"scope": "本机只读诊断；本机 HTTPS 成功不代表下游客户端已端到端验证。", "checks": {}}
    for key, command in commands.items():
        if not shutil.which(command[0]):
            report["checks"][key] = {"state": "skipped", "reason": "工具未安装"}
            continue
        try:
            result = run(command, check=False, timeout=12)
            report["checks"][key] = {"returncode": result.returncode,
                                      "output": clean_text(result.stdout) if "\n" not in result.stdout else "\n".join(clean_text(l) for l in result.stdout.splitlines()),
                                      "error": clean_text(result.stderr)}
        except UserError as exc:
            report["checks"][key] = {"state": "error", "reason": str(exc)}
    report["netplan_files"] = [str(p) for p in pathlib.Path("/etc/netplan").glob("*.yaml")]
    report["warning"] = "报告不请求 Wi-Fi/VPN 密码，但包含 IP、SSID、MAC、路由、防火墙信息；分享前请脱敏。不自动修复/清空防火墙。"
    if online:
        report["https_direct_test"] = https_test()
    return report


def address_probe(plan):
    """Optional ARP duplicate-address detection; absence of a reply is not proof."""
    if not plan.get("address") or plan["kind"] != "ethernet":
        print("ARP 占用探测：未执行（仅在待配置的有线链路上有意义）。")
        return
    target = str(validate_cidr(plan["address"]).ip)
    current = json_command(["ip", "-j", "-4", "address", "show", "dev", plan["interface"]])
    if any(a.get("local") == target for d in current for a in d.get("addr_info", [])):
        print("该地址已在目标接口上；跳过对本机地址的重复探测。")
        return
    links = json_command(["ip", "-j", "link", "show", "dev", plan["interface"]])
    if not links or "LOWER_UP" not in links[0].get("flags", []):
        print("当前网口没有确认链路载波，无法进行有效 ARP 占用探测；地址占用仍待验证。")
        return
    if not confirm("在该有线链路执行 ARP 地址占用探测？无响应也不保证地址未被使用。", True):
        print("地址占用：用户跳过，未验证。")
        return
    ensure_packages(["iputils-arping"])
    result = run(["arping", "-D", "-c", "3", "-w", "4", "-I", plan["interface"], target], check=False, timeout=8)
    if result.returncode == 1:
        raise UserError("ARP 探测收到地址冲突响应；未应用新配置，请换一个 IP。")
    if result.returncode != 0:
        raise UserError("ARP 探测失败，不能判断占用状态；请检查链路后重试。")
    print("ARP 未收到冲突响应；这不是地址绝对空闲的证明。")
