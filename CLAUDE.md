# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests (what Jenkins runs; works without pytest installed)
PYTHONPATH=$(pwd) python3 -m unittest discover tests

# Single test module / class / method
PYTHONPATH=$(pwd) python3 -m unittest tests.test_pdu_wti
PYTHONPATH=$(pwd) python3 -m unittest tests.test_pdu_wti.TestWtiDrivers.test_connect_success

# GitHub Actions uses pytest instead (Python 3.10–3.12): pip install -e ".[dev]"
PYTHONPATH=$(pwd) python -m pytest tests/ -v --tb=short

# Lint (CI fails only on the first command; the second is advisory)
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics

# Run a utility from the repo (root iss_* scripts are thin wrappers around equipment_drivers/cli/*.py;
# after `pip install -e .` the same tools exist as iss-pdu-utility, iss-pdu-utility-eng, iss-trial-utility, iss-mock-server)
PYTHONPATH=$(pwd) ./iss_pdu_utility --ip_address 192.168.1.40 --port 80 status 1-4
PYTHONPATH=$(pwd) ./iss_pdu_utility_eng --mock --model wti_vmr_hd4d20 status all   # no hardware needed
PYTHONPATH=$(pwd) ./iss_trial_utility --list          # list all model signatures
PYTHONPATH=$(pwd) ./iss_trial_utility --model apc_ap7900   # 9-phase mock trial of one model
PYTHONPATH=$(pwd) ./iss_mock_server --vendor raritan --port 8080

# Packaging
make deb                  # official .deb (needs dpkg-deb; use make deb-docker on macOS)
make deb-engineering      # engineering .deb (adds mock/trial tools + simulator.py)
make proto                # regenerate protobuf stubs (needs grpcio-tools)
```

PDU `--port` defaults to `None`, so each driver falls back to its `DEFAULT_PORT` (APC 22, WTI/Raritan 80).

## Architecture

**Driver registration is import-driven.** `import equipment_drivers` (`equipment_drivers/__init__.py`) auto-imports every module in `pdu/`, `terminal_server/`, `daq/` (each registers itself with `registry.register(type, signature, cls)` — a plain call, not a decorator), then calls `pdu/model_factory.create_and_register_models()`. Any script or test that needs the registry populated must import `equipment_drivers` first.

**PDU models are data-driven.** Vendor protocol logic lives in one base class per vendor (`BaseApcPduDriver`, `BaseWtiPduDriver`, `BaseRaritanPduDriver` in `pdu/*_models.py`). `model_factory.py` reads `pdu/models.yaml` and generates a subclass per entry with `type()`, overriding only `MODEL_NAME`, `DEFAULT_CHANNEL_COUNT`, `IP_SUFFIX`; the YAML key becomes the registry signature and a PascalCase class name (`raritan_px3_5460` → `RaritanPx35460Driver`). Adding a model to an existing vendor = one line in `models.yaml`. A new vendor needs a `Base<Vendor>PduDriver` class (the factory finds it by the `Base*PduDriver` name pattern) plus an entry in `_VENDOR_BASES`. PyYAML is optional: without it, a minimal built-in parser handles only the flat `sig: {k: v, ...}` format, so keep `models.yaml` in that shape.

Because generated classes don't exist statically, `apc_models.py` / `wti_models.py` / `raritan_models.py` use a module-level `__getattr__` to expose `APC_MODELS`/`WTI_MODELS`/`RARITAN_MODELS` dicts and individual class names (e.g. `from equipment_drivers.pdu.wti_models import WtiVmrHd4d20Driver`) by looking them up in the registry.

**Discovery** (`discovery.discover_and_instantiate`) works in 3 steps:
1. Every vendor base class that defines an `identify()` classmethod (`Base{Apc,Wti,Raritan}PduDriver`) is asked, in parallel, for the model the device reports.
2. `match_reported_model` maps that part number to a registered model driver: the longest `MODEL_NAME` part-number prefix wins. A recognized vendor with an unlisted model gets `generic_driver_class(base, model)`.
3. Only if no device answers does it fall back to `probe(ip, port)` (the IP-suffix convention from `models.yaml`), then to the `dummy_{type}_sig` driver.

Tests that use LAN-looking IPs must pass `identify=False`, or they will try to reach real hosts. Buy-off mode never asks a device; it picks the model from `--model` or the IP suffix. Terminal server and DAQ only have dummy drivers so far.

**Vendor protocols.** APC drivers talk to the NMC command line over SSH via `cli_transport.SshCliTransport` (paramiko, imported lazily). The CLI dialect is detected per session from `about` (`apc_models.CliDialect`: `RPDU2G` olOn/olStatus/prodInfo, or 1st gen `RPDU` on/status/ver); WTI uses its REST API (`/api/v2/config/powerplug`); Raritan uses Xerus JSON-RPC (POST to `/model/pdu/0/outlet/<channel-1>`). `PDUResponse.raw` is always the device's own reply (CLI text for APC, JSON for the others). Response shapes follow vendor docs. No real hardware has been tested, and `tests/fixtures/README.md` records each fixture's source and whether it is verbatim.

**Driver contract** (`interfaces.py`): `connect()` takes optional `username`/`password` and drivers fall back to their own defaults when `None`. PDU actions return a `PDUResponse` dataclass (`responses.py`); `TerminalServerDriver`/`DAQDriver` still return plain tuples. PDU drivers must call `self.validate_channel()` before acting, and raise the exceptions in `exceptions.py` (`EquipmentConnectionError`, `EquipmentCommandError`, `EquipmentNotConnectedError`). Channel specs (`3`, `1,3-5`, `all`) are parsed by `channel_spec.parse_channels`, with `all` expanded via `driver.get_max_channel()`.

**Official vs. engineering builds.** `simulator.py` (`MockPduServer`: a paramiko SSH server running `ApcCliEngine` for APC, an `HTTPServer` for WTI/Raritan; plus the trial runners) is stripped from the official `.deb`. Both PDU utilities run `equipment_drivers/cli/pdu.py` (`main` / `main_engineering`), which guards the simulator import with `HAS_MOCK`. `cli/trial.py` and `cli/mock_server.py` are removed from the official package along with `simulator.py`. Official code must not hard-depend on `simulator.py`. Against a real address, the PDU CLI refuses the dummy-driver fallback rather than reporting fake success. If you change driver HTTP endpoints or payloads, update the matching mock handler in `simulator.py` and the endpoint table in `cli/mock_server.py`. `MockPduServer(fault=...)` simulates the failures listed in `simulator.FAULTS`. `run_pdu_fault_trial` (`iss_trial_utility --faults`) checks that drivers turn each one into an `EquipmentError` or `success=False`. A new driver should pass it.

**Protobuf** (`proto/equipment/v1/equipment.proto`) is the source of truth; the generated `equipment_drivers/pb/**/equipment_pb2.py(i)` stubs are committed. `proto_convert.py` is the only module that should import protobuf — keep it out of drivers and `interfaces.py`. See `docs/protobuf-migration.md`; it isn't wired into the CLIs yet.

## Testing conventions

Tests use `unittest` with `unittest.mock`. WTI/Raritan driver tests patch HTTP at the session level (e.g. `@patch('equipment_drivers.pdu.wti_models.requests.Session.post')`) and return bodies from `tests/fixtures/`; APC tests swap `driver.transport_factory` for a fake CLI transport. `tests/test_trial_utility.py` runs the real `MockPduServer` on an ephemeral port (`port=0`). Some tests assert exact model counts per vendor (e.g. `len(WTI_MODELS) == 16`), so adding entries to `models.yaml` means updating those tests.
