import logging
from typing import Optional, Tuple
import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentCommandError, EquipmentNotConnectedError

logger = logging.getLogger(__name__)


class BaseRaritanPduDriver(PDUDriver):
    """
    Base driver for Raritan Intelligent Rack PDUs (PX2, PX3, Dominion PX series).
    Communicates via Raritan JSON-RPC / REST API (Xerus OS).
    """
    MODEL_NAME = "Raritan Intelligent Rack PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 80
    IP_SUFFIX = ""

    def __init__(self):
        self.ip = ""
        self.port = self.DEFAULT_PORT
        self.base_url = ""
        self.connected = False
        self.username = "admin"
        self.password = "raritan"
        self.auth = HTTPDigestAuth(self.username, self.password)
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
        self.auth = HTTPDigestAuth(self.username, self.password)

        try:
            response = self.session.get(f"{self.base_url}/model/pdu/0", auth=self.auth, timeout=self.timeout)
            if response.status_code == 401:
                self.auth = HTTPBasicAuth(self.username, self.password)
                response = self.session.get(f"{self.base_url}/model/pdu/0", auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            self.connected = True
            logger.info(f"Connected to {self.get_model()} at {self.ip}:{self.port}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to {self.get_model()} at {self.ip}:{self.port}: {e}")
            raise EquipmentConnectionError(f"Connection to Raritan PDU failed: {e}")

    def disconnect(self) -> bool:
        self.session.close()
        self.connected = False
        logger.info(f"Disconnected from {self.get_model()} at {self.ip}:{self.port}")
        return True

    def get_model(self) -> str:
        return self.MODEL_NAME

    def get_channel_count(self) -> int:
        return self.DEFAULT_CHANNEL_COUNT

    def _control_outlet(self, channel: int, power_state: int) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        outlet_idx = channel - 1
        url = f"{self.base_url}/model/outlet/{outlet_idx}"
        payload = {"powerState": power_state}
        try:
            response = self.session.put(url, json=payload, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            return True, response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to control outlet {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"Raritan Outlet Control API Error: {e}")

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._control_outlet(channel, 1)
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._control_outlet(channel, 0)
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        outlet_idx = channel - 1
        url = f"{self.base_url}/model/outlet/{outlet_idx}"
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            raw_text = response.text

            power_state = data.get("powerState", 0)
            status = "ON" if power_state in (1, "1", "on", "ON", "closed") else "OFF"
            return PDUResponse(success=True, action="get_status", channel=channel, raw=raw_text, status=status)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get status for outlet {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"Raritan Outlet Status API Error: {e}")


# ---------------------------------------------------------------------------
# Backward-compatible names (see apc_models.py for explanation).
# ---------------------------------------------------------------------------
from equipment_drivers.registry import registry as _registry

def _get_model_classes():
    return {sig: cls for sig, cls in _registry.get_all_drivers('pdu')
            if sig.startswith('raritan_')}

def __getattr__(name):
    if name == 'RARITAN_MODELS':
        return _get_model_classes()
    models = _get_model_classes()
    for sig, cls in models.items():
        if cls.__name__ == name:
            return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
