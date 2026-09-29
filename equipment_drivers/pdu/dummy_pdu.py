import logging
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.registry import registry
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentNotConnectedError

logger = logging.getLogger(__name__)

class DummyPDUDriver(PDUDriver):
    def __init__(self):
        self.ip = ""
        self.port = 0
        self.connected = False
        self.raw_connection = None
        self.display_ip = None
        self.display_port = None

    def connect(self, ip: str, port: int, username: str = None, password: str = None) -> bool:
        self.ip = ip
        self.port = port
        self.connected = True
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port
        import json
        self.raw_connection = json.dumps({
            "model": self.get_model(),
            "status": "operational",
            "ip": disp_ip,
            "port": disp_port,
            "outlets": self.get_channel_count()
        }, indent=2)
        logger.info(f"Connected to Dummy PDU at {disp_ip}:{disp_port}")
        return True

    def disconnect(self) -> bool:
        self.connected = False
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port
        logger.info(f"Disconnected from Dummy PDU at {disp_ip}:{disp_port}")
        return True

    def get_model(self) -> str:
        return "Dummy PDU Model X"

    def get_channel_count(self) -> int:
        # Fixed for the dummy/simulated driver -- real drivers should query
        # the actual connected unit instead of hardcoding this.
        return 8

    def turn_on(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        logger.info(f"Turning ON channel {channel} on Dummy PDU")
        raw_output = f"DUMMY_RAW: Channel {channel} set to 1"
        return PDUResponse(success=True, action="turn_on", channel=channel, raw=raw_output)

    def turn_off(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        logger.info(f"Turning OFF channel {channel} on Dummy PDU")
        raw_output = f"DUMMY_RAW: Channel {channel} set to 0"
        return PDUResponse(success=True, action="turn_off", channel=channel, raw=raw_output)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        logger.info(f"Checking status for channel {channel} on Dummy PDU")
        raw_output = f"DUMMY_RAW: Channel {channel} is 1"
        return PDUResponse(success=True, action="get_status", channel=channel, raw=raw_output, status="ON")

# Register the driver
registry.register('pdu', 'dummy_pdu_sig', DummyPDUDriver)
