"""Resolve Teslemetry Bluetooth command entities by registry identity."""

from collections.abc import Mapping
from dataclasses import dataclass
import re


DOMAIN = "teslemetry_bluetooth"
TESLEMETRY_DOMAIN = "teslemetry"
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_COMMANDS = {
    "start": ("button", "charge_start"),
    "stop": ("button", "charge_stop"),
    "rate": ("number", "set_charging_amps"),
    "limit": ("number", "set_charge_limit"),
}
_NATIVE_OPERATION_KEYS = {
    "start": "charge_state_user_charge_enable_request",
    "stop": "charge_state_user_charge_enable_request",
    "rate": "charge_state_charge_current_request",
    "limit": "charge_state_charge_limit_soc",
}


@dataclass(frozen=True, slots=True)
class BluetoothCommandTarget:
    """An immutable, identity-checked HA entity target."""

    vin: str
    operation: str
    entity_id: str
    entity_domain: str
    platform: str
    unique_id: str
    config_entry_id: str
    device_id: str
    native_config_entry_ids: tuple[str, ...]


def _canonical_vin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    vin = value.strip().upper()
    return vin if _VIN_RE.fullmatch(vin) else None


def _config_entries(hass, domain: str) -> tuple:
    manager = getattr(hass, "config_entries", None)
    entries = getattr(manager, "async_entries", None)
    if not callable(entries):
        return ()
    try:
        result = entries(domain)
    except Exception:
        return ()
    return tuple(result) if result else ()


def _registry(hass):
    try:
        from homeassistant.helpers import entity_registry as er
        return er.async_get(hass)
    except Exception:
        return None


def _registry_entities(registry) -> tuple:
    entities = getattr(registry, "entities", None)
    return tuple(entities.values()) if isinstance(entities, Mapping) else ()


def _entity_domain(entity) -> str | None:
    entity_id = getattr(entity, "entity_id", None)
    if not isinstance(entity_id, str) or entity_id.count(".") != 1:
        return None
    domain, object_id = entity_id.split(".", 1)
    return domain if domain and object_id else None


def _state_available(hass, entity_id: str) -> bool:
    states = getattr(hass, "states", None)
    get = getattr(states, "get", None)
    if not callable(get):
        return False
    try:
        state = get(entity_id)
    except Exception:
        return False
    return state is not None and str(getattr(state, "state", "")).strip().lower() != "unavailable"


def _device_for(hass, device_id: str):
    try:
        from homeassistant.helpers import device_registry as dr
        return dr.async_get(hass).async_get(device_id)
    except Exception:
        return None


def _native_ids(hass, device) -> tuple[str, ...]:
    known = {
        str(entry.entry_id)
        for entry in _config_entries(hass, TESLEMETRY_DOMAIN)
        if isinstance(getattr(entry, "entry_id", None), str)
    }
    attached = {
        str(entry_id) for entry_id in (getattr(device, "config_entries", ()) or ())
        if isinstance(entry_id, str)
    }
    return tuple(sorted(known & attached))


def _valid_device(hass, entity, vin: str) -> tuple[str, ...] | None:
    device_id = getattr(entity, "device_id", None)
    if not isinstance(device_id, str) or not device_id:
        return None
    device = _device_for(hass, device_id)
    if device is None or (TESLEMETRY_DOMAIN, vin) not in (
        getattr(device, "identifiers", ()) or ()
    ):
        return None
    ids = _native_ids(hass, device)
    return ids or None


def _resolve_entity(
    hass, vin: str, *, domain: str, platform: str, unique_id: str,
    config_domain: str, operation: str,
) -> BluetoothCommandTarget | None:
    registry = _registry(hass)
    if registry is None:
        return None
    matches = tuple(
        entity for entity in _registry_entities(registry)
        if getattr(entity, "unique_id", None) == unique_id
    )
    if len(matches) != 1:
        return None
    entity = matches[0]
    entry_id = getattr(entity, "config_entry_id", None)
    if (
        _entity_domain(entity) != domain
        or getattr(entity, "platform", None) != platform
        or getattr(entity, "disabled_by", None) is not None
        or not isinstance(entry_id, str)
        or not any(str(entry.entry_id) == entry_id
                   for entry in _config_entries(hass, config_domain))
        or not _state_available(hass, entity.entity_id)
    ):
        return None
    native_ids = _valid_device(hass, entity, vin)
    if native_ids is None:
        return None
    return BluetoothCommandTarget(
        vin=vin, operation=operation, entity_id=entity.entity_id,
        entity_domain=domain, platform=platform, unique_id=unique_id,
        config_entry_id=entry_id, device_id=entity.device_id,
        native_config_entry_ids=native_ids,
    )


def resolve_command(hass, vin: object, operation: object) -> BluetoothCommandTarget | None:
    """Resolve one exact command entity; renamed IDs remain safe."""
    canonical = _canonical_vin(vin)
    if canonical is None or not isinstance(operation, str):
        return None
    spec = _COMMANDS.get(operation)
    if spec is None:
        return None
    domain, key = spec
    return _resolve_entity(
        hass, canonical, domain=domain, platform=DOMAIN,
        unique_id=f"{canonical}-bluetooth-{key}", config_domain=DOMAIN,
        operation=operation,
    )


def _native_matches(
    hass, vin: str, *, operation: str = "rate", device_id: str | None = None
) -> list[tuple[object, tuple[str, ...]]]:
    registry = _registry(hass)
    if registry is None:
        return []
    key = _NATIVE_OPERATION_KEYS.get(operation)
    if key is None:
        return []
    unique_id = f"{vin}-{key}"
    matches = tuple(
        entity for entity in _registry_entities(registry)
        if getattr(entity, "unique_id", None) == unique_id
    )
    if len(matches) != 1:
        return []
    entity = matches[0]
    native_domain = "switch" if operation in ("start", "stop") else "number"
    if (
        _entity_domain(entity) != native_domain
        or getattr(entity, "platform", None) != TESLEMETRY_DOMAIN
        or (device_id is not None and getattr(entity, "device_id", None) != device_id)
        or getattr(entity, "disabled_by", None) is not None
        or not _state_available(hass, entity.entity_id)
    ):
        return []
    ids = _valid_device(hass, entity, vin)
    if not ids or getattr(entity, "config_entry_id", None) not in ids:
        return []
    return [(entity, ids)]


def resolve_native_current(hass, vin: object) -> BluetoothCommandTarget | None:
    """Resolve Teslemetry's native current-setting entity for ``vin``."""
    canonical = _canonical_vin(vin)
    matches = _native_matches(hass, canonical, operation="rate") if canonical else []
    if len(matches) != 1:
        return None
    entity, ids = matches[0]
    return BluetoothCommandTarget(
        vin=canonical, operation="current", entity_id=entity.entity_id,
        entity_domain="number", platform=TESLEMETRY_DOMAIN,
        unique_id=entity.unique_id, config_entry_id=entity.config_entry_id,
        device_id=entity.device_id, native_config_entry_ids=ids,
    )


def native_current_entity(hass, target_or_command_entity_id) -> str | None:
    """Return one native current-setting ID for a resolved command target."""
    target = _command_target(hass, target_or_command_entity_id)
    if target is None:
        return None
    matches = [
        (entity, ids) for entity, ids in _native_matches(
            hass, target.vin, operation="rate", device_id=target.device_id
        ) if getattr(entity, "config_entry_id", None) in target.native_config_entry_ids
    ]
    return matches[0][0].entity_id if len(matches) == 1 else None


def _command_target(hass, target_or_command_entity_id) -> BluetoothCommandTarget | None:
    if isinstance(target_or_command_entity_id, BluetoothCommandTarget):
        target = target_or_command_entity_id
        return target if (
            target.operation in _COMMANDS
            and resolve_command(hass, target.vin, target.operation) == target
        ) else None
    if not isinstance(target_or_command_entity_id, str):
        return None
    registry = _registry(hass)
    if registry is None:
        return None
    entity = next(
        (candidate for candidate in _registry_entities(registry)
         if getattr(candidate, "entity_id", None) == target_or_command_entity_id),
        None,
    )
    unique_id = getattr(entity, "unique_id", "") if entity else ""
    vin, marker, key = unique_id.partition("-bluetooth-")
    operation = next(
        (name for name, (_, expected) in _COMMANDS.items() if expected == key),
        None,
    ) if marker else None
    target = resolve_command(hass, vin, operation) if operation else None
    return target if target is not None and target.entity_id == target_or_command_entity_id else None


def _entity_for_id(hass, entity_id: str):
    registry = _registry(hass)
    if registry is None:
        return None
    return next(
        (entity for entity in _registry_entities(registry)
         if getattr(entity, "entity_id", None) == entity_id),
        None,
    )


def _entity_vin(hass, entity, platform: str) -> str | None:
    device_id = getattr(entity, "device_id", None)
    if not isinstance(device_id, str) or not device_id:
        return None
    device = _device_for(hass, device_id)
    if device is None:
        return None
    identifiers = []
    for identifier in getattr(device, "identifiers", ()) or ():
        if not isinstance(identifier, (tuple, list)) or len(identifier) < 2:
            continue
        identifiers.append((str(identifier[0]), str(identifier[1])))
    vins = {
        _canonical_vin(value)
        for domain, value in identifiers
        if domain == platform
    }
    vins.discard(None)
    return next(iter(vins)) if len(vins) == 1 else None


def _native_entity_for_target(hass, target: BluetoothCommandTarget) -> str | None:
    matches = [
        (entity, ids) for entity, ids in _native_matches(
            hass, target.vin, operation=target.operation, device_id=target.device_id
        ) if getattr(entity, "config_entry_id", None) in target.native_config_entry_ids
    ]
    return matches[0][0].entity_id if len(matches) == 1 else None


def _native_entity_for_anchor(hass, anchor, operation: str) -> str | None:
    if operation not in _COMMANDS:
        return None
    platform = str(getattr(anchor, "platform", "") or "")
    if platform == TESLEMETRY_DOMAIN:
        vin = _entity_vin(hass, anchor, TESLEMETRY_DOMAIN)
        if vin is None:
            return None
        matches = [
            entity for entity, ids in _native_matches(
                hass, vin, operation=operation,
                device_id=str(getattr(anchor, "device_id", "")),
            )
            if getattr(entity, "config_entry_id", None)
            == getattr(anchor, "config_entry_id", None)
        ]
        return matches[0].entity_id if len(matches) == 1 else None

    if platform != "tesla_fleet":
        return None
    vin = _entity_vin(hass, anchor, platform)
    entry_id = getattr(anchor, "config_entry_id", None)
    device_id = getattr(anchor, "device_id", None)
    if vin is None or not isinstance(entry_id, str) or not isinstance(device_id, str):
        return None
    expected_domain = "switch" if operation in ("start", "stop") else "number"
    marker = {
        "start": ("charge",),
        "stop": ("charge",),
        "rate": ("charging_amps", "charge_current"),
        "limit": ("charge_limit",),
    }[operation]
    registry = _registry(hass)
    if registry is None:
        return None
    candidates = []
    for entity in _registry_entities(registry):
        if (
            _entity_domain(entity) != expected_domain
            or getattr(entity, "platform", None) != platform
            or getattr(entity, "config_entry_id", None) != entry_id
            or getattr(entity, "device_id", None) != device_id
            or getattr(entity, "disabled_by", None) is not None
            or not _state_available(hass, entity.entity_id)
        ):
            continue
        lowered = str(getattr(entity, "entity_id", "")).lower()
        if operation in ("start", "stop"):
            if "charge" not in lowered or "discharge" in lowered:
                continue
        elif not any(part in lowered for part in marker):
            continue
        candidates.append(entity)
    return candidates[0].entity_id if len(candidates) == 1 else None


def native_command_entity(
    hass, target_or_command_entity_id, operation: str | None = None
) -> str | None:
    """Resolve an exact native entity for a command target or HA anchor.

    A modern command target resolves to the matching native Teslemetry entity.
    A provider entity anchor resolves within its same provider, config entry,
    and device, so route recovery cannot switch a same-VIN vehicle to another
    integration accidentally.
    """
    target = _command_target(hass, target_or_command_entity_id)
    if target is not None:
        if operation is not None and operation != target.operation:
            return None
        return _native_entity_for_target(hass, target)
    if not isinstance(target_or_command_entity_id, str):
        return None
    anchor = _entity_for_id(hass, target_or_command_entity_id)
    if anchor is None:
        return None
    selected = operation
    if selected is None and getattr(anchor, "platform", None) == TESLEMETRY_DOMAIN:
        unique_id = str(getattr(anchor, "unique_id", ""))
        for candidate, key in _NATIVE_OPERATION_KEYS.items():
            if unique_id.endswith(f"-{key}"):
                selected = candidate
                break
    return _native_entity_for_anchor(hass, anchor, selected) if selected else None
