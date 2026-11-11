"""Shared Tesla BLE entity resolution helpers."""

from __future__ import annotations

import json
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

_COMPANION_SUFFIXES = {"_sample": "sample", "_received": "received"}


def _entity_registry(hass: Any) -> Any | None:
    """Return HA's entity registry, or None where it is unavailable."""
    try:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(hass)
    except Exception:  # noqa: BLE001 - registry absence keeps exact-name lookup
        return None


def _registry_entries_for_device(registry: Any, device_id: str) -> list[Any]:
    entities = getattr(registry, "entities", None)
    getter = getattr(entities, "get_entries_for_device_id", None)
    if callable(getter):
        try:
            return list(getter(device_id, include_disabled_entities=False))
        except TypeError:
            return list(getter(device_id))
    values = entities.values() if hasattr(entities, "values") else ()
    return [entry for entry in values if getattr(entry, "device_id", None) == device_id]


def _normalized_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def get_tesla_ble_companion_state(hass: Any, entity_id: str, suffix: str) -> Any | None:
    """Return the freshness companion (``_sample``/``_received``) of a raw entity.

    The exact ``<entity>_suffix`` name is authoritative. When it is absent, the
    companion may still exist under a different entity-ID prefix: HA names new
    entities from the *current* device name, so a bridge renamed before the
    freshness package was installed yields e.g. ``garage_<prefix>_charge_power_sample``
    beside the original ``<prefix>_charge_power``. Resolve that through the
    entity registry, restricted to the same device, and only when exactly one
    candidate matches. The packet/value checks in ``get_tesla_ble_observed_at``
    still fail closed on any mismatched pairing.
    """
    if not entity_id or suffix not in _COMPANION_SUFFIXES:
        return None
    exact = hass.states.get(f"{entity_id}{suffix}")
    if exact is not None:
        return exact
    registry = _entity_registry(hass)
    if registry is None:
        return None
    try:
        raw_entry = registry.async_get(entity_id)
        device_id = getattr(raw_entry, "device_id", None)
        if raw_entry is None or not device_id:
            return None
        domain, _, raw_object = entity_id.partition(".")
        raw_name = _normalized_name(
            getattr(raw_entry, "original_name", None) or getattr(raw_entry, "name", None)
        )
        expected_name = f"{raw_name} {_COMPANION_SUFFIXES[suffix]}" if raw_name else None
        matches = set()
        for entry in _registry_entries_for_device(registry, device_id):
            candidate = str(getattr(entry, "entity_id", "") or "")
            cand_domain, _, cand_object = candidate.partition(".")
            if cand_domain != domain or candidate == entity_id:
                continue
            if getattr(entry, "disabled_by", None) is not None:
                continue
            if cand_object.endswith(f"_{raw_object}{suffix}") or (
                expected_name is not None
                and _normalized_name(getattr(entry, "original_name", None)) == expected_name
            ):
                matches.add(candidate)
        if len(matches) != 1:
            return None
        return hass.states.get(next(iter(matches)))
    except Exception:  # noqa: BLE001 - registry quirks must not break telemetry reads
        return None


def get_tesla_ble_observed_at(hass: Any, state: Any) -> datetime | None:
    """Read a bridge's receipt time, without renewing a replayed HA value.

    The optional charging-freshness package publishes ``<entity>_received``
    timestamp values on raw samples, including equal measurements which HA's
    ESPHome integration suppresses. Its state value is the receipt, not its HA
    update time. The newer ``<entity>_sample`` packet pairs a raw value and
    its timestamp; a mismatched filtered value is not fresh evidence.
    Once present, an invalid receipt must fail closed. Older
    bridges without these entities retain their existing HA timestamp contract.
    """
    if state is None:
        return None
    entity_id = getattr(state, "entity_id", "")
    sample = get_tesla_ble_companion_state(hass, entity_id, "_sample")
    if sample is not None:
        # Separate HA entities can arrive in either order. Never join a new
        # receipt to an older value while the filtered sensor is catching up.
        try:
            packet = json.loads(sample.state)
            if not isinstance(packet, dict) or type(packet.get("v")) is not int or packet["v"] != 1:
                return None
            native = packet["x"]
            if type(native) in (int, float):
                current = float(state.state)
                if not math.isfinite(native) or not math.isfinite(current) or not math.isclose(native, current, rel_tol=1e-6, abs_tol=1e-6):
                    return None
            elif not isinstance(native, str) or native != state.state:
                return None
            observed = datetime.fromisoformat(packet["t"].replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None:
                return None
            observed = observed.astimezone(timezone.utc)
            return observed if observed <= datetime.now(timezone.utc) else None
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return None
    receipt = get_tesla_ble_companion_state(hass, entity_id, "_received")
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
    """Return vehicle connectivity, respecting both node and BLE status.

    Older/example ESPHome bridge YAML exposes a generic node ``Status`` entity,
    while the Tesla BLE component itself exposes ``BLE Status``. A connected
    Wi-Fi node does not prove a vehicle BLE connection. When both exist they
    must both be on; retain node-only compatibility and component-only discovery.
    """
    node_id, connection_id = tesla_ble_status_entity_ids(prefix)
    node = hass.states.get(node_id)
    connection = hass.states.get(connection_id)
    if node is not None and str(node.state).lower() != "on":
        return node
    return connection if connection is not None else node


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
