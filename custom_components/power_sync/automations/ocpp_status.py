"""Shared helpers for normalizing OCPP charger status."""

from __future__ import annotations

import re
from typing import Optional


OCPP_CHARGING_STATES = {"charging"}
OCPP_VEHICLE_PRESENT_STATES = {
    "preparing",
    "charging",
    "suspendedev",
    "suspendedevse",
    "finishing",
}
OCPP_HARDWARE_ONLINE_STATES = OCPP_VEHICLE_PRESENT_STATES | {"available", "reserved"}
OCPP_SESSION_ACTIVE_STATES = {
    "preparing",
    "charging",
    "suspendedev",
    "suspendedevse",
}
OCPP_IDLE_STATES = {"available", "unavailable", "unknown", "faulted", "offline", ""}
OCPP_STATUS_SUFFIXES = (
    "_status_connector",
    "_status",
    "_availability",
    "_charge_control",
)
OCPP_POWER_SUFFIXES = (
    "_current_power",
    "_power_active_import",
)
OCPP_CAPABILITY_SUFFIXES = ("_power_offered",)
OCPP_ENERGY_SUFFIXES = (
    "_energy_meter",
    "_energy_active_import_register",
    "_energy_active_import_interval",
    "_energy_session",
)
OCPP_CURRENT_LIMIT_SUFFIXES = (
    "_maximum_current",
    "_max_current",
    "_current_limit",
    "_charging_current",
    "_charge_current",
)
OCPP_ENTITY_SUFFIXES = (
    *OCPP_STATUS_SUFFIXES,
    *OCPP_POWER_SUFFIXES,
    *OCPP_CAPABILITY_SUFFIXES,
    *OCPP_ENERGY_SUFFIXES,
    *OCPP_CURRENT_LIMIT_SUFFIXES,
)


def normalize_ocpp_status(status: Optional[str]) -> str:
    """Normalize common OCPP/HACS status spelling variants."""
    if status is None:
        return ""
    return str(status).strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def extract_hacs_ocpp_prefix(entity_id: str) -> Optional[str]:
    """Extract the charger prefix from a HACS OCPP entity id."""
    if "." not in entity_id:
        return None
    object_id = entity_id.lower().split(".", 1)[1]
    for suffix in OCPP_ENTITY_SUFFIXES:
        if object_id.endswith(suffix):
            return object_id[: -len(suffix)]
    return None


def split_hacs_ocpp_connector_prefix(prefix: str) -> tuple[str, int | None]:
    """Return the base charge-point id and optional connector id from a HACS prefix."""
    match = re.match(r"^(.+)_connector_(\d+)$", str(prefix))
    if not match:
        return str(prefix), None
    return match.group(1), int(match.group(2))


def is_hacs_ocpp_status_entity(entity_id: str) -> bool:
    """Return True for HACS OCPP connector/station status entities."""
    if "." not in entity_id:
        return False
    object_id = entity_id.lower().split(".", 1)[1]
    return object_id.endswith("_status_connector") or object_id.endswith("_status")


def is_hacs_ocpp_power_entity(entity_id: str) -> bool:
    """Return True for HACS OCPP delivered-power measurand entities.

    Power offered is the EVSE's advertised capacity, not power delivered to a
    vehicle, so it must not drive connected or charging state.
    """
    if "." not in entity_id:
        return False
    object_id = entity_id.lower().split(".", 1)[1]
    return any(object_id.endswith(suffix) for suffix in OCPP_POWER_SUFFIXES)


def is_hacs_ocpp_energy_entity(entity_id: str) -> bool:
    """Return True for HACS OCPP energy measurand entities."""
    if "." not in entity_id:
        return False
    object_id = entity_id.lower().split(".", 1)[1]
    return any(object_id.endswith(suffix) for suffix in OCPP_ENERGY_SUFFIXES)


def is_ocpp_charging(status: Optional[str], power_w: float = 0.0) -> bool:
    """Return True when the connector is actively charging."""
    return normalize_ocpp_status(status) in OCPP_CHARGING_STATES or power_w > 50


def is_ocpp_vehicle_present(status: Optional[str], power_w: float = 0.0) -> bool:
    """Return True when a vehicle appears connected to the connector."""
    normalized = normalize_ocpp_status(status)
    return normalized in OCPP_VEHICLE_PRESENT_STATES or power_w > 50


def is_ocpp_hardware_online(status: Optional[str]) -> bool:
    """Return True when charger hardware is reachable, even without a vehicle."""
    return normalize_ocpp_status(status) in OCPP_HARDWARE_ONLINE_STATES


def should_end_ocpp_session(status: Optional[str], power_w: float, has_session: bool) -> bool:
    """Return True when an active OCPP session should be closed."""
    if not has_session:
        return False

    normalized = normalize_ocpp_status(status)
    if power_w > 50:
        return False
    if normalized == "finishing":
        return True
    if normalized not in OCPP_SESSION_ACTIVE_STATES:
        return True
    return False
