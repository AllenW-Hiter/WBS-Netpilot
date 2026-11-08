"""Compatibility names for the former Codex-only SSH proxy feature."""
from .ssh_tunnel import SSHTunnel
from .proxy_profiles import ProfileManager

class CodexProxy:
    def configure_tunnel(self): return SSHTunnel().configure()
    def start_tunnel(self): return SSHTunnel().start()
    def configure_proxy(self): return ProfileManager().configure("ssh")
    def test_proxy(self): return ProfileManager().test("ssh")
    def menu(self):
        from .cli import ssh_menu
        return ssh_menu()
