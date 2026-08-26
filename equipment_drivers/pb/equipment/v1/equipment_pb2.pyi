from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EquipmentType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EQUIPMENT_TYPE_UNSPECIFIED: _ClassVar[EquipmentType]
    EQUIPMENT_TYPE_PDU: _ClassVar[EquipmentType]
    EQUIPMENT_TYPE_TERMINAL_SERVER: _ClassVar[EquipmentType]
    EQUIPMENT_TYPE_DAQ: _ClassVar[EquipmentType]

class Action(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTION_UNSPECIFIED: _ClassVar[Action]
    ACTION_TURN_ON: _ClassVar[Action]
    ACTION_TURN_OFF: _ClassVar[Action]
    ACTION_GET_STATUS: _ClassVar[Action]
    ACTION_START_ACQUISITION: _ClassVar[Action]
    ACTION_STOP_ACQUISITION: _ClassVar[Action]

class StatusCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STATUS_CODE_UNSPECIFIED: _ClassVar[StatusCode]
    STATUS_CODE_ON: _ClassVar[StatusCode]
    STATUS_CODE_OFF: _ClassVar[StatusCode]
    STATUS_CODE_RUNNING: _ClassVar[StatusCode]
    STATUS_CODE_STOPPED: _ClassVar[StatusCode]
    STATUS_CODE_ERROR: _ClassVar[StatusCode]
    STATUS_CODE_UNKNOWN: _ClassVar[StatusCode]
EQUIPMENT_TYPE_UNSPECIFIED: EquipmentType
EQUIPMENT_TYPE_PDU: EquipmentType
EQUIPMENT_TYPE_TERMINAL_SERVER: EquipmentType
EQUIPMENT_TYPE_DAQ: EquipmentType
ACTION_UNSPECIFIED: Action
ACTION_TURN_ON: Action
ACTION_TURN_OFF: Action
ACTION_GET_STATUS: Action
ACTION_START_ACQUISITION: Action
ACTION_STOP_ACQUISITION: Action
STATUS_CODE_UNSPECIFIED: StatusCode
STATUS_CODE_ON: StatusCode
STATUS_CODE_OFF: StatusCode
STATUS_CODE_RUNNING: StatusCode
STATUS_CODE_STOPPED: StatusCode
STATUS_CODE_ERROR: StatusCode
STATUS_CODE_UNKNOWN: StatusCode

class PDUResponse(_message.Message):
    __slots__ = ("success", "action", "channel", "raw", "status", "status_text", "model")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_TEXT_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    success: bool
    action: Action
    channel: int
    raw: str
    status: StatusCode
    status_text: str
    model: str
    def __init__(self, success: _Optional[bool] = ..., action: _Optional[_Union[Action, str]] = ..., channel: _Optional[int] = ..., raw: _Optional[str] = ..., status: _Optional[_Union[StatusCode, str]] = ..., status_text: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class TerminalServerResponse(_message.Message):
    __slots__ = ("success", "status", "status_text", "raw", "model")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_TEXT_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    success: bool
    status: StatusCode
    status_text: str
    raw: str
    model: str
    def __init__(self, success: _Optional[bool] = ..., status: _Optional[_Union[StatusCode, str]] = ..., status_text: _Optional[str] = ..., raw: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class DAQResponse(_message.Message):
    __slots__ = ("success", "action", "status", "status_text", "raw", "model")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_TEXT_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    success: bool
    action: Action
    status: StatusCode
    status_text: str
    raw: str
    model: str
    def __init__(self, success: _Optional[bool] = ..., action: _Optional[_Union[Action, str]] = ..., status: _Optional[_Union[StatusCode, str]] = ..., status_text: _Optional[str] = ..., raw: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class EquipmentEvent(_message.Message):
    __slots__ = ("event_id", "timestamp_unix_ms", "station_id", "ip_address", "port", "equipment_type", "pdu_response", "terminal_server_response", "daq_response")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    IP_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    EQUIPMENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    PDU_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_SERVER_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    DAQ_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    timestamp_unix_ms: int
    station_id: str
    ip_address: str
    port: int
    equipment_type: EquipmentType
    pdu_response: PDUResponse
    terminal_server_response: TerminalServerResponse
    daq_response: DAQResponse
    def __init__(self, event_id: _Optional[str] = ..., timestamp_unix_ms: _Optional[int] = ..., station_id: _Optional[str] = ..., ip_address: _Optional[str] = ..., port: _Optional[int] = ..., equipment_type: _Optional[_Union[EquipmentType, str]] = ..., pdu_response: _Optional[_Union[PDUResponse, _Mapping]] = ..., terminal_server_response: _Optional[_Union[TerminalServerResponse, _Mapping]] = ..., daq_response: _Optional[_Union[DAQResponse, _Mapping]] = ...) -> None: ...
