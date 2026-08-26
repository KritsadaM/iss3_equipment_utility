"""Tests for the custom exception hierarchy."""
import unittest
from equipment_drivers.exceptions import (
    EquipmentError,
    EquipmentConnectionError,
    EquipmentCommandError,
    EquipmentNotConnectedError,
)


class TestExceptionHierarchy(unittest.TestCase):
    """Verify that all custom exceptions inherit from EquipmentError."""

    def test_connection_error_is_equipment_error(self):
        with self.assertRaises(EquipmentError):
            raise EquipmentConnectionError("test")

    def test_command_error_is_equipment_error(self):
        with self.assertRaises(EquipmentError):
            raise EquipmentCommandError("test")

    def test_not_connected_error_is_equipment_error(self):
        with self.assertRaises(EquipmentError):
            raise EquipmentNotConnectedError("test")

    def test_equipment_error_is_exception(self):
        with self.assertRaises(Exception):
            raise EquipmentError("test")

    def test_message_preserved(self):
        msg = "Connection refused on 10.0.0.1:80"
        err = EquipmentConnectionError(msg)
        self.assertEqual(str(err), msg)


class TestDriversRaiseCustomExceptions(unittest.TestCase):
    """Verify drivers raise our custom exceptions, not bare Exception."""

    def test_apc_not_connected_raises_custom(self):
        from equipment_drivers.pdu.apc_models import BaseApcPduDriver
        driver = BaseApcPduDriver()
        with self.assertRaises(EquipmentNotConnectedError):
            driver.turn_on(1)

    def test_wti_not_connected_raises_custom(self):
        from equipment_drivers.pdu.wti_models import BaseWtiPduDriver
        driver = BaseWtiPduDriver()
        with self.assertRaises(EquipmentNotConnectedError):
            driver.turn_on(1)

    def test_raritan_not_connected_raises_custom(self):
        from equipment_drivers.pdu.raritan_models import BaseRaritanPduDriver
        driver = BaseRaritanPduDriver()
        with self.assertRaises(EquipmentNotConnectedError):
            driver.turn_on(1)

    def test_apc_connect_failure_raises_connection_error(self):
        from unittest.mock import patch
        from equipment_drivers.pdu.apc_models import BaseApcPduDriver
        import requests
        driver = BaseApcPduDriver()
        with patch.object(driver.session, 'get', side_effect=requests.exceptions.ConnectionError("refused")):
            with self.assertRaises(EquipmentConnectionError):
                driver.connect("10.0.0.1", 80)


if __name__ == "__main__":
    unittest.main()
