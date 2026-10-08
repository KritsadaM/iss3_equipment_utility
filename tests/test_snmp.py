"""SNMP v1/v2c client, mock agent, and APC/Raritan SNMP drivers."""
import os
import shutil
import subprocess
import sys
import unittest

import equipment_drivers  # noqa: F401 -- triggers driver registration
from equipment_drivers import snmp
from equipment_drivers.exceptions import EquipmentConnectionError
from equipment_drivers.pdu.apc_models import ApcAp7900Driver
from equipment_drivers.pdu.raritan_models import RaritanPx35460Driver
from equipment_drivers.pdu.snmp_drivers import (APC_RPDU, APC_RPDU2, RARITAN_PDU2, identify_snmp,
                                                snmp_driver_class)
from equipment_drivers.pdu.wti_models import WtiVmrHd4d20Driver
from equipment_drivers.simulator import MockPduServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASES = [  # (label, vendor, apc_generation, native driver, expected profile, expected version)
    ("apc rPDU2", "apc", "rpdu2g", ApcAp7900Driver, APC_RPDU2, "1"),
    ("apc rPDU", "apc", "rpdu", ApcAp7900Driver, APC_RPDU, "1"),
    ("raritan", "raritan", "rpdu2g", RaritanPx35460Driver, RARITAN_PDU2, "2c"),
]


def mock(vendor, generation="rpdu2g", **kwargs):
    model = {"apc": "APC AP7900 Switched Rack PDU", "raritan": "Raritan PX3-5460 Switched PDU"}[vendor]
    return MockPduServer(vendor=vendor, channel_count=8, model_name=model, snmp=True, apc_generation=generation,
                         **kwargs)


class TestBer(unittest.TestCase):
    def test_round_trip(self):
        message = snmp.encode_message(1, "private", snmp.SET, 2 ** 31 - 1,
                                      [("1.3.6.1.4.1.13742.6.4.1.2.1.2.1.42", 0),
                                       ("1.3.6.1.2.1.1.5.0", "x" * 300),          # long-form length
                                       ("1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.3", -129)])  # negative int
        version, community, pdu_type, request_id, status, index, varbinds = snmp.decode_message(message)
        self.assertEqual((version, community, pdu_type, request_id, status, index),
                         (1, "private", snmp.SET, 2 ** 31 - 1, 0, 0))
        self.assertEqual(varbinds, [("1.3.6.1.4.1.13742.6.4.1.2.1.2.1.42", 0), ("1.3.6.1.2.1.1.5.0", "x" * 300),
                                    ("1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.3", -129)])

    def test_oid_encoding_known_bytes(self):
        # 1.3.6.1.4.1.13742 -> 2b 06 01 04 01 eb 2e (13742 = 0x35ae -> 0xeb 0x2e)
        self.assertEqual(snmp.encode_oid("1.3.6.1.4.1.13742").hex(), "06072b06010401eb2e")
        self.assertEqual(snmp.encode_int(128).hex(), "02020080")
        self.assertEqual(snmp.encode_int(-1).hex(), "0201ff")


class TestSnmpDrivers(unittest.TestCase):
    def test_full_cycle_per_profile(self):
        for label, vendor, generation, native, profile, version in CASES:
            with self.subTest(label), mock(vendor, generation) as server:
                driver = snmp_driver_class(native)()
                driver.connect(server.host, server.port)
                self.assertIs(driver.profile, profile)
                self.assertEqual(driver.client.version, version)
                self.assertIn(profile.model_oid, driver.raw_connection)
                self.assertEqual(driver.get_channel_count(), 8)
                off = driver.turn_off(5)
                self.assertTrue(off.success)
                self.assertEqual(off.raw, f".{profile.control_oid}.5 = INTEGER: {profile.write_off}")
                status = driver.get_status(5)
                self.assertEqual(status.status, "OFF")
                self.assertEqual(status.raw, f".{profile.state_oid}.5 = INTEGER: {profile.read_off}")
                self.assertTrue(driver.turn_on(5).success)
                self.assertEqual(driver.get_status(5).status, "ON")
                driver.disconnect()

    def test_identify_over_snmp(self):
        for label, vendor, generation, _native, _profile, _version in CASES:
            with self.subTest(label), mock(vendor, generation) as server:
                expected = "PX3-5460" if vendor == "raritan" else "AP7900"
                self.assertEqual(identify_snmp(server.host, server.port, "private"), (vendor, expected))

    def test_wti_is_refused(self):
        with self.assertRaises(ValueError):
            snmp_driver_class(WtiVmrHd4d20Driver)

    def test_wrong_community_is_a_connection_error(self):
        with mock("raritan") as server:
            driver = snmp_driver_class(RaritanPx35460Driver)()
            driver.community = "wrong"
            driver.timeout = 0.3
            with self.assertRaises(EquipmentConnectionError):
                driver.connect(server.host, server.port)

    def test_faults(self):
        for label, vendor, generation, native, _profile, _version in CASES:
            with self.subTest(label, fault="command_error"), mock(vendor, generation, fault="command_error") as server:
                driver = snmp_driver_class(native)()
                driver.connect(server.host, server.port)
                resp = driver.turn_off(1)
                self.assertFalse(resp.success)
                self.assertIn("commitFailed" if driver.client.version == "2c" else "genErr", resp.raw)
            with self.subTest(label, fault="stuck_outlet"), mock(vendor, generation, fault="stuck_outlet") as server:
                driver = snmp_driver_class(native)()
                driver.connect(server.host, server.port)
                self.assertTrue(driver.turn_off(1).success)
                self.assertEqual(driver.get_status(1).status, "ON")
            with self.subTest(label, fault="timeout"), mock(vendor, generation, fault="timeout") as server:
                driver = snmp_driver_class(native)()
                driver.timeout = 0.3
                driver.connect(server.host, server.port)  # model/count OIDs still answer
                with self.assertRaises(EquipmentConnectionError):
                    driver.turn_off(1)


class TestSnmpCli(unittest.TestCase):
    def run_utility(self, *args):
        return subprocess.run([sys.executable, "iss_pdu_utility", *args], capture_output=True, text=True, cwd=REPO,
                              env={**os.environ, "PYTHONPATH": REPO})

    def test_real_device_path_identifies_over_snmp(self):
        with mock("raritan") as server:
            res = self.run_utility("--ip_address", server.host, "--port", str(server.port), "--snmp",
                                   "--verify", "off", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("reports 'PX3-5460' over SNMP -> RaritanPx35460Driver", res.stderr)
        self.assertIn("Channel 2 verified OFF.", res.stdout)

    def test_buyoff_snmp_and_wti_refusal(self):
        ok = self.run_utility("--buyoff", "--snmp", "status", "1")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(".1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.1 = INTEGER: 1", ok.stdout)
        wti = self.run_utility("--buyoff", "--snmp", "--model", "wti_vmr_hd4d20", "status", "1")
        self.assertEqual(wti.returncode, 1)
        self.assertIn("APC and Raritan only", wti.stderr)

    def test_community_requires_snmp(self):
        res = self.run_utility("--ip_address", "10.0.0.1", "--community", "x", "status", "1")
        self.assertEqual(res.returncode, 2)


@unittest.skipUnless(shutil.which("snmpget") and shutil.which("snmpset"), "net-snmp command-line tools not installed")
class TestAgainstNetSnmp(unittest.TestCase):
    """An independent SNMP implementation must be able to talk to the mock agent."""

    def net_snmp(self, tool, *args):
        res = subprocess.run([tool, "-t", "1", "-r", "0", *args], capture_output=True, text=True)
        return res.stdout + res.stderr

    def test_snmpget_and_snmpset(self):
        for label, vendor, generation, _native, profile, version in CASES:
            with self.subTest(label), mock(vendor, generation) as server:
                target = f"{server.host}:{server.port}"
                out = self.net_snmp("snmpget", f"-v{version}", "-c", "public", "-On", target,
                                    "." + profile.model_oid, "." + profile.outlet_count_oid)
                self.assertIn("STRING:", out)
                self.assertIn("INTEGER: 8", out)
                self.net_snmp("snmpset", f"-v{version}", "-c", "private", target,
                              f".{profile.control_oid}.6", "i", str(profile.write_off))
                self.assertEqual(server.outlet_states[6], 0)
                out = self.net_snmp("snmpget", f"-v{version}", "-c", "public", "-On", target, f".{profile.state_oid}.6")
                self.assertIn(f"INTEGER: {profile.read_off}", out)
                out = self.net_snmp("snmpget", f"-v{version}", "-c", "wrong", target, "." + profile.model_oid)
                self.assertIn("Timeout", out)


if __name__ == "__main__":
    unittest.main()
