"""Shared Tesla BLE entity resolution helpers."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .const import (
    TESLA_BLE_BINARY_CHARGE_FLAP,
    TESLA_BLE_BINARY_CHARGER,
    TESLA_BLE_BINARY_CONNECTION_STATUS,
    TESLA_BLE_BINARY_STATUS,
    TESLA_BLE_SENSOR_BATTERY,
    TESLA_BLE_SENSOR_CHARGE_LEVEL,
    TESLA_BLE_SENSOR_CHARGE_POWER,
    TESLA_BLE_SENSOR_CHARGE_CURRENT,
    TESLA_BLE_SENSOR_CHARGER_CURRENT,
    TESLA_BLE_SENSOR_CHARGER_POWER,
    TESLA_BLE_SENSOR_CHARGING,
    TESLA_BLE_SENSOR_CHARGING_STATE,
)


def get_tesla_ble_observed_at(hass: Any, state: Any) -> datetime | None:
    """Read a bridge's receipt time, without renewing a replayed HA value.

    The optional charging-freshness package publishes ``<entity>_received``
    timestamp values on raw samples, including equal measurements which HA's
    ESPHome integration suppresses. Its state value is the receipt, not its HA
    update time. Once present, an invalid receipt must fail closed. Older
    bridges without these entities retain their existing HA timestamp contract.
    """
    if state is None:
        return None
    entity_id = getattr(state, "entity_id", "")
    receipt = hass.states.get(f"{entity_id}_received") if entity_id else None
    if receipt is not None:
        try:
            observed = datetime.fromisoformat(str(receipt.state).replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None:
                return None
            observed = observed.astimezone(timezone.utc)
            return observed if observed <= datetime.now(timezone.utc) else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    observed = (
        getattr(state, "last_reported", None)
        or getattr(state, "last_updated", None)
        or getattr(state, "last_changed", None)
    )
    if not isinstance(observed, datetime):
        return None
    if observed.tzinfo is None:
        return observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def _first_available_state(hass: Any, entity_ids: tuple[str, ...]) -> Any | None:
    """Return the first current HA state from compatibility entity names."""
    unavailable_state = None
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        if state.state not in ("unavailable", "unknown", "None", None):
            return state
        if unavailable_state is None:
            unavailable_state = state
    return unavailable_state


def _newest_finite_state(hass: Any, entity_ids: tuple[str, ...]) -> Any | None:
    """Return the newest finite compatibility reading without trusting order."""
    selected = None
    selected_at = None
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", "None", None):
            continue
        try:
            if not math.isfinite(float(state.state)):
                continue
        except (TypeError, ValueError):
            continue
        observed_at = get_tesla_ble_observed_at(hass, state)
        if (
            selected is None
            or (
                observed_at is not None
                and (selected_at is None or observed_at > selected_at)
            )
        ):
            selected = state
            selected_at = observed_at
    return selected


def tesla_ble_status_entity_ids(prefix: str) -> tuple[str, str]:
    """Return supported bridge-status entities in compatibility order."""
    return (
        TESLA_BLE_BINARY_STATUS.format(prefix=prefix),
        TESLA_BLE_BINARY_CONNECTION_STATUS.format(prefix=prefix),
    )


def get_tesla_ble_status_state(hass: Any, prefix: str) -> Any | None:
    """Return the first available BLE bridge status state.

    Older/example ESPHome bridge YAML exposes a generic node ``Status`` entity,
    while the Tesla BLE component itself exposes ``BLE Status``. Prefer the
    existing node-status signal for compatibility, but do not hide a configured
    vehicle when only the component connection status is present.
    """
    for entity_id in tesla_ble_status_entity_ids(prefix):
        state = hass.states.get(entity_id)
        if state is not None:
            return state
    return None


def get_tesla_ble_battery_state(hass: Any, prefix: str) -> Any | None:
    """Resolve legacy charge-level and current Tesla BLE battery entities."""
    return _first_available_state(
        hass,
        (
            TESLA_BLE_SENSOR_CHARGE_LEVEL.format(prefix=prefix),
            TESLA_BLE_SENSOR_BATTERY.format(prefix=prefix),
        ),
    )


def get_tesla_ble_charging_state(hass: Any, prefix: str) -> Any | None:
    """Resolve legacy and current Tesla BLE charging-state entities."""
    return _first_available_state(
        hass,
        (
            TESLA_BLE_SENSOR_CHARGING_STATE.format(prefix=prefix),
            TESLA_BLE_SENSOR_CHARGING.format(prefix=prefix),
        ),
    )


def get_tesla_ble_charge_power_state(hass: Any, prefix: str) -> Any | None:
    """Resolve the freshest finite legacy/current BLE power reading."""
    return _newest_finite_state(
        hass,
        (
            TESLA_BLE_SENSOR_CHARGE_POWER.format(prefix=prefix),
            TESLA_BLE_SENSOR_CHARGER_POWER.format(prefix=prefix),
        ),
    )


def get_tesla_ble_charge_current_state(hass: Any, prefix: str) -> Any | None:
    """Resolve legacy and current Tesla BLE measured-current entities.

    ``number.*_charging_amps`` is a writable requested-current control, not a
    physical-current readback, so it is deliberately not a fallback here.
    """
    return _newest_finite_state(
        hass,
        (
            TESLA_BLE_SENSOR_CHARGE_CURRENT.format(prefix=prefix),
            TESLA_BLE_SENSOR_CHARGER_CURRENT.format(prefix=prefix),
        ),
    )


def get_tesla_ble_plug_state(hass: Any, prefix: str) -> Any | None:
    """Resolve a physical plug-state entity without treating a control as state."""
    return _first_available_state(
        hass,
        (
            TESLA_BLE_BINARY_CHARGE_FLAP.format(prefix=prefix),
            TESLA_BLE_BINARY_CHARGER.format(prefix=prefix),
        ),
    )
