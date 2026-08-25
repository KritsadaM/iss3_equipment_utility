import logging
from typing import Optional, Tuple
import requests
from requests.auth import HTTPBasicAuth
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentCommandError, EquipmentNotConnectedError

logger = logging.getLogger(__name__)


class BaseWtiPduDriver(PDUDriver):
    """
    Base driver for WTI Switched PDUs and Network Power Switches.
    Communicates via WTI REST API v2 (/api/v2/plugs).
    """
    MODEL_NAME = "WTI Switched PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 80
    IP_SUFFIX = ""

    def __init__(self):
        self.ip = ""
        self.port = self.DEFAULT_PORT
        self.base_url = ""
        self.connected = False
        self.username = "admin"
        self.password = "admin"
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
        self.base_url = f"{self.scheme}://{self.ip}:{self.port}/api/v2"

        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        self.auth = HTTPBasicAuth(self.username, self.password)

        try:
            response = self.session.get(f"{self.base_url}/status", auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            self.connected = True
            logger.info(f"Connected to {self.get_model()} at {self.ip}:{self.port}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to {self.get_model()} at {self.ip}:{self.port}: {e}")
            raise EquipmentConnectionError(f"Connection to WTI PDU failed: {e}")

    def disconnect(self) -> bool:
        self.session.close()
        self.connected = False
        logger.info(f"Disconnected from {self.get_model()} at {self.ip}:{self.port}")
        return True

    def get_model(self) -> str:
        return self.MODEL_NAME

    def get_channel_count(self) -> int:
        if not self.connected:
            return self.DEFAULT_CHANNEL_COUNT
        try:
            response = self.session.get(f"{self.base_url}/plugs", auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return len(data)
        except Exception as e:
            logger.warning(f"Could not query channel count from {self.get_model()}, falling back to default: {e}")
        return self.DEFAULT_CHANNEL_COUNT

    def _control_plug(self, channel: int, action: int) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/plugs/{channel}"
        payload = {"action": action}
        try:
            response = self.session.put(url, json=payload, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            return True, response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to control plug {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"WTI Plug Control API Error: {e}")

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._control_plug(channel, 1)
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._control_plug(channel, 0)
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/plugs/{channel}"
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            raw_text = response.text

            status_code = data.get("status")
            if status_code in (1, "1", "ON", "on"):
                status = "ON"
            elif status_code in (0, "0", "OFF", "off"):
                status = "OFF"
            else:
                status = f"UNKNOWN ({status_code})"
            return PDUResponse(success=True, action="get_status", channel=channel, raw=raw_text, status=status)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get status for plug {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"WTI Plug Status API Error: {e}")


# ---------------------------------------------------------------------------
# Backward-compatible names (see apc_models.py for explanation).
# ---------------------------------------------------------------------------
from equipment_drivers.registry import registry as _registry

def _get_model_classes():
    return {sig: cls for sig, cls in _registry.get_all_drivers('pdu')
            if sig.startswith('wti_')}

def __getattr__(name):
    if name == 'WTI_MODELS':
        return _get_model_classes()
    models = _get_model_classes()
    for sig, cls in models.items():
        if cls.__name__ == name:
            return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
