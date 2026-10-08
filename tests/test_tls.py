"""HTTPS against PDUs with self-signed certificates (the usual factory setup)."""
import os
import subprocess
import sys
import unittest

import equipment_drivers  # noqa: F401 -- triggers driver registration
from equipment_drivers.discovery import discover_and_instantiate
from equipment_drivers.exceptions import EquipmentConnectionError
from equipment_drivers.pdu.raritan_models import RaritanPx35460Driver
from equipment_drivers.pdu.wti_models import BaseWtiPduDriver, WtiVmrHd4d20Driver
from equipment_drivers.simulator import MockPduServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def https_driver(cls, verify):
    driver = cls()
    driver.use_https = True
    driver.verify_tls = verify
    return driver


class TestHttps(unittest.TestCase):
    def test_self_signed_certificate_rejected_by_default_with_hint(self):
        for vendor, cls, name in (("wti", WtiVmrHd4d20Driver, "WTI VMR-HD4D20 C19"),
                                  ("raritan", RaritanPx35460Driver, "Raritan PX3-5460 Switched PDU")):
            with self.subTest(vendor=vendor), MockPduServer(vendor=vendor, model_name=name, tls=True) as server:
                with self.assertRaises(EquipmentConnectionError) as ctx:
                    https_driver(cls, verify=True).connect(server.host, server.port)
                self.assertIn("--insecure", str(ctx.exception))

    def test_insecure_connects_over_https(self):
        for vendor, cls, name in (("wti", WtiVmrHd4d20Driver, "WTI VMR-HD4D20 C19"),
                                  ("raritan", RaritanPx35460Driver, "Raritan PX3-5460 Switched PDU")):
            with self.subTest(vendor=vendor), \
                    MockPduServer(vendor=vendor, channel_count=8, model_name=name, tls=True) as server:
                driver = https_driver(cls, verify=False)
                driver.connect(server.host, server.port)
                self.assertTrue(driver.base_url.startswith("https://"))
                self.assertEqual(driver.get_status(1).status, "ON")
                driver.disconnect()

    def test_https_defaults_to_port_443(self):
        driver = https_driver(WtiVmrHd4d20Driver, verify=False)
        driver.timeout = 0.5
        with self.assertRaises(EquipmentConnectionError):
            driver.connect("127.0.0.1")  # nothing listens on 443 here
        self.assertEqual(driver.port, 443)
        self.assertEqual(driver.base_url, "https://127.0.0.1:443/api/v2")

    def test_identification_over_https(self):
        with MockPduServer(vendor="wti", channel_count=20, model_name="WTI VMR-HD4D20 C19", tls=True) as server:
            found = discover_and_instantiate(server.host, server.port, "pdu", use_https=True, verify_tls=False)
        self.assertIsInstance(found, WtiVmrHd4d20Driver)

    def test_cli_buyoff_https_needs_insecure_for_self_signed(self):
        def run(*extra):
            return subprocess.run([sys.executable, "iss_pdu_utility", "--buyoff", "--model", "wti_vmr_hd4d20",
                                   "--https", *extra, "status", "1"], capture_output=True, text=True, cwd=REPO,
                                  env={**os.environ, "PYTHONPATH": REPO})
        self.assertEqual(run().returncode, 1)
        ok = run("--insecure")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("Channel 1 Status: ON", ok.stdout)
        self.assertNotIn("InsecureRequestWarning", ok.stderr)


class TestWtiDefaults(unittest.TestCase):
    def test_factory_default_credentials(self):
        # https://wti.com/blogs/knowledge-base/changing-the-default-password
        driver = BaseWtiPduDriver()
        self.assertEqual((driver.username, driver.password), ("super", "super"))


if __name__ == "__main__":
    unittest.main()
