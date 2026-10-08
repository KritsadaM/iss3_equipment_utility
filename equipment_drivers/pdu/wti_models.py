import logging
from typing import Optional, Tuple
import requests
from requests.auth import HTTPBasicAuth
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentCommandError, EquipmentNotConnectedError

logger = logging.getLogger(__name__)


def _status_ok(data) -> bool:
    """WTI wraps every reply in {"status": {"code": "0", "text": "OK"}, ...}; code "0" means success."""
    status = data.get("status") if isinstance(data, dict) else None
    return isinstance(status, dict) and str(status.get("code")) == "0"


class BaseWtiPduDriver(PDUDriver):
    """
    Base driver for WTI Switched PDUs and Network Power Switches.

    Uses the WTI RESTful API (same endpoints as WTI's own wti.remote Ansible
    collection):
        GET  /api/v2/status/status                 unit identity
        GET  /api/v2/config/powerplug[?plug=N]     plug state(s)
        POST /api/v2/config/powerplug              {"plug": "N", "state": "on"|"off"|"boot"}
    PDUResponse.raw is the JSON body the PDU returned.
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

    def connect(self, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                password: Optional[str] = None) -> bool:
        self.ip = ip
        self.port = port or self.DEFAULT_PORT
        self.scheme = "https" if self.port == 443 else self.scheme
        self.base_url = f"{self.scheme}://{self.ip}:{self.port}/api/v2"

        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        self.auth = HTTPBasicAuth(self.username, self.password)

        try:
            response = self.session.get(f"{self.base_url}/status/status", auth=self.auth, timeout=self.timeout)
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
            response = self.session.get(f"{self.base_url}/config/powerplug", auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            plugs = response.json().get("powerplugs")
            if isinstance(plugs, list) and plugs:
                return len(plugs)
        except Exception as e:
            logger.warning(f"Could not query channel count from {self.get_model()}, falling back to default: {e}")
        return self.DEFAULT_CHANNEL_COUNT

    def _control_plug(self, channel: int, state: str) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/config/powerplug"
        payload = {"plug": str(channel), "state": state}
        try:
            response = self.session.post(url, json=payload, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to control plug {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"WTI Plug Control API Error: {e}")
        try:
            success = _status_ok(response.json())
        except ValueError:
            success = False
        return success, response.text

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._control_plug(channel, "on")
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._control_plug(channel, "off")
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)

        url = f"{self.base_url}/config/powerplug"
        try:
            response = self.session.get(url, params={"plug": str(channel)}, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get status for plug {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"WTI Plug Status API Error: {e}")
        except ValueError as e:
            raise EquipmentCommandError(f"WTI Plug Status API returned non-JSON body: {e}")

        plug = next((p for p in data.get("powerplugs", []) if str(p.get("plug")) == str(channel)), None)
        if not _status_ok(data) or plug is None:
            return PDUResponse(success=False, action="get_status", channel=channel, raw=response.text,
                               status="UNKNOWN")
        state = str(plug.get("state", "")).lower()
        status = {"on": "ON", "off": "OFF"}.get(state, f"UNKNOWN ({state})")
        return PDUResponse(success=True, action="get_status", channel=channel, raw=response.text, status=status)


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
