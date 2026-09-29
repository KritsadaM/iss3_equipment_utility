import logging
from typing import Optional, Tuple
import requests
from requests.auth import HTTPBasicAuth
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentCommandError, EquipmentNotConnectedError

logger = logging.getLogger(__name__)


class BaseApcPduDriver(PDUDriver):
    """
    Base driver for APC Switched Rack PDUs (AP79xx / AP89xx / AP86xx series).
    Supports HTTP/REST interaction with APC Network Management Cards.
    """
    MODEL_NAME = "APC Switched Rack PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 80
    IP_SUFFIX = ""

    def __init__(self):
        self.ip = ""
        self.port = self.DEFAULT_PORT
        self.base_url = ""
        self.connected = False
        self.username = "apc"
        self.password = "apc"
        self.auth = HTTPBasicAuth(self.username, self.password)
        self.session = requests.Session()
        self.timeout = 5
        self.scheme = "http"

    @classmethod
    def probe(cls, ip: str, port: int) -> bool:
        if cls.IP_SUFFIX:
            return ip.endswith(cls.IP_SUFFIX)
        return False

    def connect(self, ip: str, port: int, username: Optional[str] = None, password: Optional[str] = None) -> bool:
        self.ip = ip
        self.port = port
        self.scheme = "https" if port == 443 else self.scheme
        self.base_url = f"{self.scheme}://{self.ip}:{self.port}"
        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        self.auth = HTTPBasicAuth(self.username, self.password)

        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port

        try:
            response = self.session.get(f"{self.base_url}/rest/v1/device", auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            self.connected = True
            self.raw_connection = str(response.text) if hasattr(response, "text") else ""
            logger.info(f"Connected to {self.get_model()} at {disp_ip}:{disp_port}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to {self.get_model()} at {disp_ip}:{disp_port}: {e}")
            raise EquipmentConnectionError(f"Connection to APC PDU failed: {e}")

    def disconnect(self) -> bool:
        self.session.close()
        self.connected = False
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port
        logger.info(f"Disconnected from {self.get_model()} at {disp_ip}:{disp_port}")
        return True

    def get_model(self) -> str:
        return self.MODEL_NAME

    def get_channel_count(self) -> int:
        return self.DEFAULT_CHANNEL_COUNT

    def _control_outlet(self, channel: int, state: str) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/rest/v1/power/outlets/{channel}"
        payload = {"state": state}
        try:
            response = self.session.put(url, json=payload, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            return True, response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to control outlet {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"APC Outlet Control API Error: {e}")

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._control_outlet(channel, "ON")
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._control_outlet(channel, "OFF")
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/rest/v1/power/outlets/{channel}"
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            raw_text = response.text

            state = data.get("state", "").upper()
            status = "ON" if state in ("ON", "1", "TRUE") else ("OFF" if state in ("OFF", "0", "FALSE") else f"UNKNOWN ({state})")
            return PDUResponse(success=True, action="get_status", channel=channel, raw=raw_text, status=status)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get status for outlet {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"APC Outlet Status API Error: {e}")


# ---------------------------------------------------------------------------
# Backward-compatible names.
# The concrete subclasses are now generated by model_factory.py from
# models.yaml.  The dict and individual class names below are re-exported
# so that existing tests and callers that import them by name keep working.
# ---------------------------------------------------------------------------
from equipment_drivers.registry import registry as _registry

def _get_model_classes():
    """Lazy lookup of data-driven model classes from the registry."""
    return {sig: cls for sig, cls in _registry.get_all_drivers('pdu')
            if sig.startswith('apc_')}

# Expose APC_MODELS dict and individual class names via module __getattr__
def __getattr__(name):
    if name == 'APC_MODELS':
        return _get_model_classes()
    models = _get_model_classes()
    # Map old class names to registry signatures
    for sig, cls in models.items():
        if cls.__name__ == name:
            return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
