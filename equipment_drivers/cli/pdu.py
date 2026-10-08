"""
PDU control CLI shared by iss_pdu_utility (official) and iss_pdu_utility_eng
(engineering). The two differ only in that the engineering edition requires the
simulator; Buy-off mode is available in either wherever simulator.py is installed.
"""
import argparse
import contextlib
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


def resolve_pdu_driver(model_arg, ip_arg, port_arg, is_buyoff, username=None, password=None):
    """
    Resolves (signature, driver_class, display_ip, display_port).
    Handles explicit model signature, auto-discovery, and Buy-off defaults.
    Against a real PDU, discovery asks the device for its model first and only
    falls back to the IP-address convention if it doesn't answer.
    """
    def port_for(cls):
        # No --port given: use the vendor's own default (APC 22/SSH, WTI and Raritan 80/HTTP).
        return port_arg if port_arg is not None else getattr(cls, "DEFAULT_PORT", 80)

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
        driver_instance = discover_and_instantiate(ip_arg, port, "pdu", username=username, password=password)
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
    parser.add_argument("--verify", action="store_true",
                        help="After on/off, read each outlet back and fail unless it reports the new state "
                             f"(up to {VERIFY_ATTEMPTS} reads, {VERIFY_INTERVAL:g}s apart). Catches PDUs that "
                             "acknowledge a command without switching.")
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
            print(f"Captured {capture.count} exchange(s) to {capture.path}")


def _run(args, username, password):
    sig, driver_cls, display_ip, display_port = resolve_pdu_driver(
        args.model, args.ip_address, args.port, args.buyoff, username=username, password=password
    )

    mock_server = None

    if args.buyoff:
        vendor = determine_vendor(sig, driver_cls)
        temp_inst = driver_cls()
        model_name = temp_inst.get_model()
        max_ch = temp_inst.get_max_channel()
        voltage = determine_voltage(sig, model_name)

        mock_server = MockPduServer(vendor=vendor, channel_count=max_ch, model_name=model_name,
                                    model_signature=sig, voltage=voltage, fault=args.fault)
        mock_server.start()
        if args.fault:
            print(f"[BUY-OFF] Simulating fault '{args.fault}': {FAULTS[args.fault]}")
        target_ip = mock_server.host
        target_port = mock_server.port
        driver = driver_cls()
        driver.display_ip = display_ip
        driver.display_port = display_port
    else:
        target_ip = display_ip
        target_port = display_port
        driver = driver_cls()
        driver.display_ip = display_ip
        driver.display_port = display_port

    try:
        connected = driver.connect(target_ip, target_port, username=username, password=password)
        if not connected:
            logger.error("Failed to connect to PDU.")
            sys.exit(1)

        model = driver.get_model()
        print(f"Detected Model: {model}")

        raw_conn = getattr(driver, "raw_connection", None)
        if raw_conn:
            print(f"RAW_CONNECTION:\n{raw_conn.strip()}")

        try:
            channel_count = driver.get_max_channel()
        except EquipmentConnectionError:
            raise  # device stopped answering; reported below
        except Exception as e:
            logger.warning(f"Could not determine driver channel count: {e}")
            channel_count = None

        try:
            channels = parse_channels(args.channel, channel_count=channel_count)
        except ValueError as e:
            logger.error(f"Usage error: {e}")
            sys.exit(1)

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
                print(f"RAW_OUTPUT:\n{response.raw.strip()}")
                continue

            if response.action == "get_status":
                print(f"Channel {response.channel} Status: {response.status}")
            else:
                verb = "ON" if response.action == "turn_on" else "OFF"
                print(f"Channel {response.channel} turned {verb} successfully.")
            print(f"RAW_OUTPUT:\n{response.raw.strip()}")

            if args.verify and response.action != "get_status":
                if not verify_outlet(driver, channel, verb):
                    any_failed = True

        if any_failed:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error executing action: {e}")
        sys.exit(1)
    finally:
        driver.disconnect()
        if mock_server:
            mock_server.stop()



def verify_outlet(driver, channel: int, expected: str) -> bool:
    """Read the outlet back until it reports `expected` ('ON'/'OFF'). Prints the
    outcome and the last readback; returns whether the state was confirmed."""
    readback = None
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        readback = driver.get_status(channel)
        if readback.success and readback.status == expected:
            print(f"Channel {channel} verified {expected}.")
            print(f"VERIFY_OUTPUT:\n{readback.raw.strip()}")
            return True
        if attempt < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_INTERVAL)
    logger.error(f"Channel {channel} still reports {readback.status} after {VERIFY_ATTEMPTS} reads; "
                 f"expected {expected}. The PDU acknowledged the command but the outlet did not switch.")
    print(f"VERIFY_OUTPUT:\n{readback.raw.strip()}")
    return False


def main_engineering(argv=None):
    main(argv, engineering=True)
