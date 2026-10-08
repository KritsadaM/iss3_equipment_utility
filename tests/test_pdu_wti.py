import json
import os
import unittest
from unittest.mock import patch, MagicMock
from equipment_drivers.pdu.wti_models import WTI_MODELS, WtiVmrHd4d20Driver

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "wti")


def fixture_response(name):
    with open(os.path.join(FIXTURES, name)) as f:
        text = f.read()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = text
    response.json.return_value = json.loads(text)
    return response


class TestWtiDrivers(unittest.TestCase):
    def setUp(self):
        self.driver_hd20 = WtiVmrHd4d20Driver()
        self.ip = "192.168.1.40"
        self.port = 80

    def _connect(self):
        self.driver_hd20.connected = True
        self.driver_hd20.base_url = f"http://{self.ip}:{self.port}/api/v2"

    def test_all_wti_models_registered_and_configured(self):
        self.assertEqual(len(WTI_MODELS), 16)
        for sig, driver_cls in WTI_MODELS.items():
            driver = driver_cls()
            self.assertTrue(driver.get_model().startswith("WTI"))
            self.assertGreater(driver.get_max_channel(), 0)
            self.assertEqual(driver.get_channel_count(), driver.get_max_channel())

    def test_sample_channel_counts(self):
        self.assertEqual(WTI_MODELS['wti_vmr_hd4d20']().get_max_channel(), 20)
        self.assertEqual(WTI_MODELS['wti_vmr_16hd20']().get_max_channel(), 16)
        self.assertEqual(WTI_MODELS['wti_vmr_24hd20']().get_max_channel(), 24)
        self.assertEqual(WTI_MODELS['wti_nps_8hd20']().get_max_channel(), 8)
        self.assertEqual(WTI_MODELS['wti_ips_400']().get_max_channel(), 4)
        self.assertEqual(WTI_MODELS['wti_cpm_1600']().get_max_channel(), 16)

    @patch('equipment_drivers.pdu.wti_models.requests.Session.get')
    def test_connect_success(self, mock_get):
        mock_get.return_value = fixture_response("status_status.json")

        self.assertTrue(self.driver_hd20.connect(self.ip, self.port))
        self.assertTrue(self.driver_hd20.connected)
        self.assertEqual(mock_get.call_args.args[0], f"http://{self.ip}:{self.port}/api/v2/status/status")

    @patch('equipment_drivers.pdu.wti_models.requests.Session.get')
    def test_connect_with_credential_override(self, mock_get):
        mock_get.return_value = fixture_response("status_status.json")

        result = self.driver_hd20.connect(self.ip, self.port, username="custom_user", password="custom_pass")
        self.assertTrue(result)
        self.assertEqual(self.driver_hd20.username, "custom_user")
        self.assertEqual(self.driver_hd20.password, "custom_pass")

    @patch('equipment_drivers.pdu.wti_models.requests.Session.post')
    @patch('equipment_drivers.pdu.wti_models.BaseWtiPduDriver.get_channel_count', return_value=20)
    def test_turn_on_posts_powerplug_and_returns_device_json(self, mock_count, mock_post):
        self._connect()
        mock_post.return_value = fixture_response("powerplug_post_plug1_on.json")

        response = self.driver_hd20.turn_on(1)
        self.assertTrue(response.success)
        self.assertEqual(response.raw, mock_post.return_value.text)
        self.assertEqual(response.action, "turn_on")
        self.assertEqual(mock_post.call_args.args[0], f"http://{self.ip}:{self.port}/api/v2/config/powerplug")
        self.assertEqual(mock_post.call_args.kwargs["json"], {"plug": "1", "state": "on"})

    @patch('equipment_drivers.pdu.wti_models.requests.Session.post')
    @patch('equipment_drivers.pdu.wti_models.BaseWtiPduDriver.get_channel_count', return_value=20)
    def test_non_zero_status_code_is_failure(self, mock_count, mock_post):
        self._connect()
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.text = '{"status": {"code": "1", "text": "Invalid plug"}}'
        response.json.return_value = json.loads(response.text)
        mock_post.return_value = response

        self.assertFalse(self.driver_hd20.turn_off(2).success)

    @patch('equipment_drivers.pdu.wti_models.requests.Session.get')
    @patch('equipment_drivers.pdu.wti_models.BaseWtiPduDriver.get_channel_count', return_value=20)
    def test_get_status_reads_powerplug_state(self, mock_count, mock_get):
        self._connect()
        mock_get.return_value = fixture_response("powerplug_get_plug1.json")

        response = self.driver_hd20.get_status(1)
        self.assertTrue(response.success)
        self.assertEqual(response.status, "OFF")
        self.assertEqual(response.raw, mock_get.return_value.text)
        self.assertEqual(mock_get.call_args.kwargs["params"], {"plug": "1"})

    @patch('equipment_drivers.pdu.wti_models.requests.Session.get')
    def test_get_channel_count_from_device(self, mock_get):
        self._connect()
        plugs = [{"plug": str(i), "state": "on"} for i in range(1, 9)]
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": {"code": "0", "text": "OK"}, "powerplugs": plugs}
        mock_get.return_value = response

        self.assertEqual(self.driver_hd20.get_channel_count(), 8)

    @patch('equipment_drivers.pdu.wti_models.BaseWtiPduDriver.get_channel_count', return_value=20)
    def test_out_of_range_channel_raises_value_error(self, mock_count):
        self._connect()

        with self.assertRaises(ValueError):
            self.driver_hd20.turn_on(0)

        with self.assertRaises(ValueError):
            self.driver_hd20.turn_off(21)

        with self.assertRaises(ValueError):
            self.driver_hd20.get_status(-1)


if __name__ == '__main__':
    unittest.main()
