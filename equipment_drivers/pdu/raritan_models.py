import itertools
import logging
from typing import Any, Optional, Tuple
import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentConnectionError, EquipmentCommandError, EquipmentNotConnectedError
from equipment_drivers.pdu.wti_models import _scheme, _tls_hint

logger = logging.getLogger(__name__)

# pdumodel.Outlet.PowerState enum values as they appear on the wire.
_PS_OFF = 0
_PS_ON = 1


class BaseRaritanPduDriver(PDUDriver):
    """
    Base driver for Raritan Intelligent Rack PDUs (PX2, PX3, Dominion PX series).

    Uses the Xerus JSON-RPC 2.0 API: every call is an HTTP POST of
    {"jsonrpc": "2.0", "method": ..., "params": ..., "id": N} to a resource
    ID (RID) path, and the reply carries the return value in result._ret_.
        /model/pdu/0             getMetaData, getOutlets
        /model/pdu/0/outlet/<i>  getState, setPowerState {"pstate": 0|1}   (i = channel - 1)
    PDUResponse.raw is the JSON-RPC response body the PDU returned.
    """
    MODEL_NAME = "Raritan Intelligent Rack PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 80
    IP_SUFFIX = ""
    # None: HTTPS only on port 443. True/False forces it on any port.
    use_https: Optional[bool] = None
    # False skips TLS certificate checks (PDUs usually ship self-signed certificates).
    verify_tls: bool = True

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
        self._request_ids = itertools.count(1)

    @classmethod
    def probe(cls, ip: str, port: int) -> bool:
        if cls.IP_SUFFIX:
            return ip.endswith(cls.IP_SUFFIX)
        return False

    def _rpc(self, rid: str, method: str, params: Optional[dict] = None) -> Tuple[Any, str]:
        """POST one JSON-RPC call to `rid`. Returns (result._ret_, raw response body).
        Raises EquipmentCommandError on transport errors or a JSON-RPC error reply."""
        body = {"jsonrpc": "2.0", "method": method, "id": next(self._request_ids)}
        if params is not None:
            body["params"] = params
        response = self.session.post(f"{self.base_url}{rid}", json=body, auth=self.auth, timeout=self.timeout)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as e:
            raise EquipmentCommandError(f"Raritan {method} on {rid} returned non-JSON body: {e}")
        if "error" in data:
            err = data["error"]
            raise EquipmentCommandError(f"Raritan {method} on {rid} failed: {err.get('code')} {err.get('message')}")
        return data.get("result", {}).get("_ret_"), response.text

    def _set_endpoint(self, ip: str, port: Optional[int], username: Optional[str], password: Optional[str]):
        self.ip = ip
        self.port = port or (443 if self.use_https else self.DEFAULT_PORT)
        self.scheme = _scheme(self.port, self.use_https)
        self.base_url = f"{self.scheme}://{self.ip}:{self.port}"
        self.session.verify = self.verify_tls
        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        self.auth = HTTPDigestAuth(self.username, self.password)

    def _get_metadata(self) -> Tuple[Any, str]:
        """getMetaData on the PDU, retrying with Basic auth if Digest is refused."""
        try:
            return self._rpc("/model/pdu/0", "getMetaData")
        except requests.exceptions.HTTPError as e:
            if e.response is None or e.response.status_code != 401:
                raise
            self.auth = HTTPBasicAuth(self.username, self.password)
            return self._rpc("/model/pdu/0", "getMetaData")

    @classmethod
    def identify(cls, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                 password: Optional[str] = None, timeout: float = 3.0,
                 use_https: Optional[bool] = None, verify_tls: bool = True) -> Optional[str]:
        """
        Ask the device what it is. Returns nameplate.model from getMetaData
        (e.g. "PX3-5460"), "" if it is a Xerus PDU that didn't report one, or
        None if the device doesn't answer the Xerus JSON-RPC API.
        """
        probe = cls()
        probe.timeout = timeout
        probe.use_https = use_https
        probe.verify_tls = verify_tls
        probe._set_endpoint(ip, port, username, password)
        try:
            metadata, _ = probe._get_metadata()
        except (requests.exceptions.RequestException, EquipmentCommandError) as e:
            logger.debug(f"Raritan identify: no Xerus API at {ip}:{probe.port}: {e}")
            return None
        finally:
            probe.session.close()
        if not isinstance(metadata, dict) or not isinstance(metadata.get("nameplate"), dict):
            return None
        return str(metadata["nameplate"].get("model", ""))

    def connect(self, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                password: Optional[str] = None) -> bool:
        self._set_endpoint(ip, port, username, password)

        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port

        try:
            _, raw = self._get_metadata()
            self.connected = True
            self.raw_connection = raw
            logger.info(f"Connected to {self.get_model()} at {disp_ip}:{disp_port}")
            return True
        except (requests.exceptions.RequestException, EquipmentCommandError) as e:
            logger.error(f"Failed to connect to {self.get_model()} at {disp_ip}:{disp_port}: {e}")
            raise EquipmentConnectionError(f"Connection to Raritan PDU failed: {e}{_tls_hint(e)}")

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
        if not self.connected:
            return self.DEFAULT_CHANNEL_COUNT
        try:
            outlets, _ = self._rpc("/model/pdu/0", "getOutlets")
            if isinstance(outlets, list) and outlets:
                return len(outlets)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # The device stopped answering; don't retry with a guessed count.
            raise EquipmentConnectionError(f"Raritan PDU stopped responding: {e}")
        except Exception as e:
            logger.warning(f"Could not query outlet count from {self.get_model()}, falling back to default: {e}")
        return self.DEFAULT_CHANNEL_COUNT

    def _outlet_rpc(self, channel: int, method: str, params: Optional[dict] = None) -> Tuple[Any, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        try:
            return self._rpc(f"/model/pdu/0/outlet/{channel - 1}", method, params)
        except requests.exceptions.RequestException as e:
            logger.error(f"{method} failed for outlet {channel} on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"Raritan Outlet API Error: {e}")

    def _set_power_state(self, channel: int, pstate: int) -> Tuple[bool, str]:
        ret, raw = self._outlet_rpc(channel, "setPowerState", {"pstate": pstate})
        # setPowerState returns 0 on success, a non-zero error code otherwise.
        return ret == 0, raw

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._set_power_state(channel, _PS_ON)
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._set_power_state(channel, _PS_OFF)
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        state, raw = self._outlet_rpc(channel, "getState")
        if not isinstance(state, dict) or not state.get("available", True):
            return PDUResponse(success=False, action="get_status", channel=channel, raw=raw, status="UNKNOWN")
        power_state = state.get("powerState")
        status = {_PS_ON: "ON", _PS_OFF: "OFF"}.get(power_state, f"UNKNOWN ({power_state})")
        return PDUResponse(success=True, action="get_status", channel=channel, raw=raw, status=status)


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
