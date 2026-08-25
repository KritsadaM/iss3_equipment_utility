"""
Custom exception hierarchy for ISS3 equipment drivers.

All equipment-related errors inherit from EquipmentError so callers
can catch a single base class when they don't need to distinguish
connection failures from command failures.
"""


class EquipmentError(Exception):
    """Base exception for all equipment driver errors."""
    pass


class EquipmentConnectionError(EquipmentError):
    """Raised when a driver cannot establish or maintain a connection."""
    pass


class EquipmentCommandError(EquipmentError):
    """Raised when a connected driver fails to execute a command."""
    pass


class EquipmentNotConnectedError(EquipmentError):
    """Raised when an operation is attempted on a driver that is not connected."""
    pass
