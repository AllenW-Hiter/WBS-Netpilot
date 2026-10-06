import contextlib
import io
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

import netpilot
import netpilot_lib.network as network
from netpilot_lib.common import Cancelled, UserError


class SubnetInputTests(unittest.TestCase):
    def test_subnet_shorthand_and_explicit_prefix(self):
        for value in ("192.168.52", "192.168.52.", "192.168.52.0", "192.168.52.0/24"):
            with self.subTest(value=value):
                self.assertEqual(str(network.validate_subnet(value)), "192.168.52.0/24")

    def test_other_prefixes_remain_supported(self):
        for value in ("10.70.0.0/16", "192.168.52.128/25", "192.168.52.0/30"):
            with self.subTest(value=value):
                self.assertEqual(str(network.validate_subnet(value)), value)

    def test_invalid_subnets_rejected(self):
        for value in ("", None, "192.168.52.1/24", "300.1.1", "192.168.52.0/31",
                      "10.0.0.0/7", "127.0.0.0/24", "224.0.0.0/24", "0.0.0.0/24"):
            with self.subTest(value=value), self.assertRaises(UserError):
                network.validate_subnet(value)

    def test_suffix_and_full_address(self):
        subnet = network.validate_subnet("192.168.52")
        for value in ("37", ".37", "192.168.52.37"):
            with self.subTest(value=value):
                self.assertEqual(network.subnet_address(subnet, value), "192.168.52.37/24")

    def test_full_address_on_larger_subnet(self):
        subnet = network.validate_subnet("10.70.0.0/16")
        self.assertEqual(network.subnet_address(subnet, "10.70.4.37"), "10.70.4.37/16")

    def test_invalid_host_addresses_rejected(self):
        subnet = network.validate_subnet("192.168.52")
        for value in ("0", ".255", "256", "-1", "abc", None, "192.168.53.37", "37/24"):
            with self.subTest(value=value), self.assertRaises(UserError):
                network.subnet_address(subnet, value)

    def test_suffix_must_fit_smaller_subnet(self):
        subnet = network.validate_subnet("192.168.52.128/25")
        self.assertEqual(network.subnet_address(subnet, ".130"), "192.168.52.130/25")
        for value in ("37", "128", "255"):
            with self.subTest(value=value), self.assertRaises(UserError):
                network.subnet_address(subnet, value)


class PairInputTests(unittest.TestCase):
    def test_invalid_subnet_and_suffix_are_retried(self):
        with patch.object(network, "ask", side_effect=["bad", "192.168.52", "255", "37", ".62"]), \
             patch.object(network, "json_command", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(network.choose_pair_addresses("eth0"), ("192.168.52.37/24", "192.168.52.62/24"))

    def test_duplicate_client_address_is_retried(self):
        with patch.object(network, "ask", side_effect=["192.168.52", "37", ".37", "62"]), \
             patch.object(network, "json_command", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(network.choose_pair_addresses("eth0")[1], "192.168.52.62/24")
        self.assertIn("主从机地址不能相同", output.getvalue())

    def test_conflicting_subnet_is_reentered(self):
        occupied = [{"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.52.2", "prefixlen": 24}]}]
        with patch.object(network, "ask", side_effect=["192.168.52", "37", "62", "192.168.53", "17", "29"]), \
             patch.object(network, "json_command", side_effect=[occupied, [], [], []]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(network.choose_pair_addresses("eth0"), ("192.168.53.17/24", "192.168.53.29/24"))
        self.assertIn("路由重叠", output.getvalue())

    def test_unreadable_network_state_stops_instead_of_guessing(self):
        with patch.object(network, "ask", side_effect=["192.168.52", "37", "62"]), \
             patch.object(network, "json_command", side_effect=UserError("state unavailable")), \
             self.assertRaisesRegex(UserError, "state unavailable"):
            network.choose_pair_addresses("eth0")

    def test_cancel_subnet_or_address_does_not_loop(self):
        for answers in ([Cancelled("cancel")], ["192.168.52", Cancelled("cancel")],
                        ["192.168.52", "37", Cancelled("cancel")]):
            with self.subTest(answers=answers), patch.object(network, "ask", side_effect=answers), \
                 patch.object(network, "json_command") as query, self.assertRaises(Cancelled):
                network.choose_pair_addresses("eth0")
            query.assert_not_called()


class AddressWizardTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.ask = self.stack.enter_context(patch.object(network, "ask"))
        self.choose = self.stack.enter_context(patch.object(network, "choose"))
        self.select = self.stack.enter_context(patch.object(network, "select_device"))
        self.confirm = self.stack.enter_context(patch.object(network, "confirm", return_value=True))
        self.pool = self.stack.enter_context(patch.object(network, "shared_pool_supported", return_value=True))
        self.stack.enter_context(patch.object(network, "json_command", return_value=[]))
        self.stack.enter_context(patch.object(network, "route_to", return_value={"dev": "wlan0"}))
        self.up = network.Device("wlan0", "wifi", "connected", "uplink", True, "phy0")
        self.down = network.Device("eth0", "ethernet", "disconnected", "", True)

    def prepare_host(self, answers):
        self.choose.side_effect = [("wifi", "ethernet", "shared"), "single-static"]
        self.select.side_effect = [self.up, self.down]
        self.ask.side_effect = ["robot主机共享"] + answers

    def prepare_client(self, mode, answers):
        self.choose.return_value = mode
        self.select.return_value = self.down
        self.ask.side_effect = ["robot从机连接"] + answers

    def test_host_uses_custom_pair_and_pool_without_presets(self):
        self.prepare_host(["192.168.52", "37", ".62", "192.168.52.100", "192.168.52.199"])
        plan = network.host_plan(True)
        self.assertEqual(plan["name"], "robot主机共享")
        self.assertEqual(plan["address"], "192.168.52.37/24")
        self.assertEqual(plan["client_address_hint"], "192.168.52.62/24")
        self.assertEqual(plan["dhcp_range"], "192.168.52.100,192.168.52.199")
        self.assertEqual(self.choose.call_count, 1)
        settings = network.build_settings(plan)
        self.assertEqual(settings["connection"]["id"], "robot主机共享 [试运行]")
        self.assertEqual(settings["ipv4"]["address-data"], [{"address": "192.168.52.37", "prefix": 24}])
        self.assertNotIn("client_address_hint", settings["ipv4"])

    def test_pool_covering_client_is_retried(self):
        self.prepare_host(["192.168.52", "37", "150", "192.168.52.100", "192.168.52.199",
                           "192.168.52.160", "192.168.52.199"])
        plan = network.host_plan(True)
        self.assertEqual(plan["dhcp_range"], "192.168.52.160,192.168.52.199")
        self.assertEqual(self.ask.call_args_list[4].args, ("DHCP 起点", ""))

    def test_pool_covering_host_is_retried(self):
        self.prepare_host(["192.168.52", "150", "37", "192.168.52.100", "192.168.52.199",
                           "192.168.52.160", "192.168.52.199"])
        self.assertEqual(network.host_plan(True)["dhcp_range"], "192.168.52.160,192.168.52.199")

    def test_older_nm_requires_explicit_address_usage(self):
        self.prepare_host(["192.168.52", "37", "62"])
        self.pool.return_value = False
        plan = network.host_plan(True)
        self.assertEqual(plan["dhcp_range"], "")
        self.assertIn("不要混接 DHCP 客户端", " ".join(plan["notes"]))
        self.assertEqual(self.choose.call_count, 2)

    def test_declining_custom_pool_still_requires_address_usage(self):
        self.prepare_host(["192.168.52", "37", "62"])
        self.confirm.side_effect = [False, True]
        plan = network.host_plan(True)
        self.assertEqual(plan["dhcp_range"], "")
        self.assertIn("不要混接 DHCP 客户端", " ".join(plan["notes"]))

    def test_automatic_dhcp_does_not_claim_fixed_client_assignment(self):
        self.prepare_host(["192.168.52", "37", "62"])
        self.pool.return_value = False
        self.choose.side_effect = [("wifi", "ethernet", "shared"), "dhcp"]
        self.assertIn("不是固定分配", " ".join(network.host_plan(True)["notes"]))

    def test_local_host_accepts_custom_pair_without_dhcp(self):
        self.choose.return_value = (None, "ethernet", "local")
        self.select.return_value = self.down
        self.ask.side_effect = ["robot本地管理", "192.168.52", "37", "62"]
        plan = network.host_plan(True)
        self.assertEqual(plan["method"], "manual")
        self.assertEqual(plan["name"], "robot本地管理")
        self.assertEqual(plan["client_address_hint"], "192.168.52.62/24")
        self.pool.assert_not_called()

    def test_pool_input_cancellation_propagates(self):
        self.prepare_host(["192.168.52", "37", "62", Cancelled("cancel")])
        with self.assertRaises(Cancelled):
            network.host_plan(True)

    def test_client_on_demand_uses_entered_host_as_gateway_and_dns_default(self):
        self.prepare_client("on-demand", ["192.168.52", "17", "62", "192.168.52.17"])
        plan = network.client_plan(True)
        self.assertEqual(plan["address"], "192.168.52.62/24")
        self.assertEqual(plan["on_demand_gateway"], "192.168.52.17")
        self.assertEqual(plan["gateway"], "")
        self.assertEqual(plan["name"], "robot从机连接")
        self.assertEqual(self.ask.call_args_list[-1].args[1], "192.168.52.17")
        self.assertEqual(self.choose.call_count, 1)

    def test_client_permanent_gateway_uses_custom_host(self):
        self.prepare_client("permanent", ["192.168.52", ".17", ".62", "192.168.52.17"])
        plan = network.client_plan(True)
        self.assertEqual(plan["gateway"], "192.168.52.17")
        self.assertEqual(plan["on_demand_gateway"], "")

    def test_local_client_keeps_custom_address_without_gateway(self):
        self.prepare_client("local", ["192.168.52", "17", "62"])
        plan = network.client_plan(True)
        self.assertEqual(plan["address"], "192.168.52.62/24")
        self.assertEqual(plan["gateway"], "")
        self.assertEqual(plan["dns"], [])

    def test_dhcp_client_does_not_request_static_addresses(self):
        self.prepare_client("dhcp", [])
        plan = network.client_plan(True)
        self.assertEqual(plan["method"], "auto")
        self.assertEqual(plan["name"], "robot从机连接")
        self.assertEqual(self.ask.call_count, 1)


class ClientHintPlanTests(unittest.TestCase):
    def host_plan(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        plan = json.loads((root / "examples/host_shared.json").read_text())
        plan["client_address_hint"] = "192.168.1.40/24"
        return plan

    def test_hint_rejects_wrong_network_same_address_and_wrong_type(self):
        for value in ("192.168.2.40/24", "192.168.1.20/24", "192.168.1.40/25", None):
            plan = self.host_plan()
            plan["client_address_hint"] = value
            with self.subTest(value=value), self.assertRaises(UserError):
                network.validate_plan(plan)

    def test_hint_is_limited_to_static_host_plans(self):
        plan = self.host_plan()
        plan.update(role="client", method="manual", dhcp_range="")
        with self.assertRaises(UserError):
            network.validate_plan(plan)

    def test_imported_pool_must_not_cover_hint_including_endpoints(self):
        for value in ("192.168.1.30,192.168.1.40", "192.168.1.40,192.168.1.50"):
            plan = self.host_plan()
            plan["dhcp_range"] = value
            with self.subTest(value=value), self.assertRaisesRegex(UserError, "预留的固定地址"):
                network.validate_plan(plan)

    def test_hint_survives_json_and_configuration_card_export(self):
        plan = network.validate_plan(self.host_plan())
        with tempfile.TemporaryDirectory() as directory:
            account = types.SimpleNamespace(pw_dir=directory)
            with patch.object(netpilot, "operator_account", return_value=account), \
                 patch.object(netpilot, "as_operator", return_value=contextlib.nullcontext()), \
                 contextlib.redirect_stdout(io.StringIO()):
                netpilot.export_plan(plan)
            files = pathlib.Path(directory) / "netpilot-exports"
            exported = json.loads(next(files.glob("*.json")).read_text())
            card = next(files.glob("*.md")).read_text()
            self.assertEqual(exported["client_address_hint"], "192.168.1.40/24")
            self.assertIn("从机固定地址建议：`192.168.1.40/24`", card)


if __name__ == "__main__":
    unittest.main()
