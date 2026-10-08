import logging
import re
from typing import Dict, Optional, Tuple
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.cli_transport import SshCliTransport
from equipment_drivers.exceptions import EquipmentCommandError, EquipmentConnectionError, EquipmentNotConnectedError

logger = logging.getLogger(__name__)

# Result codes printed by the APC NMC CLI ("E000: Success", "E102: Parameter Error", ...).
# E000/E001 mean the command was accepted; everything else is a failure.
_APC_SUCCESS_CODES = ("000", "001")
_RESULT_CODE_RE = re.compile(r"^\s*E(\d{3}):", re.MULTILINE)
# olStatus lines look like " 3: Outlet 3: On" -- number, outlet name, state.
# prodInfo prints "Key: value" lines, one of them "Model:            AP7920B".
_MODEL_RE = re.compile(r"^\s*Model\s*:\s*(\S+)", re.MULTILINE)
_OUTLET_LINE_RE = re.compile(r"^\s*(\d+):\s*(.*?):\s*(On|Off)\b", re.MULTILINE | re.IGNORECASE)


def parse_result_code(output: str) -> Optional[str]:
    """Return the 3-digit code from the 'Exxx: <message>' line, or None if absent."""
    match = _RESULT_CODE_RE.search(output)
    return match.group(1) if match else None


def parse_model(prod_info: str) -> Optional[str]:
    """Return the value of the 'Model:' line from `prodInfo` output (e.g. 'AP7920B')."""
    match = _MODEL_RE.search(prod_info)
    return match.group(1) if match else None


def parse_outlet_states(output: str) -> Dict[int, str]:
    """Map outlet number -> 'ON'/'OFF' from olStatus output."""
    return {int(num): state.upper() for num, _name, state in _OUTLET_LINE_RE.findall(output)}


class BaseApcPduDriver(PDUDriver):
    """
    Base driver for APC Switched Rack PDUs (AP79xx / AP89xx / AP86xx series).

    APC Network Management Cards have no REST API for outlet control; this
    driver uses the NMC command line over SSH (olOn / olOff / olStatus), so
    PDUResponse.raw is the CLI text the PDU printed, e.g.:

        E000: Success
         3: Outlet 3: On
    """
    MODEL_NAME = "APC Switched Rack PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 22
    IP_SUFFIX = ""
    # Swappable so tests can inject a fake CLI session.
    transport_factory = SshCliTransport

    def __init__(self):
        self.ip = ""
        self.port = self.DEFAULT_PORT
        self.connected = False
        # NMC factory-default credentials.
        self.username = "apc"
        self.password = "apc"
        self.timeout = 10
        self.transport = None
        self._device_channel_count: Optional[int] = None

    @classmethod
    def probe(cls, ip: str, port: int) -> bool:
        if cls.IP_SUFFIX:
            return ip.endswith(cls.IP_SUFFIX)
        return False

    @classmethod
    def identify(cls, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                 password: Optional[str] = None, timeout: float = 3.0) -> Optional[str]:
        """
        Log in and ask the device what it is. Returns the model the PDU reports
        via `prodInfo` (e.g. "AP7920B"), "" if it is an APC NMC that didn't
        report a model, or None if the device doesn't speak the APC NMC CLI.
        """
        defaults = cls()
        transport = cls.transport_factory()
        try:
            transport.open(ip, port or cls.DEFAULT_PORT, username or defaults.username,
                           password or defaults.password, timeout=timeout)
        except EquipmentConnectionError as e:
            logger.debug(f"APC identify: no NMC CLI at {ip}:{port or cls.DEFAULT_PORT}: {e}")
            return None
        try:
            if "Network Management Card" not in getattr(transport, "banner", ""):
                return None
            try:
                return parse_model(transport.send("prodInfo")) or ""
            except Exception as e:
                logger.debug(f"APC identify: prodInfo failed at {ip}: {e}")
                return ""
        finally:
            transport.close()

    def connect(self, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                password: Optional[str] = None) -> bool:
        self.ip = ip
        self.port = port or self.DEFAULT_PORT
        if username is not None:
            self.username = username
        if password is not None:
            self.password = password

        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port

        transport = self.transport_factory()
        try:
            transport.open(self.ip, self.port, self.username, self.password, timeout=self.timeout)
        except EquipmentConnectionError as e:
            logger.error(f"Failed to connect to {self.get_model()} at {disp_ip}:{disp_port}: {e}")
            raise
        self.transport = transport
        self.connected = True
        # The NMC's login banner (firmware versions, name, uptime) is what the device sends on connect.
        self.raw_connection = getattr(transport, "banner", "")
        logger.info(f"Connected to {self.get_model()} at {disp_ip}:{disp_port} (SSH CLI)")
        return True

    def disconnect(self) -> bool:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.connected = False
        self._device_channel_count = None
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port
        logger.info(f"Disconnected from {self.get_model()} at {disp_ip}:{disp_port}")
        return True

    def get_model(self) -> str:
        return self.MODEL_NAME

    def get_channel_count(self) -> int:
        if not self.connected:
            return self.DEFAULT_CHANNEL_COUNT
        # Queried once per session: validate_channel() calls this before every action.
        if self._device_channel_count is None:
            try:
                states = parse_outlet_states(self._run("olStatus all"))
                self._device_channel_count = max(states) if states else self.DEFAULT_CHANNEL_COUNT
            except EquipmentConnectionError:
                raise  # the device stopped answering; don't retry with a guessed count
            except Exception as e:
                logger.warning(f"Could not query outlet count from {self.get_model()}, falling back to default: {e}")
                self._device_channel_count = self.DEFAULT_CHANNEL_COUNT
        return self._device_channel_count

    def _run(self, command: str) -> str:
        if not self.connected or self.transport is None:
            raise EquipmentNotConnectedError("Not connected to PDU")
        try:
            return self.transport.send(command)
        except EquipmentConnectionError as e:
            # Timed out or connection dropped: the session is unusable.
            logger.error(f"CLI command '{command}' failed on {self.get_model()}: {e}")
            raise EquipmentConnectionError(f"APC CLI command '{command}' failed: {e}")
        except Exception as e:
            logger.error(f"CLI command '{command}' failed on {self.get_model()}: {e}")
            raise EquipmentCommandError(f"APC CLI command '{command}' failed: {e}")

    def _run_outlet_command(self, command: str, channel: int) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        raw = self._run(f"{command} {channel}")
        code = parse_result_code(raw)
        if code not in _APC_SUCCESS_CODES:
            logger.error(f"{command} {channel} on {self.get_model()} returned: {raw.strip() or '<no output>'}")
            return False, raw
        return True, raw

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._run_outlet_command("olOn", channel)
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._run_outlet_command("olOff", channel)
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        success, raw = self._run_outlet_command("olStatus", channel)
        status = None
        if success:
            status = parse_outlet_states(raw).get(channel)
            if status is None:
                success = False
                status = "UNKNOWN"
        return PDUResponse(success=success, action="get_status", channel=channel, raw=raw, status=status)


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
