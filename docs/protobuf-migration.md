# Protobuf schema for equipment responses

Adds a `.proto` schema for PDU/terminal-server/DAQ responses so Station
Control (or the log viewer) can consume a compact, typed payload instead of
parsing free-text CLI output or ad-hoc JSON.

Verified: schema compiles with `grpc_tools.protoc`, and `PDUResponse` round-trips
losslessly through it. A serialized `PDUResponse` came out to 56 bytes vs. 178
bytes for an equivalent JSON object in a quick test — expect a similar
multiple in practice, more if you add more fields.

## Files in this bundle

```
proto/equipment/v1/equipment.proto      <- schema (source of truth)
equipment_drivers/pb/                   <- generated stubs (equipment_pb2.py/.pyi)
equipment_drivers/proto_convert.py      <- glue between dataclasses/tuples and protobuf
```

Drop these into the repo root, preserving paths.

## Design choices

- **PDUResponse maps 1:1** to your existing dataclass in `responses.py` — no
  changes needed there.
- **TerminalServerResponse / DAQResponse are new** — today those drivers
  return plain `(str, str)` / `(bool, str)` tuples (see `interfaces.py`).
  `proto_convert.py` builds the proto message straight from those tuples, so
  you can adopt protobuf as the wire format *before* deciding whether to
  refactor the drivers themselves to return structured objects. If you do
  that refactor later, swap the tuple-based helpers for direct field access,
  same pattern as `pdu_response_to_proto`.
- **`EquipmentEvent`** is the envelope — station ID, IP, timestamp, and a
  `oneof` payload. This is what you'd actually hand to Station Control's
  log-shipping worker, one message per action, so it doesn't need to know
  the difference between a PDU/terminal/DAQ payload beyond checking
  `equipment_type`.
- **Protobuf stays out of `equipment_drivers/pdu/*.py` and `interfaces.py`.**
  Drivers keep working with plain dataclasses/tuples; `proto_convert.py` is
  the only place that imports the generated stubs. Keeps protobuf an
  optional dependency of the boundary, not the core logic.
- `status` is a normalized enum (`STATUS_CODE_ON`, `_OFF`, `_ERROR`, ...)
  separate from `status_text`, which keeps the original vendor string
  (`"ON"`, `"Online"`, whatever APC/WTI/Raritan actually returns). Consumers
  that just want on/off/error can switch on the enum; anyone who needs the
  exact vendor wording still has it.

## Setup

Add to `requirements.txt`:

```
protobuf>=5,<6
```

Add to a dev/build-only requirements file (or install ad hoc when
regenerating stubs — this isn't needed at runtime):

```
grpcio-tools>=1.60
```

Add to `Makefile`:

```make
# ==========================================================================
# Protobuf codegen
# ==========================================================================
proto:
	python3 -m grpc_tools.protoc \
		-I proto \
		--python_out=equipment_drivers/pb \
		--pyi_out=equipment_drivers/pb \
		proto/equipment/v1/equipment.proto
```

Regenerate any time `equipment.proto` changes: `make proto`. Commit the
generated `equipment_drivers/pb/**/*_pb2.py` files — that's the more common
convention for a small repo like this without a separate CI codegen step,
since it means `pip install -r requirements.txt` is enough to run the
utilities with no protoc dependency at runtime.

## Example: emitting an event from `iss_pdu_utility`

```python
from equipment_drivers.pb.equipment.v1 import equipment_pb2 as pb
from equipment_drivers.proto_convert import pdu_response_to_proto, make_event

# ... after driver.turn_on(channel) returns `response` ...
event = make_event(
    station_id=os.environ.get("STATION_ID", ""),
    ip_address=args.ip_address,
    port=args.port,
    equipment_type=pb.EQUIPMENT_TYPE_PDU,
    pdu_response=pdu_response_to_proto(response),
)

# Drop it wherever your log-shipping worker watches, e.g.:
with open(f"/var/spool/iss3-events/{event.event_id}.pb", "wb") as f:
    f.write(event.SerializeToString())
```

That gives your Station Control log-shipping worker a small, versioned,
typed file to pick up per action instead of scraping stdout.

## Not covered here (next steps, if wanted)

- Wiring this into `iss_pdu_utility` / `iss_terminal_utility` / `iss_daq_utility`
  for real (the snippet above is illustrative, not wired in).
- Deciding the actual transport into Station Control — spool file, a small
  HTTP endpoint with `Content-Type: application/x-protobuf`, or a Unix
  socket the log-shipping worker listens on. Whichever fits how that worker
  is being built.
- Refactoring `TerminalServerDriver`/`DAQDriver` to return structured
  dataclasses instead of tuples, matching `PDUDriver`. Not required for this
  proto schema to work, but would make `proto_convert.py`'s tuple-unpacking
  helpers unnecessary.
