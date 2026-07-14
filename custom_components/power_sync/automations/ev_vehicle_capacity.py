"""Shared usable EV battery-capacity resolution.

Capacity is deliberately resolved from explicit metadata only.  Range,
charging power, elapsed time, and state-of-health are not capacity signals.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping


MIN_EV_BATTERY_CAPACITY_KWH = 1.0
MAX_EV_BATTERY_CAPACITY_KWH = 250.0
DEFAULT_EV_BATTERY_CAPACITY_KWH = 60.0

CAPACITY_SOURCE_MANUAL = "manual"
CAPACITY_SOURCE_CHARGER_FALLBACK = "charger_fallback"
CAPACITY_SOURCE_PROVIDER = "provider"
CAPACITY_SOURCE_MODEL_ESTIMATE = "model_estimate"
CAPACITY_SOURCE_DEFAULT_ESTIMATE = "default_estimate"

CAPACITY_SOURCES = frozenset(
    {
        CAPACITY_SOURCE_MANUAL,
        CAPACITY_SOURCE_CHARGER_FALLBACK,
        CAPACITY_SOURCE_PROVIDER,
        CAPACITY_SOURCE_MODEL_ESTIMATE,
        CAPACITY_SOURCE_DEFAULT_ESTIMATE,
    }
)


@dataclass(frozen=True)
class ResolvedEVBatteryCapacity:
    """Resolved usable capacity and the metadata needed by API consumers."""

    effective_battery_capacity_kwh: float
    battery_capacity_source: str
    battery_capacity_kwh: float | None = None

    def to_dict(self) -> dict[str, float | str | None]:
        """Return the public capacity data contract."""
        return {
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "effective_battery_capacity_kwh": self.effective_battery_capacity_kwh,
            "battery_capacity_source": self.battery_capacity_source,
        }


def validate_ev_battery_capacity(
    value: Any,
    *,
    allow_none: bool = True,
) -> float | None:
    """Validate and normalize a usable EV battery capacity.

    ``None`` is the public clear-override value.  Booleans and non-finite
    numeric values are rejected even though Python otherwise treats booleans
    as numbers.
    """
    if value is None:
        if allow_none:
            return None
        raise ValueError("EV battery capacity is required")
    if isinstance(value, bool):
        raise ValueError("EV battery capacity must be a number")
    try:
        capacity = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("EV battery capacity must be a number") from err
    if not math.isfinite(capacity):
        raise ValueError("EV battery capacity must be finite")
    if not MIN_EV_BATTERY_CAPACITY_KWH <= capacity <= MAX_EV_BATTERY_CAPACITY_KWH:
        raise ValueError(
            "EV battery capacity must be between "
            f"{MIN_EV_BATTERY_CAPACITY_KWH:.1f} and "
            f"{MAX_EV_BATTERY_CAPACITY_KWH:.1f} kWh"
        )
    return capacity


def canonical_vehicle_id(vehicle_id: Any) -> str:
    """Return a stable comparison key for VIN, BLE, and provider identifiers."""
    value = str(vehicle_id or "").strip()
    if not value:
        return ""
    if value.lower().startswith("ble_"):
        return f"ble_{value[4:].strip().lower()}"
    if len(value) == 17 and value.isalnum():
        return value.upper()
    return value.lower()


def vehicle_ids_match(first: Any, second: Any) -> bool:
    """Match stable vehicle IDs, including BLE prefix aliases."""
    left = canonical_vehicle_id(first)
    right = canonical_vehicle_id(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if left.startswith("ble_") and left[4:] == right:
        return True
    if right.startswith("ble_") and right[4:] == left:
        return True
    return False


def _normalize_model_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


# Exact usable-capacity estimates only.  Ambiguous labels such as "Model 3"
# and "Model Y" intentionally do not appear here because they do not identify
# a battery trim.
_KNOWN_MODEL_CAPACITIES_KWH: Mapping[str, float] = {
    "tesla model 3 standard range": 57.5,
    "tesla model 3 standard range plus": 57.5,
    "model 3 standard range": 57.5,
    "model 3 standard range plus": 57.5,
    "tesla model 3 long range": 82.0,
    "model 3 long range": 82.0,
    "tesla model y standard range": 57.5,
    "model y standard range": 57.5,
    "tesla model y long range": 82.0,
    "model y long range": 82.0,
    "byd shark 6": 29.6,
    "byd shark": 29.6,
    "shark 6": 29.6,
}


def known_model_capacity_kwh(model: Any, trim: Any = None) -> float | None:
    """Return an estimate only for an exact known model/trim label."""
    model_name = _normalize_model_name(model)
    trim_name = _normalize_model_name(trim)
    candidates = []
    if model_name and trim_name:
        candidates.append(f"{model_name} {trim_name}")
    if model_name:
        candidates.append(model_name)
    for candidate in candidates:
        if candidate in _KNOWN_MODEL_CAPACITIES_KWH:
            return _KNOWN_MODEL_CAPACITIES_KWH[candidate]
    return None


def _valid_capacity_or_none(value: Any) -> float | None:
    try:
        return validate_ev_battery_capacity(value)
    except ValueError:
        return None


def resolve_ev_battery_capacity(
    *,
    manual_capacity_kwh: Any = None,
    charger_fallback_capacity_kwh: Any = None,
    provider_capacity_kwh: Any = None,
    model: Any = None,
    trim: Any = None,
    anonymous_loadpoint: bool = False,
) -> ResolvedEVBatteryCapacity:
    """Resolve usable capacity using the shared PowerSync precedence.

    Invalid persisted/provider metadata is skipped defensively.  API write
    paths should call :func:`validate_ev_battery_capacity` so invalid manual
    input is rejected rather than silently cleared.
    """
    manual = _valid_capacity_or_none(manual_capacity_kwh)
    if manual is not None:
        return ResolvedEVBatteryCapacity(
            manual,
            CAPACITY_SOURCE_MANUAL,
            manual,
        )

    if anonymous_loadpoint:
        charger_fallback = _valid_capacity_or_none(charger_fallback_capacity_kwh)
        if charger_fallback is not None:
            return ResolvedEVBatteryCapacity(
                charger_fallback,
                CAPACITY_SOURCE_CHARGER_FALLBACK,
            )

    provider = _valid_capacity_or_none(provider_capacity_kwh)
    if provider is not None:
        return ResolvedEVBatteryCapacity(provider, CAPACITY_SOURCE_PROVIDER)

    model_capacity = known_model_capacity_kwh(model, trim)
    if model_capacity is not None:
        return ResolvedEVBatteryCapacity(
            model_capacity,
            CAPACITY_SOURCE_MODEL_ESTIMATE,
        )

    return ResolvedEVBatteryCapacity(
        DEFAULT_EV_BATTERY_CAPACITY_KWH,
        CAPACITY_SOURCE_DEFAULT_ESTIMATE,
    )


def resolve_ev_battery_capacity_contract(
    config: Mapping[str, Any],
    *,
    anonymous_loadpoint: bool,
    shared_charger_fallback_capacity_kwh: Any = None,
) -> dict[str, float | str | None]:
    """Resolve the API contract while preserving a profile-local fallback.

    Anonymous generic/OCPP profiles historically stored their capacity in
    ``battery_capacity_kwh``.  Expose that legacy value through the dedicated
    charger-fallback field so API clients can distinguish and clear it.  A
    shared integration fallback remains resolution-only and is deliberately
    not presented as a profile-local override.
    """
    explicit_charger_fallback = None
    if anonymous_loadpoint:
        if "charger_fallback_battery_capacity_kwh" in config:
            explicit_charger_fallback = config.get(
                "charger_fallback_battery_capacity_kwh"
            )
        else:
            explicit_charger_fallback = config.get("battery_capacity_kwh")

    resolved = resolve_ev_battery_capacity(
        manual_capacity_kwh=(
            None if anonymous_loadpoint else config.get("battery_capacity_kwh")
        ),
        charger_fallback_capacity_kwh=(
            explicit_charger_fallback
            if explicit_charger_fallback is not None
            else shared_charger_fallback_capacity_kwh
        ),
        provider_capacity_kwh=config.get("provider_battery_capacity_kwh"),
        model=config.get("vehicle_model", config.get("model")),
        trim=config.get("vehicle_trim", config.get("trim")),
        anonymous_loadpoint=anonymous_loadpoint,
    )
    return {
        **resolved.to_dict(),
        "charger_fallback_battery_capacity_kwh": _valid_capacity_or_none(
            explicit_charger_fallback
        ),
    }
