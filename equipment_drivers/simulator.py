import json
import logging
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Type
from urllib.parse import parse_qs, urlparse

try:
    import paramiko
except ImportError:  # only needed for the APC (SSH CLI) mock
    paramiko = None

# Ensure equipment_drivers interfaces and registry are importable
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.registry import registry
from equipment_drivers.channel_spec import parse_channels
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentError, EquipmentCommandError, EquipmentConnectionError

logger = logging.getLogger(__name__)

# ANSI Color Codes for terminal formatting
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# Faults a mock PDU can simulate, so error handling can be exercised without hardware.
FAULTS = {
    "auth_fail": "rejects every login (HTTP 401 / SSH auth failure)",
    "timeout": "accepts the connection, then never answers a command",
    "drop": "accepts the connection, then drops it on the first command",
    "command_error": "answers outlet commands with the vendor's error reply",
    "stuck_outlet": "reports outlet commands as successful but never changes outlet state",
}
# How long a "timeout" fault holds a request open (released early when the mock stops).
FAULT_HANG_SECONDS = 30


def part_number(model_name: str, default: str) -> str:
    """The part number in a display name: its first word containing a digit
    ("Raritan Dominion PX DPXR8A-16" -> "DPXR8A-16")."""
    for word in (model_name or "").split():
        if any(ch.isdigit() for ch in word):
            return word
    return default


class MockEquipmentHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler that simulates the WTI RESTful API and the Raritan
    Xerus JSON-RPC API. Response shapes follow the vendors' published
    documentation (see tests/fixtures/README.md for sources). APC PDUs are
    CLI-only and are simulated by _ApcSshServer instead.
    """
    vendor: str = "wti"
    model_name: str = ""
    model_signature: str = ""
    channel_count: int = 8
    voltage: float = 120.0
    outlet_states: Dict[int, int] = {}  # channel -> 1 (ON) or 0 (OFF)
    fault: Optional[str] = None
    stop_event: threading.Event = threading.Event()

    def log_message(self, format, *args):
        # Suppress standard http.server stdout logging during trials
        pass

    def _send_json(self, status_code: int, data: dict | list):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status_code: int, text: str):
        body = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return None

    def _is_connect_request(self, path: str, payload=None) -> bool:
        """The request a driver makes in connect(); connection-level faults let it through."""
        if self.vendor == "wti":
            return path == "/api/v2/status/status"
        return path == "/model/pdu/0" and isinstance(payload, dict) and payload.get("method") == "getMetaData"

    def _fault_consumed(self, path: str, payload=None) -> bool:
        """Apply connection-level faults. Returns True if the request was handled by the fault."""
        if self.fault == "auth_fail":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="PDU"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if self.fault in ("timeout", "drop") and not self._is_connect_request(path, payload):
            if self.fault == "timeout":
                self.stop_event.wait(FAULT_HANG_SECONDS)
            # Close without sending a response.
            self.close_connection = True
            return True
        return False

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        query = parse_qs(url.query)
        if self._fault_consumed(path):
            return

        if self.vendor == "wti":
            if path == "/api/v2/status/status":
                return self._send_json(200, self._wti_unit_status())
            if path == "/api/v2/config/powerplug":
                plug_ids = query.get("plug")
                if not plug_ids:
                    channels = list(range(1, self.channel_count + 1))
                else:
                    try:
                        channels = [int(plug_ids[0])]
                    except ValueError:
                        return self._send_json(200, _WTI_INVALID_PLUG)
                    if not 1 <= channels[0] <= self.channel_count:
                        return self._send_json(200, _WTI_INVALID_PLUG)
                return self._send_json(200, {"status": _WTI_OK,
                                             "powerplugs": [self._wti_plug(ch) for ch in channels]})

        self._send_text(404, "Endpoint not found")

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        payload = self._read_json_body()
        if self._fault_consumed(path, payload):
            return

        if self.vendor == "wti" and path == "/api/v2/config/powerplug":
            if not isinstance(payload, dict):
                return self._send_json(400, _WTI_INVALID_PLUG)
            try:
                channel = int(payload.get("plug", ""))
            except ValueError:
                return self._send_json(200, _WTI_INVALID_PLUG)
            state = str(payload.get("state", "")).lower()
            if not 1 <= channel <= self.channel_count or state not in ("on", "off", "boot", "default"):
                return self._send_json(200, _WTI_INVALID_PLUG)
            if self.fault == "command_error":
                return self._send_json(200, _WTI_COMMAND_FAILED)
            if self.fault != "stuck_outlet":
                # "boot" power-cycles and "default" restores the configured default -- both end up ON here.
                self.outlet_states[channel] = 0 if state == "off" else 1
            return self._send_json(200, {"status": _WTI_OK, "powerplugs": [self._wti_plug(channel)]})

        if self.vendor == "raritan":
            if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                return self._send_json(200, _jsonrpc_error(None, -32700, "Parse error"))
            req_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}

            if path == "/model/pdu/0":
                if method == "getMetaData":
                    return self._send_json(200, _jsonrpc_result(req_id, self._raritan_metadata()))
                if method == "getOutlets":
                    outlets = [{"rid": f"/model/pdu/0/outlet/{i}", "type": "pdumodel.Outlet:2.1.4"}
                               for i in range(self.channel_count)]
                    return self._send_json(200, _jsonrpc_result(req_id, outlets))
                return self._send_json(200, _jsonrpc_error(req_id, -32601, "Method not found"))

            if path.startswith("/model/pdu/0/outlet/"):
                try:
                    idx = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    idx = -1
                if not 0 <= idx < self.channel_count:
                    return self._send_text(404, "Resource not found")
                channel = idx + 1
                if method == "getState":
                    return self._send_json(200, _jsonrpc_result(req_id, self._raritan_outlet_state(channel)))
                if method == "setPowerState":
                    pstate = params.get("pstate")
                    if pstate not in (0, 1):
                        return self._send_json(200, _jsonrpc_error(req_id, -32602, "Invalid params"))
                    if self.fault == "command_error":
                        # setPowerState returns a non-zero error code on failure.
                        return self._send_json(200, _jsonrpc_result(req_id, 1))
                    if self.fault != "stuck_outlet":
                        self.outlet_states[channel] = pstate
                    return self._send_json(200, _jsonrpc_result(req_id, 0))
                return self._send_json(200, _jsonrpc_error(req_id, -32601, "Method not found"))

        self._send_text(404, "Endpoint not found")

    # -- WTI payloads -------------------------------------------------------

    def _wti_unit_status(self) -> dict:
        product = part_number(self.model_name, "VMR-HD4D20")
        return {
            "status": _WTI_OK,
            "vendor": "wti",
            "product": product,
            "totalports": "0",
            "totalplugs": str(self.channel_count),
            "softwareversion": "6.60 19 Feb 2020",
            "serialnumber": f"{abs(hash(product)) % 10**14:014d}",
            "assettag": "",
            "siteid": "",
        }

    def _wti_plug(self, channel: int) -> dict:
        return {
            "plug": str(channel),
            "plugname": f"Outlet_{channel}",
            "state": "on" if self.outlet_states.get(channel, 1) == 1 else "off",
            "busy": "0",
            "bootdelay": "0.5 Secs",
            "priority": str(channel),
            "plugoffreason": "0",
        }

    # -- Raritan payloads ---------------------------------------------------

    def _raritan_metadata(self) -> dict:
        model = part_number(self.model_name, "PX3-5460")
        return {
            "nameplate": {
                "manufacturer": "Raritan",
                "brand": "",
                "model": model,
                "partNumber": "",
                "serialNumber": f"R{abs(hash(model)) % 10**10:010d}",
                "rating": {
                    "voltage": "200-240V" if self.voltage > 120 else "100-120V",
                    "current": "30A",
                    "frequency": "50/60Hz",
                    "power": "",
                },
            },
            "fwRevision": "4.1.0.5-49727",
            "macAddress": "00:0d:5d:12:34:56",
            "hasSwitchableOutlets": True,
            "hasMeteredOutlets": True,
        }

    def _raritan_outlet_state(self, channel: int) -> dict:
        return {
            "available": True,
            "powerState": self.outlet_states.get(channel, 1),
            "switchOnInProgress": False,
            "cycleInProgress": False,
            "isLoadShed": False,
            "lastPowerStateChange": int(datetime.now(timezone.utc).timestamp()),
        }


_WTI_OK = {"code": "0", "text": "OK"}
# The WTI documentation doesn't show error bodies; these shapes are assumptions.
_WTI_INVALID_PLUG = {"status": {"code": "1", "text": "Invalid plug"}}
_WTI_COMMAND_FAILED = {"status": {"code": "2", "text": "Command failed"}}


def _jsonrpc_result(req_id, ret) -> dict:
    return {"jsonrpc": "2.0", "result": {"_ret_": ret}, "id": req_id}


def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


# ---------------------------------------------------------------------------
# APC NMC CLI simulator (served over SSH, like the real Network Management Card)
# ---------------------------------------------------------------------------

APC_PROMPT = "apc>"


class ApcCliEngine:
    """Executes APC NMC CLI command lines against the mock's outlet state and
    returns the text the real CLI would print (without the trailing prompt)."""

    def __init__(self, model_name: str, channel_count: int, outlet_states: Dict[int, int],
                 fault: Optional[str] = None):
        self.model_name = model_name or "APC Switched Rack PDU"
        self.channel_count = channel_count
        self.outlet_states = outlet_states
        self.fault = fault

    def banner(self) -> str:
        # Layout copied from a Schneider/APC rack PDU login captured in
        # openbmc-test-automation (lib/pdu/schneider.robot); only the date and time vary.
        now = datetime.now()
        return (
            "\r\n"
            "Schneider Electric                      Network Management Card AOS      v6.9.6\r\n"
            "(c) Copyright 2020 All Rights Reserved  RPDU 2g APP                      v6.9.6\r\n"
            "-------------------------------------------------------------------------------\r\n"
            f"Name      : apc566BF4                                 Date : {now:%m/%d/%Y}\r\n"
            f"Contact   : Unknown                                   Time : {now:%H:%M:%S}\r\n"
            "Location  : Unknown                                   User : Super User\r\n"
            "Up Time   : 0 Days 12 Hours 17 Minutes                Stat : P+ N4+ N6+ A+\r\n"
            "\r\n"
            "\r\n"
            "Type ? for command listing\r\n"
            "Use tcpip command for IP address(-i), subnet(-s), and gateway(-g)\r\n"
            "\r\n"
        )

    def execute(self, line: str) -> Optional[str]:
        """Returns the command output, or None when the session should end."""
        parts = line.split()
        if not parts:
            return ""
        command = parts[0].lower()
        arg = " ".join(parts[1:])

        if command in ("exit", "quit", "bye"):
            return None
        if command == "prodinfo":
            # Key/value layout as documented by AVI-SPL's APC PDU driver (observed on an AP7920B).
            return "\n".join([
                "AOS:              v6.9.6",
                "Switched Rack PDU: v6.9.6",
                f"Model:            {part_number(self.model_name, 'AP7900')}",
                f"Present Outlets:  {self.channel_count}",
                "Max Current:      16 A",
                "Present Phases:   1",
            ])
        if command in ("olon", "oloff", "olstatus"):
            if not arg:
                return "E102: Parameter Error"
            try:
                channels = parse_channels(arg, channel_count=self.channel_count)
            except ValueError:
                return "E102: Parameter Error"
            if command == "olstatus":
                lines = ["E000: Success"]
                for ch in channels:
                    state = "On" if self.outlet_states.get(ch, 1) == 1 else "Off"
                    lines.append(f" {ch}: Outlet {ch}: {state}")
                return "\n".join(lines)
            if self.fault == "command_error":
                return "E100: Command failed"
            if self.fault != "stuck_outlet":
                for ch in channels:
                    self.outlet_states[ch] = 1 if command == "olon" else 0
            return "E000: Success"
        return "E101: Command Not Found"


_HOST_KEY = None
_TLS_CERT = None


def _self_signed_cert():
    """(certfile, keyfile) for a throwaway self-signed localhost certificate, like
    the ones PDUs ship with. Generated once per process with the openssl CLI."""
    global _TLS_CERT
    if _TLS_CERT is None:
        directory = tempfile.mkdtemp(prefix="iss-mock-tls-")
        cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                            "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise RuntimeError(f"The TLS mock needs the openssl command to make a certificate: {e}")
        _TLS_CERT = (cert, key)
    return _TLS_CERT
_SSH_SERVER_LOG = "equipment_drivers.simulator.ssh_server"
logging.getLogger(_SSH_SERVER_LOG).setLevel(logging.CRITICAL)


def _host_key():
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.RSAKey.generate(2048)
    return _HOST_KEY


if paramiko is not None:
    class _ApcSshInterface(paramiko.ServerInterface):
        def __init__(self, username: Optional[str], password: Optional[str], reject_all: bool = False):
            self.username = username
            self.password = password
            self.reject_all = reject_all
            self.shell_requested = threading.Event()

        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            if self.reject_all:
                return paramiko.AUTH_FAILED
            if self.username is None or (username, password) == (self.username, self.password):
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def check_channel_request(self, kind, chanid):
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
            return True

        def check_channel_shell_request(self, channel):
            self.shell_requested.set()
            return True


class _ApcSshServer:
    """Minimal SSH server presenting the APC NMC CLI. Exposes the same
    serve_forever/shutdown/server_close/server_port surface as HTTPServer so
    MockPduServer can drive either one."""

    def __init__(self, address, engine_factory, username: Optional[str] = None, password: Optional[str] = None,
                 fault: Optional[str] = None):
        if paramiko is None:
            raise RuntimeError("The APC mock server requires the 'paramiko' package (pip install paramiko)")
        self._engine_factory = engine_factory
        self._username = username
        self._password = password
        self._fault = fault
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(address)
        self._sock.listen(5)
        self.server_port = self._sock.getsockname()[1]
        # accept() polls (see serve_forever) so shutdown() is noticed without relying on close() waking it.
        self._stopping = threading.Event()
        self._stopped = threading.Event()
        self._transports = []

    def serve_forever(self, poll_interval: float = 0.2):
        self._sock.settimeout(poll_interval)
        try:
            while not self._stopping.is_set():
                try:
                    client, _addr = self._sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
        finally:
            self._stopped.set()

    def shutdown(self):
        self._stopping.set()
        self._stopped.wait(timeout=2.0)
        for transport in list(self._transports):
            transport.close()

    def server_close(self):
        self._sock.close()

    def _handle_client(self, client_sock):
        transport = paramiko.Transport(client_sock)
        # Non-SSH clients (e.g. HTTP identify probes from discovery) are normal here; keep
        # paramiko's server-side tracebacks out of the console.
        transport.set_log_channel(_SSH_SERVER_LOG)
        self._transports.append(transport)
        try:
            transport.add_server_key(_host_key())
            iface = _ApcSshInterface(self._username, self._password, reject_all=self._fault == "auth_fail")
            try:
                transport.start_server(server=iface)
            except paramiko.SSHException:
                return
            chan = transport.accept(timeout=10)
            if chan is None or not iface.shell_requested.wait(timeout=10):
                return
            self._run_shell(chan, self._engine_factory())
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()
            if transport in self._transports:
                self._transports.remove(transport)

    def _run_shell(self, chan, engine: "ApcCliEngine"):
        chan.sendall((engine.banner() + APC_PROMPT).encode())
        if self._fault == "timeout":
            # Swallow everything and never answer, until the client gives up or the mock stops.
            chan.settimeout(0.2)
            while not self._stopping.is_set():
                try:
                    if not chan.recv(1024):
                        return
                except socket.timeout:
                    continue
            return
        line = ""
        last_was_cr = False
        while not self._stopping.is_set():
            data = chan.recv(1024)
            if not data:
                return
            for ch in data.decode("utf-8", errors="replace"):
                if ch == "\n" and last_was_cr:
                    last_was_cr = False
                    continue
                last_was_cr = ch == "\r"
                if ch in "\r\n":
                    if self._fault == "drop":
                        chan.close()
                        return
                    chan.sendall(b"\r\n")
                    output = engine.execute(line)
                    line = ""
                    if output is None:
                        chan.sendall(b"Connection Closed - Bye\r\n")
                        chan.close()
                        return
                    if output:
                        chan.sendall((output.replace("\n", "\r\n") + "\r\n").encode())
                    chan.sendall(APC_PROMPT.encode())
                elif ch in "\x08\x7f":
                    if line:
                        line = line[:-1]
                        chan.sendall(b"\x08 \x08")
                else:
                    line += ch
                    chan.sendall(ch.encode())  # terminal echo, as the real CLI does


class MockPduServer:
    """
    Context manager that starts a local mock PDU: an SSH CLI server for APC,
    an HTTP server for WTI (REST) and Raritan (JSON-RPC).
    """
    def __init__(self, vendor: str = "apc", channel_count: int = 8, port: int = 0,
                 model_name: str = "", model_signature: str = "", voltage: float = 120.0,
                 username: Optional[str] = None, password: Optional[str] = None, fault: Optional[str] = None,
                 tls: bool = False):
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"Unknown fault '{fault}'. Choose from: {', '.join(FAULTS)}")
        self.vendor = vendor.lower()
        self.channel_count = channel_count
        self.requested_port = port
        self.model_name = model_name
        self.model_signature = model_signature
        self.voltage = voltage
        # Credentials the mock enforces; None accepts any login.
        self.username = username
        self.password = password
        self.fault = fault
        # HTTPS with a self-signed certificate (WTI/Raritan only; APC is SSH).
        self.tls = tls
        self._stop_event = threading.Event()
        self.server = None
        self.thread: Optional[threading.Thread] = None
        self.host = "127.0.0.1"
        self.port = port
        self.outlet_states: Dict[int, int] = {}

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        self.outlet_states = {ch: 1 for ch in range(1, self.channel_count + 1)}

        if self.vendor == "apc":
            self.server = _ApcSshServer(
                (self.host, self.requested_port),
                lambda: ApcCliEngine(self.model_name, self.channel_count, self.outlet_states, fault=self.fault),
                username=self.username, password=self.password, fault=self.fault)
        else:
            class CustomHandler(MockEquipmentHandler):
                pass

            CustomHandler.vendor = self.vendor
            CustomHandler.model_name = self.model_name
            CustomHandler.model_signature = self.model_signature
            CustomHandler.voltage = self.voltage
            CustomHandler.channel_count = self.channel_count
            CustomHandler.outlet_states = self.outlet_states
            CustomHandler.fault = self.fault
            CustomHandler.stop_event = self._stop_event
            # Threaded so a request held open by the "timeout" fault can't block shutdown.
            self.server = ThreadingHTTPServer((self.host, self.requested_port), CustomHandler)
            self.server.daemon_threads = True
            self.server.block_on_close = False
            if self.tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(*_self_signed_cert())
                self.server.socket = context.wrap_socket(self.server.socket, server_side=True)

        self.port = self.server.server_port
        self._stop_event.clear()
        # A short poll interval keeps stop() fast (HTTPServer's default 0.5 s dominated trial runtimes).
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        logger.debug(f"Started Mock {self.vendor.upper()} Server on {self.host}:{self.port}")

    def stop(self):
        self._stop_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
            self.thread = None
        logger.debug("Stopped Mock Server")


def determine_vendor(signature: str, driver_cls) -> str:
    sig = signature.lower()
    name = driver_cls.__name__.lower() if hasattr(driver_cls, "__name__") else ""
    if "apc" in sig or "apc" in name:
        return "apc"
    elif "wti" in sig or "wti" in name:
        return "wti"
    elif "raritan" in sig or "raritan" in name:
        return "raritan"
    elif "dummy" in sig or "dummy" in name:
        return "dummy"
    return "apc"


def determine_voltage(signature: str, model_name: str) -> float:
    sig = signature.lower()
    name = model_name.lower()
    if "208" in name or "208v" in name or "230" in name or "eu3" in sig or "3-phase" in name or "raritan" in sig:
        return 208.0
    return 120.0


def run_pdu_mock_action(sig: str, driver_cls, action: str, channel_spec: str = "1", verbose: bool = True) -> bool:
    """
    Executes a single or multi-channel action (on, off, status) against a mock PDU server
    and prints formatted outputs and RAW_OUTPUT matching real physical equipment.
    """
    vendor = determine_vendor(sig, driver_cls)
    temp_driver = driver_cls()
    model_name = temp_driver.get_model()
    max_channels = temp_driver.get_max_channel()
    voltage = determine_voltage(sig, model_name)

    if verbose:
        print(f"\n{BOLD}========================================================================")
        print(f" MOCK EXECUTION: {model_name}")
        print(f" Signature: {sig} | Action: {action.upper()} | Channels: {channel_spec}")
        print(f" Mode: {CYAN}MOCK SIMULATION (Real PDU Response Reproduction){RESET}")
        print(f"========================================================================{RESET}")

    with MockPduServer(vendor=vendor, channel_count=max_channels, model_name=model_name,
                       model_signature=sig, voltage=voltage) as server:
        driver = driver_cls()
        driver.connect(server.host, server.port)

        try:
            channels = parse_channels(channel_spec, channel_count=max_channels)
        except ValueError as ve:
            if verbose:
                print(f"\n{RED}[USAGE ERROR]{RESET} {ve}")
            return False

        all_success = True
        for ch in channels:
            if action == "on":
                resp = driver.turn_on(ch)
            elif action == "off":
                resp = driver.turn_off(ch)
            else:
                resp = driver.get_status(ch)

            resp.model = model_name

            if verbose:
                status_tag = f"{GREEN}[SUCCESS]{RESET}" if resp.success else f"{RED}[FAILED]{RESET}"
                if resp.action == "get_status":
                    print(f"\nChannel {ch} Status: {BOLD}{resp.status}{RESET} -> {status_tag}")
                else:
                    verb = "ON" if resp.action == "turn_on" else "OFF"
                    print(f"\nChannel {ch} switched {BOLD}{verb}{RESET} -> {status_tag}")

                print(f"{YELLOW}RAW_OUTPUT:{RESET}\n{DIM}{resp.raw.strip()}{RESET}")

            if not resp.success:
                all_success = False

        driver.disconnect()
        return all_success


def run_pdu_blackbox_trial(sig: str, driver_cls, live_ip: str = None, live_port: int = None, verbose: bool = True) -> bool:
    """
    Executes a complete 9-phase blackbox trial on a PDU model using local mock simulator
    with authentic PDU device responses.
    """
    vendor = determine_vendor(sig, driver_cls)
    temp_driver = driver_cls()
    model_name = temp_driver.get_model()
    max_channels = temp_driver.get_max_channel()
    voltage = determine_voltage(sig, model_name)

    if verbose:
        print(f"\n{BOLD}========================================================================")
        print(f" TRIAL: {model_name}")
        print(f" Signature: {sig} | Vendor Family: {vendor.upper()} | Outlets: {max_channels}")
        print(f" Mode: {CYAN}MOCK SIMULATION (Real PDU Device Reproduction){RESET}")
        print(f"========================================================================{RESET}")

    all_passed = True

    def log_step(step_no: int, description: str, passed: bool, details: str = "", raw: str = ""):
        nonlocal all_passed
        status_str = f"{GREEN}[PASS]{RESET}" if passed else f"{RED}[FAIL]{RESET}"
        if verbose:
            print(f"\n{BOLD}Step {step_no}: {description}{RESET} -> {status_str}")
            if details:
                print(f"  {DIM}{details}{RESET}")
            if raw:
                print(f"  {YELLOW}RAW_OUTPUT:{RESET}\n{DIM}{raw.strip()}{RESET}")
        if not passed:
            all_passed = False

    # Spin up local mock server simulating physical hardware
    server_ctx = MockPduServer(vendor=vendor, channel_count=max_channels, model_name=model_name,
                               model_signature=sig, voltage=voltage)
    server_ctx.start()
    target_ip = server_ctx.host
    target_port = server_ctx.port

    try:
        driver = driver_cls()

        # Step 1: Connect
        try:
            connected = driver.connect(target_ip, target_port)
            log_step(1, f"Connect to Mock Server ({target_ip}:{target_port})", connected, f"Connected status: {driver.connected}")
        except Exception as e:
            log_step(1, f"Connect to Mock Server", False, f"Exception: {e}")
            return False

        # Step 2: Validate Model and Channel Count Reporting
        detected_model = driver.get_model()
        reported_channels = driver.get_max_channel()
        ch_ok = (reported_channels == max_channels)
        log_step(2, "Query Model & Channel Metadata", ch_ok, f"Model: '{detected_model}', Max Channel: {reported_channels}")

        # Step 3: Check Initial Channel 1 Status
        try:
            resp_init = driver.get_status(1)
            log_step(3, "Query Initial Status for Channel 1", resp_init.success,
                     f"Status: {resp_init.status}, Action: {resp_init.action}, Channel: {resp_init.channel}",
                     raw=resp_init.raw)
        except Exception as e:
            log_step(3, "Query Initial Status for Channel 1", False, f"Exception: {e}")

        # Step 4: Turn ON Channel 1
        try:
            resp_on = driver.turn_on(1)
            log_step(4, "Execute Turn ON for Channel 1", resp_on.success,
                     f"Success: {resp_on.success}, Action: {resp_on.action}",
                     raw=resp_on.raw)
        except Exception as e:
            log_step(4, "Execute Turn ON for Channel 1", False, f"Exception: {e}")

        # Step 5: Verify Status is ON
        try:
            resp_verify_on = driver.get_status(1)
            is_on = (resp_verify_on.status == "ON")
            log_step(5, "Verify Channel 1 State is ON", is_on,
                     f"Current Status: {resp_verify_on.status}",
                     raw=resp_verify_on.raw)
        except Exception as e:
            log_step(5, "Verify Channel 1 State is ON", False, f"Exception: {e}")

        # Step 6: Turn OFF Channel 1
        try:
            resp_off = driver.turn_off(1)
            log_step(6, "Execute Turn OFF for Channel 1", resp_off.success,
                     f"Success: {resp_off.success}, Action: {resp_off.action}",
                     raw=resp_off.raw)
        except Exception as e:
            log_step(6, "Execute Turn OFF for Channel 1", False, f"Exception: {e}")

        # Step 7: Boundary Check: Channel 0 (Must reject with ValueError)
        try:
            driver.turn_on(0)
            log_step(7, "Boundary Check: Channel 0 Rejection", False, "Expected ValueError was NOT raised for Channel 0")
        except ValueError as ve:
            log_step(7, "Boundary Check: Channel 0 Rejection", True, f"Correctly caught usage error: '{ve}'")
        except Exception as e:
            log_step(7, "Boundary Check: Channel 0 Rejection", False, f"Wrong exception type: {type(e).__name__}: {e}")

        # Step 8: Boundary Check: Upper Bound Overflow (Max + 1)
        overflow_ch = max_channels + 1
        try:
            driver.turn_on(overflow_ch)
            log_step(8, f"Boundary Check: Channel {overflow_ch} (> Max {max_channels}) Rejection", False,
                     f"Expected ValueError was NOT raised for Channel {overflow_ch}")
        except ValueError as ve:
            log_step(8, f"Boundary Check: Channel {overflow_ch} (> Max {max_channels}) Rejection", True,
                     f"Correctly caught usage error: '{ve}'")
        except Exception as e:
            log_step(8, f"Boundary Check: Channel {overflow_ch} Rejection", False, f"Wrong exception: {e}")

        # Step 9: Disconnect
        try:
            disconnected = driver.disconnect()
            log_step(9, "Disconnect Session", disconnected, "Clean session shutdown")
        except Exception as e:
            log_step(9, "Disconnect Session", False, f"Exception: {e}")

    finally:
        server_ctx.stop()

    if verbose:
        print(f"\n{BOLD}------------------------------------------------------------------------")
        if all_passed:
            print(f" RESULT: {GREEN}ALL BLACKBOX CHECKS PASSED FOR {model_name.upper()}{RESET}")
        else:
            print(f" RESULT: {RED}ONE OR MORE CHECKS FAILED FOR {model_name.upper()}{RESET}")
        print(f"------------------------------------------------------------------------{RESET}\n")

    return all_passed


def _fault_scenario(fault: str, driver, host: str, port: int) -> tuple:
    """Run one fault against a fresh driver. Returns (passed, detail).

    A pass means the driver turned the device failure into its own error
    handling: an EquipmentError or success=False -- never an unexpected
    exception type, and never a false success that a readback can't expose.
    """
    if fault == "auth_fail":
        try:
            driver.connect(host, port)
        except EquipmentConnectionError as e:
            return True, f"connect() raised EquipmentConnectionError: {e}"
        return False, "connect() succeeded although every login is rejected"

    driver.connect(host, port)
    try:
        if fault in ("timeout", "drop"):
            try:
                resp = driver.turn_off(1)
            except EquipmentError as e:
                return True, f"turn_off raised {type(e).__name__}: {e}"
            return False, f"turn_off returned success={resp.success} from an unresponsive device"

        if fault == "command_error":
            try:
                resp = driver.turn_off(1)
            except EquipmentCommandError as e:
                return True, f"turn_off raised EquipmentCommandError: {e}"
            if resp.success:
                return False, f"turn_off reported success for an error reply: {resp.raw.strip()}"
            return True, f"turn_off returned success=False, raw: {resp.raw.strip()}"

        if fault == "stuck_outlet":
            resp = driver.turn_off(1)
            status = driver.get_status(1).status
            if status == "OFF":
                return False, "readback shows OFF although the outlet never changed"
            return True, (f"turn_off reported success={resp.success} (the device claims success); "
                          f"readback shows {status}, exposing the stuck outlet")
    finally:
        try:
            driver.disconnect()
        except Exception:
            pass
    raise ValueError(f"No scenario for fault '{fault}'")


def run_pdu_fault_trial(sig: str, driver_cls, verbose: bool = True, timeout: float = 1.0) -> bool:
    """
    Runs every simulated fault (see FAULTS) against one PDU model and checks the
    driver reports each failure through its own error handling. Driver timeouts
    are shortened to `timeout` seconds so the run stays quick.
    """
    vendor = determine_vendor(sig, driver_cls)
    temp_driver = driver_cls()
    model_name = temp_driver.get_model()
    max_channels = temp_driver.get_max_channel()
    voltage = determine_voltage(sig, model_name)

    if verbose:
        print(f"\n{BOLD}========================================================================")
        print(f" FAULT TRIAL: {model_name}")
        print(f" Signature: {sig} | Vendor Family: {vendor.upper()} | Faults: {len(FAULTS)}")
        print(f"========================================================================{RESET}")

    all_passed = True
    for step_no, fault in enumerate(FAULTS, start=1):
        with MockPduServer(vendor=vendor, channel_count=max_channels, model_name=model_name,
                           model_signature=sig, voltage=voltage, fault=fault) as server:
            driver = driver_cls()
            driver.timeout = timeout
            started = time.monotonic()
            try:
                passed, detail = _fault_scenario(fault, driver, server.host, server.port)
            except Exception as e:
                passed, detail = False, f"unexpected {type(e).__name__}: {e}"
            elapsed = time.monotonic() - started
        all_passed = all_passed and passed
        if verbose:
            tag = f"{GREEN}[PASS]{RESET}" if passed else f"{RED}[FAIL]{RESET}"
            print(f"\n{BOLD}Fault {step_no}: {fault}{RESET} ({FAULTS[fault]}) -> {tag} {DIM}[{elapsed:.1f}s]{RESET}")
            print(f"  {DIM}{detail}{RESET}")

    if verbose:
        print(f"\n{BOLD}------------------------------------------------------------------------")
        verdict = (f"{GREEN}ALL FAULTS HANDLED" if all_passed else f"{RED}ONE OR MORE FAULTS MISHANDLED")
        print(f" RESULT: {verdict} FOR {model_name.upper()}{RESET}")
        print(f"------------------------------------------------------------------------{RESET}\n")
    return all_passed
