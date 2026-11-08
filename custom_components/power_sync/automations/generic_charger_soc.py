"""Helpers for generic charger EV state of charge sensors."""

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
        soc = _valid_soc(getattr(state, "state", None))
        if soc is not None:
            return soc
    return None
