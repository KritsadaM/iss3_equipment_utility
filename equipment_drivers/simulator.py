import json
import logging
import socket
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
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

logger = logging.getLogger(__name__)

# ANSI Color Codes for terminal formatting
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


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

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        query = parse_qs(url.query)

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
                    self.outlet_states[channel] = pstate
                    return self._send_json(200, _jsonrpc_result(req_id, 0))
                return self._send_json(200, _jsonrpc_error(req_id, -32601, "Method not found"))

        self._send_text(404, "Endpoint not found")

    # -- WTI payloads -------------------------------------------------------

    def _wti_unit_status(self) -> dict:
        product = self.model_name.replace("WTI ", "").split()[0] if self.model_name else "VMR-HD4D20"
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
        model = self.model_name.replace("Raritan ", "").split()[0] if self.model_name else "PX3-5460"
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
# The WTI documentation doesn't show an error body; this shape is an assumption.
_WTI_INVALID_PLUG = {"status": {"code": "1", "text": "Invalid plug"}}


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

    def __init__(self, model_name: str, channel_count: int, outlet_states: Dict[int, int]):
        self.model_name = model_name or "APC Switched Rack PDU"
        self.channel_count = channel_count
        self.outlet_states = outlet_states

    def banner(self) -> str:
        now = datetime.now()
        return (
            "\r\n"
            "American Power Conversion               Network Management Card AOS      v6.9.6\r\n"
            "(c) Copyright 2020 All Rights Reserved  RPDU 2g Application               v6.9.6\r\n"
            "-------------------------------------------------------------------------------\r\n"
            f"Name      : {self.model_name[:38]:<38}  Date : {now:%m/%d/%Y}\r\n"
            f"Contact   : Unknown                                 Time : {now:%H:%M:%S}\r\n"
            "Location  : Unknown                                 User : Administrator\r\n"
            "Up Time   : 0 Days 0 Hours 1 Minute                 Stat : P+ N4+ N6+ A+\r\n"
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
            for ch in channels:
                self.outlet_states[ch] = 1 if command == "olon" else 0
            return "E000: Success"
        return "E101: Command Not Found"


_HOST_KEY = None


def _host_key():
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.RSAKey.generate(2048)
    return _HOST_KEY


if paramiko is not None:
    class _ApcSshInterface(paramiko.ServerInterface):
        def __init__(self, username: Optional[str], password: Optional[str]):
            self.username = username
            self.password = password
            self.shell_requested = threading.Event()

        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
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

    def __init__(self, address, engine_factory, username: Optional[str] = None, password: Optional[str] = None):
        if paramiko is None:
            raise RuntimeError("The APC mock server requires the 'paramiko' package (pip install paramiko)")
        self._engine_factory = engine_factory
        self._username = username
        self._password = password
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(address)
        self._sock.listen(5)
        # Poll so shutdown() is noticed without relying on close() waking accept().
        self._sock.settimeout(0.2)
        self.server_port = self._sock.getsockname()[1]
        self._stopping = threading.Event()
        self._stopped = threading.Event()
        self._transports = []

    def serve_forever(self):
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
        self._transports.append(transport)
        try:
            transport.add_server_key(_host_key())
            iface = _ApcSshInterface(self._username, self._password)
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
                 username: Optional[str] = None, password: Optional[str] = None):
        self.vendor = vendor.lower()
        self.channel_count = channel_count
        self.requested_port = port
        self.model_name = model_name
        self.model_signature = model_signature
        self.voltage = voltage
        # Credentials the mock enforces; None accepts any login.
        self.username = username
        self.password = password
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
                lambda: ApcCliEngine(self.model_name, self.channel_count, self.outlet_states),
                username=self.username, password=self.password)
        else:
            class CustomHandler(MockEquipmentHandler):
                pass

            CustomHandler.vendor = self.vendor
            CustomHandler.model_name = self.model_name
            CustomHandler.model_signature = self.model_signature
            CustomHandler.voltage = self.voltage
            CustomHandler.channel_count = self.channel_count
            CustomHandler.outlet_states = self.outlet_states
            self.server = HTTPServer((self.host, self.requested_port), CustomHandler)

        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logger.debug(f"Started Mock {self.vendor.upper()} Server on {self.host}:{self.port}")

    def stop(self):
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
