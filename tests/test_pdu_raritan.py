import json
import os
import unittest
from unittest.mock import patch, MagicMock
from equipment_drivers.pdu.raritan_models import RARITAN_MODELS, RaritanPx25190RDriver, RaritanPx35460Driver
from equipment_drivers.exceptions import EquipmentCommandError

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "raritan")


def fixture_response(name):
    with open(os.path.join(FIXTURES, name)) as f:
        text = f.read()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = text
    response.json.return_value = json.loads(text)
    return response


def connected(driver):
    driver.connected = True
    driver.base_url = "http://192.168.1.60:80"
    return driver


class TestRaritanDrivers(unittest.TestCase):
    def test_all_raritan_models_registered_and_configured(self):
        self.assertEqual(len(RARITAN_MODELS), 21)
        for sig, driver_cls in RARITAN_MODELS.items():
            driver = driver_cls()
            self.assertTrue(driver.get_model().startswith("Raritan"))
            self.assertGreater(driver.get_max_channel(), 0)
            self.assertEqual(driver.get_channel_count(), driver.get_max_channel())

    def test_sample_channel_counts(self):
        self.assertEqual(RARITAN_MODELS['raritan_px2_5190r']().get_max_channel(), 8)
        self.assertEqual(RARITAN_MODELS['raritan_px2_5440']().get_max_channel(), 20)
        self.assertEqual(RARITAN_MODELS['raritan_px2_5460']().get_max_channel(), 30)  # unverified
        self.assertEqual(RARITAN_MODELS['raritan_px3_5460']().get_max_channel(), 20)  # (20) C13
        self.assertEqual(RARITAN_MODELS['raritan_px2_5804']().get_max_channel(), 42)
        self.assertEqual(RARITAN_MODELS['raritan_px3_5724']().get_max_channel(), 36)
        self.assertEqual(RARITAN_MODELS['raritan_px3_5904']().get_max_channel(), 54)
        self.assertEqual(RARITAN_MODELS['raritan_dpxr12a_16']().get_max_channel(), 12)

    @patch('equipment_drivers.pdu.raritan_models.requests.Session.post')
    def test_connect_calls_getMetaData(self, mock_post):
        mock_post.return_value = fixture_response("pdu_getMetaData.json")
        driver = RaritanPx25190RDriver()

        self.assertTrue(driver.connect("192.168.1.60", 80))
        self.assertTrue(driver.connected)
        self.assertEqual(mock_post.call_args.args[0], "http://192.168.1.60:80/model/pdu/0")
        self.assertEqual(mock_post.call_args.kwargs["json"]["method"], "getMetaData")
        self.assertEqual(mock_post.call_args.kwargs["json"]["jsonrpc"], "2.0")

    @patch('equipment_drivers.pdu.raritan_models.requests.Session.post')
    def test_turn_on_and_off_call_setPowerState(self, mock_post):
        driver = connected(RaritanPx25190RDriver())
        outlets = fixture_response("pdu_getOutlets_8.json")
        set_ok = fixture_response("outlet_setPowerState.json")
        mock_post.side_effect = [outlets, set_ok, outlets, set_ok]

        resp_on = driver.turn_on(1)
        self.assertTrue(resp_on.success)
        self.assertEqual(resp_on.raw, set_ok.text)
        self.assertEqual(mock_post.call_args.args[0], "http://192.168.1.60:80/model/pdu/0/outlet/0")
        self.assertEqual(mock_post.call_args.kwargs["json"]["params"], {"pstate": 1})

        resp_off = driver.turn_off(8)
        self.assertTrue(resp_off.success)
        self.assertEqual(mock_post.call_args.args[0], "http://192.168.1.60:80/model/pdu/0/outlet/7")
        self.assertEqual(mock_post.call_args.kwargs["json"]["params"], {"pstate": 0})

    @patch('equipment_drivers.pdu.raritan_models.requests.Session.post')
    @patch('equipment_drivers.pdu.raritan_models.BaseRaritanPduDriver.get_channel_count', return_value=30)
    def test_get_status_calls_getState(self, mock_count, mock_post):
        driver = connected(RaritanPx35460Driver())
        mock_post.return_value = fixture_response("outlet_getState_on.json")

        resp = driver.get_status(30)
        self.assertTrue(resp.success)
        self.assertEqual(resp.status, "ON")
        self.assertEqual(resp.raw, mock_post.return_value.text)
        self.assertEqual(mock_post.call_args.args[0], "http://192.168.1.60:80/model/pdu/0/outlet/29")
        self.assertEqual(mock_post.call_args.kwargs["json"]["method"], "getState")

    @patch('equipment_drivers.pdu.raritan_models.requests.Session.post')
    @patch('equipment_drivers.pdu.raritan_models.BaseRaritanPduDriver.get_channel_count', return_value=8)
    def test_jsonrpc_error_raises(self, mock_count, mock_post):
        driver = connected(RaritanPx25190RDriver())
        mock_post.return_value = fixture_response("jsonrpc_error.json")

        with self.assertRaises(EquipmentCommandError):
            driver.get_status(1)

    @patch('equipment_drivers.pdu.raritan_models.requests.Session.post')
    def test_channel_count_from_getOutlets(self, mock_post):
        driver = connected(RaritanPx35460Driver())
        mock_post.return_value = fixture_response("pdu_getOutlets_8.json")

        self.assertEqual(driver.get_channel_count(), 8)

    def test_channel_validation(self):
        driver = RaritanPx25190RDriver()
        with self.assertRaises(ValueError):
            driver.validate_channel(0)

        with self.assertRaises(ValueError):
            driver.validate_channel(9)

        with self.assertRaises(ValueError):
            RaritanPx35460Driver().validate_channel(31)


if __name__ == '__main__':
    unittest.main()
