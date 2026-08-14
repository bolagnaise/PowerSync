"""Identity-safe Tesla Fleet and ESPHome BLE bridge pairing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .const import (
    CONF_EV_PROVIDER,
    CONF_TESLA_BLE_ENTITY_PREFIX,
    CONF_TESLA_BLE_VEHICLE_MAPPING,
    DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    EV_PROVIDER_BOTH,
)

_BLE_PREFIX_PATTERN = re.compile(r"^[a-z0-9_]+$")


class TeslaBleMappingError(ValueError):
    """Raised when a Tesla VIN-to-BLE mapping is malformed or ambiguous."""


def configured_ble_prefixes(config: Mapping[str, Any]) -> list[str]:
    """Return configured ESPHome BLE prefixes in user-defined display order."""
    raw = str(
        config.get(
            CONF_TESLA_BLE_ENTITY_PREFIX,
            DEFAULT_TESLA_BLE_ENTITY_PREFIX,
        )
        or ""
    )
    prefixes = [prefix.strip() for prefix in raw.split(",") if prefix.strip()]
    return prefixes or [DEFAULT_TESLA_BLE_ENTITY_PREFIX]


def resolve_ble_prefixes(hass: Any, config: Mapping[str, Any]) -> list[str]:
    """Resolve configured BLE prefixes with an unambiguous discovery fallback.

    Explicit VIN mappings always keep their configured prefixes. Automatic
    discovery is only safe for a single, unmapped prefix with exactly one
    matching BLE charging-state entity.
    """
    from .tesla_ble import get_tesla_ble_status_state

    prefixes = configured_ble_prefixes(config)
    states = getattr(hass, "states", None)
    if states is None:
        return prefixes
    try:
        has_explicit_mapping = bool(
            parse_tesla_ble_vehicle_mapping(
                config.get(CONF_TESLA_BLE_VEHICLE_MAPPING)
            )
        )
    except TeslaBleMappingError:
        has_explicit_mapping = True

    resolved: list[str] = []
    for prefix in prefixes:
        if get_tesla_ble_status_state(hass, prefix) is not None:
            resolved.append(prefix)
            continue

        if len(prefixes) != 1 or has_explicit_mapping:
            resolved.append(prefix)
            continue

        detected_prefixes: list[str] = []
        for state in states.async_all():
            match = re.match(r"sensor\.(\w+)_charging_state$", state.entity_id)
            if not match or "ble" not in match.group(1).lower():
                continue
            detected = match.group(1)
            if detected not in detected_prefixes:
                detected_prefixes.append(detected)

        resolved.append(
            detected_prefixes[0] if len(detected_prefixes) == 1 else prefix
        )
    return resolved


def parse_tesla_ble_vehicle_mapping(raw_mapping: Any) -> dict[str, str]:
    """Parse comma/newline-separated ``VIN=prefix`` associations."""
    if raw_mapping is None or not str(raw_mapping).strip():
        return {}

    mapping: dict[str, str] = {}
    assigned_prefixes: set[str] = set()
    for item in re.split(r"[\n,]+", str(raw_mapping)):
        item = item.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise TeslaBleMappingError("Each mapping must use VIN=prefix")
        vin, prefix = (part.strip() for part in item.split("=", 1))
        normalized_vin = vin.upper()
        if (
            len(normalized_vin) != 17
            or not normalized_vin.isalnum()
            or normalized_vin.isdigit()
        ):
            raise TeslaBleMappingError("Mappings require a valid 17-character VIN")
        if not _BLE_PREFIX_PATTERN.fullmatch(prefix):
            raise TeslaBleMappingError(
                "BLE prefixes may contain only lowercase letters, numbers, and underscores"
            )
        if normalized_vin in mapping:
            raise TeslaBleMappingError("Each VIN may be mapped only once")
        if prefix in assigned_prefixes:
            raise TeslaBleMappingError("Each BLE prefix may be mapped only once")
        mapping[normalized_vin] = prefix
        assigned_prefixes.add(prefix)
    return mapping


def vehicle_ble_prefix(
    config: Mapping[str, Any],
    vehicle_vin: str | None,
    fleet_vins: Sequence[str] = (),
    resolved_prefixes: Sequence[str] | None = None,
) -> str | None:
    """Resolve one vehicle's BLE prefix without relying on discovery order."""
    if vehicle_vin and vehicle_vin.startswith("ble_"):
        return vehicle_vin[4:]

    configured_prefixes = configured_ble_prefixes(config)
    prefixes = (
        list(dict.fromkeys(str(prefix) for prefix in resolved_prefixes if prefix))
        if resolved_prefixes is not None
        else configured_prefixes
    )
    if not (
        vehicle_vin
        and len(vehicle_vin) == 17
        and vehicle_vin.isalnum()
        and config.get(CONF_EV_PROVIDER) == EV_PROVIDER_BOTH
    ):
        return prefixes[0] if prefixes else None

    try:
        explicit_mapping = parse_tesla_ble_vehicle_mapping(
            config.get(CONF_TESLA_BLE_VEHICLE_MAPPING)
        )
    except TeslaBleMappingError:
        return None

    mapped_prefix = explicit_mapping.get(vehicle_vin.upper())
    if mapped_prefix is not None:
        return mapped_prefix if mapped_prefix in configured_prefixes else None

    unique_fleet_vins = list(dict.fromkeys(vin.upper() for vin in fleet_vins if vin))
    if len(unique_fleet_vins) == 1 and len(prefixes) == 1:
        return prefixes[0] if vehicle_vin.upper() == unique_fleet_vins[0] else None
    return None


def ble_prefix_vehicle_pairs(
    config: Mapping[str, Any],
    fleet_vins: Sequence[str],
    resolved_prefixes: Sequence[str] | None = None,
) -> dict[str, str]:
    """Return BLE-prefix-to-VIN pairs that are explicit or unambiguous."""
    pairs: dict[str, str] = {}
    for vin in dict.fromkeys(str(candidate) for candidate in fleet_vins if candidate):
        prefix = vehicle_ble_prefix(
            config,
            vin,
            fleet_vins,
            resolved_prefixes,
        )
        if prefix:
            pairs[prefix] = vin
    return pairs


def canonical_tesla_vehicle_id(
    config: Mapping[str, Any],
    vehicle_id: str | None,
    fleet_vins: Sequence[str] = (),
    resolved_prefixes: Sequence[str] | None = None,
) -> str:
    """Return the physical VIN for a paired BLE alias when it is known.

    Standalone BLE vehicles deliberately retain their ``ble_*`` identifier.
    Pairing is meaningful only in ``Both`` mode; BLE-only installations must
    never borrow stale Fleet device-registry identities.
    Explicit mappings can resolve an alias even before Fleet discovery has
    populated a vehicle list; single-vehicle inference still requires the
    caller to supply the unambiguous Fleet VIN.
    """
    normalized_id = str(vehicle_id or "")
    if not normalized_id.startswith("ble_"):
        return normalized_id
    if config.get(CONF_EV_PROVIDER) != EV_PROVIDER_BOTH:
        return normalized_id

    prefix = normalized_id[4:]
    configured_prefixes = configured_ble_prefixes(config)
    try:
        explicit_mapping = parse_tesla_ble_vehicle_mapping(
            config.get(CONF_TESLA_BLE_VEHICLE_MAPPING)
        )
    except TeslaBleMappingError:
        explicit_mapping = {}

    explicit_vins = [
        vin
        for vin, mapped_prefix in explicit_mapping.items()
        if mapped_prefix == prefix and prefix in configured_prefixes
    ]
    if len(explicit_vins) == 1:
        return explicit_vins[0]

    paired_vin = ble_prefix_vehicle_pairs(
        config,
        fleet_vins,
        resolved_prefixes,
    ).get(prefix)
    return str(paired_vin or normalized_id)


def coalesce_paired_vehicle_configs(
    config: Mapping[str, Any],
    vehicle_configs: Sequence[Mapping[str, Any]],
    resolved_prefixes: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Remove stored BLE profiles that duplicate a canonical Fleet vehicle.

    A VIN-backed profile is authoritative when both identities exist. This is
    intentionally conservative: stale BLE profiles commonly contain default
    limits and priorities, so merging those values into a configured VIN could
    silently change charging behaviour. A paired alias without an existing VIN
    profile is migrated to the VIN so it remains configurable.
    """
    copied_configs = [
        dict(vehicle_config)
        for vehicle_config in vehicle_configs
        if isinstance(vehicle_config, Mapping)
    ]
    fleet_vins = [
        vehicle_id
        for vehicle_config in copied_configs
        if (
            (vehicle_id := str(vehicle_config.get("vehicle_id") or ""))
            and len(vehicle_id) == 17
            and vehicle_id.isalnum()
            and not vehicle_id.isdigit()
        )
    ]

    canonical_configs: list[dict[str, Any]] = []
    canonical_indexes: dict[str, int] = {}

    # Establish every non-BLE profile first so its settings win even when a
    # legacy alias happened to appear earlier in persisted storage.
    for vehicle_config in copied_configs:
        vehicle_id = str(vehicle_config.get("vehicle_id") or "")
        if vehicle_id.startswith("ble_"):
            continue
        canonical_indexes[vehicle_id.casefold()] = len(canonical_configs)
        canonical_configs.append(vehicle_config)

    for vehicle_config in copied_configs:
        vehicle_id = str(vehicle_config.get("vehicle_id") or "")
        if not vehicle_id.startswith("ble_"):
            continue
        canonical_id = canonical_tesla_vehicle_id(
            config,
            vehicle_id,
            fleet_vins,
            resolved_prefixes,
        )
        canonical_key = canonical_id.casefold()
        if canonical_key in canonical_indexes:
            continue
        migrated_config = {
            **vehicle_config,
            "vehicle_id": canonical_id,
        }
        canonical_indexes[canonical_key] = len(canonical_configs)
        canonical_configs.append(migrated_config)

    return canonical_configs
