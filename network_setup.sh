#!/usr/bin/env bash
# NetPilot bootstrap. No remote shell scripts, pip installs, or automatic upgrades.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
PY=/usr/bin/python3
MODE="${1:-menu}"
# Any preview/help flag suppresses bootstrap writes, even if its position is invalid.
# The Python parser will reject misplaced/unknown arguments rather than ignore them.
for arg in "$@"; do
  case "$arg" in
    --dry-run|--help|-h|--version|--check|--status|--diagnose|--demo|--self-test) MODE="$arg"; break ;;
    proxy-run|proxy-apt-on|proxy-apt-off|proxy-cleanup) MODE="$arg"; break ;;
  esac
done

# These entries do not install packages or escalate privileges in the bootstrap.
# Proxy functions explicitly invoke their APT helper with sudo; cleanup needs sudo.
case "$MODE" in
  --help|-h|help|--version|--status|--diagnose|--demo|--dry-run|--check|proxy-run|proxy-apt-on|proxy-apt-off|proxy-cleanup|--self-test)
    if [[ ! -x "$PY" ]]; then echo "缺少 /usr/bin/python3；此只读模式不会自动安装。" >&2; exit 1; fi
    exec "$PY" "$ROOT/netpilot.py" "$@" ;;
esac

if [[ "$(uname -s)" != Linux ]]; then echo "本工具仅支持 Linux。" >&2; exit 1; fi
if [[ ! -r /etc/os-release ]]; then echo "无法识别发行版。" >&2; exit 1; fi
# shellcheck source=/dev/null
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *ubuntu*|*debian*) ;;
  *) echo "本版自动安装仅支持 Ubuntu/Debian 系列；已停止。" >&2; exit 1 ;;
esac

as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo -- "$@"; fi; }
if [[ $EUID -ne 0 ]] && ! command -v sudo >/dev/null; then
  echo "缺少 sudo；请管理员安装，或在本机 root 终端运行。" >&2; exit 1
fi

missing=()
for package in python3 python3-dbus network-manager iproute2 dbus systemd; do
  if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != "install ok installed" ]]; then
    missing+=("$package")
  fi
done
if ((${#missing[@]})); then
  printf '缺少软件包：%s\n' "${missing[*]}"
  echo "将使用系统 APT 软件源安装；不会先断网，不执行系统升级。"
  if [[ " ${missing[*]} " == *" network-manager "* ]]; then
    echo "警告：安装 NetworkManager 可能启动服务；本工具不会自动改 Netplan renderer。"
    echo "当前通过 SSH 操作且没有恢复通道时，请取消并在本机/串口完成安装。"
  fi
  read -r -p "确认现在联网安装？[y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || exit 0
  if ! as_root apt-get -o DPkg::Lock::Timeout=60 update ||
     ! as_root apt-get -o DPkg::Lock::Timeout=60 install --no-install-recommends -y "${missing[@]}"; then
    echo "安装失败。请先建立上行网络或准备离线包；未进行网络配置。" >&2
    exit 1
  fi
fi
if ! "$PY" -c 'import dbus' >/dev/null 2>&1; then
  echo "系统 Python 无法导入 dbus；请检查 python3-dbus，勿用 Conda/venv 替代此入口。" >&2; exit 1
fi
# Launch privileged operations as the operator's explicit local utility. Carry only
# the SSH peer string, not the complete environment or user-supplied proxy secrets.
if [[ $EUID -eq 0 ]]; then
  exec "$PY" "$ROOT/netpilot.py" "$@"
else
  exec sudo -- /usr/bin/env "NETPILOT_SSH_CONNECTION=${SSH_CONNECTION:-}" \
    "$PY" "$ROOT/netpilot.py" "$@"
fi
