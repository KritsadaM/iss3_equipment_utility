# ISS3 Equipment Utility

Command-line utilities to discover and control ISS3 equipments — PDUs, terminal servers, and DAQs — through a common, pluggable driver architecture. Each utility auto-detects the connected device and dispatches to the right vendor driver, so operators don't need to know which model is on the other end of the IP address.

## Utilities

| Utility                | Equipment type   | Actions                  |
|-------------------------|------------------|---------------------------|
| `iss_pdu_utility`        | PDU              | `on`, `off`, `status`      |
| `iss_terminal_utility`   | Terminal Server  | status dump                |
| `iss_daq_utility`        | DAQ              | `start`, `stop`, `status`  |

## Installation

```bash
pip install -r requirements.txt
```

Or build a `.deb` package (see [Packaging](#packaging) below) and install it on the target host — the utilities land in `/usr/bin`.

## Usage

### PDU

```bash
iss_pdu_utility --ip_address 192.168.1.40 --port 80 on 3
iss_pdu_utility --ip_address 192.168.1.40 --port 80 off 3
iss_pdu_utility --ip_address 192.168.1.40 --port 80 status 3
```

`--port` is optional. If you leave it out, each vendor uses its own default:

| Vendor | Protocol | Default port | `RAW_OUTPUT` is |
|---|---|---|---|
| APC | NMC command line over SSH (`olOn` / `olOff` / `olStatus`) | 22 | CLI text, e.g. `E000: Success` / ` 3: Outlet 3: On` |
| WTI | REST API (`/api/v2/config/powerplug`) | 80 (HTTPS on 443) | JSON from the PDU |
| Raritan | Xerus JSON-RPC (`/model/pdu/0/outlet/<n>`) | 80 (HTTPS on 443) | JSON-RPC response from the PDU |

`RAW_OUTPUT` is always exactly what the PDU sent back, so WTI and Raritan show JSON because that is what those PDUs return.

Multiple channels can be controlled simultaneously:

```bash
# Comma-separated list
iss_pdu_utility --ip_address 192.168.1.40 --port 80 on 1,3,5

# Range of channels
iss_pdu_utility --ip_address 192.168.1.40 --port 80 on 1-4

# All channels
iss_pdu_utility --ip_address 192.168.1.40 --port 80 status all
```

#### Buy-off Mode (Simulation / Offline)

Use `--buyoff` (or `--mock`) to run against the internal PDU simulator with authentic device responses without physical hardware:

```bash
# Default Buy-off (simulates APC AP7900)
iss_pdu_utility --buyoff status 1

# Buy-off with specific PDU model signature
iss_pdu_utility --buyoff --model wti_vmr_hd4d20 status 1-3

# Buy-off with IP auto-detection (e.g. 192.168.1.40 detects WTI)
iss_pdu_utility --buyoff --ip_address 192.168.1.40 on 2
```

Both Buy-off and Real PDU modes print `RAW_CONNECTION:` (what the PDU sent on connect) and `RAW_OUTPUT:` (its reply to each action), in the vendor's own format: CLI text for APC (login banner, `E000: Success` ...), JSON for WTI and Raritan.

### Terminal Server

```bash
iss_terminal_utility --ip_address 192.168.1.20 --port 22
```

### DAQ

```bash
iss_daq_utility --ip_address 192.168.1.30 --port 502 start
iss_daq_utility --ip_address 192.168.1.30 --port 502 stop
iss_daq_utility --ip_address 192.168.1.30 --port 502 status
```

### Credentials

Some equipment (e.g. the WTI PDU) requires a username/password. Override the default in one of two ways:

```bash
# Flag (visible in shell history / process list -- fine for username, avoid for password)
iss_pdu_utility --ip_address 192.168.1.40 --port 80 --username admin --password secret on 3

# Environment variable (preferred for passwords)
PDU_USERNAME=admin PDU_PASSWORD=secret iss_pdu_utility --ip_address 192.168.1.40 --port 80 on 3
```

Precedence: `--username`/`--password` flag → `PDU_USERNAME`/`PDU_PASSWORD` env var (`TS_*` / `DAQ_*` for the other two utilities) → the driver's own built-in default, if it has one.

Built-in defaults are the vendors' factory credentials:

| Vendor | Default username / password |
|---|---|
| APC | `apc` / `apc` |
| WTI | `super` / `super` ([WTI KB](https://wti.com/blogs/knowledge-base/changing-the-default-password)) |
| Raritan | `admin` / `raritan` |

### HTTPS

WTI and Raritan use HTTPS automatically on port 443. Use `--https` to force HTTPS on any other port; without `--port` it then defaults to 443. Most PDUs ship with a self-signed certificate, which fails verification. Add `--insecure` to skip the check:

```bash
iss_pdu_utility --ip_address 192.168.1.40 --https --insecure status all
```

## How it works

```
equipment_drivers/
├── interfaces.py         # Abstract base classes every driver implements
├── registry.py           # equipment_type -> signature -> driver class
├── discovery.py          # Finds the right driver for a given ip:port
├── responses.py          # PDUResponse: structured result of a PDU action
├── pdu/
│   ├── dummy_pdu.py
│   ├── apc_models.py
│   ├── wti_models.py
│   └── raritan_models.py
├── terminal_server/
│   └── dummy_ts.py
└── daq/
    └── dummy_daq.py
```

1. **Discovery** (`discover_and_instantiate`) first **asks the device what it is**, trying every vendor's protocol in parallel (3 s each):

   | Vendor | How the model is read |
   |---|---|
   | APC | SSH login banner says "Network Management Card", then `prodInfo` → `Model:` |
   | WTI | `GET /api/v2/status/status` → `product` |
   | Raritan | JSON-RPC `getMetaData` → `nameplate.model` |

   The reported part number is matched to a model in `models.yaml` (e.g. `AP7920B` → `apc_ap7920`). If a known vendor reports a model that isn't listed, a generic driver for that vendor is used and the outlet count is read from the device. This needs the right credentials (`--username`/`--password` or `PDU_USERNAME`/`PDU_PASSWORD`).
2. If no device answers, discovery falls back to the IP-address convention: each driver's `probe(ip, port)` (today, the `suffix` in `models.yaml`), and logs a warning that it guessed.
3. Drivers registered under the conventional `dummy_{equipment_type}_sig` signature are used only as a last resort if nothing else matches.
3. Once a driver is instantiated, the utility calls `connect()`, runs the requested action, and calls `disconnect()`.

### PDU responses

`PDUDriver.turn_on` / `turn_off` / `get_status` return a `PDUResponse` dataclass rather than a bare tuple, so other Python code that imports a driver directly gets full detail:

```python
from equipment_drivers.pdu.wti_models import WtiVmrHd4d20Driver

driver = WtiVmrHd4d20Driver()
driver.connect("192.168.1.40", 80)
response = driver.turn_on(3)
# response.success, response.action, response.channel, response.raw, response.status, response.model
```

### Machine-readable output

`--json` prints one JSON document instead of the text lines:
- `model`, `ip_address`, `port`
- `raw_connection`
- `results`: one entry per channel, with `success`, `status`, `raw`, and `verify` when `--verify` is used
- `errors`: every ERROR log line from the run
- `success`

It always prints the document, even when no PDU answers, so scripts don't have to scrape the text output.

`--event-dir DIR` writes one protobuf `EquipmentEvent` (`proto/equipment/v1/equipment.proto`) per channel result as `DIR/<event_id>.pb`, taking `station_id` from `$STATION_ID`. It needs the `protobuf` package (`pip install '.[events]'`).

```bash
iss_pdu_utility --ip_address 192.168.1.40 --json status all | jq '.results[] | {channel, status}'
STATION_ID=PPTR-V2-004 iss_pdu_utility --ip_address 192.168.1.40 --event-dir /var/spool/iss3-events off 3
```

### Capturing real device output

Add `--capture DIR` to any real-device run. It records every exchange with the PDU, including the identification step, into `DIR/<timestamp>-<ip>/`:
- `exchanges.jsonl`: the request, HTTP status, timing and errors for each exchange.
- `NNN-<what>.json|.txt`: each reply exactly as the device sent it.

```bash
PDU_PASSWORD=... iss_pdu_utility --ip_address 192.168.1.40 --capture captures status all
```

Passwords, `Authorization` headers and `user:pass@` in URLs are never recorded. The reply files can be copied into `tests/fixtures/<vendor>/` in place of the documentation-derived samples, so the tests check the drivers against real hardware.

### Mock / engineering tools

Without hardware, the engineering package can simulate every PDU model. APC is served as an SSH CLI, WTI and Raritan as HTTP APIs. The response formats follow vendor documentation, see `tests/fixtures/README.md` for sources.

```bash
iss_pdu_utility_eng --mock --model apc_ap7900 status all   # in-process mock
iss_trial_utility --all                                    # 9-step trial of every model
iss_mock_server --vendor apc                               # standalone: ssh -p 2222 apc@127.0.0.1
```

#### Simulating faults

The mock can misbehave on purpose so error handling can be tested without hardware:

| `--fault` | The simulated PDU... |
|---|---|
| `auth_fail` | rejects every login (HTTP 401 / SSH auth failure) |
| `timeout` | accepts the connection, then never answers a command |
| `drop` | accepts the connection, then drops it on the first command |
| `command_error` | answers outlet commands with the vendor's error reply (`E100`, WTI status code ≠ 0, Raritan `_ret_` ≠ 0) |
| `stuck_outlet` | reports outlet commands as successful but never changes outlet state |

```bash
iss_pdu_utility --buyoff --model wti_vmr_hd4d20 --fault timeout off 1   # see what operators would see
iss_mock_server --vendor raritan --fault drop                            # standalone faulty PDU
iss_trial_utility --all --faults                                          # every model x every fault
```

`stuck_outlet` is the one fault a plain `on`/`off` can't detect: the PDU claims success, so the utility exits 0. Add `--verify` to read each outlet back after switching it. It retries up to 3 reads, 1 s apart, and exits 1 if the outlet never reports the new state:

```bash
iss_pdu_utility --ip_address 192.168.1.40 --verify off 3
```

## Adding a new driver

1. Create a new file under the right subfolder (`pdu/`, `terminal_server/`, or `daq/`) — it's auto-imported, no need to edit `__init__.py`.
2. Subclass the corresponding base interface (`PDUDriver`, `TerminalServerDriver`, or `DAQDriver`) from `equipment_drivers.interfaces`.
3. Implement `probe(cls, ip, port) -> bool`, `connect(...) -> bool`, `disconnect() -> bool`, and the equipment-specific action methods.
4. Decorate the class with `@registry.register('<equipment_type>', '<driver_signature>')`.
5. Add unit tests in `tests/`.

## Running tests

```bash
PYTHONPATH=$(pwd) python3 -m unittest discover tests
```

## Packaging

Build a Debian package:

```bash
make deb
```

Or build inside Docker without installing debian packaging tools locally:

```bash
make deb-docker
```
