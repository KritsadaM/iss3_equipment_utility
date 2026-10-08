"""
PDU control CLI shared by iss_pdu_utility (official) and iss_pdu_utility_eng
(engineering). The two differ only in that the engineering edition requires the
simulator; Buy-off mode is available in either wherever simulator.py is installed.
"""
import argparse
import contextlib
import json
import logging
import os
import sys
import time

import equipment_drivers  # noqa: F401 -- triggers driver registration
from equipment_drivers.discovery import discover_and_instantiate
from equipment_drivers.registry import registry
from equipment_drivers.channel_spec import parse_channels
from equipment_drivers.exceptions import EquipmentConnectionError
from equipment_drivers.capture import CaptureSession

try:
    # simulator.py is stripped from the official package (see Makefile).
    from equipment_drivers.simulator import MockPduServer, determine_vendor, determine_voltage, FAULTS
    HAS_MOCK = True
except ImportError:
    HAS_MOCK = False
    FAULTS = {}

logger = logging.getLogger("pdu_utility")

# --verify: how many times to read an outlet back after switching it, and how
# long to wait between reads (PDUs may apply power-on sequencing delays).
VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL = 1.0


def resolve_pdu_driver(model_arg, ip_arg, port_arg, is_buyoff, username=None, password=None, http_options=None):
    """
    Resolves (signature, driver_class, display_ip, display_port).
    Handles explicit model signature, auto-discovery, and Buy-off defaults.
    Against a real PDU, discovery asks the device for its model first and only
    falls back to the IP-address convention if it doesn't answer.
    """
    https = bool((http_options or {}).get("use_https"))

    def port_for(cls):
        # No --port given: use the vendor's own default (APC 22/SSH, WTI and Raritan 80/HTTP, 443 with --https).
        if port_arg is not None:
            return port_arg
        default = getattr(cls, "DEFAULT_PORT", 80)
        return 443 if https and default == 80 else default

    port = port_arg
    all_drivers = dict(registry.get_all_drivers("pdu"))

    if model_arg:
        sig = model_arg.strip().lower()
        driver_cls = registry.get_driver("pdu", sig)
        if not driver_cls:
            for prefix in ("apc_", "wti_", "raritan_"):
                candidate_sig = f"{prefix}{sig}"
                if candidate_sig in all_drivers:
                    sig = candidate_sig
                    driver_cls = all_drivers[sig]
                    break
        if not driver_cls:
            logger.error(f"Unknown model signature '{model_arg}'. Use iss_trial_utility --list to see all signatures.")
            sys.exit(1)
        display_ip = ip_arg or "192.168.1.50"
        return sig, driver_cls, display_ip, port_for(driver_cls)

    if ip_arg and not is_buyoff:
        driver_instance = discover_and_instantiate(ip_arg, port, "pdu", username=username, password=password,
                                                   **(http_options or {}))
        if not driver_instance:
            logger.error("Failed to detect PDU model or find suitable driver.")
            sys.exit(1)
        driver_cls = type(driver_instance)
        # Generic drivers for unlisted models aren't registered under a signature.
        sig = next((s for s, c in all_drivers.items() if c is driver_cls), None)
        if sig == "dummy_pdu_sig":
            # The dummy driver fakes success; against a real address that would tell the
            # operator an outlet switched when nothing happened.
            logger.error(f"No PDU answered at {ip_arg}" + (f":{port}" if port else "") + " and its address matches "
                         "no known model. Check the address, port and credentials, force a driver with --model, "
                         "or use --buyoff to simulate.")
            sys.exit(1)
        return sig, driver_cls, ip_arg, port_for(driver_cls)

    if ip_arg:
        # Buy-off: there is no device to ask, so pick the model from the IP-address convention.
        for signature, cls in all_drivers.items():
            if signature.startswith("dummy"):
                continue
            try:
                if cls.probe(ip_arg, port):
                    return signature, cls, ip_arg, port_for(cls)
            except Exception:
                pass

        sig = "apc_ap7900"
        driver_cls = registry.get_driver("pdu", sig)
        return sig, driver_cls, ip_arg, port_for(driver_cls)

    sig = "apc_ap7900"
    driver_cls = registry.get_driver("pdu", sig)
    return sig, driver_cls, "192.168.1.50", port_for(driver_cls)


def main(argv=None, engineering: bool = False):
    # Line-buffered stdout keeps prints and log lines in order when piped.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if engineering and not HAS_MOCK:
        logger.error("The engineering edition needs equipment_drivers/simulator.py, which this install doesn't include.")
        sys.exit(1)
    description = "PDU Control Utility"
    if engineering:
        description += " [Engineering Edition]"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--ip_address", default=None,
                        help="IP address of the PDU (required unless in Buy-off mode)")
    parser.add_argument("--port", type=int, default=None,
                        help="Port of the PDU (default: the vendor's own port -- APC 22/SSH, WTI and Raritan 80/HTTP)")
    parser.add_argument("--buyoff", "--buy-off", "--mock", action="store_true", dest="buyoff",
                        help="Run in Buy-off mode against internal PDU simulator (simulates authentic hardware responses)")
    parser.add_argument("--model", type=str, default=None,
                        help="Explicit driver model signature (e.g. apc_ap7900, wti_vmr_hd4d20, raritan_px3_5460)")
    parser.add_argument("--fault", choices=sorted(FAULTS) or None, default=None,
                        help="Buy-off only: make the simulated PDU misbehave ("
                             + "; ".join(f"{k}: {v}" for k, v in FAULTS.items()) + ")")
    parser.add_argument("--username", default=None,
                        help="Username override for the PDU (falls back to PDU_USERNAME env var, then driver default)")
    parser.add_argument("--password", default=None,
                        help="Password override for the PDU (falls back to PDU_PASSWORD env var, then driver default). "
                             "Prefer the env var over this flag to avoid the password showing up in shell history.")
    parser.add_argument("--https", action="store_true",
                        help="Use HTTPS for WTI/Raritan on any port (default: HTTPS only on port 443)")
    parser.add_argument("--insecure", action="store_true",
                        help="Don't verify the PDU's TLS certificate (most PDUs ship a self-signed one)")
    parser.add_argument("--verify", action="store_true",
                        help="After on/off, read each outlet back and fail unless it reports the new state "
                             f"(up to {VERIFY_ATTEMPTS} reads, {VERIFY_INTERVAL:g}s apart). Catches PDUs that "
                             "acknowledge a command without switching.")
    parser.add_argument("--json", action="store_true",
                        help="Print one JSON document (model, raw connection reply, per-channel results, errors) "
                             "instead of text lines")
    parser.add_argument("--event-dir", metavar="DIR", default=None,
                        help="Write one protobuf EquipmentEvent (proto/equipment/v1) per channel result to DIR "
                             "as <event_id>.pb; station_id comes from $STATION_ID. Needs the protobuf package.")
    parser.add_argument("--capture", metavar="DIR", default=None,
                        help="Record every request/response exchanged with the PDU (identification included) "
                             "under DIR, one file per reply, for turning real-device output into test fixtures. "
                             "Credentials are not recorded.")
    parser.add_argument("action", choices=["on", "off", "status"], help="Action to perform")
    parser.add_argument("channel", type=str,
                        help="Channel spec: a single channel (3), a comma-separated list (3,4,5 or "
                             "'3, 4, 5'), a range (3-6), a combination (1,3-5,8), or 'all'")

    args = parser.parse_args(argv)

    # Backwards compatibility alias
    args.mock = args.buyoff

    if args.buyoff:
        if not HAS_MOCK:
            logger.error("--buyoff / --mock is an engineering feature and simulator is not included in official production packages.")
            sys.exit(1)
    elif not args.ip_address:
        parser.error("the following arguments are required: --ip_address (unless in Buy-off mode)")

    if args.event_dir:
        try:
            import equipment_drivers.proto_convert  # noqa: F401
        except ImportError as e:
            parser.error(f"--event-dir needs the protobuf package (pip install 'protobuf>=5,<6'): {e}")

    if args.fault and not args.buyoff:
        parser.error("--fault only applies in Buy-off mode (--buyoff / --mock)")

    username = args.username or os.environ.get("PDU_USERNAME")
    password = args.password or os.environ.get("PDU_PASSWORD")

    capture = None
    if args.capture:
        label = "buyoff-" + (args.model or args.ip_address or "default") if args.buyoff else args.ip_address
        capture = CaptureSession(args.capture, label=label)
    try:
        with capture or contextlib.nullcontext():
            _run(args, username, password)
    finally:
        if capture is not None and capture.count:
            # stderr under --json so stdout stays one parseable document
            print(f"Captured {capture.count} exchange(s) to {capture.path}", file=sys.stderr if args.json else sys.stdout)


class Report:
    """
    Everything a run produced. In text mode the classic lines (Detected Model,
    RAW_CONNECTION, Channel N ..., RAW_OUTPUT, VERIFY_OUTPUT) are printed as
    they happen; with --json one document is printed at the end instead, and
    every ERROR log line of the run is collected into its "errors" list.
    """

    def __init__(self, args):
        self.as_json = args.json
        self.event_dir = args.event_dir
        self.doc = {
            "action": args.action, "channels": args.channel, "ip_address": args.ip_address, "port": args.port,
            "buyoff": bool(args.buyoff), "fault": args.fault, "model": None, "raw_connection": None,
            "results": [], "success": False, "errors": [],
        }
        self._error_handler = _ListHandler(self.doc["errors"])
        logging.getLogger().addHandler(self._error_handler)

    def _say(self, text):
        if not self.as_json:
            print(text)

    def target(self, ip, port):
        self.doc["ip_address"], self.doc["port"] = ip, port

    def connected(self, model, raw_connection):
        self.doc["model"], self.doc["raw_connection"] = model, raw_connection
        self._say(f"Detected Model: {model}")
        if raw_connection:
            self._say(f"RAW_CONNECTION:\n{raw_connection.strip()}")

    def result(self, response):
        self.doc["results"].append({"channel": response.channel, "action": response.action,
                                    "success": response.success, "status": response.status, "raw": response.raw})
        if response.success:
            if response.action == "get_status":
                self._say(f"Channel {response.channel} Status: {response.status}")
            else:
                verb = "ON" if response.action == "turn_on" else "OFF"
                self._say(f"Channel {response.channel} turned {verb} successfully.")
        self._say(f"RAW_OUTPUT:\n{response.raw.strip()}")
        if self.event_dir:
            write_event(self.event_dir, self.doc["ip_address"], self.doc["port"], response)

    def verified(self, channel, expected, confirmed, readback):
        self.doc["results"][-1]["verify"] = {"expected": expected, "confirmed": confirmed,
                                             "status": readback.status, "raw": readback.raw}
        if confirmed:
            self._say(f"Channel {channel} verified {expected}.")
        self._say(f"VERIFY_OUTPUT:\n{readback.raw.strip()}")

    def finish(self, success):
        logging.getLogger().removeHandler(self._error_handler)
        self.doc["success"] = success
        if self.as_json:
            print(json.dumps(self.doc, indent=2))


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(level=logging.ERROR)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


def write_event(directory, ip, port, response):
    """Write one protobuf EquipmentEvent (proto/equipment/v1/equipment.proto) per
    channel result, as <event_id>.pb, for Station Control's log shipping."""
    from equipment_drivers.pb.equipment.v1 import equipment_pb2 as pb
    from equipment_drivers.proto_convert import make_event, pdu_response_to_proto
    os.makedirs(directory, exist_ok=True)
    event = make_event(station_id=os.environ.get("STATION_ID", ""), ip_address=ip or "", port=port or 0,
                       equipment_type=pb.EQUIPMENT_TYPE_PDU, pdu_response=pdu_response_to_proto(response))
    with open(os.path.join(directory, f"{event.event_id}.pb"), "wb") as f:
        f.write(event.SerializeToString())


def _run(args, username, password):
    report = Report(args)
    code = 1
    try:
        code = _execute(args, username, password, report)
    except SystemExit as e:  # resolve_pdu_driver and usage errors exit after logging why
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        logger.error(f"Error executing action: {e}")
        code = 1
    finally:
        report.finish(code == 0)
    if code:
        sys.exit(code)


def _execute(args, username, password, report) -> int:
    """Resolve, connect, run the action on every channel. Returns the exit code."""
    http_options = {"use_https": True if args.https else None, "verify_tls": not args.insecure}
    if args.insecure:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning("TLS certificate verification is disabled (--insecure).")

    sig, driver_cls, display_ip, display_port = resolve_pdu_driver(
        args.model, args.ip_address, args.port, args.buyoff, username=username, password=password,
        http_options=http_options,
    )
    report.target(display_ip, display_port)

    mock_server = None
    if args.buyoff:
        vendor = determine_vendor(sig, driver_cls)
        temp_inst = driver_cls()
        model_name = temp_inst.get_model()
        max_ch = temp_inst.get_max_channel()
        voltage = determine_voltage(sig, model_name)

        mock_server = MockPduServer(vendor=vendor, channel_count=max_ch, model_name=model_name,
                                    model_signature=sig, voltage=voltage, fault=args.fault,
                                    tls=args.https and vendor != "apc")
        mock_server.start()
        if args.fault:
            report._say(f"[BUY-OFF] Simulating fault '{args.fault}': {FAULTS[args.fault]}")
        target_ip, target_port = mock_server.host, mock_server.port
    else:
        target_ip, target_port = display_ip, display_port

    driver = driver_cls()
    driver.display_ip = display_ip
    driver.display_port = display_port
    driver.use_https = http_options["use_https"]
    driver.verify_tls = http_options["verify_tls"]

    try:
        if not driver.connect(target_ip, target_port, username=username, password=password):
            logger.error("Failed to connect to PDU.")
            return 1

        model = driver.get_model()
        report.connected(model, getattr(driver, "raw_connection", None))

        try:
            channel_count = driver.get_max_channel()
        except EquipmentConnectionError:
            raise  # device stopped answering; reported by _run
        except Exception as e:
            logger.warning(f"Could not determine driver channel count: {e}")
            channel_count = None

        try:
            channels = parse_channels(args.channel, channel_count=channel_count)
        except ValueError as e:
            logger.error(f"Usage error: {e}")
            return 1

        any_failed = False
        for channel in channels:
            if args.action == "on":
                response = driver.turn_on(channel)
            elif args.action == "off":
                response = driver.turn_off(channel)
            else:
                response = driver.get_status(channel)
            response.model = model

            if not response.success:
                any_failed = True
                logger.error(f"Action '{response.action}' on channel {response.channel} failed.")
            report.result(response)

            if response.success and args.verify and response.action != "get_status":
                expected = "ON" if response.action == "turn_on" else "OFF"
                if not verify_outlet(driver, channel, expected, report):
                    any_failed = True

        return 1 if any_failed else 0
    except Exception as e:
        logger.error(f"Error executing action: {e}")
        return 1
    finally:
        driver.disconnect()
        if mock_server:
            mock_server.stop()


def verify_outlet(driver, channel: int, expected: str, report: Report) -> bool:
    """Read the outlet back until it reports `expected` ('ON'/'OFF'). Reports the
    outcome and the last readback; returns whether the state was confirmed."""
    readback = None
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        readback = driver.get_status(channel)
        if readback.success and readback.status == expected:
            report.verified(channel, expected, True, readback)
            return True
        if attempt < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_INTERVAL)
    logger.error(f"Channel {channel} still reports {readback.status} after {VERIFY_ATTEMPTS} reads; "
                 f"expected {expected}. The PDU acknowledged the command but the outlet did not switch.")
    report.verified(channel, expected, False, readback)
    return False


def main_engineering(argv=None):
    main(argv, engineering=True)
