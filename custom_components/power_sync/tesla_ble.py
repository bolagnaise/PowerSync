"""Shared Tesla BLE entity resolution helpers."""

from __future__ import annotations

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
    """Resolve legacy and current Tesla BLE charger-power entities."""
    return _first_available_state(
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
    return _first_available_state(
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
