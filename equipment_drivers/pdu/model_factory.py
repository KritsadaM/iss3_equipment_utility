"""
Data-driven PDU driver factory.

Reads models.yaml and dynamically creates concrete driver subclasses
for each vendor's models, then registers them with the global registry.
This replaces the 57 hand-written subclasses that each only overrode
MODEL_NAME, DEFAULT_CHANNEL_COUNT, and IP_SUFFIX.
"""
import os
import logging
from typing import Dict, Any, Type

try:
    import yaml
except ImportError:
    # PyYAML is optional at runtime -- fall back to a minimal inline parser
    # that handles only the flat {key: value} maps used in models.yaml.
    yaml = None

from equipment_drivers.registry import registry

logger = logging.getLogger(__name__)

_MODELS_FILE = os.path.join(os.path.dirname(__file__), "models.yaml")

# Maps vendor key in YAML -> (base_class_import_path, module_name)
_VENDOR_BASES = {
    "apc": "equipment_drivers.pdu.apc_models",
    "wti": "equipment_drivers.pdu.wti_models",
    "raritan": "equipment_drivers.pdu.raritan_models",
}


def _parse_simple_yaml(path: str) -> dict:
    """Minimal YAML-subset parser for models.yaml (no PyYAML needed).
    
    Handles the specific structure of models.yaml:
    - Top-level keys (vendors) ending with ':'
    - Nested entries in {key: value, ...} format
    """
    result = {}
    current_vendor = None

    with open(path, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Top-level vendor key
            if not line.startswith(" ") and stripped.endswith(":"):
                current_vendor = stripped[:-1]
                result[current_vendor] = {}
                continue

            # Nested model entry: "  sig: {model: ..., channels: N, suffix: ...}"
            if current_vendor and "{" in stripped:
                sig_part, _, brace_part = stripped.partition(":")
                sig = sig_part.strip()
                brace_content = brace_part.strip().strip("{}")
                attrs = {}
                for pair in brace_content.split(","):
                    k, _, v = pair.partition(":")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "channels":
                        v = int(v)
                    attrs[k] = v
                result[current_vendor][sig] = attrs

    return result


def _load_models() -> dict:
    """Load model definitions from models.yaml."""
    if yaml is not None:
        with open(_MODELS_FILE, "r") as f:
            return yaml.safe_load(f)
    else:
        return _parse_simple_yaml(_MODELS_FILE)


def create_and_register_models() -> Dict[str, Type]:
    """
    Create concrete driver subclasses from models.yaml and register them.
    
    Returns a dict of signature -> driver_class for all created models.
    """
    all_models = {}
    data = _load_models()

    for vendor, models in data.items():
        base_module = _VENDOR_BASES.get(vendor)
        if base_module is None:
            logger.warning(f"Unknown vendor '{vendor}' in models.yaml, skipping")
            continue

        # Import the base class dynamically
        import importlib
        mod = importlib.import_module(base_module)

        # Find the Base*PduDriver class
        base_cls = None
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type) and
                    attr_name.startswith("Base") and
                    attr_name.endswith("PduDriver")):
                base_cls = obj
                break

        if base_cls is None:
            logger.error(f"No Base*PduDriver found in {base_module}")
            continue

        for signature, attrs in models.items():
            model_name = attrs["model"]
            channels = attrs["channels"]
            ip_suffix = attrs["suffix"]

            # Create a new class dynamically
            # Build PascalCase class name, preserving uppercase for trailing
            # letters in model numbers (e.g. "5190r" -> "5190R")
            parts = signature.split("_")
            cls_name = ""
            for part in parts:
                if part[0].isdigit():
                    # Numeric segment: keep digits, uppercase any trailing alpha
                    cls_name += part.upper()
                else:
                    cls_name += part.capitalize()
            cls_name += "Driver"

            driver_cls = type(cls_name, (base_cls,), {
                "MODEL_NAME": model_name,
                "DEFAULT_CHANNEL_COUNT": channels,
                "IP_SUFFIX": ip_suffix,
            })

            registry.register("pdu", signature, driver_cls)
            all_models[signature] = driver_cls
            logger.debug(f"Registered {cls_name} ({model_name}) with suffix {ip_suffix}")

    return all_models
