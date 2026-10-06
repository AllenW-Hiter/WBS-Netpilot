"""Small, dependency-free helpers. No networking is changed on import."""
from __future__ import annotations

import contextlib
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import pathlib
import pwd
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

STATE = pathlib.Path("/var/lib/netpilot")
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


class UserError(Exception):
    """An actionable, safe-to-display error."""


class Cancelled(UserError):
    pass


def clean_text(value: Any) -> str:
    """Strip terminal control characters from untrusted SSIDs/profile names."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))


def run(args: Sequence[str], check: bool = True, timeout: int = 25,
        input_text: Optional[str] = None, env: Optional[Dict[str, str]] = None,
        sensitive: bool = False) -> subprocess.CompletedProcess:
    """Never invoke a shell. Do not echo command input or secret arguments."""
    child_env = dict(os.environ)
    child_env.update({"LC_ALL": "C", "LANG": "C"})
    if env is not None:
        child_env.update(env)
    try:
        result = subprocess.run(list(args), input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, env=child_env, check=False)
    except FileNotFoundError:
        raise UserError("缺少工具：{}".format(args[0]))
    except subprocess.TimeoutExpired:
        raise UserError("命令超时：{}（未把超时当成成功）".format(args[0]))
    if check and result.returncode:
        detail = "敏感操作失败；参数未写入日志。" if sensitive else clean_text(result.stderr.strip())
        raise UserError("{} 失败（{}）：{}".format(args[0], result.returncode, detail[:1200]))
    return result


def json_command(args: Sequence[str]) -> List[Dict[str, Any]]:
    result = run(args, check=False)
    if result.returncode:
        raise UserError("无法读取网络状态：{}".format(clean_text(result.stderr)))
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        raise UserError("网络工具未返回预期的 JSON；已停止，未猜测状态。")


def split_nmcli(line: str) -> List[str]:
    """Parse nmcli --terse --escape yes (escaped colon and backslash)."""
    parts, chars, escaped = [], [], False
    for ch in line:
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            parts.append("".join(chars))
            chars = []
        else:
            chars.append(ch)
    if escaped:
        chars.append("\\")
    parts.append("".join(chars))
    return parts


def ask(prompt: str, default: Optional[str] = None, secret: bool = False) -> str:
    suffix = " [{}]".format(default) if default is not None and not secret else ""
    try:
        value = (getpass.getpass if secret else input)(prompt + suffix + ": ")
        if not secret:
            value = value.strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled("已取消。")
    return value if value else (default if default is not None else "")


def confirm(prompt: str, default: bool = False) -> bool:
    value = ask(prompt + (" [Y/n]" if default else " [y/N]"))
    return default if not value else value.lower() in ("y", "yes")


def choose(title: str, items: Sequence[Any], label=str) -> Any:
    if not items:
        raise UserError("没有符合条件的选项。")
    print("\n" + title)
    for i, item in enumerate(items, 1):
        print("  {}. {}".format(i, clean_text(label(item))))
    print("  0. 返回")
    while True:
        value = ask("选择编号")
        if value == "0":
            raise Cancelled("已返回。")
        if value.isdigit() and 1 <= int(value) <= len(items):
            return items[int(value) - 1]
        print("请输入列表中的编号。")


def validate_interface(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", value) or value.startswith("-"):
        raise UserError("接口名称不合法或不在本工具支持的命名范围内。")
    return value


def validate_connection_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not value.strip() or clean_text(value) != value:
        raise UserError("连接名称必须为 1～100 个可打印字符，不能为空或只包含空格。")
    return value


def validate_cidr(value: str) -> ipaddress.IPv4Interface:
    try:
        result = ipaddress.IPv4Interface(value)
    except (ValueError, TypeError):
        raise UserError("请输入 IPv4/前缀，例如 192.168.50.20/24。")
    if "/" not in value or not 8 <= result.network.prefixlen <= 30:
        raise UserError("本版局域网配置支持 /8～/30；必须显式输入前缀。")
    if result.ip in (result.network.network_address, result.network.broadcast_address):
        raise UserError("不能把网络地址或广播地址分配给主机。")
    if result.ip.is_loopback or result.ip.is_multicast or result.ip.is_unspecified:
        raise UserError("不能使用回环、组播或未指定地址。")
    return result


def validate_gateway(cidr: str, value: str) -> str:
    addr = validate_cidr(cidr)
    try:
        gw = ipaddress.IPv4Address(value)
    except ValueError:
        raise UserError("网关必须是 IPv4 地址。")
    if gw not in addr.network or gw in (addr.ip, addr.network.network_address,
                                        addr.network.broadcast_address):
        raise UserError("网关必须位于同一子网，且不能等于本机、网络或广播地址。")
    return str(gw)


def validate_dns(value: str) -> List[str]:
    result = []
    for item in value.replace(",", " ").split():
        try:
            addr = ipaddress.IPv4Address(item)
        except ValueError:
            raise UserError("本版 DNS 字段只接受 IPv4 地址，以空格分隔。")
        if addr.is_unspecified or addr.is_multicast:
            raise UserError("DNS 地址不合法。")
        if str(addr) not in result:
            result.append(str(addr))
    return result


def validate_ssid(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in value):
        raise UserError("SSID 必须为 1～32 个 UTF-8 字节，不能有控制字符。")
    return value


def validate_psk(value: str) -> str:
    if not isinstance(value, str):
        raise UserError("Wi-Fi 密码必须是字符串。")
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value
    if not 8 <= len(value) <= 63 or not all(32 <= ord(c) <= 126 for c in value):
        raise UserError("为兼容 WPA2，本版要求密码为 8～63 个可打印 ASCII 字符或 64 位十六进制。")
    return value


def validate_pool(cidr: str, start: str, end: str,
                  reserved_addresses: Sequence[str] = ()) -> str:
    host = validate_cidr(cidr)
    try:
        lo, hi = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    except ValueError:
        raise UserError("DHCP 地址池端点必须是 IPv4 地址。")
    if not (host.network.network_address < lo <= hi < host.network.broadcast_address):
        raise UserError("DHCP 地址池必须位于该子网内部，且起点不大于终点。")
    if lo <= host.ip <= hi:
        raise UserError("DHCP 地址池不能包含主机／网关自己的地址。")
    for value in reserved_addresses:
        address = ipaddress.IPv4Address(value)
        if lo <= address <= hi:
            raise UserError("DHCP 地址池不能包含预留的固定地址：{}".format(address))
    return "{},{}".format(lo, hi)


def overlapping_routes(cidr: str, interface: str,
                       addresses: List[Dict[str, Any]], routes: List[Dict[str, Any]]) -> List[str]:
    target = validate_cidr(cidr).network
    conflicts = set()
    for dev in addresses:
        if dev.get("ifname") == interface:
            continue
        for addr in dev.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            net = ipaddress.IPv4Network("{}/{}".format(addr["local"], addr["prefixlen"]), strict=False)
            if target.overlaps(net):
                conflicts.add("接口 {} 已使用 {}".format(dev.get("ifname"), net))
    for route in routes:
        dst = route.get("dst", "default")
        if dst in ("default", "0.0.0.0/0") or route.get("dev") == interface:
            continue
        if route.get("table") in ("local", 255) or route.get("type") in ("local", "broadcast"):
            continue
        try:
            net = ipaddress.IPv4Network(dst, strict=False)
        except ValueError:
            continue
        # /1 split-tunnel defaults are an egress warning, not a LAN address conflict.
        if net.prefixlen > 1 and target.overlaps(net):
            conflicts.add("路由 {} 经 {}（表 {}）".format(net, route.get("dev", "?"), route.get("table", "main")))
    return sorted(conflicts)


def mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("<隐藏>" if any(x in str(k).lower() for x in ("password", "psk", "secret"))
                         else mask_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def safe_directory(path: pathlib.Path, root_only: bool = False) -> None:
    if path.is_symlink():
        raise UserError("拒绝使用符号链接目录：{}".format(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root_only and path.stat().st_uid != 0:
        raise UserError("系统状态目录不是 root 所有，拒绝使用：{}".format(path))
    path.chmod(0o700)


def atomic_write(path: pathlib.Path, text: str, mode: int = 0o600) -> None:
    """Atomic replace; never follow a destination symlink."""
    if path.is_symlink():
        raise UserError("拒绝覆盖符号链接：{}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".netpilot-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_json(path: pathlib.Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    if path.stat().st_size > 10 * 1024 * 1024:
        raise UserError("配置文件过大，拒绝读取。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise UserError("配置文件不可读或格式损坏：{}".format(path))


@contextlib.contextmanager
def file_lock(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def require_root() -> None:
    if os.geteuid() != 0:
        raise UserError("此操作需要管理员权限；请通过 ./network_setup.sh 启动。")


def operator_account():
    uid = int(os.environ.get("SUDO_UID", os.getuid())) if os.geteuid() == 0 else os.getuid()
    return pwd.getpwuid(uid)


@contextlib.contextmanager
def as_operator():
    """Write user-owned files without root privileges (avoid root symlink writes)."""
    account = operator_account()
    old_uid, old_gid = os.geteuid(), os.getegid()
    changed = old_uid == 0 and account.pw_uid != 0
    old_groups = os.getgroups()
    if changed:
        os.setgroups(os.getgrouplist(account.pw_name, account.pw_gid))
        os.setegid(account.pw_gid)
        os.seteuid(account.pw_uid)
    try:
        yield account
    finally:
        if changed:
            os.seteuid(old_uid)
            os.setegid(old_gid)
            os.setgroups(old_groups)


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def audit(event: str, detail: Dict[str, Any]) -> None:
    if os.geteuid() != 0:
        return
    safe_directory(STATE, root_only=True)
    path = STATE / "audit.jsonl"
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
                                 "detail": mask_secrets(detail)}, ensure_ascii=False) + "\n")


def https_test(url: str = "https://example.com", proxy: Optional[str] = None) -> Dict[str, Any]:
    if not url.startswith("https://") or any(ord(c) < 32 for c in url):
        raise UserError("测试网址必须以 https:// 开头且不能含控制字符。")
    # -q must be the first option: ignore ~/.curlrc. Explicitly disable proxy bypass
    # during a proxy test; never put proxy credentials in argv or logs.
    args = ["curl", "-q", "--silent", "--show-error", "--output", "/dev/null",
            "--connect-timeout", "5", "--max-time", "12", "--proto", "=https",
            "--write-out", "%{http_code}"]
    if proxy:
        escaped = proxy.replace("\\", "\\\\").replace('"', '\\"')
        config = 'proxy = "{}"\nnoproxy = ""\n'.format(escaped)
        args += ["--config", "-", "--url", url]
    else:
        config = None
        args += ["--proxy", "", "--noproxy", "*", "--url", url]
    result = run(args, check=False, timeout=16, input_text=config, sensitive=bool(proxy))
    code = result.stdout.strip()
    return {"transport_ok": result.returncode == 0 and code.isdigit() and code != "000",
            "http_status": code, "curl_exit": result.returncode,
            "detail": "代理测试失败（未回显敏感参数）" if proxy and result.returncode else clean_text(result.stderr)}


def ensure_packages(packages: Sequence[str]) -> None:
    """Install only missing distribution packages, after explicit consent."""
    missing = []
    for package in packages:
        result = run(["dpkg-query", "-W", "-f=${Status}", package], check=False)
        if result.returncode or result.stdout.strip() != "install ok installed":
            missing.append(package)
    if not missing:
        return
    print("该功能缺少软件包：" + ", ".join(missing))
    print("将使用当前发行版 APT 软件源；现有网络不会先被断开。")
    if not confirm("现在联网安装这些包？"):
        raise Cancelled("依赖未安装，该功能未执行。")
    require_root()
    for args in (["apt-get", "-o", "DPkg::Lock::Timeout=60", "update"],
                 ["apt-get", "-o", "DPkg::Lock::Timeout=60", "install", "--no-install-recommends", "-y"] + missing):
        result = subprocess.run(args, check=False)
        if result.returncode:
            raise UserError("APT 安装失败；请先恢复可用上行或准备同版本/架构的离线包。未继续配置网络。")
