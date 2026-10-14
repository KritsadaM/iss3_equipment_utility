"""
Minimal SNMP v1 / v2c client: GET and SET of single scalar variables over UDP.

Only what PDU outlet control needs, with no third-party dependency (pysnmp's
API has changed across major versions and isn't packaged under the same name
everywhere). The BER encoding follows RFC 1157 (v1) / RFC 3416 (v2c PDUs); it
is checked against net-snmp's snmpd/snmpget/snmpset in tests/test_snmp.py.

SNMPv3 (authentication/privacy) is not implemented.
"""
import itertools
import random
import socket
from typing import List, Optional, Tuple, Union

# ASN.1 / SNMP tags
_INTEGER, _OCTET_STRING, _NULL, _OID, _SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
_IPADDRESS, _COUNTER32, _GAUGE32, _TIMETICKS, _COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x46
_NO_SUCH_OBJECT, _NO_SUCH_INSTANCE, _END_OF_MIB_VIEW = 0x80, 0x81, 0x82
GET, GET_RESPONSE, SET = 0xA0, 0xA2, 0xA3

VERSIONS = {"1": 0, "2c": 1}

# RFC 3416 error-status values
ERROR_NAMES = {
    0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr",
    6: "noAccess", 7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue",
    11: "noCreation", 12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
    15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName",
}


class SnmpError(Exception):
    """The agent answered with an error-status (e.g. noSuchName, notWritable)."""

    def __init__(self, status: int, index: int, oid: str):
        self.status, self.index, self.oid = status, index, oid
        super().__init__(f"SNMP {ERROR_NAMES.get(status, status)} (index {index}) for {oid}")


class SnmpTimeout(Exception):
    """No answer. With SNMP v1/v2c a wrong community string looks exactly like this."""


class NoSuchObject:
    """Value returned for an OID the agent doesn't have (v2c exception or v1 noSuchName)."""

    def __repr__(self):
        return "NoSuchObject"

    def __bool__(self):
        return False


NO_SUCH_OBJECT = NoSuchObject()
Value = Union[int, str, None, NoSuchObject]


# ---------------------------------------------------------------- BER encoding

def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def encode_int(value: int, tag: int = _INTEGER) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)  # room for the sign bit
    return _tlv(tag, value.to_bytes(size, "big", signed=True))


def encode_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".")]
    if len(parts) < 2:
        raise ValueError(f"OID too short: {oid}")
    body = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        body.extend(reversed(chunk))
    return _tlv(_OID, bytes(body))


def encode_value(value) -> bytes:
    if value is None:
        return _tlv(_NULL, b"")
    if isinstance(value, bool):
        raise TypeError("bool is not an SNMP type")
    if isinstance(value, int):
        return encode_int(value)
    if isinstance(value, str):
        return _tlv(_OCTET_STRING, value.encode("utf-8"))
    if isinstance(value, bytes):
        return _tlv(_OCTET_STRING, value)
    if isinstance(value, NoSuchObject):
        return _tlv(_NO_SUCH_OBJECT, b"")
    raise TypeError(f"Unsupported SNMP value: {value!r}")


def encode_message(version: int, community: str, pdu_type: int, request_id: int,
                   varbinds: List[Tuple[str, object]], error_status: int = 0, error_index: int = 0) -> bytes:
    binds = b"".join(_tlv(_SEQUENCE, encode_oid(oid) + encode_value(value)) for oid, value in varbinds)
    pdu = _tlv(pdu_type, encode_int(request_id) + encode_int(error_status) + encode_int(error_index)
               + _tlv(_SEQUENCE, binds))
    return _tlv(_SEQUENCE, encode_int(version) + _tlv(_OCTET_STRING, community.encode()) + pdu)


# ---------------------------------------------------------------- BER decoding

def _read_tlv(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    end = pos + length
    if end > len(data):
        raise ValueError("Truncated BER data")
    return tag, data[pos:end], end


def decode_oid(body: bytes) -> str:
    parts = [body[0] // 40, body[0] % 40]
    value = 0
    for byte in body[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def _decode_value(tag: int, body: bytes) -> Value:
    if tag == _INTEGER:
        return int.from_bytes(body, "big", signed=True)
    if tag in (_COUNTER32, _GAUGE32, _TIMETICKS, _COUNTER64):
        return int.from_bytes(body, "big", signed=False)
    if tag == _OCTET_STRING:
        return body.decode("utf-8", errors="replace")
    if tag == _OID:
        return decode_oid(body)
    if tag == _IPADDRESS:
        return ".".join(str(b) for b in body)
    if tag == _NULL:
        return None
    if tag in (_NO_SUCH_OBJECT, _NO_SUCH_INSTANCE, _END_OF_MIB_VIEW):
        return NO_SUCH_OBJECT
    return body.hex()


def decode_message(data: bytes):
    """Returns (version, community, pdu_type, request_id, error_status, error_index, [(oid, value)])."""
    _tag, message, _ = _read_tlv(data, 0)
    _tag, version, pos = _read_tlv(message, 0)
    _tag, community, pos = _read_tlv(message, pos)
    pdu_type, pdu, _ = _read_tlv(message, pos)
    fields = []
    pos = 0
    for _ in range(3):
        _tag, body, pos = _read_tlv(pdu, pos)
        fields.append(int.from_bytes(body, "big", signed=True))
    _tag, binds, _ = _read_tlv(pdu, pos)
    varbinds = []
    pos = 0
    while pos < len(binds):
        _tag, bind, pos = _read_tlv(binds, pos)
        _tag, oid, inner = _read_tlv(bind, 0)
        vtag, vbody, _ = _read_tlv(bind, inner)
        varbinds.append((decode_oid(oid), _decode_value(vtag, vbody)))
    return (int.from_bytes(version, "big"), community.decode(errors="replace"), pdu_type,
            fields[0], fields[1], fields[2], varbinds)


# ---------------------------------------------------------------- client

class SnmpClient:
    def __init__(self, host: str, port: int = 161, community: str = "public", version: str = "2c",
                 timeout: float = 2.0, retries: int = 1):
        if version not in VERSIONS:
            raise ValueError(f"Unsupported SNMP version '{version}' (supported: {', '.join(VERSIONS)})")
        self.host, self.port, self.community, self.version = host, port, community, version
        self.timeout, self.retries = timeout, retries
        self._ids = itertools.count(random.randint(1, 2 ** 30))

    def get(self, oid: str) -> Value:
        return self._request(GET, oid, None)

    def set(self, oid: str, value: Union[int, str]) -> Value:
        return self._request(SET, oid, value)

    def _request(self, pdu_type: int, oid: str, value) -> Value:
        request_id = next(self._ids) & 0x7FFFFFFF
        packet = encode_message(VERSIONS[self.version], self.community, pdu_type, request_id, [(oid, value)])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            for _attempt in range(self.retries + 1):
                sock.sendto(packet, (self.host, self.port))
                try:
                    while True:
                        data, _addr = sock.recvfrom(65535)
                        try:
                            reply = decode_message(data)
                        except (ValueError, IndexError):
                            continue  # not a well-formed SNMP message; keep waiting
                        if reply[2] == GET_RESPONSE and reply[3] == request_id:
                            break
                except socket.timeout:
                    continue
                _ver, _comm, _type, _id, status, index, varbinds = reply
                if status == 2 and self.version == "1" and pdu_type == GET:
                    return NO_SUCH_OBJECT  # v1 reports a missing OID as noSuchName
                if status:
                    raise SnmpError(status, index, oid)
                return varbinds[0][1] if varbinds else None
        raise SnmpTimeout(f"No SNMP v{self.version} response from {self.host}:{self.port} for {oid} "
                          f"(unreachable, SNMP disabled, or wrong community)")


def format_varbind(oid: str, value: Value) -> str:
    """net-snmp style "OID = TYPE: value" line, used as the raw reply text."""
    if isinstance(value, NoSuchObject):
        return f".{oid.strip('.')} = No Such Object available on this agent at this OID"
    if isinstance(value, int):
        return f".{oid.strip('.')} = INTEGER: {value}"
    if value is None:
        return f".{oid.strip('.')} = NULL"
    return f'.{oid.strip(".")} = STRING: "{value}"'


def optional_int(value: Value) -> Optional[int]:
    return value if isinstance(value, int) else None
