from abc import ABC, abstractmethod
from typing import Tuple, Optional
from equipment_drivers.responses import PDUResponse
from equipment_drivers.exceptions import EquipmentNotConnectedError

class EquipmentDriver(ABC):
    @classmethod
    def probe(cls, ip: str, port: int) -> bool:
        """
        Return True if this driver positively identifies the device at ip:port
        as one it can handle. Default is False, so a driver that hasn't
        implemented real detection yet is simply never matched by discovery,
        rather than silently matching everything. Override in concrete
        drivers with real vendor-specific detection.
        """
        return False

    @abstractmethod
    def connect(self, ip: str, port: int, username: Optional[str] = None, password: Optional[str] = None) -> bool:
        """
        username/password are optional overrides for equipment that requires
        auth (e.g. HTTP/SNMP credentials). Drivers with no concept of auth
        (most dummy/simulated drivers) can simply ignore them. Drivers that
        do use credentials should fall back to their own default if these
        are left as None, so existing callers that don't pass them keep working.
        """
        pass

    @abstractmethod
    def disconnect(self) -> bool:
        pass

    @abstractmethod
    def get_model(self) -> str:
        pass

class PDUDriver(EquipmentDriver):
    @abstractmethod
    def turn_on(self, channel: int) -> PDUResponse:
        pass

    @abstractmethod
    def turn_off(self, channel: int) -> PDUResponse:
        pass

    @abstractmethod
    def get_status(self, channel: int) -> PDUResponse:
        pass

    @abstractmethod
    def get_channel_count(self) -> int:
        """
        Total number of controllable channels/outlets on this PDU. Used to
        expand 'all' in a channel spec (see channel_spec.py). Should reflect
        the actual connected unit where possible (e.g. queried from the
        device), not just a guess -- an overstated count would attempt writes
        to channels that don't exist, an understated one would silently skip
        real outlets.
        """
        pass

    def get_max_channel(self) -> int:
        """
        Returns the maximum valid channel number for this PDU (1..max_channel).
        Defaults to get_channel_count().
        """
        return self.get_channel_count()

    def validate_channel(self, channel: int) -> None:
        """
        Validates that the channel is within the allowed range [1, get_max_channel()].
        Raises ValueError with a descriptive usage error message if out of range.
        """
        if not isinstance(channel, int):
            raise ValueError(f"Channel must be an integer, got {type(channel).__name__}: {channel}")
        max_ch = self.get_max_channel()
        if channel < 1 or channel > max_ch:
            raise ValueError(
                f"Channel {channel} is out of range. Valid channels for {self.get_model()} are 1 to {max_ch}."
            )

class TerminalServerDriver(EquipmentDriver):
    @abstractmethod
    def get_status(self) -> Tuple[str, str]:
        pass

class DAQDriver(EquipmentDriver):
    @abstractmethod
    def start_acquisition(self) -> Tuple[bool, str]:
        pass

    @abstractmethod
    def stop_acquisition(self) -> Tuple[bool, str]:
        pass

    @abstractmethod
    def get_status(self) -> Tuple[str, str]:
        pass
