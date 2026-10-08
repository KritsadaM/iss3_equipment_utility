import os
import unittest
from equipment_drivers.pdu.apc_models import (APC_MODELS, ApcAp7900Driver, ApcAp8959Driver,
                                              parse_result_code, parse_outlet_states)
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentNotConnectedError
from equipment_drivers.simulator import MockPduServer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "apc")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class FakeCliTransport:
    """Stands in for SshCliTransport: records commands, replies from a table."""
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.sent = []
        self.opened_with = None
        self.closed = False

    def open(self, host, port, username, password, timeout=10.0):
        self.opened_with = (host, port, username, password)

    def send(self, command):
        self.sent.append(command)
        return self.replies[command]

    def close(self):
        self.closed = True


def connected_driver(driver_cls, replies):
    driver = driver_cls()
    transport = FakeCliTransport(replies)
    driver.transport_factory = lambda: transport
    driver.connect("192.168.1.50")
    return driver, transport


class TestApcModels(unittest.TestCase):
    def test_all_apc_models_registered_and_configured(self):
        self.assertEqual(len(APC_MODELS), 20)
        for sig, driver_cls in APC_MODELS.items():
            driver = driver_cls()
            self.assertTrue(driver.get_model().startswith("APC"))
            self.assertGreater(driver.get_max_channel(), 0)
            self.assertEqual(driver.get_channel_count(), driver.get_max_channel())

    def test_sample_channel_counts(self):
        self.assertEqual(APC_MODELS['apc_ap7900']().get_max_channel(), 8)
        self.assertEqual(APC_MODELS['apc_ap7902']().get_max_channel(), 16)
        self.assertEqual(APC_MODELS['apc_ap7930']().get_max_channel(), 24)
        self.assertEqual(APC_MODELS['apc_ap8941']().get_max_channel(), 24)
        self.assertEqual(APC_MODELS['apc_ap8959']().get_max_channel(), 24)  # (21) C13 + (3) C19
        self.assertEqual(APC_MODELS['apc_ap8958']().get_max_channel(), 8)  # (7) C13 + (1) C19


class TestApcCliParsing(unittest.TestCase):
    def test_result_codes(self):
        self.assertEqual(parse_result_code(fixture("olon_success.txt")), "000")
        self.assertEqual(parse_result_code(fixture("e102_parameter_error.txt")), "102")
        self.assertIsNone(parse_result_code("garbage"))

    def test_outlet_states(self):
        states = parse_outlet_states(fixture("olstatus_all.txt"))
        self.assertEqual(len(states), 8)
        self.assertEqual(states[1], "ON")
        self.assertEqual(states[3], "OFF")


class TestApcDriver(unittest.TestCase):
    def test_connect_uses_ssh_default_port_and_credentials(self):
        driver, transport = connected_driver(ApcAp7900Driver, {})
        self.assertTrue(driver.connected)
        self.assertEqual(transport.opened_with, ("192.168.1.50", 22, "apc", "apc"))

    def test_connect_credential_override(self):
        driver = ApcAp7900Driver()
        transport = FakeCliTransport()
        driver.transport_factory = lambda: transport
        driver.connect("192.168.1.50", 2222, username="user", password="pwd")
        self.assertEqual(transport.opened_with, ("192.168.1.50", 2222, "user", "pwd"))

    def test_connect_failure_raises(self):
        class FailingTransport(FakeCliTransport):
            def open(self, *args, **kwargs):
                raise EquipmentConnectionError("auth failed")

        driver = ApcAp7900Driver()
        driver.transport_factory = FailingTransport
        with self.assertRaises(EquipmentConnectionError):
            driver.connect("192.168.1.50")
        self.assertFalse(driver.connected)

    def test_turn_on_returns_cli_text_as_raw(self):
        driver, transport = connected_driver(ApcAp7900Driver, {
            "olStatus all": fixture("olstatus_all.txt"),
            "olOn 1": fixture("olon_success.txt"),
            "olOff 1": fixture("olon_success.txt"),
        })
        resp_on = driver.turn_on(1)
        self.assertTrue(resp_on.success)
        self.assertEqual(resp_on.action, "turn_on")
        self.assertEqual(resp_on.raw, fixture("olon_success.txt"))

        resp_off = driver.turn_off(1)
        self.assertTrue(resp_off.success)
        self.assertEqual(resp_off.action, "turn_off")
        self.assertIn("olOff 1", transport.sent)

    def test_get_status_parses_cli_output(self):
        driver, _ = connected_driver(ApcAp7900Driver, {
            "olStatus all": fixture("olstatus_all.txt"),
            "olStatus 3": fixture("olstatus_3_off.txt"),
        })
        resp = driver.get_status(3)
        self.assertTrue(resp.success)
        self.assertEqual(resp.status, "OFF")
        self.assertEqual(resp.raw, fixture("olstatus_3_off.txt"))

    def test_cli_error_code_is_reported_as_failure(self):
        driver, _ = connected_driver(ApcAp7900Driver, {
            "olStatus all": fixture("olstatus_all.txt"),
            "olOn 2": fixture("e102_parameter_error.txt"),
        })
        resp = driver.turn_on(2)
        self.assertFalse(resp.success)
        self.assertIn("E102", resp.raw)

    def test_channel_count_is_read_from_device_once(self):
        driver, transport = connected_driver(ApcAp8959Driver, {
            "olStatus all": fixture("olstatus_all.txt"),
            "olOn 1": fixture("olon_success.txt"),
        })
        self.assertEqual(driver.get_channel_count(), 8)  # device reports 8, not the model default 24
        driver.turn_on(1)
        self.assertEqual(transport.sent.count("olStatus all"), 1)
        with self.assertRaises(ValueError):
            driver.turn_on(9)

    def test_disconnect_closes_session(self):
        driver, transport = connected_driver(ApcAp7900Driver, {})
        driver.disconnect()
        self.assertTrue(transport.closed)
        self.assertFalse(driver.connected)
        with self.assertRaises(EquipmentNotConnectedError):
            driver.turn_on(1)

    def test_channel_validation(self):
        driver = ApcAp7900Driver()
        driver.connected = True
        with self.assertRaises(ValueError):
            driver.turn_on(0)

        driver._device_channel_count = 8
        with self.assertRaises(ValueError):
            driver.turn_on(9)

        driver_8959 = ApcAp8959Driver()
        driver_8959.connected = True
        driver_8959._device_channel_count = 24
        with self.assertRaises(ValueError):
            driver_8959.turn_on(25)


class TestApcOverSsh(unittest.TestCase):
    """End-to-end over a real SSH session against the mock NMC CLI."""

    def test_full_cycle_over_ssh(self):
        with MockPduServer(vendor="apc", channel_count=8, model_name="APC AP7900 Switched Rack PDU",
                           username="apc", password="apc") as server:
            driver = ApcAp7900Driver()
            driver.connect(server.host, server.port)
            try:
                self.assertEqual(driver.get_channel_count(), 8)
                self.assertEqual(driver.turn_off(4).raw, "E000: Success")
                status = driver.get_status(4)
                self.assertEqual(status.status, "OFF")
                self.assertEqual(status.raw, "E000: Success\n 4: Outlet 4: Off")
            finally:
                driver.disconnect()

    def test_wrong_password_is_rejected(self):
        with MockPduServer(vendor="apc", channel_count=8, username="apc", password="apc") as server:
            driver = ApcAp7900Driver()
            with self.assertRaises(EquipmentConnectionError):
                driver.connect(server.host, server.port, password="wrong")


if __name__ == '__main__':
    unittest.main()
