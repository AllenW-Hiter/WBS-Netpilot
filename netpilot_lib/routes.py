"""Temporary default routes: exact ownership receipts + systemd rollback timer."""
from __future__ import annotations
import ipaddress
import os
import pathlib
import re
import time
import uuid
from typing import Any, Dict

from .common import (STATE, UserError, ask, audit, choose, confirm, file_lock,
                     json_command, load_json, require_root, run, safe_directory,
                     save_json, atomic_write, validate_gateway)
from .network import inventory, select_device
from .route_guard import PROTO, route_command, validated_route


def boot_id() -> str:
    return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def route_key(route: Dict[str, Any]) -> str:
    r = validated_route(route)
    return "{}|{}|{}".format(r["interface"], r["gateway"], r["metric"])


def current_defaults():
    return json_command(["ip", "-j", "-4", "route", "show", "table", "main", "default"])


def route_present(route: Dict[str, Any], tagged: bool = False) -> bool:
    args = ["ip", "-j", "-4", "route", "show", "table", "main", "default"]
    if tagged:
        args += ["proto", str(PROTO)]
    for item in json_command(args):
        if item.get("dev") == route["interface"] and item.get("gateway") == route["gateway"] and int(item.get("metric", 0)) == route["metric"]:
            return True
    return False


class RouteActions:
    def __init__(self, timeout=180):
        require_root()
        self.timeout = timeout
        safe_directory(STATE, root_only=True)
        safe_directory(STATE / "route-actions", root_only=True)

    def path(self, token):
        if not re.fullmatch(r"[a-f0-9]{12}", token):
            raise UserError("事务编号格式不合法。")
        return STATE / "route-actions" / (token + ".json")

    def active(self):
        return load_json(STATE / "routes.json", {})

    def begin(self, route, operation):
        route = validated_route(route)
        if operation not in ("add", "delete"):
            raise UserError("不支持的路由操作。")
        if run(["systemctl", "is-system-running"], check=False).stdout.strip() not in ("running", "degraded", "starting"):
            raise UserError("没有可用的 systemd 运行环境，不能安排独立回滚；未修改临时路由。")
        for old in (STATE / "route-actions").glob("*.json"):
            data = load_json(old)
            if data.get("status") == "pending" and data.get("boot_id") == boot_id():
                raise UserError("还有待确认的临时路由事务 {}。".format(data["token"]))
        token = uuid.uuid4().hex[:12]
        data = {"schema": 1, "kind": "route", "token": token, "status": "pending",
                "operation": operation, "route": route, "boot_id": boot_id(),
                "created": time.time(), "timeout": self.timeout}
        target = STATE / "route_guard.py"
        source = pathlib.Path(__file__).with_name("route_guard.py")
        # Copy to a root-owned directory: a delayed root timer must NOT run mutable
        # code from the operator's Downloads/project directory.
        atomic_write(target, source.read_text(encoding="utf-8"), 0o700)
        unit = "netpilot-route-" + token
        save_json(self.path(token), data)
        print("临时路由事务：{}；{} 秒未确认会恢复这条路由原来的状态。".format(token, self.timeout))
        print("新终端确认：./network_setup.sh confirm " + token)
        with file_lock(self.path(token).with_suffix(".lock")):
            route_started = False
            try:
                # Timer is installed BEFORE touching the kernel route.
                run(["systemd-run", "--quiet", "--collect", "--unit=" + unit,
                     "--on-active={}s".format(self.timeout), "--timer-property=AccuracySec=1s",
                     "/usr/bin/python3", str(target), "--expire", token])
                route_started = True
                run(route_command(operation, route))
            except BaseException:
                if route_started:
                    # A timeout or Ctrl-C can arrive after the kernel changed the route.
                    # Retain the pending receipt and the already installed rollback timer;
                    # cancelling it here would strand an unconfirmed modification.
                    print("路由操作未确认；独立定时恢复任务仍保留，请记录上方事务编号。")
                else:
                    data["status"] = "failed"
                    save_json(self.path(token), data)
                    run(["systemctl", "stop", unit + ".timer"], check=False)
                raise
        return data

    def commit(self, token):
        with file_lock(self.path(token).with_suffix(".lock")):
            data = load_json(self.path(token))
            if not data or data["status"] != "pending":
                raise UserError("路由事务已结束或不存在；请重新检查当前路由。")
            if data["boot_id"] != boot_id():
                raise UserError("系统已经重启，原临时路由事务不能提交。")
            exists = route_present(data["route"], tagged=True)
            if exists != (data["operation"] == "add"):
                raise UserError("当前路由与事务预期不一致，拒绝提交。")
            active = self.active()
            key = route_key(data["route"])
            if data["operation"] == "add":
                active[key] = {"route": data["route"], "boot_id": data["boot_id"], "token": token}
            else:
                active.pop(key, None)
            save_json(STATE / "routes.json", active)
            data["status"] = "committed"
            save_json(self.path(token), data)
        run(["systemctl", "stop", "netpilot-route-" + token + ".timer"], check=False)
        audit("temporary-route-commit", {"token": token, "operation": data["operation"], "route": data["route"]})
        return data

    def rollback(self, token):
        run(["/usr/bin/python3", str(STATE / "route_guard.py"), "--expire", token])
        data = load_json(self.path(token))
        if not data or data.get("status") not in ("rolled-back", "expired-after-reboot"):
            raise UserError("不能确认临时路由恢复成功；请检查事务记录和 ip route。")
        run(["systemctl", "stop", "netpilot-route-" + token + ".timer"], check=False)
        return data

    def prepare_on(self, nm):
        dev = select_device(title="选择从机用于临时借主机联网的接口")
        active_uuid = nm.active_uuid(dev.name)
        if not active_uuid:
            raise UserError("该接口没有活动连接；请先配置固定地址。")
        managed = load_json(STATE / "managed.json", {}).get(active_uuid, {})
        suggestion = managed.get("plan", {}).get("on_demand_gateway", "")
        gateway = ask("主机／临时网关 IPv4", suggestion or None)
        ipaddress.IPv4Address(gateway)
        addr_data = json_command(["ip", "-j", "-4", "address", "show", "dev", dev.name])
        valid = False
        for entry in addr_data:
            for addr in entry.get("addr_info", []):
                if addr.get("family") == "inet":
                    try:
                        validate_gateway("{}/{}".format(addr["local"], addr["prefixlen"]), gateway)
                        valid = True
                    except UserError:
                        pass
        if not valid:
            raise UserError("网关与该接口当前地址不在可用的同一子网。")
        value = ask("临时默认路由 metric（越小越优先）", "100")
        if not value.isdigit():
            raise UserError("metric 必须是整数。")
        route = validated_route({"interface": dev.name, "gateway": gateway, "metric": int(value), "protocol": PROTO})
        existing = self.active().get(route_key(route))
        if route_present(route):
            if existing and existing["boot_id"] == boot_id() and route_present(route, tagged=True):
                print("这条本工具登记的临时路由已经存在，无需重复添加。")
                return None
            raise UserError("同样的路由已存在，但不属于本次可验证的工具记录；不会接管或替换它。")
        print("当前主路由表默认路由：", current_defaults())
        if any(int(r.get("metric", 0)) == route["metric"] for r in current_defaults()):
            raise UserError("已有同 metric 默认路由。请重新选择不同的 metric，避免出口歧义。")
        print("只添加这一条带协议标记的临时默认路由；不会替换其他默认路由，也不会修改持久配置。")
        print(route)
        if not confirm("确认主机已经开启共享，并试运行这条路由？"):
            return None
        return self.begin(route, "add")

    def prepare_off(self):
        active = self.active()
        candidates = []
        changed = False
        for key, record in list(active.items()):
            if record.get("boot_id") != boot_id() or not route_present(record["route"], tagged=True):
                active.pop(key)
                changed = True
            else:
                candidates.append(record["route"])
        if changed:
            save_json(STATE / "routes.json", active)
        if not candidates:
            print("没有本工具登记且仍存在的临时默认路由；未删除其他路由。")
            return None
        route = choose("选择要撤销的本工具临时路由", candidates,
                       lambda r: "{} → {} metric {}".format(r["interface"], r["gateway"], r["metric"]))
        if not confirm("只移除该路由（不关闭代理/其他上行），继续？"):
            return None
        return self.begin(route, "delete")
