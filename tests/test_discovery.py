import unittest
import equipment_drivers  # noqa: F401 -- triggers driver registration
from equipment_drivers.discovery import discover_and_instantiate, match_reported_model, model_token
from equipment_drivers.registry import registry
from equipment_drivers.pdu.dummy_pdu import DummyPDUDriver
from equipment_drivers.pdu.apc_models import BaseApcPduDriver, ApcAp7920Driver, ApcAp8959Driver, ApcAp8959eu3Driver
from equipment_drivers.pdu.wti_models import BaseWtiPduDriver, WtiVmrHd4d20Driver, WtiCpm800Driver
from equipment_drivers.pdu.raritan_models import RaritanDpxr8a16Driver, RaritanPx35460Driver
from equipment_drivers.simulator import MockPduServer


class TestDiscoveryByIpAddress(unittest.TestCase):
    """identify=False: no network traffic, IP-suffix convention and dummy fallback only."""

    def test_discover_dummy_pdu(self):
        driver = discover_and_instantiate("192.168.1.10", 161, "pdu", identify=False)
        self.assertIsInstance(driver, DummyPDUDriver)

    def test_discover_wti_pdu(self):
        driver = discover_and_instantiate("192.168.1.40", 80, "pdu", identify=False)
        self.assertIsInstance(driver, WtiVmrHd4d20Driver)

    def test_discover_unknown(self):
        # Should fallback to dummy_pdu_sig in the current implementation
        driver = discover_and_instantiate("192.168.1.99", 161, "pdu", identify=False)
        self.assertIsInstance(driver, DummyPDUDriver)


class TestModelMatching(unittest.TestCase):
    def setUp(self):
        self.pdus = registry.get_all_drivers("pdu")

    def test_model_token(self):
        self.assertEqual(model_token("WTI VMR-HD4D20 C19"), "VMRHD4D20")
        self.assertEqual(model_token("Raritan Dominion PX DPXR8A-16"), "DPXR8A16")
        self.assertEqual(model_token("APC AP8959EU3 3-Phase Switched Rack PDU 2G"), "AP8959EU3")

    def test_revision_suffix_still_matches(self):
        self.assertIs(match_reported_model(BaseApcPduDriver, "AP7920B", self.pdus)[1], ApcAp7920Driver)

    def test_longest_part_number_wins(self):
        self.assertIs(match_reported_model(BaseApcPduDriver, "AP8959EU3", self.pdus)[1], ApcAp8959eu3Driver)
        self.assertIs(match_reported_model(BaseApcPduDriver, "AP8959", self.pdus)[1], ApcAp8959Driver)

    def test_part_number_with_options_suffix(self):
        # WTI's own docs show products like "CPM-800-1-CA".
        self.assertIs(match_reported_model(BaseWtiPduDriver, "CPM-800-1-CA", self.pdus)[1], WtiCpm800Driver)

    def test_matching_stays_within_the_vendor(self):
        self.assertIsNone(match_reported_model(BaseWtiPduDriver, "AP7920", self.pdus))
        self.assertIsNone(match_reported_model(BaseWtiPduDriver, "", self.pdus))


class TestDiscoveryByAskingTheDevice(unittest.TestCase):
    """Mocks listen on 127.0.0.1, which matches no IP suffix, so a match here can
    only come from the device identifying itself."""

    def discover(self, **mock_kwargs):
        with MockPduServer(**mock_kwargs) as server:
            return discover_and_instantiate(server.host, server.port, "pdu")

    def test_apc_identified_via_prodinfo(self):
        driver = self.discover(vendor="apc", channel_count=8, model_name="APC AP7920 Switched Rack PDU")
        self.assertIsInstance(driver, ApcAp7920Driver)

    def test_wti_identified_via_status(self):
        driver = self.discover(vendor="wti", channel_count=20, model_name="WTI VMR-HD4D20 C19")
        self.assertIsInstance(driver, WtiVmrHd4d20Driver)

    def test_raritan_identified_via_getmetadata(self):
        driver = self.discover(vendor="raritan", channel_count=30, model_name="Raritan PX3-5460 Switched PDU")
        self.assertIsInstance(driver, RaritanPx35460Driver)

    def test_raritan_dominion_name_without_leading_part_number(self):
        driver = self.discover(vendor="raritan", channel_count=8, model_name="Raritan Dominion PX DPXR8A-16")
        self.assertIsInstance(driver, RaritanDpxr8a16Driver)

    def test_unlisted_model_gets_generic_vendor_driver(self):
        with MockPduServer(vendor="wti", channel_count=12, model_name="WTI VPS-12 Switched PDU") as server:
            driver = discover_and_instantiate(server.host, server.port, "pdu")
            self.assertIsInstance(driver, BaseWtiPduDriver)
            self.assertIn("VPS-12", driver.get_model())
            driver.connect(server.host, server.port)
            self.assertEqual(driver.get_channel_count(), 12)  # read from the device
            driver.disconnect()

    def test_wrong_credentials_fall_back_to_ip_guess(self):
        with MockPduServer(vendor="wti", channel_count=20, fault="auth_fail") as server:
            driver = discover_and_instantiate(server.host, server.port, "pdu")
        self.assertIsInstance(driver, DummyPDUDriver)

    def test_nothing_listening_falls_back(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        self.assertIsInstance(discover_and_instantiate("127.0.0.1", closed_port, "pdu"), DummyPDUDriver)


if __name__ == '__main__':
    unittest.main()
