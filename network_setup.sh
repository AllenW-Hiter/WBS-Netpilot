#!/usr/bin/env bash
# The menu is unprivileged; Python dispatches dependencies/sudo per operation.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
if [[ ! -x /usr/bin/python3 ]]; then
  echo "缺少 /usr/bin/python3；请管理员先准备系统 Python。" >&2
  exit 1
fi
exec /usr/bin/python3 "$ROOT/netpilot.py" "$@"
