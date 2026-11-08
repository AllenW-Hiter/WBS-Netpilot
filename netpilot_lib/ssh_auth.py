"""OpenSSH askpass bridge: credentials exist only in memory and a private socket."""
from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import struct
import sys
import tempfile
import threading

from .common import UserError, clean_text, confirm

ASKPASS = '''import json, os, socket, sys
with socket.socket(socket.AF_UNIX) as sock:
    sock.settimeout(180)
    sock.connect(os.environ["NETPILOT_ASKPASS_SOCKET"])
    request = {"prompt": sys.argv[1] if len(sys.argv) > 1 else "", "kind": os.environ.get("SSH_ASKPASS_PROMPT", "")}
    sock.sendall((json.dumps(request) + "\\n").encode())
    with sock.makefile("rb") as stream:
        answer = json.loads(stream.readline(65536))
    if answer.get("ok"):
        sys.stdout.write(answer["value"] + "\\n")
    else:
        sys.exit(1)
'''


class PasswordBridge:
    """Executable helper has no secret; authenticate socket callers by local uid."""

    def __init__(self, password, trust_prompt=None):
        if not isinstance(password, str) or len(password) > 4096 or any(c in password for c in "\r\n\0"):
            raise UserError("SSH 密码格式无效。")
        self.password = password
        self.trust_prompt = trust_prompt or self.confirm_host
        self.used = False
        self.stopping = threading.Event()

    @staticmethod
    def confirm_host(prompt):
        if not sys.stdin.isatty():
            return False
        print(clean_text(prompt))
        return confirm("核对服务器主机指纹后，信任本次首次连接？")

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="netpilot-auth-")
        root = pathlib.Path(self.temp.name)
        self.helper = root / "askpass"
        self.helper.write_text("#!" + sys.executable + "\n" + ASKPASS)
        self.helper.chmod(0o700)
        self.socket_path = root / "auth.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.listener.listen(2)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def environment(self):
        return dict(os.environ, SSH_ASKPASS=str(self.helper), SSH_ASKPASS_REQUIRE="force",
                    NETPILOT_ASKPASS_SOCKET=str(self.socket_path), LC_ALL="C", LANG="C")

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                answer = {"ok": False}
                try:
                    if hasattr(socket, "SO_PEERCRED"):
                        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if uid != os.geteuid():
                            continue
                    with connection.makefile("rb") as stream:
                        request = json.loads(stream.readline(8192))
                    prompt = request.get("prompt", "")
                    if not isinstance(prompt, str):
                        continue
                    if ("fingerprint" in prompt.lower() or "yes/no" in prompt.lower()) and self.trust_prompt(prompt):
                        answer = {"ok": True, "value": "yes"}
                    elif re.search(r"password[^\n]*:\s*$", prompt, re.I) and not self.used and self.password:
                        self.used = True
                        answer = {"ok": True, "value": self.password}
                    connection.sendall((json.dumps(answer) + "\n").encode())
                except Exception:
                    # Never echo the prompt, credential, or helper communication on errors.
                    continue

    def __exit__(self, *args):
        self.password = ""
        self.stopping.set()
        self.listener.close()
        self.thread.join(timeout=0.5)
        self.temp.cleanup()
