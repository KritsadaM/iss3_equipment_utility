import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple, Type

from equipment_drivers.registry import registry
from equipment_drivers.interfaces import EquipmentDriver

logger = logging.getLogger(__name__)

# How long each vendor's identify() may take when asking an unknown device who it is.
IDENTIFY_TIMEOUT = 3.0


def discover_and_instantiate(ip: str, port: Optional[int], equipment_type: str,
                             username: Optional[str] = None, password: Optional[str] = None,
                             identify: bool = True) -> Optional[EquipmentDriver]:
    """
    Picks the driver for the device at ip:port, in order of confidence:

    1. Ask the device. Every vendor base class that implements identify()
       is tried in parallel; a vendor that recognizes the device returns the
       model string the unit reports, which is matched to a registered model
       driver. A recognized vendor with an unlisted model gets a generic
       driver for that vendor (the outlet count is read from the device).
    2. Guess from the IP address: each driver's probe() classmethod (today an
       IP-suffix convention).
    3. The "dummy_{equipment_type}_sig" driver as a last resort.

    Pass identify=False to skip step 1 (no network traffic).
    """
    candidates = registry.get_all_drivers(equipment_type)
    if not candidates:
        logger.error(f"No drivers registered for equipment type '{equipment_type}'")
        return None

    if identify:
        driver = _identify_device(ip, port, candidates, username, password)
        if driver is not None:
            return driver

    logger.info(f"Probing {ip}:{port} against {len(candidates)} registered {equipment_type} driver(s)...")

    fallback_signature = f"dummy_{equipment_type}_sig"
    fallback_class = None

    for signature, driver_class in candidates:
        if signature == fallback_signature:
            # Held back for last -- only used if nothing positively matches.
            fallback_class = driver_class
            continue
        try:
            if driver_class.probe(ip, port):
                if identify:
                    logger.warning(f"Device at {ip} did not identify itself; guessed {driver_class.__name__} "
                                   f"(signature: {signature}) from its IP address.")
                else:
                    logger.info(f"Matched {driver_class.__name__} (signature: {signature}) for {ip}:{port}")
                return driver_class()
        except Exception as e:
            logger.warning(f"probe() raised for {driver_class.__name__} ({signature}): {e}")

    if fallback_class:
        logger.warning(
            f"No driver positively identified {ip}:{port}; falling back to "
            f"{fallback_class.__name__} (signature '{fallback_signature}')."
        )
        return fallback_class()

    logger.error(f"No registered driver could identify device at {ip}:{port}")
    return None


def _identifiers(candidates) -> List[type]:
    """Vendor base classes (in registration order) that define identify() themselves."""
    found = []
    for _sig, cls in candidates:
        for klass in cls.__mro__:
            if "identify" in vars(klass) and klass not in found:
                found.append(klass)
                break
    return found


def _identify_device(ip, port, candidates, username, password) -> Optional[EquipmentDriver]:
    identifiers = _identifiers(candidates)
    if not identifiers:
        return None

    logger.info(f"Asking {ip} to identify itself ({len(identifiers)} vendor protocol(s))...")

    def ask(base):
        try:
            return base.identify(ip, port, username, password, timeout=IDENTIFY_TIMEOUT)
        except Exception as e:
            logger.debug(f"{base.__name__}.identify raised: {e}")
            return None

    with ThreadPoolExecutor(max_workers=len(identifiers)) as pool:
        answers = list(zip(identifiers, pool.map(ask, identifiers)))

    for base, reported_model in answers:
        if reported_model is None:
            continue
        match = match_reported_model(base, reported_model, candidates)
        if match:
            signature, driver_class = match
            logger.info(f"Device at {ip} identified itself as '{reported_model}' -> "
                        f"{driver_class.__name__} (signature: {signature})")
            return driver_class()
        driver_class = generic_driver_class(base, reported_model)
        logger.warning(f"Device at {ip} identified itself as '{reported_model or 'unknown model'}', which is not in "
                       f"models.yaml; using generic {base.__name__} (outlet count read from the device).")
        return driver_class()
    return None


def _normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def model_token(model_name: str) -> str:
    """The part number inside a display name: its first word containing a digit,
    normalized. "WTI VMR-HD4D20 C19" -> "VMRHD4D20", "Raritan Dominion PX DPXR8A-16" -> "DPXR8A16"."""
    for word in model_name.split():
        if any(ch.isdigit() for ch in word):
            return _normalize(word)
    return ""


def match_reported_model(base: type, reported_model: str,
                         candidates) -> Optional[Tuple[str, Type[EquipmentDriver]]]:
    """
    Find the registered driver (a subclass of `base`) whose part number the
    device's reported model starts with; the longest such part number wins, so
    "AP8959EU3" picks apc_ap8959eu3 over apc_ap8959, and "AP7920B" (a revision
    suffix) still picks apc_ap7920.
    """
    reported = _normalize(reported_model)
    if not reported:
        return None
    best = None
    for signature, cls in candidates:
        if not (isinstance(cls, type) and issubclass(cls, base)) or cls is base:
            continue
        token = model_token(getattr(cls, "MODEL_NAME", ""))
        if token and reported.startswith(token) and (best is None or len(token) > best[0]):
            best = (len(token), signature, cls)
    return (best[1], best[2]) if best else None


def generic_driver_class(base: type, reported_model: str) -> type:
    """A driver class for a recognized vendor whose model isn't in models.yaml."""
    label = reported_model or "unknown model"
    return type(f"Generic{base.__name__}", (base,), {"MODEL_NAME": f"{base.MODEL_NAME} ({label})"})
