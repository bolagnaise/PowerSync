"""Shared EV state of charge sources for generic and OCPP chargers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..const import (
    CONF_GENERIC_CHARGER_SOC_ENTITY,
    CONF_GENERIC_CHARGER_SOC_ENTITY_2,
)


UNAVAILABLE_STATES = {"", "unavailable", "unknown", "none", "None"}


def generic_charger_soc_entities(opts: Mapping[str, Any]) -> list[str]:
    """Return configured generic charger SOC entities in priority order."""
    entities: list[str] = []
    for key in (CONF_GENERIC_CHARGER_SOC_ENTITY, CONF_GENERIC_CHARGER_SOC_ENTITY_2):
        entity_id = str(opts.get(key) or "").strip()
        if entity_id:
            entities.append(entity_id)
    return entities


def _valid_soc(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in UNAVAILABLE_STATES or text.lower() in UNAVAILABLE_STATES:
        return None
    try:
        soc = float(text)
    except (TypeError, ValueError):
        return None
    if 0 <= soc <= 100:
        return soc
    return None


def _soc_from_state(state: Any) -> float | None:
    """Accept percentage states, preserving legacy sensors without units."""
    unit = (getattr(state, "attributes", {}) or {}).get("unit_of_measurement")
    if unit is not None and str(unit).strip().lower() not in ("", "%", "percent"):
        return None
    return _valid_soc(getattr(state, "state", None))


def resolve_generic_charger_soc(hass: Any, opts: Mapping[str, Any]) -> float | None:
    """Return the first valid generic charger SOC from primary then fallback."""
    from .shared_generic_charger import context_for_options, profile_soc

    shared = context_for_options(hass, opts)
    if shared is not None:
        return profile_soc(hass, shared)
    for entity_id in generic_charger_soc_entities(opts):
        state = hass.states.get(entity_id)
        if state is None:
            continue
        soc = _soc_from_state(state)
        if soc is not None:
            return soc
    return None


def resolve_ocpp_charger_soc(
    hass: Any, entry: Any, vehicle_id: str | None = None,
) -> float | None:
    """Read native OCPP SoC, then the single charger's configured EV sensors.

    A global primary/fallback pair describes one car, not two chargers. Include
    offline chargers in discovery so losing telemetry cannot reassign that car.
    Generic shared-vehicle selection belongs to its own physical charger.
    """
    from homeassistant.helpers import entity_registry as er
    from .ocpp_status import extract_hacs_ocpp_prefix
    from .shared_generic_charger import context_for_options

    opts = {**entry.data, **entry.options}
    if not opts.get("ocpp_enabled"):
        return None
    registry = er.async_get(hass)
    entities = tuple(getattr(registry, "entities", {}).values())
    prefixes = {
        prefix
        for entity in entities
        if getattr(entity, "platform", None) == "ocpp"
        and (prefix := extract_hacs_ocpp_prefix(entity.entity_id))
    }
    runtime = getattr(hass, "data", {}).get("power_sync", {}).get(entry.entry_id, {})
    server = runtime.get("ocpp_server")
    prefixes.update(str(cp_id) for cp_id in getattr(server, "charge_points", {}))
    requested = str(vehicle_id or "ocpp_charger")
    if requested in ("ocpp_charger", "_default", "ev"):
        if len(prefixes) != 1:
            return None
        prefix = next(iter(prefixes))
    else:
        prefix = requested.removeprefix("ocpp_")
        if not requested.startswith("ocpp_") or prefix not in prefixes:
            return None

    # The standard HACS OCPP SoC measurand is scoped to this exact connector.
    native_entity = f"sensor.{prefix}_soc"
    if any(
        entity.entity_id == native_entity and getattr(entity, "platform", None) == "ocpp"
        for entity in entities
    ):
        state = hass.states.get(native_entity)
        native_soc = _soc_from_state(state)
        if native_soc is not None:
            return native_soc

    if len(prefixes) != 1 or opts.get("generic_charger_enabled"):
        return None
    if context_for_options(hass, opts) is not None:
        return None
    return resolve_generic_charger_soc(hass, opts)
