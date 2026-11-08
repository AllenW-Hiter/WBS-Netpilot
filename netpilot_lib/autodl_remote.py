"""Self-contained remote bootstrap, transported on SSH stdin, never with credentials."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import sys
import tarfile
import tempfile

RESULT = "NETPILOT_AUTODL_RESULT="


def private_directory(path):
    if path.is_symlink() or (path.exists() and path.stat().st_uid != os.geteuid()):
        raise RuntimeError("运行目录归属或类型异常，未覆盖。")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def file_name(name):
    path = pathlib.PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and
            (name in ("netpilot.py", "network_setup.sh") or
             (len(path.parts) == 2 and path.parts[0] == "netpilot_lib" and path.suffix == ".py") or
             (len(path.parts) == 2 and path.parts[0] == "examples" and path.suffix == ".json")))


def deploy(payload):
    files = payload["files"]
    if not isinstance(files, dict) or not files or any(not file_name(name) for name in files):
        raise RuntimeError("运行文件清单无效。")
    archive = base64.b64decode(payload["archive"], validate=True)
    if len(archive) > 4 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != payload["archive_sha256"]:
        raise RuntimeError("运行文件包校验失败。")
    deployment = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]
    root = pathlib.Path.home() / ".local" / "share" / "netpilot" / "autodl-runtime"
    for parent in (root.parent.parent.parent, root.parent.parent, root.parent, root):
        if parent.is_symlink():
            raise RuntimeError("拒绝使用符号链接运行目录。")
    private_directory(root)
    target = root / deployment

    def verify():
        marker = target / ".netpilot-owned"
        if target.is_symlink() or target.stat().st_uid != os.geteuid() or target.stat().st_mode & 0o077 or marker.is_symlink() or not marker.is_file() or marker.read_text() != deployment + "\n":
            raise RuntimeError("同名运行目录没有工具归属，未覆盖。")
        for name, digest in files.items():
            path = target / name
            if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("已安装的运行文件被改变，未覆盖。")

    if target.exists() or target.is_symlink():
        verify()
        return target
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if {entry.name for entry in members} != set(files) or len(members) != len(files):
                raise RuntimeError("运行文件包与清单不一致。")
            for entry in members:
                if not entry.isfile() or entry.size > 2 * 1024 * 1024:
                    raise RuntimeError("运行文件包含不支持的条目。")
                content = bundle.extractfile(entry).read()
                if hashlib.sha256(content).hexdigest() != files[entry.name]:
                    raise RuntimeError("运行文件内容校验失败。")
                path = staging / entry.name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(content)
                path.chmod(0o700 if entry.name == "network_setup.sh" else 0o600)
        (staging / ".netpilot-owned").write_text(deployment + "\n")
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    verify()
    return target


def loopback_listener(port):
    listeners = []
    for name in ("tcp", "tcp6"):
        for line in (pathlib.Path("/proc/net") / name).read_text().splitlines()[1:]:
            fields = line.split()
            address, number = fields[1].split(":")
            if int(number, 16) == port and fields[3] == "0A":
                listeners.append((name, address))
    if listeners != [("tcp", "0100007F")]:
        raise RuntimeError("远端监听不是唯一的 IPv4 回环监听；请检查端口和 GatewayPorts 策略。")


def execute(payload):
    for key in ("SUDO_UID", "SUDO_GID", "SUDO_USER"):
        os.environ.pop(key, None)
    runtime = deploy(payload)
    sys.path.insert(0, str(runtime))
    from netpilot_lib.proxy_profiles import ProfileManager, fingerprint, validate_profile
    from netpilot_lib.common import https_test, load_json, save_json, safe_directory
    from netpilot_lib.proxy import validate_proxy_config
    from netpilot_lib.proxy_effects import regular_file
    manager = ProfileManager()
    plan = payload["plan"]
    owner_id = payload["owner_id"]
    if not isinstance(owner_id, str) or not re.fullmatch(r"[a-f0-9]{32}", owner_id):
        raise RuntimeError("AutoDL 归属编号无效。")
    profile = validate_profile(dict(schema=2, source="ssh", apt=plan["apt"], git=plan["git"],
        proxy=dict(schema=1, scheme=plan["scheme"], host="127.0.0.1", port=plan["remote_port"],
                   username="", password="", no_proxy="localhost,127.0.0.1,::1")))
    owner_file = manager.directory / "autodl-owner.json"
    regular_file(owner_file, os.geteuid(), private=True)
    owner = load_json(owner_file)
    if payload["action"] == "restore":
        if not owner or owner.get("owner_id") != owner_id:
            return {"restored": True, "owned_profile": False}
        session = manager.session()
        if session and session["source"] == "ssh" and session["fingerprint"] != owner.get("fingerprint"):
            raise RuntimeError("远端 SSH 联动已被其他配置改变，未撤销其他归属；请在远端检查后关闭。")
        manager.disable("ssh")
        owner_file.unlink()
        return {"restored": True, "owned_profile": True}
    if owner and owner.get("owner_id") != owner_id:
        raise RuntimeError("远端已有其他 AutoDL 配置归属；请由原管理端停止后再配置。")
    safe_directory(manager.directory)
    cli = manager.directory / "autodl-cli"
    regular_file(cli, os.geteuid(), private=True)
    cli_marker = "#!/bin/sh\n# Managed by NetPilot AutoDL\n"
    if cli.exists() and not cli.read_text().startswith(cli_marker):
        raise RuntimeError("远端管理入口是其他文件，未覆盖。")
    from netpilot_lib.common import atomic_write
    atomic_write(cli, cli_marker + "exec " + shlex.join([sys.executable, str(runtime / "netpilot.py")]) + ' "$@"\n', 0o700)
    loopback_listener(plan["remote_port"])
    result = https_test(plan["test_url"], validate_proxy_config(profile["proxy"]))
    if not result["transport_ok"]:
        raise RuntimeError("远端 HTTPS 传输失败（curl={}，HTTP={}），尚未安装开关；连接保留后可用 autodl-start 重试。".format(result.get("curl_exit", "未知"), result.get("http_status", "未知")))
    if plan["apt"] and not shutil.which("apt-config"):
        raise RuntimeError("远端缺少 apt-config，无法选择 APT 联动。")
    if plan["git"] and not shutil.which("git"):
        raise RuntimeError("远端缺少 Git，无法选择 Git 联动。")
    session = manager.session()
    if session and (session["source"] != "ssh" or session["status"] != "active" or session["fingerprint"] != fingerprint(profile)):
        raise RuntimeError("远端存在不兼容的联动会话，请先执行原来源的 _proxy_off。")
    safe_directory(manager.directory)
    receipt = dict(schema=1, owner_id=owner_id, fingerprint=fingerprint(profile), phase="installing")
    save_json(owner_file, receipt)
    manager.install(profile)
    receipt["phase"] = "configured"
    save_json(owner_file, receipt)
    return {"runtime": str(runtime), "python": sys.executable, "cli": str(cli), "http_status": result["http_status"],
            "endpoint": validate_proxy_config(profile["proxy"]), "apt": plan["apt"], "git": plan["git"],
            "manual": True}


def run_encoded(encoded):
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True))
        result = {"ok": True, "result": execute(payload)}
    except Exception as exc:
        # No remote tracebacks, arbitrary file contents, or credential values in local logs.
        from_context = str(exc) if isinstance(exc, RuntimeError) else "远端配置未全部完成，请检查端口、工具归属及私有备份。"
        result = {"ok": False, "error": from_context}
    print(RESULT + json.dumps(result, ensure_ascii=False), flush=True)


def bootstrap(encoded, *, scheme, port, install_deps, git_selected, action):
    """All interpolated shell values are validated enums, integers or base64."""
    if scheme not in ("http", "https", "socks5h") or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid bootstrap proxy")
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", encoded):
        raise ValueError("Invalid bootstrap payload")
    installer = pathlib.Path(__file__).read_text()
    prefix = '''set -eu
export LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1
@RESTORE_PROBE@
PY=""
find_python() {
  for candidate in /usr/bin/python3 "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3" /opt/conda/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PY="$candidate"
      return
    fi
  done
}
find_python
missing=""
[ -n "$PY" ] || missing="python3"
@CURL@
@GIT@
if [ -n "$missing" ]; then
  if [ @INSTALL@ != yes ] || [ "$(id -u)" != 0 ] || ! command -v apt-get >/dev/null; then
    printf 'NETPILOT_AUTODL_RESULT={"ok":false,"error":"远端缺少 Python 3.10+/curl/Git；可选择允许安装依赖，或由管理员准备。"}\\n'
    exit 1
  fi
  set -- -o DPkg::Lock::Timeout=30 -o Acquire::Retries=1 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::http::Proxy=@PROXY@ -o Acquire::https::Proxy=@PROXY@
  printf '正在准备远端缺失依赖。\\n'
  if ! DEBIAN_FRONTEND=noninteractive apt-get "$@" update >/dev/null 2>&1 || ! DEBIAN_FRONTEND=noninteractive apt-get "$@" install --no-install-recommends --no-upgrade -y $missing ca-certificates >/dev/null 2>&1; then
    printf 'NETPILOT_AUTODL_RESULT={"ok":false,"error":"远端依赖安装失败；未写持久 APT 代理，也未安装代理开关。"}\\n'
    exit 1
  fi
  find_python
fi
if [ -z "$PY" ]; then
  printf 'NETPILOT_AUTODL_RESULT={"ok":false,"error":"未找到 Python 3.10+，已停止。"}\\n'
  exit 1
fi
'''.replace("@RESTORE_PROBE@", 'if [ ! -e "$HOME/.config/netpilot/autodl-owner.json" ]; then printf \'NETPILOT_AUTODL_RESULT={"ok":true,"result":{"restored":true,"owned_profile":false}}\\n\'; exit 0; fi' if action == "restore" else "").replace(
        "@CURL@", 'command -v curl >/dev/null || missing="$missing curl"' if action == "setup" else "").replace(
        "@GIT@", 'command -v git >/dev/null || missing="$missing git"' if git_selected and action == "setup" else "").replace(
        "@INSTALL@", "yes" if install_deps and action == "setup" else "no").replace(
        "@PROXY@", "{}://127.0.0.1:{}".format(scheme, port))
    marker = "NETPILOT_BOOTSTRAP_" + hashlib.sha256(installer.encode()).hexdigest()[:20]
    return prefix + '"$PY" - <<\'' + marker + "'\n" + installer + "\nrun_encoded(" + json.dumps(encoded) + ")\n" + marker + "\n"
