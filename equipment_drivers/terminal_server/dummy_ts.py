import logging
from typing import Tuple
from equipment_drivers.interfaces import TerminalServerDriver
from equipment_drivers.registry import registry
from equipment_drivers.exceptions import EquipmentNotConnectedError

logger = logging.getLogger(__name__)

class DummyTSDriver(TerminalServerDriver):
    def __init__(self):
        self.ip = ""
        self.port = 0
        self.connected = False

    def connect(self, ip: str, port: int, username: str = None, password: str = None) -> bool:
        self.ip = ip
        self.port = port
        self.connected = True
        logger.info(f"Connected to Dummy TS at {ip}:{port}")
        return True

    def disconnect(self) -> bool:
        self.connected = False
        logger.info(f"Disconnected from Dummy TS at {self.ip}:{self.port}")
        return True

    def get_model(self) -> str:
        return "Dummy Terminal Server Y"

    def get_status(self) -> Tuple[str, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to Terminal Server")
        logger.info(f"Checking status on Dummy TS")
        raw_output = "DUMMY_RAW: TS ONLINE, 16 PORTS ACTIVE"
        return "ONLINE - 16 Ports Active", raw_output

# Register the driver
registry.register('terminal_server', 'dummy_ts_sig', DummyTSDriver)
