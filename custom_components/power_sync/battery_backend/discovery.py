"""Bounded upstream Home Assistant battery-entity discovery."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import (
    BATTERY_SENSOR_DISPLAY_ALL,
    BATTERY_SENSOR_DISPLAY_OFF,
    BATTERY_SYSTEM_SUNGROW,
)

_UNAVAILABLE = {"", "unknown", "unavailable", "none"}

_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "battery_level": (
        "battery_soc", "battery_state_of_charge", "battery_level", "state_of_charge", "soc",
    ),
    "battery_power": (
        "battery_power", "battery_charge_discharge_power", "battery_active_power", "bat_power",
    ),
    "grid_power": (
        "grid_power", "meter_active_power", "grid_active_power", "export_power_raw", "export_power",
    ),
    "solar_power": (
        "pv_power", "total_pv_power", "solar_power", "total_dc_power", "photovoltaic_power",
    ),
    "load_power": (
        "load_power", "house_consumption", "consumption_power", "home_load", "loads_power",
    ),
}

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("battery", ("battery", "bms", "soc", "soh", "cell")),
    ("solar", ("solar", "pv", "mppt", "string")),
    ("grid", ("grid", "meter", "phase", "import", "export")),
    ("load", ("load", "house", "home_consumption", "consumption_power")),
    ("energy", ("energy", "yield", "generation", "consumption", "today", "total")),
    ("inverter", ("inverter", "backup", "eps", "frequency", "firmware")),
    ("diagnostics", ("fault", "alarm", "warning", "status", "temperature", "voltage", "current")),
    ("charger", ("charger", "evac", "evdc", "vehicle")),
)

_RECOMMENDED_KEYWORDS = (
    "soc", "soh", "battery_power", "battery_temperature", "pv_power",
    "solar_power", "grid_power", "load_power", "today", "status", "fault",
    "work_mode", "backup", "firmware",
)


def _entry_config_ids(registry_entry: Any) -> set[str]:
    ids = set(getattr(registry_entry, "config_entry_ids", ()) or ())
    single = getattr(registry_entry, "config_entry_id", None)
    if single:
        ids.add(single)
    return ids


def _normalized_identity(registry_entry: Any) -> str:
    entity_id = str(getattr(registry_entry, "entity_id", "") or "")
    object_id = entity_id.split(".", 1)[-1].lower()
    unique_id = str(getattr(registry_entry, "unique_id", "") or "").lower()
    return f"{object_id} {unique_id}"


def _category(identity: str) -> str:
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in identity for keyword in keywords):
            return category
    return "other"


def _is_recommended(identity: str, category: str) -> bool:
    return category != "other" and any(keyword in identity for keyword in _RECOMMENDED_KEYWORDS)


def _anchor_namespace(anchor: Any) -> tuple[str, str]:
    unique_id = str(getattr(anchor, "unique_id", "") or "").lower()
    if unique_id.startswith("sg_"):
        return "unique_id", "sg_"
    entity_id = str(getattr(anchor, "entity_id", "") or "")
    object_id = entity_id.split(".", 1)[-1]
    prefix = object_id.split("_", 1)[0] + "_" if "_" in object_id else object_id
    return "entity_id", prefix


def _source_entries(
    hass: HomeAssistant,
    *,
    config_entry_id: str | None,
    anchor_entity_id: str | None,
    allowed_domains: Iterable[str],
) -> list[Any]:
    registry = er.async_get(hass)
    allowed = set(allowed_domains)
    if config_entry_id:
        return [
            entry
            for entry in er.async_entries_for_config_entry(registry, config_entry_id)
            if not allowed or str(getattr(entry, "platform", "")) in allowed
        ]

    if not anchor_entity_id:
        return []
    anchor = registry.async_get(anchor_entity_id)
    if anchor is None or (allowed and str(getattr(anchor, "platform", "")) not in allowed):
        return []
    config_ids = _entry_config_ids(anchor)
    if config_ids:
        entries: list[Any] = []
        for source_id in config_ids:
            entries.extend(er.async_entries_for_config_entry(registry, source_id))
        return [
            entry for entry in entries
            if not allowed or str(getattr(entry, "platform", "")) in allowed
        ]

    namespace_kind, namespace = _anchor_namespace(anchor)
    candidates = []
    for entry in registry.entities.values():
        if str(getattr(entry, "platform", "")) != str(getattr(anchor, "platform", "")):
            continue
        if namespace_kind == "unique_id":
            if str(getattr(entry, "unique_id", "") or "").lower().startswith(namespace):
                candidates.append(entry)
        elif str(getattr(entry, "entity_id", "")).split(".", 1)[-1].startswith(namespace):
            candidates.append(entry)
    return candidates


def discover_battery_sensor_catalog(
    hass: HomeAssistant,
    *,
    battery_system: str,
    profile_id: str,
    allowed_domains: Iterable[str] = (),
    config_entry_id: str | None = None,
    anchor_entity_id: str | None = None,
    display_mode: str = "recommended",
) -> dict[str, Any]:
    """Return a versioned catalog scoped to one selected upstream source."""
    if display_mode == BATTERY_SENSOR_DISPLAY_OFF:
        return {
            "version": 1,
            "profile_id": profile_id,
            "battery_system": battery_system,
            "display_mode": display_mode,
            "entity_ids": [],
            "groups": {},
            "metrics": [],
            "disabled_count": 0,
        }

    entries = _source_entries(
        hass,
        config_entry_id=config_entry_id,
        anchor_entity_id=anchor_entity_id,
        allowed_domains=allowed_domains,
    )
    groups: dict[str, list[str]] = defaultdict(list)
    metrics: list[dict[str, Any]] = []
    disabled_count = 0
    for registry_entry in sorted(entries, key=lambda item: str(item.entity_id)):
        entity_id = str(registry_entry.entity_id)
        domain = entity_id.split(".", 1)[0]
        if domain not in {"sensor", "binary_sensor"}:
            continue
        identity = _normalized_identity(registry_entry)
        category = _category(identity)
        recommended = _is_recommended(identity, category)
        if display_mode != BATTERY_SENSOR_DISPLAY_ALL and not recommended:
            continue
        disabled = getattr(registry_entry, "disabled_by", None) is not None
        state = hass.states.get(entity_id)
        available = bool(
            state is not None
            and str(getattr(state, "state", "")).strip().lower() not in _UNAVAILABLE
        )
        if disabled:
            disabled_count += 1
        else:
            groups[category].append(entity_id)
        metrics.append(
            {
                "entity_id": entity_id,
                "unique_id": str(getattr(registry_entry, "unique_id", "") or ""),
                "device_id": str(getattr(registry_entry, "device_id", "") or ""),
                "category": category,
                "recommended": recommended,
                "enabled": not disabled,
                "available": available,
            }
        )

    entity_ids = [entity_id for category in groups.values() for entity_id in category]
    return {
        "version": 1,
        "profile_id": profile_id,
        "battery_system": battery_system,
        "display_mode": display_mode,
        "source_config_entry_id": config_entry_id or "",
        "anchor_entity_id": anchor_entity_id or "",
        "entity_ids": entity_ids,
        "groups": dict(groups),
        "metrics": metrics,
        "disabled_count": disabled_count,
    }


def discover_canonical_entities(
    catalog: dict[str, Any],
    *,
    battery_system: str,
) -> tuple[dict[str, str], list[str]]:
    """Resolve the five normalized monitoring roles from a scoped catalog."""
    metrics = [metric for metric in catalog.get("metrics", []) if metric.get("enabled")]
    resolved: dict[str, str] = {}
    for role, aliases in _CANONICAL_ALIASES.items():
        candidates: list[tuple[int, str]] = []
        for metric in metrics:
            entity_id = str(metric.get("entity_id") or "")
            unique_id = str(metric.get("unique_id") or "")
            identity = f"{entity_id.split('.', 1)[-1]} {unique_id}".lower()
            for priority, alias in enumerate(aliases):
                if identity.endswith(alias) or f"_{alias} " in identity or f" {alias}" in identity:
                    available_bonus = 100 if metric.get("available") else 0
                    exact_bonus = 50 if entity_id.split(".", 1)[-1].endswith(alias) else 0
                    candidates.append((available_bonus + exact_bonus - priority, entity_id))
                    break
        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            resolved[role] = candidates[0][1]

    missing = [role for role in _CANONICAL_ALIASES if role not in resolved]
    if battery_system == BATTERY_SYSTEM_SUNGROW and "grid_power" in resolved:
        catalog["grid_power_multiplier"] = (
            -1.0 if "export_power" in resolved["grid_power"] else 1.0
        )
    return resolved, missing


def infer_entity_prefix(canonical_entities: dict[str, str]) -> str:
    """Infer a bounded object-id prefix from an already validated role match."""
    for role in ("battery_level", "battery_power", "solar_power", "grid_power"):
        entity_id = canonical_entities.get(role, "")
        object_id = entity_id.split(".", 1)[-1]
        for alias in _CANONICAL_ALIASES.get(role, ()):
            suffix = f"_{alias}"
            if object_id.endswith(suffix):
                return object_id[: -len(suffix)]
            if object_id == alias:
                return ""
    return ""
