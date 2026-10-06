#!/usr/bin/env python3
"""Standalone root-owned rollback helper for EXACT NetPilot temporary routes.

Copied to /var/lib/netpilot/route_guard.py before a systemd transient timer is used.
No package imports, shell evaluation, user-supplied executable paths, or broad route
flushes. The helper only reads fixed-schema root-owned receipts in a fixed directory.
"""
from __future__ import annotations
import fcntl
import ipaddress
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

BASE = pathlib.Path("/var/lib/netpilot")
PROTO = 242


def validated_route(data):
    if not isinstance(data, dict):
        raise ValueError("invalid route receipt")
    interface = data.get("interface", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) or interface.startswith("-"):
        raise ValueError("invalid interface")
    gateway = str(ipaddress.IPv4Address(data["gateway"]))
    metric = data["metric"]
    if isinstance(metric, bool) or not isinstance(metric, int) or not 1 <= metric <= 32000:
        raise ValueError("invalid metric")
    if data.get("protocol") != PROTO:
        raise ValueError("foreign route protocol")
    return {"interface": interface, "gateway": gateway, "metric": metric, "protocol": PROTO}


def route_command(action, data):
    if action not in ("add", "delete"):
        raise ValueError("invalid route operation")
    r = validated_route(data)
    return ["/usr/sbin/ip" if pathlib.Path("/usr/sbin/ip").exists() else "/usr/bin/ip",
            "-4", "route", action, "default", "via", r["gateway"], "dev", r["interface"],
            "metric", str(r["metric"]), "proto", str(PROTO), "table", "main"]


def atomic(path, data):
    fd, temp = tempfile.mkstemp(prefix=".route-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def expire(token):
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ValueError("root and a valid token are required")
    directory = BASE / "route-actions"
    path = directory / (token + ".json")
    for item in (BASE, directory, path):
        if item.is_symlink() or item.stat().st_uid != 0:
            raise ValueError("untrusted receipt path")
    lock_path = directory / (token + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "pending":
            return
        boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if data.get("boot_id") != boot:
            data["status"] = "expired-after-reboot"
            atomic(path, data)
            return
        operation = data.get("operation")
        if operation not in ("add", "delete"):
            raise ValueError("invalid original operation")
        reverse = "delete" if operation == "add" else "add"
        result = subprocess.run(route_command(reverse, data["route"]),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                timeout=15, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        # Verify the exact tagged route. EEXIST alone does not prove the old
        # gateway was restored: another route with the same metric may exist.
        query = route_command("add", data["route"])[0:1] + ["-j", "-4", "route", "show", "table", "main", "default", "proto", str(PROTO)]
        state = subprocess.run(query, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=15, check=False,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        present = False
        if state.returncode == 0:
            r = validated_route(data["route"])
            present = any(item.get("dev") == r["interface"] and item.get("gateway") == r["gateway"]
                          and int(item.get("metric", 0)) == r["metric"] for item in json.loads(state.stdout or "[]"))
        expected = reverse == "add"
        data["status"] = "rolled-back" if state.returncode == 0 and present == expected else "rollback-needs-attention"
        data["rollback_exit"] = result.returncode
        data["rollback_error"] = result.stderr[:1000]
        atomic(path, data)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--expire":
            raise ValueError("usage: route_guard.py --expire TOKEN")
        expire(sys.argv[2])
    except Exception as error:
        print("NetPilot route rollback failed: " + str(error), file=sys.stderr)
        sys.exit(1)
