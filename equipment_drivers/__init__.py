"""
Importing this package triggers auto-discovery of all driver modules under
pdu/, terminal_server/, and daq/. Each driver module is expected to register
itself against the global registry at import time.

After module-level imports, the data-driven model factory reads models.yaml
and registers all vendor-specific PDU models (APC, WTI, Raritan) automatically.
Adding a new PDU model is just one line in models.yaml — no Python needed.
"""
import pkgutil
import importlib
import os
import logging

logger = logging.getLogger(__name__)

_PACKAGE_DIR = os.path.dirname(__file__)
_DRIVER_SUBPACKAGES = ("pdu", "terminal_server", "daq")

for _subpkg in _DRIVER_SUBPACKAGES:
    _subpkg_path = os.path.join(_PACKAGE_DIR, _subpkg)
    if not os.path.isdir(_subpkg_path):
        continue
    for _finder, _module_name, _is_pkg in pkgutil.iter_modules([_subpkg_path]):
        # Skip model_factory — it's called explicitly below
        if _module_name == "model_factory":
            continue
        _full_name = f"{__name__}.{_subpkg}.{_module_name}"
        try:
            importlib.import_module(_full_name)
        except Exception as e:
            logger.error(f"Failed to import driver module {_full_name}: {e}")

# Register all data-driven PDU models from models.yaml
try:
    from equipment_drivers.pdu.model_factory import create_and_register_models
    create_and_register_models()
except Exception as e:
    logger.error(f"Failed to register data-driven PDU models: {e}")
