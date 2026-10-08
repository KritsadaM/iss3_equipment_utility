"""Fault injection: the mock PDUs misbehave on purpose, and the drivers must
report every failure through their own error handling."""
import os
import subprocess
import sys
import unittest

import equipment_drivers  # noqa: trigger driver registration
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentError
from equipment_drivers.pdu.apc_models import ApcAp7900Driver, parse_model
from equipment_drivers.pdu.wti_models import WtiVmrHd4d20Driver
from equipment_drivers.pdu.raritan_models import RaritanPx35460Driver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.simulator import FAULTS, MockPduServer, run_pdu_fault_trial

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "tests", "fixtures", "apc")

MODELS = {
    "apc": ("apc_ap7900", ApcAp7900Driver, "APC AP7900 Switched Rack PDU"),
    "wti": ("wti_vmr_hd4d20", WtiVmrHd4d20Driver, "WTI VMR-HD4D20 C19"),
    "raritan": ("raritan_px3_5460", RaritanPx35460Driver, "Raritan PX3-5460 Switched PDU"),
}


def mock(vendor, fault):
    _sig, _cls, model_name = MODELS[vendor]
    return MockPduServer(vendor=vendor, channel_count=8, model_name=model_name, fault=fault)


class TestFaultTrial(unittest.TestCase):
    def test_every_vendor_handles_every_fault(self):
        for vendor, (sig, cls, _name) in MODELS.items():
            with self.subTest(vendor=vendor):
                self.assertTrue(run_pdu_fault_trial(sig, cls, verbose=False))

    def test_trial_catches_a_driver_that_hides_command_errors(self):
        class OptimisticWti(WtiVmrHd4d20Driver):
            def turn_off(self, channel):
                return PDUResponse(success=True, action="turn_off", channel=channel, raw="")

        self.assertFalse(run_pdu_fault_trial("wti_vmr_hd4d20", OptimisticWti, verbose=False))

    def test_trial_catches_unexpected_exception_types(self):
        class LeakyApc(ApcAp7900Driver):
            def turn_off(self, channel):
                raise KeyError("raw parser bug")

        self.assertFalse(run_pdu_fault_trial("apc_ap7900", LeakyApc, verbose=False))


class TestIndividualFaults(unittest.TestCase):
    def test_auth_fail_rejects_connect(self):
        for vendor, (_sig, cls, _name) in MODELS.items():
            with self.subTest(vendor=vendor), mock(vendor, "auth_fail") as server:
                with self.assertRaises(EquipmentConnectionError):
                    cls().connect(server.host, server.port)

    def test_drop_after_connect_raises_equipment_error(self):
        for vendor, (_sig, cls, _name) in MODELS.items():
            with self.subTest(vendor=vendor), mock(vendor, "drop") as server:
                driver = cls()
                driver.connect(server.host, server.port)
                with self.assertRaises(EquipmentError):
                    driver.get_status(1)
                driver.disconnect()

    def test_unresponsive_device_costs_one_timeout_not_two(self):
        import time
        for vendor, (_sig, cls, _name) in MODELS.items():
            with self.subTest(vendor=vendor), mock(vendor, "timeout") as server:
                driver = cls()
                driver.timeout = 0.5
                driver.connect(server.host, server.port)
                started = time.monotonic()
                with self.assertRaises(EquipmentConnectionError):
                    driver.get_max_channel()  # validate_channel() calls this before every action
                self.assertLess(time.monotonic() - started, 0.95)
                driver.disconnect()

    def test_command_error_keeps_device_reply_in_raw(self):
        with mock("apc", "command_error") as server:
            driver = ApcAp7900Driver()
            driver.connect(server.host, server.port)
            resp = driver.turn_on(2)
            driver.disconnect()
        self.assertFalse(resp.success)
        self.assertEqual(resp.raw, "E100: Command failed")

    def test_unknown_fault_rejected(self):
        with self.assertRaises(ValueError):
            MockPduServer(vendor="wti", fault="meteor_strike")
        self.assertEqual(set(FAULTS), {"auth_fail", "timeout", "drop", "command_error", "stuck_outlet"})


class TestFaultCli(unittest.TestCase):
    def run_utility(self, *args):
        return subprocess.run([sys.executable, "iss_pdu_utility", *args], capture_output=True, text=True, cwd=REPO,
                              env={**os.environ, "PYTHONPATH": REPO})

    def test_buyoff_fault_exits_non_zero(self):
        res = self.run_utility("--buyoff", "--model", "wti_vmr_hd4d20", "--fault", "command_error", "off", "1")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Simulating fault 'command_error'", res.stdout)

    def test_fault_requires_buyoff(self):
        res = self.run_utility("--ip_address", "10.0.0.1", "--fault", "drop", "status", "1")
        self.assertEqual(res.returncode, 2)
        self.assertIn("--fault only applies in Buy-off mode", res.stderr)


class TestApcIdentityFixtures(unittest.TestCase):
    def test_real_banner_is_recognized_as_nmc(self):
        with open(os.path.join(FIXTURES, "login_banner.txt")) as f:
            self.assertIn("Network Management Card", f.read())

    def test_prodinfo_model(self):
        with open(os.path.join(FIXTURES, "prodinfo.txt")) as f:
            self.assertEqual(parse_model(f.read()), "AP7920B")
        self.assertEqual(parse_model("E000: Success\nModel:            AP8941\n"), "AP8941")


if __name__ == "__main__":
    unittest.main()
