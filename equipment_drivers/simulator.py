import json
import logging
import socket
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Type

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
    HTTP Request Handler that simulates authentic APC, WTI, and Raritan REST APIs,
    returning structured responses matching real physical PDU equipment.
    """
    vendor: str = "apc"
    model_name: str = "APC AP7900 Switched Rack PDU"
    model_signature: str = "apc_ap7900"
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

    def do_GET(self):
        path = self.path

        # -------------------------------------------------------------
        # APC Mock Endpoints (Matching real APC NMC2/NMC3 REST API)
        # -------------------------------------------------------------
        if self.vendor == "apc":
            if path in ("/rest/v1/device", "/rest/v1/device/"):
                model_id = self.model_signature.replace("apc_", "").upper() if self.model_signature else "AP7900"
                name = self.model_name or f"APC {model_id} Switched Rack PDU"
                return self._send_json(200, {
                    "model": model_id,
                    "name": name,
                    "hardwareRevision": "B2",
                    "firmwareVersion": "v6.9.6",
                    "serialNumber": f"ZA{abs(hash(name)) % 10000000000:010d}",
                    "manufactureDate": "10/24/2020",
                    "macAddress": "00:C0:B7:12:34:56",
                    "status": "operational",
                    "outlets": self.channel_count,
                    "uptime": "124 days, 14:22:10"
                })
            if path.startswith("/rest/v1/power/outlets/"):
                try:
                    channel = int(path.split("/")[-1])
                    state_int = self.outlet_states.get(channel, 1)
                    state_str = "ON" if state_int == 1 else "OFF"
                    voltage = self.voltage
                    current = 1.25 if state_int == 1 else 0.0
                    power = round(voltage * current, 1) if state_int == 1 else 0.0
                    power_factor = 0.98 if state_int == 1 else 0.0
                    energy = 45.2 if state_int == 1 else 0.0
                    return self._send_json(200, {
                        "id": channel,
                        "name": f"Outlet {channel}",
                        "state": state_str,
                        "status": "Normal",
                        "externalId": channel,
                        "voltage": voltage,
                        "current": current,
                        "power": power,
                        "energy": energy,
                        "powerFactor": power_factor
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid outlet id"})

        # -------------------------------------------------------------
        # WTI Mock Endpoints (Matching real WTI REST API v2)
        # -------------------------------------------------------------
        elif self.vendor == "wti":
            if path in ("/api/v2/status", "/api/v2/status/"):
                prod_name = self.model_name.replace("WTI ", "") if self.model_name else "VMR-HD4D20 C19"
                return self._send_json(200, {
                    "status": 0,
                    "status_message": "successful",
                    "product": prod_name,
                    "hostname": "WTI-PDU",
                    "version": "v3.52",
                    "serial_number": f"WT{abs(hash(prod_name)) % 100000000:08d}",
                    "total_plugs": self.channel_count,
                    "active_alarms": 0,
                    "unit_status": "normal",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            if path in ("/api/v2/plugs", "/api/v2/plugs/"):
                plugs = []
                for ch in range(1, self.channel_count + 1):
                    st = self.outlet_states.get(ch, 1)
                    plugs.append({
                        "id": ch,
                        "name": f"Plug {ch}",
                        "status": st,
                        "plug_status": st,
                        "boot_delay": 5,
                        "sequence_delay": 1,
                        "default_state": 1,
                        "current": 1.25 if st == 1 else 0.0,
                        "voltage": self.voltage,
                        "power": round(self.voltage * 1.25, 1) if st == 1 else 0.0
                    })
                return self._send_json(200, plugs)
            if path.startswith("/api/v2/plugs/"):
                try:
                    channel = int(path.split("/")[-1])
                    st = self.outlet_states.get(channel, 1)
                    return self._send_json(200, {
                        "status": st,
                        "status_message": "successful",
                        "id": channel,
                        "name": f"Plug {channel}",
                        "plug_status": st,
                        "boot_delay": 5,
                        "sequence_delay": 1,
                        "default_state": 1,
                        "current": 1.25 if st == 1 else 0.0,
                        "voltage": self.voltage,
                        "power": round(self.voltage * 1.25, 1) if st == 1 else 0.0
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid plug id"})

        # -------------------------------------------------------------
        # Raritan Mock Endpoints (Matching real Raritan Xerus firmware JSON API)
        # -------------------------------------------------------------
        elif self.vendor == "raritan":
            if path in ("/model/pdu/0", "/model/pdu/0/"):
                model_str = self.model_name.replace("Raritan ", "").split()[0] if self.model_name else "PX3-5460"
                return self._send_json(200, {
                    "model": model_str,
                    "name": self.model_name or f"Raritan {model_str} Switched PDU",
                    "serial": f"PXC{abs(hash(model_str)) % 10000000:07d}",
                    "manufacturer": "Raritan",
                    "firmware": "3.6.0.5-47021",
                    "outlets": self.channel_count,
                    "status": "ready",
                    "rating": {
                        "voltage": int(self.voltage),
                        "current": 30,
                        "phases": 3 if self.voltage > 120 else 1
                    }
                })
            if path.startswith("/model/outlet/"):
                try:
                    idx = int(path.split("/")[-1])
                    channel = idx + 1
                    power_state = self.outlet_states.get(channel, 1)
                    return self._send_json(200, {
                        "outlet": idx,
                        "label": f"Outlet {channel}",
                        "powerState": power_state,
                        "isSwitchable": True,
                        "activePower": 150.0 if power_state == 1 else 0.0,
                        "apparentPower": 152.3 if power_state == 1 else 0.0,
                        "voltage": self.voltage,
                        "current": 0.72 if power_state == 1 else 0.0,
                        "powerFactor": 0.98 if power_state == 1 else 0.0,
                        "energy": 124.5 if power_state == 1 else 0.0
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid outlet index"})

        self._send_text(404, "Endpoint not found")

    def do_PUT(self):
        path = self.path
        content_len = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_len).decode('utf-8')
        try:
            payload = json.loads(post_body) if post_body else {}
        except json.JSONDecodeError:
            payload = {}

        # -------------------------------------------------------------
        # APC Mock Endpoints
        # -------------------------------------------------------------
        if self.vendor == "apc":
            if path.startswith("/rest/v1/power/outlets/"):
                try:
                    channel = int(path.split("/")[-1])
                    state = str(payload.get("state", "ON")).upper()
                    self.outlet_states[channel] = 1 if state in ("ON", "1", "TRUE") else 0
                    state_str = "ON" if self.outlet_states[channel] == 1 else "OFF"
                    return self._send_json(200, {
                        "id": channel,
                        "name": f"Outlet {channel}",
                        "state": state_str,
                        "status": "Normal",
                        "commandStatus": "success",
                        "message": f"Outlet {channel} state changed to {state_str} successfully",
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid outlet id"})

        # -------------------------------------------------------------
        # WTI Mock Endpoints
        # -------------------------------------------------------------
        elif self.vendor == "wti":
            if path.startswith("/api/v2/plugs/"):
                try:
                    channel = int(path.split("/")[-1])
                    action = int(payload.get("action", 1))
                    self.outlet_states[channel] = 1 if action == 1 else 0
                    st = self.outlet_states[channel]
                    verb = "ON" if st == 1 else "OFF"
                    return self._send_json(200, {
                        "status": 0,
                        "status_message": "successful",
                        "id": channel,
                        "name": f"Plug {channel}",
                        "action": action,
                        "status": st,
                        "plug_status": st,
                        "message": f"Plug {channel} switched {verb} successfully"
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid plug id"})

        # -------------------------------------------------------------
        # Raritan Mock Endpoints
        # -------------------------------------------------------------
        elif self.vendor == "raritan":
            if path.startswith("/model/outlet/"):
                try:
                    idx = int(path.split("/")[-1])
                    channel = idx + 1
                    power_state = int(payload.get("powerState", 1))
                    self.outlet_states[channel] = 1 if power_state == 1 else 0
                    verb = "ON" if power_state == 1 else "OFF"
                    return self._send_json(200, {
                        "outlet": idx,
                        "label": f"Outlet {channel}",
                        "powerState": power_state,
                        "result": "ok",
                        "status": f"Outlet {channel} switched {verb} successfully"
                    })
                except ValueError:
                    return self._send_json(400, {"error": "Invalid outlet index"})

        self._send_text(404, "Endpoint not found")


class MockPduServer:
    """
    Context manager that starts a local mock HTTP server simulating authentic
    APC, WTI, or Raritan physical PDU behavior and REST APIs.
    """
    def __init__(self, vendor: str = "apc", channel_count: int = 8, port: int = 0,
                 model_name: str = "", model_signature: str = "", voltage: float = 120.0):
        self.vendor = vendor.lower()
        self.channel_count = channel_count
        self.requested_port = port
        self.model_name = model_name
        self.model_signature = model_signature
        self.voltage = voltage
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.host = "127.0.0.1"
        self.port = port

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        class CustomHandler(MockEquipmentHandler):
            pass

        CustomHandler.vendor = self.vendor
        CustomHandler.model_name = self.model_name
        CustomHandler.model_signature = self.model_signature
        CustomHandler.voltage = self.voltage
        CustomHandler.channel_count = self.channel_count
        CustomHandler.outlet_states = {ch: 1 for ch in range(1, self.channel_count + 1)}

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
