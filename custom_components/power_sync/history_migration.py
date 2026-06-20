"""Entity registry helpers for PowerSync history relinks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .const import CONF_HISTORY_RELINKS, DOMAIN

STATUS_READY = "ready"
STATUS_MISSING_OLD = "missing_old"
STATUS_MISSING_NEW = "missing_new"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_BLOCKED_COLLISION = "blocked_collision"
STATUS_ALREADY_LINKED = "already_linked"


@dataclass(frozen=True)
class HistoryRelinkSpec:
    """One supported mkaiser-to-PowerSync energy sensor mapping."""

    old_unique_id: str
    old_entity_id: str
    new_sensor_key: str
    label: str
    optional: bool = False


HISTORY_RELINK_SPECS: tuple[HistoryRelinkSpec, ...] = (
    HistoryRelinkSpec(
        "sg_daily_pv_generation",
        "sensor.daily_pv_generation",
        "daily_solar_energy",
        "Daily solar energy",
    ),
    HistoryRelinkSpec(
        "sg_daily_imported_energy",
        "sensor.daily_imported_energy",
        "daily_grid_import",
        "Daily grid import",
    ),
    HistoryRelinkSpec(
        "sg_daily_exported_energy",
        "sensor.daily_exported_energy",
        "daily_grid_export",
        "Daily grid export",
    ),
    HistoryRelinkSpec(
        "sg_daily_battery_charge",
        "sensor.daily_battery_charge",
        "daily_battery_charge",
        "Daily battery charge",
    ),
    HistoryRelinkSpec(
        "sg_daily_battery_discharge",
        "sensor.daily_battery_discharge",
        "daily_battery_discharge",
        "Daily battery discharge",
    ),
    HistoryRelinkSpec(
        "sg_daily_consumed_energy",
        "sensor.daily_consumed_energy",
        "daily_load",
        "Daily home consumption",
        optional=True,
    ),
)


def _registry_entities(registry: Any) -> list[Any]:
    return list(getattr(registry, "entities", {}).values())


def _entity_domain(entity: Any) -> str:
    domain = getattr(entity, "domain", None)
    if domain:
        return str(domain)
    return str(getattr(entity, "entity_id", "")).split(".", 1)[0]


def _entity_object_id(entity: Any) -> str:
    return str(getattr(entity, "entity_id", "")).split(".", 1)[-1]


def _legacy_entity_id(entity_id: str) -> str:
    domain, _, object_id = entity_id.partition(".")
    if not domain or not object_id:
        return f"{entity_id}_legacy"
    return f"{domain}.{object_id}_legacy"


def _registry_get(registry: Any, entity_id: str) -> Any | None:
    getter = getattr(registry, "async_get", None)
    if getter is not None:
        return getter(entity_id)
    return getattr(registry, "entities", {}).get(entity_id)


def _unique_id_match(entity: Any, unique_id: str) -> bool:
    return str(getattr(entity, "unique_id", "") or "").lower() == unique_id.lower()


def _old_candidates(registry: Any, spec: HistoryRelinkSpec) -> list[Any]:
    candidates: dict[str, Any] = {}
    expected_object_id = spec.old_entity_id.split(".", 1)[-1]
    for entity in _registry_entities(registry):
        entity_id = str(getattr(entity, "entity_id", "") or "")
        if _entity_domain(entity) != "sensor":
            continue
        if str(getattr(entity, "platform", "") or "") == DOMAIN:
            continue
        if _unique_id_match(entity, spec.old_unique_id) or _entity_object_id(entity) == expected_object_id:
            candidates[entity_id] = entity
    return list(candidates.values())


def _find_power_sync_entity(registry: Any, entry_id: str, sensor_key: str) -> Any | None:
    unique_id = f"{entry_id}_{sensor_key}"
    get_entity_id = getattr(registry, "async_get_entity_id", None)
    if get_entity_id is not None:
        entity_id = get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            return _registry_get(registry, entity_id)

    for entity in _registry_entities(registry):
        if (
            _entity_domain(entity) == "sensor"
            and str(getattr(entity, "platform", "") or "") == DOMAIN
            and str(getattr(entity, "unique_id", "") or "") == unique_id
        ):
            return entity
    return None


def _mapping_result(
    registry: Any,
    entry_id: str,
    spec: HistoryRelinkSpec,
    stored_relink: dict[str, Any] | None = None,
) -> dict[str, Any]:
    new_entity = _find_power_sync_entity(registry, entry_id, spec.new_sensor_key)
    old_candidates = _old_candidates(registry, spec)
    default_legacy_id = _legacy_entity_id(spec.old_entity_id)
    stored_entity_id = str((stored_relink or {}).get("entity_id") or "")
    stored_legacy_id = str((stored_relink or {}).get("legacy_entity_id") or "")
    active_old = [
        entity
        for entity in old_candidates
        if str(getattr(entity, "entity_id", "") or "") not in {
            default_legacy_id,
            stored_legacy_id,
        }
    ]

    target_entity_id = stored_entity_id or (
        str(getattr(active_old[0], "entity_id", "") or "")
        if len(active_old) == 1
        else spec.old_entity_id
    )
    legacy_id = stored_legacy_id or _legacy_entity_id(target_entity_id)

    if new_entity is not None and str(getattr(new_entity, "entity_id", "") or "") == target_entity_id:
        status = STATUS_ALREADY_LINKED
    elif len(active_old) > 1:
        status = STATUS_AMBIGUOUS
    elif not active_old:
        status = STATUS_MISSING_OLD
    elif new_entity is None:
        status = STATUS_MISSING_NEW
    elif _registry_get(registry, target_entity_id) is not active_old[0]:
        status = STATUS_BLOCKED_COLLISION
    elif _registry_get(registry, legacy_id) is not None:
        status = STATUS_BLOCKED_COLLISION
    else:
        status = STATUS_READY

    return {
        "label": spec.label,
        "status": status,
        "optional": spec.optional,
        "old_unique_id": spec.old_unique_id,
        "old_entity_id": target_entity_id,
        "expected_old_entity_id": spec.old_entity_id,
        "legacy_entity_id": legacy_id,
        "old_candidates": [
            str(getattr(entity, "entity_id", "") or "") for entity in old_candidates
        ],
        "new_sensor_key": spec.new_sensor_key,
        "new_unique_id": f"{entry_id}_{spec.new_sensor_key}",
        "new_entity_id": (
            str(getattr(new_entity, "entity_id", "") or "")
            if new_entity is not None
            else None
        ),
    }


def preview_history_relink_for_registry(
    registry: Any,
    entry_id: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a dry-run history relink report for a config entry."""
    relinks = (options or {}).get(CONF_HISTORY_RELINKS) or {}
    mappings = [
        _mapping_result(registry, entry_id, spec, relinks.get(spec.new_sensor_key))
        for spec in HISTORY_RELINK_SPECS
    ]
    status_counts: dict[str, int] = {}
    for mapping in mappings:
        status = str(mapping["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

    ready_count = status_counts.get(STATUS_READY, 0)
    return {
        "success": True,
        "entry_id": entry_id,
        "ready_count": ready_count,
        "applied_count": 0,
        "can_apply": ready_count > 0,
        "status_counts": status_counts,
        "mappings": mappings,
    }


def preview_history_relink(hass: Any, entry: Any) -> dict[str, Any]:
    """Return a dry-run history relink report for Home Assistant."""
    from homeassistant.helpers import entity_registry as er

    return preview_history_relink_for_registry(er.async_get(hass), entry.entry_id, entry.options)


def apply_history_relink_for_registry(
    registry: Any,
    entry_id: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply all ready relinks and return an updated report."""
    preview = preview_history_relink_for_registry(registry, entry_id, options)
    relinks = dict((options or {}).get(CONF_HISTORY_RELINKS) or {})
    applied: list[dict[str, Any]] = []

    for mapping in preview["mappings"]:
        if mapping["status"] != STATUS_READY:
            continue

        old_entity_id = str(mapping["old_entity_id"])
        new_entity_id = str(mapping["new_entity_id"])
        legacy_entity_id = str(mapping["legacy_entity_id"])

        registry.async_update_entity(old_entity_id, new_entity_id=legacy_entity_id)
        registry.async_update_entity(new_entity_id, new_entity_id=old_entity_id)

        relinks[mapping["new_sensor_key"]] = {
            "old_unique_id": mapping["old_unique_id"],
            "entity_id": old_entity_id,
            "legacy_entity_id": legacy_entity_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        applied.append(mapping)

    result = preview_history_relink_for_registry(
        registry,
        entry_id,
        {CONF_HISTORY_RELINKS: relinks},
    )
    result["applied_count"] = len(applied)
    result["applied"] = applied
    result["history_relinks"] = relinks
    return result


def apply_history_relink(hass: Any, entry: Any) -> dict[str, Any]:
    """Apply all ready history relinks for Home Assistant."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    result = apply_history_relink_for_registry(registry, entry.entry_id, entry.options)
    if result["applied_count"]:
        new_options = dict(entry.options)
        new_options[CONF_HISTORY_RELINKS] = result["history_relinks"]
        hass.config_entries.async_update_entry(entry, options=new_options)
    return result


def history_relink_applied_for_key(options: dict[str, Any] | None, sensor_key: str) -> bool:
    """Return True when canonical entity migration should skip a relinked key."""
    relinks = (options or {}).get(CONF_HISTORY_RELINKS) or {}
    return sensor_key in relinks


def format_history_relink_summary(preview: dict[str, Any]) -> str:
    """Format a compact text summary for config-flow descriptions."""
    counts = preview.get("status_counts") or {}
    lines = [
        (
            "Ready: {ready}; already linked: {linked}; missing: {missing}; "
            "blocked: {blocked}; ambiguous: {ambiguous}."
        ).format(
            ready=counts.get(STATUS_READY, 0),
            linked=counts.get(STATUS_ALREADY_LINKED, 0),
            missing=counts.get(STATUS_MISSING_OLD, 0) + counts.get(STATUS_MISSING_NEW, 0),
            blocked=counts.get(STATUS_BLOCKED_COLLISION, 0),
            ambiguous=counts.get(STATUS_AMBIGUOUS, 0),
        )
    ]
    for mapping in preview.get("mappings", []):
        lines.append(
            "{label}: {status} ({old} -> {new})".format(
                label=mapping.get("label"),
                status=mapping.get("status"),
                old=mapping.get("old_entity_id") or mapping.get("expected_old_entity_id"),
                new=mapping.get("new_entity_id") or mapping.get("new_unique_id"),
            )
        )
    return "\n".join(lines)
