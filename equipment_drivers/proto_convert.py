"""
Conversion between internal Python types (equipment_drivers.responses.PDUResponse,
and the plain tuples TerminalServerDriver/DAQDriver return today) and the
protobuf messages in proto/equipment/v1/equipment.proto.

Kept as a separate module on purpose: the dataclasses/tuples stay the
lightweight internal representation drivers work with, and protobuf only
enters the picture at serialization boundaries (writing to a spool file,
sending to Station Control, etc.). Nothing in equipment_drivers/pdu/*.py
needs to import protobuf at all.

Regenerate the *_pb2.py stubs after editing the .proto:
    make proto
"""
import time
import uuid
from typing import Optional, Tuple

from equipment_drivers.responses import PDUResponse
from equipment_drivers.pb.equipment.v1 import equipment_pb2 as pb

_ACTION_TO_PB = {
    "turn_on": pb.ACTION_TURN_ON,
    "turn_off": pb.ACTION_TURN_OFF,
    "get_status": pb.ACTION_GET_STATUS,
    "start_acquisition": pb.ACTION_START_ACQUISITION,
    "stop_acquisition": pb.ACTION_STOP_ACQUISITION,
}
_PB_TO_ACTION = {v: k for k, v in _ACTION_TO_PB.items()}

_STATUS_TEXT_TO_CODE = {
    "on": pb.STATUS_CODE_ON,
    "off": pb.STATUS_CODE_OFF,
    "running": pb.STATUS_CODE_RUNNING,
    "started": pb.STATUS_CODE_RUNNING,
    "stopped": pb.STATUS_CODE_STOPPED,
    "error": pb.STATUS_CODE_ERROR,
}


def _status_code(status_text: Optional[str]) -> int:
    if not status_text:
        return pb.STATUS_CODE_UNSPECIFIED
    return _STATUS_TEXT_TO_CODE.get(status_text.strip().lower(), pb.STATUS_CODE_UNKNOWN)


def pdu_response_to_proto(response: PDUResponse) -> pb.PDUResponse:
    return pb.PDUResponse(
        success=response.success,
        action=_ACTION_TO_PB.get(response.action, pb.ACTION_UNSPECIFIED),
        channel=response.channel,
        raw=response.raw or "",
        status=_status_code(response.status),
        status_text=response.status or "",
        model=response.model or "",
    )


def pdu_response_from_proto(msg: "pb.PDUResponse") -> PDUResponse:
    return PDUResponse(
        success=msg.success,
        action=_PB_TO_ACTION.get(msg.action, ""),
        channel=msg.channel,
        raw=msg.raw,
        status=msg.status_text or None,
        model=msg.model or None,
    )


def terminal_server_response_to_proto(
    status: Tuple[str, str], success: bool = True, model: str = ""
) -> pb.TerminalServerResponse:
    status_text, raw = status
    return pb.TerminalServerResponse(
        success=success,
        status=_status_code(status_text),
        status_text=status_text or "",
        raw=raw or "",
        model=model,
    )


def daq_response_to_proto(
    action: str, result: Tuple[bool, str], model: str = ""
) -> pb.DAQResponse:
    success, raw = result
    return pb.DAQResponse(
        success=success,
        action=_ACTION_TO_PB.get(action, pb.ACTION_UNSPECIFIED),
        status=pb.STATUS_CODE_UNSPECIFIED,
        status_text="",
        raw=raw or "",
        model=model,
    )


def make_event(
    *,
    station_id: str,
    ip_address: str,
    port: int,
    equipment_type: int,
    **payload_kwargs,
) -> pb.EquipmentEvent:
    """
    Wrap any one of the three response messages in the common envelope.
    Pass exactly one of pdu_response=, terminal_server_response=, or
    daq_response= as a keyword.

    Example:
        event = make_event(
            station_id="PPTR-V2-004",
            ip_address="192.168.1.40",
            port=80,
            equipment_type=pb.EQUIPMENT_TYPE_PDU,
            pdu_response=pdu_response_to_proto(response),
        )
        with open(spool_path, "wb") as f:
            f.write(event.SerializeToString())
    """
    return pb.EquipmentEvent(
        event_id=str(uuid.uuid4()),
        timestamp_unix_ms=int(time.time() * 1000),
        station_id=station_id,
        ip_address=ip_address,
        port=port,
        equipment_type=equipment_type,
        **payload_kwargs,
    )
