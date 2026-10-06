import copy
import json
import pathlib
import unittest
from netpilot_lib.common import (UserError, split_nmcli, validate_cidr, validate_gateway,
    validate_pool, validate_psk, validate_ssid, validate_dns, validate_interface,
    overlapping_routes, mask_secrets, clean_text)
from netpilot_lib.network import validate_plan, build_settings, Device, require_distinct_phys
from netpilot_lib.route_guard import route_command, validated_route, PROTO
from netpilot_lib.proxy import proxy_url, proxy_environment, apt_text

ROOT = pathlib.Path(__file__).resolve().parents[1]

def example(name="client_static.json"):
    return json.loads((ROOT / "examples" / name).read_text())


class ParsingTests(unittest.TestCase):
    def test_escaped_colon(self):
        self.assertEqual(split_nmcli(r"home\:lab:uuid:wifi:wlan0"), ["home:lab", "uuid", "wifi", "wlan0"])

    def test_escaped_backslash(self):
        self.assertEqual(split_nmcli(r"home\\lab:x"), [r"home\lab", "x"])

    def test_empty_fields(self):
        self.assertEqual(split_nmcli("a::b:"), ["a", "", "b", ""])

    def test_chinese_and_spaces(self):
        self.assertEqual(split_nmcli("有线连接 1:id:ethernet:eth0")[0], "有线连接 1")

    def test_terminal_controls_removed(self):
        self.assertNotIn("\x1b", clean_text("SSID\x1b[31m"))


class AddressTests(unittest.TestCase):
    def test_valid_cidr(self):
        self.assertEqual(str(validate_cidr("192.168.1.40/24").network), "192.168.1.0/24")

    def test_network_address_rejected(self):
        with self.assertRaises(UserError): validate_cidr("192.168.1.0/24")

    def test_broadcast_rejected(self):
        with self.assertRaises(UserError): validate_cidr("192.168.1.255/24")

    def test_missing_prefix_rejected(self):
        with self.assertRaises(UserError): validate_cidr("192.168.1.40")

    def test_loopback_rejected(self):
        with self.assertRaises(UserError): validate_cidr("127.0.0.1/24")

    def test_gateway_same_subnet(self):
        self.assertEqual(validate_gateway("192.168.1.40/24", "192.168.1.20"), "192.168.1.20")

    def test_gateway_outside(self):
        with self.assertRaises(UserError): validate_gateway("192.168.1.40/24", "192.168.2.20")

    def test_gateway_is_self(self):
        with self.assertRaises(UserError): validate_gateway("192.168.1.40/24", "192.168.1.40")

    def test_pool_valid(self):
        self.assertEqual(validate_pool("192.168.1.20/24", "192.168.1.100", "192.168.1.199"), "192.168.1.100,192.168.1.199")

    def test_pool_contains_gateway(self):
        with self.assertRaises(UserError): validate_pool("192.168.1.20/24", "192.168.1.10", "192.168.1.30")

    def test_pool_reverse(self):
        with self.assertRaises(UserError): validate_pool("192.168.1.20/24", "192.168.1.199", "192.168.1.100")

    def test_pool_outside(self):
        with self.assertRaises(UserError): validate_pool("192.168.1.20/24", "192.168.2.100", "192.168.2.199")

    def test_dns_deduplicates(self):
        self.assertEqual(validate_dns("1.1.1.1, 1.1.1.1 192.168.1.20"), ["1.1.1.1", "192.168.1.20"])

    def test_dns_no_domain(self):
        with self.assertRaises(UserError): validate_dns("dns.example.com")

    def test_interface_injection_rejected(self):
        with self.assertRaises(UserError): validate_interface("eth0; reboot")

    def test_interface_option_rejected(self):
        with self.assertRaises(UserError): validate_interface("--all")

    def test_interface_wrong_type(self):
        with self.assertRaises(UserError): validate_interface(None)


class ConflictTests(unittest.TestCase):
    def test_other_interface_overlap(self):
        addresses = [{"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.2", "prefixlen": 24}]}]
        self.assertTrue(overlapping_routes("192.168.1.20/24", "eth0", addresses, []))

    def test_same_interface_not_false_conflict(self):
        addresses = [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.2", "prefixlen": 24}]}]
        self.assertEqual(overlapping_routes("192.168.1.20/24", "eth0", addresses, []), [])

    def test_docker_subnet_overlap(self):
        self.assertTrue(overlapping_routes("172.17.0.20/16", "eth0", [], [{"dst": "172.17.0.0/16", "dev": "docker0"}]))

    def test_nonmain_vpn_route_overlap(self):
        self.assertTrue(overlapping_routes("192.168.50.20/24", "eth0", [], [{"dst": "192.168.0.0/16", "dev": "tun0", "table": 100}]))

    def test_default_routes_not_subnet_conflicts(self):
        routes = [{"dst": "default", "dev": "wlan0"}, {"dst": "0.0.0.0/1", "dev": "tun0"}]
        self.assertEqual(overlapping_routes("192.168.50.20/24", "eth0", [], routes), [])

    def test_distinct_wireless_phys(self):
        require_distinct_phys(Device("wlan0", "wifi", "connected", "A", True, "phy0"),
                              Device("wlan1", "wifi", "disconnected", "", True, "phy1"))

    def test_same_phy_rejected(self):
        with self.assertRaises(UserError):
            require_distinct_phys(Device("wlan0", "wifi", "connected", "A", True, "phy0"),
                                  Device("wlan1", "wifi", "disconnected", "", True, "phy0"))

    def test_unknown_phy_rejected(self):
        with self.assertRaises(UserError):
            require_distinct_phys(Device("wlan0", "wifi", "connected", "A", True),
                                  Device("wlan1", "wifi", "disconnected", "", True))


class WirelessTests(unittest.TestCase):
    def test_short_password_rejected(self):
        with self.assertRaises(UserError): validate_psk("short")

    def test_password_spaces_preserved(self):
        self.assertEqual(validate_psk(" pass word "), " pass word ")

    def test_hex_psk(self):
        self.assertEqual(validate_psk("a" * 64), "a" * 64)

    def test_nonhex_64_rejected(self):
        with self.assertRaises(UserError): validate_psk("g" * 64)

    def test_ssid_utf8_bytes(self):
        with self.assertRaises(UserError): validate_ssid("中" * 11)

    def test_ssid_control(self):
        with self.assertRaises(UserError): validate_ssid("x\ny")

    def test_ssid_unicode_valid(self):
        self.assertEqual(validate_ssid("实验室"), "实验室")


class PlanTests(unittest.TestCase):
    def test_all_examples_validate(self):
        for file in (ROOT / "examples").glob("*.json"):
            with self.subTest(file=file.name): validate_plan(json.loads(file.read_text()))

    def test_client_on_demand_no_saved_gateway(self):
        settings = build_settings(example())
        self.assertNotIn("gateway", settings["ipv4"])
        self.assertTrue(settings["ipv4"]["never-default"])

    def test_staged_profile_cannot_autoconnect(self):
        self.assertFalse(build_settings(example())["connection"]["autoconnect"])

    def test_client_never_shared(self):
        p = example(); p["method"] = "shared"
        with self.assertRaises(UserError): validate_plan(p)

    def test_host_no_persistent_gateway(self):
        p = example("host_shared.json"); p["gateway"] = "192.168.1.1"
        with self.assertRaises(UserError): validate_plan(p)

    def test_unknown_field_rejected(self):
        p = example(); p["run_shell"] = "bad"
        with self.assertRaises(UserError): validate_plan(p)

    def test_password_not_in_portable_plan(self):
        p = example("dual_wifi_host.json"); p["wifi"]["password"] = "secret123"
        with self.assertRaises(UserError): validate_plan(p)

    def test_client_ipv6_explicit(self):
        self.assertEqual(build_settings(example())["ipv6"]["method"], "disabled")

    def test_wpa2_no_wep_or_tkip(self):
        sec = build_settings(example("dual_wifi_host.json"), "example-pass")["802-11-wireless-security"]
        self.assertEqual(sec["proto"], ["rsn"])
        self.assertEqual(sec["pairwise"], ["ccmp"])
        self.assertEqual(sec["key-mgmt"], "wpa-psk")

    def test_wpa3_requires_pmf(self):
        p = example("dual_wifi_host.json"); p["wifi"]["security"] = "wpa3"
        sec = build_settings(p, "example-pass")["802-11-wireless-security"]
        self.assertEqual(sec["key-mgmt"], "sae")
        self.assertEqual(sec["pmf"], 3)

    def test_no_implicit_dns_catchall(self):
        self.assertNotIn("dns-search", build_settings(example())["ipv4"])

    def test_temp_and_saved_gateway_mutually_exclusive(self):
        p = example(); p["gateway"] = "192.168.1.20"
        with self.assertRaises(UserError): validate_plan(p)

    def test_dhcp_no_static_fields(self):
        p = example(); p["method"] = "auto"
        with self.assertRaises(UserError): validate_plan(p)

    def test_autoconnect_requires_bool(self):
        p = example(); p["autoconnect"] = "yes"
        with self.assertRaises(UserError): validate_plan(p)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.route = {"interface": "end1", "gateway": "192.168.1.20", "metric": 100, "protocol": PROTO}

    def test_exact_delete(self):
        args = route_command("delete", self.route)
        self.assertIn("delete", args)
        self.assertIn("192.168.1.20", args)
        self.assertIn("end1", args)
        self.assertIn(str(PROTO), args)
        self.assertNotIn("flush", args)
        self.assertNotIn("replace", args)

    def test_route_protocol_ownership(self):
        self.route["protocol"] = 4
        with self.assertRaises(ValueError): validated_route(self.route)

    def test_route_interface_injection(self):
        self.route["interface"] = "end1; reboot"
        with self.assertRaises(ValueError): route_command("add", self.route)

    def test_route_bad_metric(self):
        self.route["metric"] = -1
        with self.assertRaises(ValueError): validated_route(self.route)

    def test_route_cannot_replace(self):
        with self.assertRaises(ValueError): route_command("replace", self.route)


class ProxyValidationTests(unittest.TestCase):
    def test_credentials_encoded(self):
        self.assertEqual(proxy_url("http", "192.168.1.20", 7890, "a@b", "p:a/ss"),
                         "http://a%40b:p%3Aa%2Fss@192.168.1.20:7890")

    def test_ipv6_proxy(self):
        self.assertEqual(proxy_url("socks5h", "::1", 1080), "socks5h://[::1]:1080")

    def test_bad_port(self):
        with self.assertRaises(UserError): proxy_url("http", "host", 65536)

    def test_host_url_injection_rejected(self):
        with self.assertRaises(UserError): proxy_url("http", "host/path;bad", 7890)

    def test_control_in_password_rejected(self):
        with self.assertRaises(UserError): proxy_url("http", "host", 7890, "u", "p\nx")

    def test_socks_dns_remote(self):
        cfg = {"schema": 1, "scheme": "socks5h", "host": "192.168.1.20", "port": 1080}
        self.assertTrue(proxy_environment(cfg)["all_proxy"].startswith("socks5h://"))

    def test_apt_protocol_not_silently_converted(self):
        text = apt_text({"schema": 1, "scheme": "socks5h", "host": "host", "port": 1080})
        self.assertIn('"socks5h://host:1080"', text)
        self.assertNotIn('"http://', text)

    def test_mask_nested_secrets(self):
        value = mask_secrets({"wifi": {"psk": "abc", "ssid": "lab"}, "password": "def"})
        self.assertNotIn("abc", str(value)); self.assertNotIn("def", str(value))


if __name__ == "__main__": unittest.main()
