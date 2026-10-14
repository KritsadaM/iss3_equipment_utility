"""
Outlet control over SNMP for APC (PowerNet-MIB) and Raritan (PDU2-MIB) PDUs.

An alternative to each vendor's native protocol (APC CLI over SSH, Raritan
JSON-RPC), selected with iss_pdu_utility --snmp. OIDs come from the vendor
MIBs as published in LibreNMS (mibs/apc/PowerNet-MIB, mibs/raritan/PDU2-MIB)
and were resolved with net-snmp's snmptranslate.

WTI is not supported: WTI-POWER-MIB defines plugAction as INTEGER (0..8)
without documenting what the values do, so writing it would be a guess.

PDUResponse.raw is the varbind in net-snmp's "OID = TYPE: value" form.
"""
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from equipment_drivers.exceptions import EquipmentCommandError, EquipmentConnectionError, EquipmentNotConnectedError
from equipment_drivers.interfaces import PDUDriver
from equipment_drivers.responses import PDUResponse
from equipment_drivers.snmp import (NO_SUCH_OBJECT, NoSuchObject, SnmpClient, SnmpError, SnmpTimeout,
                                    format_varbind, optional_int)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnmpProfile:
    """Where one MIB keeps a PDU's model, outlet count and per-outlet state/control."""
    name: str
    vendor: str
    model_oid: str
    outlet_count_oid: str
    control_oid: str        # + ".<outlet>"; written to switch
    state_oid: str          # + ".<outlet>"; read for status
    write_on: int
    write_off: int
    read_on: int
    read_off: int
    default_version: str

    def outlet(self, base: str, channel: int) -> str:
        return f"{base}.{channel}"


# PowerNet-MIB, 2nd generation rack PDUs (rPDU2 branch). rPDU2OutletSwitchedControlCommand is
# read-write: setting immediateOn(1)/immediateOff(2) switches; getting returns immediateOn(1) if on,
# immediateOff(2) if off, outletUnknown(4). Indexed by table row = outlet number on a single PDU.
APC_RPDU2 = SnmpProfile(
    name="PowerNet-MIB rPDU2", vendor="apc",
    model_oid="1.3.6.1.4.1.318.1.1.26.2.1.8.1",            # rPDU2IdentModelNumber.1
    outlet_count_oid="1.3.6.1.4.1.318.1.1.26.4.2.1.5.1",   # rPDU2DevicePropertiesNumSwitchedOutlets.1
    control_oid="1.3.6.1.4.1.318.1.1.26.9.2.4.1.5",        # rPDU2OutletSwitchedControlCommand
    state_oid="1.3.6.1.4.1.318.1.1.26.9.2.4.1.5",
    write_on=1, write_off=2, read_on=1, read_off=2, default_version="1")

# PowerNet-MIB, 1st generation switched rack PDUs (rPDU branch); same read/write semantics.
APC_RPDU = SnmpProfile(
    name="PowerNet-MIB rPDU", vendor="apc",
    model_oid="1.3.6.1.4.1.318.1.1.12.1.5.0",              # rPDUIdentModelNumber.0
    outlet_count_oid="1.3.6.1.4.1.318.1.1.12.3.1.3.0",     # rPDUOutletDevNumCntrlOutlets.0
    control_oid="1.3.6.1.4.1.318.1.1.12.3.3.1.1.4",        # rPDUOutletControlOutletCommand
    state_oid="1.3.6.1.4.1.318.1.1.12.3.3.1.1.4",
    write_on=1, write_off=2, read_on=1, read_off=2, default_version="1")

# PDU2-MIB. switchingOperation takes off(0)/on(1)/cycle(2); the state is read from
# outletSwitchingState, a SensorStateEnumeration where on is 7 and off is 8.
# Indexed by pduId (1 unless PDUs are linked) and outletId (1-based).
RARITAN_PDU2 = SnmpProfile(
    name="PDU2-MIB", vendor="raritan",
    model_oid="1.3.6.1.4.1.13742.6.3.2.1.1.3.1",           # pduModel.1
    outlet_count_oid="1.3.6.1.4.1.13742.6.3.2.2.1.4.1",    # outletCount.1
    control_oid="1.3.6.1.4.1.13742.6.4.1.2.1.2.1",         # switchingOperation.1
    state_oid="1.3.6.1.4.1.13742.6.4.1.2.1.3.1",           # outletSwitchingState.1
    write_on=1, write_off=0, read_on=7, read_off=8, default_version="2c")

PROFILES = {"apc": (APC_RPDU2, APC_RPDU), "raritan": (RARITAN_PDU2,)}


class SnmpPduDriver(PDUDriver):
    """Base SNMP driver; concrete per-model classes come from snmp_driver_class()."""
    MODEL_NAME = "SNMP PDU"
    DEFAULT_CHANNEL_COUNT = 8
    DEFAULT_PORT = 161
    VENDOR = ""
    # Write community; APC's factory default is "private" for read-write access.
    DEFAULT_COMMUNITY = "private"

    def __init__(self):
        self.ip = ""
        self.port = self.DEFAULT_PORT
        self.connected = False
        self.community = self.DEFAULT_COMMUNITY
        self.snmp_version: Optional[str] = None  # None: the profile's default (v1 for APC, v2c for Raritan)
        self.timeout = 2.0
        self.profile: Optional[SnmpProfile] = None
        self.client: Optional[SnmpClient] = None
        self.raw_connection = None
        self._device_channel_count: Optional[int] = None

    @classmethod
    def probe(cls, ip: str, port: int) -> bool:
        return False

    def connect(self, ip: str, port: Optional[int] = None, username: Optional[str] = None,
                password: Optional[str] = None) -> bool:
        """SNMP v1/v2c has no username; `password`, if given, is used as the community string."""
        self.ip = ip
        self.port = port or self.DEFAULT_PORT
        if password is not None:
            self.community = password
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port

        try:
            self.profile, model, self.client = find_profile(self.VENDOR, self.ip, self.port, self.community,
                                                            self.snmp_version, self.timeout)
        except SnmpTimeout as e:
            logger.error(f"Failed to connect to {self.get_model()} at {disp_ip}:{disp_port}: {e}")
            raise EquipmentConnectionError(f"SNMP connection failed: {e}")
        if self.profile is None:
            raise EquipmentConnectionError(f"{disp_ip}:{disp_port} answers SNMP but has none of the "
                                           f"{self.VENDOR} PDU MIB objects")
        self.connected = True
        self.raw_connection = format_varbind(self.profile.model_oid, model)
        logger.info(f"Connected to {self.get_model()} at {disp_ip}:{disp_port} "
                    f"(SNMP v{self.client.version}, {self.profile.name})")
        return True

    def disconnect(self) -> bool:
        self.connected = False
        self.client = None
        self._device_channel_count = None
        disp_ip = getattr(self, "display_ip", None) or self.ip
        disp_port = getattr(self, "display_port", None) or self.port
        logger.info(f"Disconnected from {self.get_model()} at {disp_ip}:{disp_port}")
        return True

    def get_model(self) -> str:
        return self.MODEL_NAME

    def get_channel_count(self) -> int:
        if not self.connected:
            return self.DEFAULT_CHANNEL_COUNT
        if self._device_channel_count is None:
            count = optional_int(self._snmp("get", self.profile.outlet_count_oid))
            self._device_channel_count = count if count and count > 0 else self.DEFAULT_CHANNEL_COUNT
        return self._device_channel_count

    def _snmp(self, op: str, oid: str, value=None):
        if not self.connected or self.client is None:
            raise EquipmentNotConnectedError("Not connected to PDU")
        try:
            return self.client.get(oid) if op == "get" else self.client.set(oid, value)
        except SnmpTimeout as e:
            raise EquipmentConnectionError(f"SNMP {op} {oid}: {e}")
        except SnmpError as e:
            raise EquipmentCommandError(str(e))

    def _switch(self, channel: int, value: int) -> Tuple[bool, str]:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        oid = self.profile.outlet(self.profile.control_oid, channel)
        try:
            reply = self._snmp("set", oid, value)
        except EquipmentCommandError as e:
            logger.error(f"SNMP set {oid} = {value} on {self.get_model()} failed: {e}")
            return False, f"{format_varbind(oid, value)}\n{e}"
        return True, format_varbind(oid, reply if reply is not None else value)

    def turn_on(self, channel: int) -> PDUResponse:
        logger.info(f"Turning ON channel {channel} on {self.get_model()}")
        success, raw = self._switch(channel, self.profile.write_on)
        return PDUResponse(success=success, action="turn_on", channel=channel, raw=raw)

    def turn_off(self, channel: int) -> PDUResponse:
        logger.info(f"Turning OFF channel {channel} on {self.get_model()}")
        success, raw = self._switch(channel, self.profile.write_off)
        return PDUResponse(success=success, action="turn_off", channel=channel, raw=raw)

    def get_status(self, channel: int) -> PDUResponse:
        if not self.connected:
            raise EquipmentNotConnectedError("Not connected to PDU")
        self.validate_channel(channel)
        oid = self.profile.outlet(self.profile.state_oid, channel)
        value = self._snmp("get", oid)
        raw = format_varbind(oid, value)
        if value == self.profile.read_on:
            return PDUResponse(success=True, action="get_status", channel=channel, raw=raw, status="ON")
        if value == self.profile.read_off:
            return PDUResponse(success=True, action="get_status", channel=channel, raw=raw, status="OFF")
        return PDUResponse(success=False, action="get_status", channel=channel, raw=raw, status=f"UNKNOWN ({value})")


def find_profile(vendor: str, ip: str, port: int, community: str, version: Optional[str],
                 timeout: float) -> Tuple[Optional[SnmpProfile], object, Optional[SnmpClient]]:
    """
    Try each of the vendor's MIB profiles; the first whose model OID exists wins.
    Returns (profile, model value, client) or (None, None, None). Raises SnmpTimeout
    if the agent never answers (unreachable, SNMP off, or wrong community).
    """
    for profile in PROFILES.get(vendor, ()):
        client = SnmpClient(ip, port, community, version or profile.default_version, timeout=timeout)
        model = client.get(profile.model_oid)
        if not isinstance(model, NoSuchObject) and model is not None:
            return profile, model, client
    return None, None, None


def snmp_driver_class(model_cls: type) -> type:
    """The SNMP counterpart of a registered (or generic) APC/Raritan model driver."""
    from equipment_drivers.pdu.apc_models import BaseApcPduDriver
    from equipment_drivers.pdu.raritan_models import BaseRaritanPduDriver
    if issubclass(model_cls, BaseApcPduDriver):
        vendor = "apc"
    elif issubclass(model_cls, BaseRaritanPduDriver):
        vendor = "raritan"
    else:
        raise ValueError(f"SNMP control isn't supported for {model_cls.MODEL_NAME} (APC and Raritan only)")
    return type(f"{model_cls.__name__}Snmp", (SnmpPduDriver,), {
        "MODEL_NAME": model_cls.MODEL_NAME,
        "DEFAULT_CHANNEL_COUNT": model_cls.DEFAULT_CHANNEL_COUNT,
        "VENDOR": vendor,
    })


def identify_snmp(ip: str, port: Optional[int], community: str, version: Optional[str] = None,
                  timeout: float = 2.0) -> Optional[Tuple[str, str]]:
    """Ask an SNMP agent which vendor/model it is. Returns (vendor, reported model) or None."""
    for vendor in PROFILES:
        try:
            profile, model, _client = find_profile(vendor, ip, port or SnmpPduDriver.DEFAULT_PORT, community,
                                                   version, timeout)
        except SnmpTimeout as e:
            # Nothing answers at all (or the community is wrong): other vendors won't fare better.
            logger.debug(f"SNMP identify at {ip}: {e}")
            return None
        except (SnmpError, OSError) as e:
            logger.debug(f"SNMP identify ({vendor}) at {ip}: {e}")
            continue
        if profile is not None:
            return vendor, str(model)
    return None


__all__ = ["APC_RPDU", "APC_RPDU2", "RARITAN_PDU2", "PROFILES", "SnmpPduDriver", "SnmpProfile",
           "find_profile", "identify_snmp", "snmp_driver_class", "NO_SUCH_OBJECT"]
