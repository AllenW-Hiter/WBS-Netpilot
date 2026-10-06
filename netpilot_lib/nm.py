"""NetworkManager D-Bus backend: credentials never go into process arguments.

Public APIs used: Settings.AddConnectionUnsaved, Connection.GetSettings/GetSecrets/
Update/Delete, Manager.ActivateConnection/Checkpoint*. D-Bus is imported lazily so
validation, help and unit tests work on systems without NetworkManager.
"""
from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import re
import socket
import struct
import time
import uuid
from typing import Any, Dict, List

from .common import (STATE, UserError, atomic_write, audit, clean_text, file_lock,
                     load_json, require_root, safe_directory, save_json, timestamp)

SERVICE = "org.freedesktop.NetworkManager"
BASE = "/org/freedesktop/NetworkManager"
SETTINGS = BASE + "/Settings"
PROPS = "org.freedesktop.DBus.Properties"
CON_IFACE = SERVICE + ".Settings.Connection"
DEV_IFACE = SERVICE + ".Device"
AC_IFACE = SERVICE + ".Connection.Active"


def dbus_module():
    try:
        import dbus
        return dbus
    except ImportError:
        raise UserError("缺少 python3-dbus；请使用启动脚本和 /usr/bin/python3。")


def encode_typed(value: Any) -> Any:
    """Lossless JSON representation of D-Bus value types (no pickle/eval)."""
    name = type(value).__name__
    if isinstance(value, dict):
        return {"t": "Dictionary", "s": str(getattr(value, "signature", "sv") or "sv"),
                "v": [[encode_typed(k), encode_typed(v)] for k, v in value.items()]}
    if isinstance(value, (bytes, bytearray)):
        return {"t": "ByteArray", "v": list(value)}
    if isinstance(value, (list, tuple)):
        return {"t": "Struct" if name == "Struct" else "Array",
                "s": str(getattr(value, "signature", "v") or "v"),
                "v": [encode_typed(v) for v in value]}
    if isinstance(value, bool) or name == "Boolean":
        return {"t": "Boolean", "v": bool(value)}
    if isinstance(value, int):
        return {"t": name if name in {"Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"} else "Int32", "v": int(value)}
    if isinstance(value, float):
        return {"t": "Double", "v": float(value)}
    if isinstance(value, str):
        return {"t": name if name in ("ObjectPath", "Signature") else "String", "v": str(value)}
    raise UserError("无法备份 D-Bus 数据类型：{}".format(name))


def decode_typed(node: Any, dbus=None, depth: int = 0) -> Any:
    dbus = dbus or dbus_module()
    if depth > 24 or not isinstance(node, dict) or "t" not in node or "v" not in node:
        raise UserError("备份的 D-Bus 数据格式无效。")
    kind, value = node["t"], node["v"]
    if kind == "Dictionary":
        return dbus.Dictionary({decode_typed(k, dbus, depth + 1): decode_typed(v, dbus, depth + 1)
                                for k, v in value}, signature=node["s"])
    if kind == "Array":
        return dbus.Array([decode_typed(v, dbus, depth + 1) for v in value], signature=node["s"])
    if kind == "Struct":
        return dbus.Struct([decode_typed(v, dbus, depth + 1) for v in value])
    if kind == "ByteArray":
        return dbus.ByteArray(bytes(value))
    allowed = {"Boolean", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64",
               "Double", "String", "ObjectPath", "Signature"}
    if kind not in allowed:
        raise UserError("备份包含不允许的 D-Bus 类型。")
    return getattr(dbus, kind)(value)


def settings_to_dbus(settings: Dict[str, Any], dbus=None):
    """Encode our narrow settings schema with the exact NM property signatures."""
    d = dbus or dbus_module()
    outer = {}
    signed = {("connection", "autoconnect-priority"), ("connection", "autoconnect-retries"),
              ("ipv4", "route-metric"), ("ipv4", "dns-priority"),
              ("802-11-wireless-security", "pmf")}
    for section, values in settings.items():
        inner = {}
        for key, value in values.items():
            if (section, key) == ("802-11-wireless", "ssid"):
                val = d.ByteArray(value.encode("utf-8") if isinstance(value, str) else value)
            elif (section, key) == ("ipv4", "dns"):
                val = d.Array([d.UInt32(struct.unpack("=I", socket.inet_aton(v))[0]) for v in value], signature="u")
            elif key == "address-data":
                val = d.Array([d.Dictionary({"address": d.String(v["address"]),
                                            "prefix": d.UInt32(v["prefix"])}, signature="sv") for v in value], signature="a{sv}")
            elif isinstance(value, bool):
                val = d.Boolean(value)
            elif (section, key) == ("ipv4", "route-metric"):
                val = d.Int64(value)
            elif isinstance(value, int):
                val = d.Int32(value) if (section, key) in signed else d.UInt32(value)
            elif isinstance(value, list):
                val = d.Array([d.String(v) for v in value], signature="s")
            elif isinstance(value, str):
                val = d.String(value)
            else:
                raise UserError("不支持的配置字段：{}.{}".format(section, key))
            inner[key] = val
        outer[section] = d.Dictionary(inner, signature="sv")
    return d.Dictionary(outer, signature="sa{sv}")


class NM:
    def __init__(self):
        self.d = dbus_module()
        try:
            self.bus = self.d.SystemBus()
            self.manager = self.interface(BASE, SERVICE)
            self.settings = self.interface(SETTINGS, SERVICE + ".Settings")
            self.version = str(self.properties(BASE, SERVICE)["Version"])
        except self.d.DBusException as exc:
            raise UserError("无法连接 NetworkManager。请检查服务是否运行；不会自动接管 networkd 接口。\n" + clean_text(exc))

    def interface(self, path, interface):
        return self.d.Interface(self.bus.get_object(SERVICE, path), interface)

    def properties(self, path, interface):
        return dict(self.interface(path, PROPS).GetAll(interface))

    def device_path(self, interface: str) -> str:
        try:
            return str(self.manager.GetDeviceByIpIface(interface))
        except self.d.DBusException:
            raise UserError("NetworkManager 没有发现接口：{}".format(interface))

    def profiles(self) -> List[Dict[str, Any]]:
        result = []
        for path in self.settings.ListConnections():
            try:
                settings = self.interface(path, CON_IFACE).GetSettings()
                con = settings["connection"]
                props = self.properties(path, CON_IFACE)
                result.append({"path": str(path), "uuid": str(con["uuid"]), "name": str(con["id"]),
                               "type": str(con["type"]), "interface": str(con.get("interface-name", "")),
                               "autoconnect": bool(con.get("autoconnect", True)),
                               "filename": str(props.get("Filename", "")), "unsaved": bool(props.get("Unsaved", False))})
            except self.d.DBusException:
                continue  # A profile can disappear while enumerating.
        return result

    def profile(self, uuid_value: str):
        for item in self.profiles():
            if item["uuid"] == uuid_value:
                return item
        return None

    def active_uuid(self, interface: str) -> str:
        props = self.properties(self.device_path(interface), DEV_IFACE)
        active = str(props.get("ActiveConnection", "/"))
        if active == "/":
            return ""
        return str(self.properties(active, AC_IFACE).get("Uuid", ""))

    def settings_with_secrets(self, path: str):
        con = self.interface(path, CON_IFACE)
        settings = con.GetSettings()
        errors = []
        for section in ("802-11-wireless-security", "802-1x", "vpn", "pppoe", "gsm", "wireguard"):
            if section not in settings:
                continue
            try:
                secrets = con.GetSecrets(section)
                for name, values in secrets.items():
                    settings.setdefault(name, self.d.Dictionary({}, signature="sv")).update(values)
            except self.d.DBusException:
                errors.append(section)
        return settings, errors

    def backup_profile(self, uuid_value: str) -> str:
        require_root()
        item = self.profile(uuid_value)
        if not item:
            raise UserError("连接已经不存在，无法备份。")
        settings, errors = self.settings_with_secrets(item["path"])
        directory = STATE / "backups" / "profiles"
        safe_directory(directory, root_only=True)
        target = directory / (timestamp() + "-" + uuid.uuid4().hex[:8] + "-" + uuid_value + ".json")
        save_json(target, {"schema": 1, "metadata": item, "secret_errors": errors,
                           "settings": encode_typed(settings)})
        return str(target)

    def checkpoint(self, interfaces: List[str], timeout: int) -> str:
        paths = [self.d.ObjectPath(self.device_path(i)) for i in interfaces]
        try:
            # flags=0: do NOT destroy unrelated checkpoints or delete other apps' new profiles.
            return str(self.manager.CheckpointCreate(self.d.Array(paths, signature="o"),
                                                     self.d.UInt32(timeout), self.d.UInt32(0)))
        except self.d.DBusException as exc:
            raise UserError("不能建立安全检查点，已停止修改（不会无保护地继续）：" + clean_text(exc))

    def checkpoint_exists(self, path: str) -> bool:
        return path in [str(p) for p in self.properties(BASE, SERVICE).get("Checkpoints", [])]

    def rollback(self, path: str) -> Dict[str, int]:
        if not self.checkpoint_exists(path):
            return {}  # Native timeout may already have rolled it back.
        return {str(k): int(v) for k, v in self.manager.CheckpointRollback(path).items()}

    def destroy_checkpoint(self, path: str) -> None:
        if not self.checkpoint_exists(path):
            raise UserError("检查点已失效或已超时回滚，不能提交。请检查现状后重新配置。")
        self.manager.CheckpointDestroy(path)

    def add_unsaved(self, typed_settings) -> str:
        return str(self.settings.AddConnectionUnsaved(typed_settings))

    def activate(self, path: str, interface: str, timeout: int = 65) -> None:
        active = str(self.manager.ActivateConnection(path, self.device_path(interface), "/"))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                state = int(self.properties(active, AC_IFACE)["State"])
            except self.d.DBusException:
                raise UserError("连接激活已结束或被系统回滚。")
            if state == 2:
                return
            if state == 4:
                raise UserError("连接激活失败；请检查网线、Wi-Fi 密码或 AP/驱动能力。")
            time.sleep(0.3)
        raise UserError("等待连接激活超时。")

    def delete(self, uuid_value: str) -> None:
        item = self.profile(uuid_value)
        if item:
            self.interface(item["path"], CON_IFACE).Delete()

    def disconnect(self, interface: str) -> None:
        self.interface(self.device_path(interface), DEV_IFACE).Disconnect()

    def persist(self, uuid_value: str, name: str, autoconnect: bool) -> None:
        item = self.profile(uuid_value)
        if not item:
            raise UserError("试运行配置已不存在，无法保存。")
        settings, errors = self.settings_with_secrets(item["path"])
        if errors:
            raise UserError("无法读取试运行连接的认证信息，拒绝不完整保存。")
        settings["connection"]["id"] = self.d.String(name)
        settings["connection"]["autoconnect"] = self.d.Boolean(autoconnect)
        self.interface(item["path"], CON_IFACE).Update(settings)
        if self.properties(item["path"], CON_IFACE).get("Unsaved", True):
            raise UserError("NetworkManager 未确认配置已持久保存。")

    def set_autoconnect(self, uuid_value: str, value: bool) -> None:
        item = self.profile(uuid_value)
        if not item:
            raise UserError("连接不存在。")
        settings, errors = self.settings_with_secrets(item["path"])
        if errors:
            raise UserError("无法完整读取认证信息；为避免破坏原配置，未修改。")
        settings["connection"]["autoconnect"] = self.d.Boolean(value)
        self.interface(item["path"], CON_IFACE).Update(settings)


class Transactions:
    """Trial profiles stay memory-only and autoconnect=false until confirmation."""
    def __init__(self, nm: NM, timeout: int = 180):
        self.nm = nm
        self.timeout = timeout
        require_root()
        safe_directory(STATE, root_only=True)
        safe_directory(STATE / "pending", root_only=True)

    def path(self, token: str) -> pathlib.Path:
        if not re.fullmatch(r"[a-f0-9]{12}", token):
            raise UserError("事务编号格式错误。")
        return STATE / "pending" / (token + ".json")

    def registry(self):
        return load_json(STATE / "managed.json", {})

    def pending(self):
        result = []
        for path in sorted((STATE / "pending").glob("*.json")):
            record = load_json(path)
            if record and record.get("status") == "pending":
                result.append(record)
        return result

    def ensure_idle(self):
        for record in self.pending():
            if self.nm.checkpoint_exists(record["checkpoint"]):
                raise UserError("已有待确认事务 {}；请先 confirm 或 rollback。".format(record["token"]))
            self.rollback(record["token"])

    def begin(self, interface: str, action: str, metadata=None):
        self.ensure_idle()
        old_uuid = self.nm.active_uuid(interface)
        backup = self.nm.backup_profile(old_uuid) if old_uuid else ""
        checkpoint = self.nm.checkpoint([interface], self.timeout)
        token = uuid.uuid4().hex[:12]
        record = {"schema": 1, "kind": "network", "token": token, "status": "pending",
                  "time": timestamp(), "created": time.time(), "checkpoint": checkpoint,
                  "interface": interface, "action": action, "old_uuid": old_uuid,
                  "old_backup": backup, "new_uuid": "", "timeout": self.timeout,
                  "metadata": metadata or {}}
        try:
            save_json(self.path(token), record)
        except Exception:
            self.nm.rollback(checkpoint)
            raise
        print("\n安全事务编号：{}；未确认将在 {} 秒后由 NetworkManager 回滚。".format(token, self.timeout))
        print("SSH 换地址后，可在新终端运行：./network_setup.sh confirm " + token)
        print("立即撤销：./network_setup.sh rollback " + token)
        return record

    def stage(self, typed_settings, interface: str, name: str, autoconnect: bool,
              slot: str, public_plan: Dict[str, Any]):
        record = self.begin(interface, "new", {"name": name, "autoconnect": autoconnect,
                                               "slot": slot, "plan": public_plan})
        token = record["token"]
        new_uuid = str(typed_settings["connection"]["uuid"])
        record["new_uuid"] = new_uuid
        save_json(self.path(token), record)  # Intent recorded BEFORE adding profile.
        try:
            path = self.nm.add_unsaved(typed_settings)
            self.nm.activate(path, interface)
        except BaseException:
            self.rollback(token)
            raise
        return record

    def activate_existing(self, uuid_value: str, interface: str):
        item = self.nm.profile(uuid_value)
        if not item:
            raise UserError("连接不存在。")
        record = self.begin(interface, "activate", {"target_uuid": uuid_value})
        try:
            self.nm.activate(item["path"], interface)
        except BaseException:
            self.rollback(record["token"])
            raise
        return record

    def disconnect(self, interface: str):
        record = self.begin(interface, "disconnect")
        try:
            self.nm.disconnect(interface)
        except BaseException:
            self.rollback(record["token"])
            raise
        return record

    def commit(self, token: str):
        with file_lock(STATE / "transaction.lock"):
            record = load_json(self.path(token))
            if not record:
                raise UserError("事务不存在。")
            if record["status"] == "committed":
                return record
            if record["status"] != "pending":
                raise UserError("此事务已结束，不能再次提交。")
            if record["action"] == "new" and self.nm.active_uuid(record["interface"]) != record["new_uuid"]:
                raise UserError("目标试运行连接不是当前活动连接，拒绝保存。")
            if record["action"] == "activate" and self.nm.active_uuid(record["interface"]) != record["metadata"]["target_uuid"]:
                raise UserError("当前活动连接已不是选定连接，拒绝提交。")
            if record["action"] == "disconnect" and self.nm.active_uuid(record["interface"]):
                raise UserError("该接口已被其他程序重新连接，拒绝把它当作断开成功。")
            # Destroy first: no unconfirmed persistent/autoconnect profile exists.
            # If disk persistence then fails, retain the prior saved profile and expose
            # the 'active-not-saved' result rather than claiming successful persistence.
            self.nm.destroy_checkpoint(record["checkpoint"])
            record["status"] = "confirmed-not-saved" if record["action"] == "new" else "committed"
            save_json(self.path(token), record)
            if record["action"] == "new":
                meta = record["metadata"]
                self.nm.persist(record["new_uuid"], meta["name"], meta["autoconnect"])
                registry = self.registry()
                old_managed = [u for u, m in registry.items()
                               if m.get("slot") == meta["slot"] and u != record["new_uuid"]]
                registry[record["new_uuid"]] = dict(meta, token=token)
                save_json(STATE / "managed.json", registry)
                # Remove only OUR older profiles for this slot, after the new one is saved.
                # Original user profiles are never silently deleted or modified.
                cleanup_errors = []
                for old in old_managed:
                    try:
                        if self.nm.profile(old):
                            self.nm.backup_profile(old)
                            self.nm.delete(old)
                        registry.pop(old, None)
                    except Exception:
                        cleanup_errors.append(old)
                save_json(STATE / "managed.json", registry)
                record["cleanup_warnings"] = cleanup_errors
                record["status"] = "committed"
                save_json(self.path(token), record)
            audit("network-commit", {"token": token, "interface": record["interface"], "action": record["action"]})
            return record

    def rollback(self, token: str):
        with file_lock(STATE / "transaction.lock"):
            record = load_json(self.path(token))
            if not record:
                raise UserError("事务不存在。")
            if record["status"] in ("committed", "confirmed-not-saved"):
                raise UserError("原检查点已提交；请用备份恢复或重新激活原连接，不能冒充自动回滚。")
            if record["status"] == "rolled-back":
                return record
            checkpoint_was_present = self.nm.checkpoint_exists(record["checkpoint"])
            result = self.nm.rollback(record["checkpoint"])
            failures = {k: v for k, v in result.items() if v != 0}
            record["rollback_result"] = result
            if failures:
                record["status"] = "rollback-needs-attention"
                save_json(self.path(token), record)
                raise UserError("系统报告部分接口恢复失败；请通过本机/串口检查。结果：{}".format(failures))
            if not checkpoint_was_present and self.nm.active_uuid(record["interface"]) != record.get("old_uuid", ""):
                record["status"] = "rollback-needs-attention"
                save_json(self.path(token), record)
                raise UserError("检查点已消失，且活动连接未恢复为原 UUID；不能确认超时恢复成功，请检查现状。")
            new_uuid = record.get("new_uuid")
            if new_uuid:
                # Do not delete a still-active profile if the checkpoint disappeared
                # due to an external actor rather than the expected timer.
                if self.nm.active_uuid(record["interface"]) == new_uuid:
                    record["status"] = "rollback-needs-attention"
                    save_json(self.path(token), record)
                    raise UserError("检查点已消失但试运行连接仍活动；未擅自删除，请检查现状。")
                self.nm.delete(new_uuid)
            record["status"] = "rolled-back"
            save_json(self.path(token), record)
            audit("network-rollback", {"token": token, "results": result})
            return record
