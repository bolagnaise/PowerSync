"""Home Assistant inputs for the away-car return decision.

Reads location, range and navigation from the car's own Tesla entities and
keeps the owner's per-car "not home until / home at" answer. Read-only towards
the vehicle: nothing here wakes a car or sends a command.
"""
from __future__ import annotations

from datetime import datetime
import logging
import math
import re
from typing import Any, Mapping

from .ev_return import (
    HOME_RADIUS_KM,
    MODE_AUTO,
    MODE_HOME_AT,
    MODE_NOT_HOME,
    EVReturnOverride,
    EVReturnTelemetry,
    haversine_km,
)

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "ev_return_overrides"
_MILES_TO_KM = 1.609344
_UNUSABLE = ("unavailable", "unknown", "none", "")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _distance_km(state: Any) -> float | None:
    if state is None or str(state.state).lower() in _UNUSABLE:
        return None
    value = _number(state.state)
    if value is None or value < 0:
        return None
    unit = str((state.attributes or {}).get("unit_of_measurement", "km")).lower()
    return value * _MILES_TO_KM if unit in ("mi", "miles") else value


def _timestamp(state: Any) -> datetime | None:
    if state is None or str(state.state).lower() in _UNUSABLE:
        return None
    try:
        value = datetime.fromisoformat(str(state.state).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def telemetry_from_states(
    states: Mapping[str, Any], *, home_lat: float, home_lon: float,
    soc: float | None,
) -> EVReturnTelemetry:
    """Build telemetry from one car's entity states (entity_id -> state)."""
    latitude = longitude = range_km = nav_eta = nav_distance = None
    navigating_home = False
    for entity_id, state in states.items():
        if state is None:
            continue
        # HA appends _2, _3 to duplicate entity ids; match on the base name.
        name = re.sub(r"_\d+$", "", entity_id.lower())
        attrs = getattr(state, "attributes", None) or {}
        if name.startswith("device_tracker.") and name.endswith("_location"):
            latitude, longitude = _number(attrs.get("latitude")), _number(attrs.get("longitude"))
        elif name.startswith("device_tracker.") and name.endswith("_route"):
            if str(state.state).lower() == "home":
                navigating_home = True
            else:
                lat, lon = _number(attrs.get("latitude")), _number(attrs.get("longitude"))
                hlat, hlon = _number(home_lat), _number(home_lon)
                if None not in (lat, lon, hlat, hlon):
                    navigating_home = haversine_km(lat, lon, hlat, hlon) <= HOME_RADIUS_KM
        elif (name.startswith("sensor.") and name.endswith("_battery_range")
              and not any(word in name for word in ("estimate", "ideal"))):
            # Rated range only; "estimate"/"ideal" variants are separate entities.
            range_km = _distance_km(state)
        elif name.startswith("sensor.") and name.endswith("_time_to_arrival"):
            nav_eta = _timestamp(state)
        elif name.startswith("sensor.") and name.endswith("_distance_to_arrival"):
            nav_distance = _distance_km(state)
        elif (soc is None and name.startswith("sensor.") and name.endswith("_battery_level")
              and str(state.state).lower() not in _UNUSABLE):
            level = _number(state.state)
            if level is not None and 0 <= level <= 100:
                soc = level
    return EVReturnTelemetry(
        latitude=latitude, longitude=longitude, range_km=range_km, soc=soc,
        navigating_home=navigating_home and nav_eta is not None,
        nav_eta=nav_eta if navigating_home else None,
        nav_distance_km=nav_distance if navigating_home else None,
    )


def read_return_telemetry(hass: Any, vin: str | None, soc: float | None) -> EVReturnTelemetry:
    """Read a Tesla vehicle's location, range and route entities by VIN."""
    home_lat = getattr(hass.config, "latitude", None)
    home_lon = getattr(hass.config, "longitude", None)
    if not vin:
        return EVReturnTelemetry(soc=soc)
    try:
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        from ..automations.ev_charging_planner import _iter_tesla_vehicle_devices

        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        device_ids = {device.id for device, device_vin in _iter_tesla_vehicle_devices(device_registry)
                      if device_vin.upper() == vin.upper()}
        states = {entity.entity_id: hass.states.get(entity.entity_id)
                  for entity in entity_registry.entities.values()
                  if entity.device_id in device_ids}
    except Exception as err:  # Registry shape differs in tests/older cores.
        _LOGGER.debug("EV return telemetry unavailable for %s: %s", vin, err)
        return EVReturnTelemetry(soc=soc)
    return telemetry_from_states(states, home_lat=home_lat, home_lon=home_lon, soc=soc)


def load_overrides(store: Any) -> dict[str, EVReturnOverride]:
    data = getattr(store, "_data", None)
    raw = data.get(STORE_KEY) if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    result = {}
    for vehicle_id, value in raw.items():
        override = EVReturnOverride.from_dict(value)
        if override is not None:
            result[str(vehicle_id)] = override
    return result


async def async_save_overrides(store: Any, overrides: Mapping[str, EVReturnOverride]) -> None:
    data = getattr(store, "_data", None)
    if not isinstance(data, dict):
        raise RuntimeError("EV charging storage is not available")
    previous = data.get(STORE_KEY)
    data[STORE_KEY] = {key: value.to_dict() for key, value in overrides.items()}
    try:
        await store.async_save()
    except Exception:
        if previous is None:
            data.pop(STORE_KEY, None)
        else:
            data[STORE_KEY] = previous
        raise


def build_override(mode: str, until: datetime | None, now: datetime) -> EVReturnOverride | None:
    """Validate a service request. ``auto`` clears the answer (returns None)."""
    if mode == MODE_AUTO:
        return None
    if mode not in (MODE_NOT_HOME, MODE_HOME_AT):
        raise ValueError(f"Unknown return mode: {mode}")
    if until is None:
        raise ValueError("A time is required")
    if until.tzinfo is None:
        raise ValueError("The time must include a time zone")
    if until <= now and mode == MODE_NOT_HOME:
        raise ValueError("The 'not home until' time must be in the future")
    return EVReturnOverride(mode, until, now)
