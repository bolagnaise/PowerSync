"""Shared helpers for EV solar-surplus charging configuration."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC = 80
DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS = 15.0

DEFAULT_SOLAR_SURPLUS_CONFIG: dict[str, Any] = {
    "enabled": False,
    "household_buffer_kw": 0.5,
    "surplus_calculation": "grid_based",
    "sustained_surplus_minutes": 2,
    "stop_delay_minutes": 5,
    "dual_vehicle_strategy": "priority_first",
    "home_battery_minimum": DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
    "min_battery_soc": DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
    "allow_parallel_charging": False,
    "max_battery_charge_rate_kw": 5.0,
    "max_export_price_cents": DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS,
}


def _coerce_percentage(value: Any) -> int | None:
    """Coerce a value to a clamped percentage, returning None if invalid."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def get_solar_surplus_min_battery_soc(
    config: Mapping[str, Any] | None,
    default: int = DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
) -> int:
    """Return the configured home-battery SOC threshold for solar EV charging."""
    if config:
        for key in ("home_battery_minimum", "min_battery_soc"):
            if key in config:
                value = _coerce_percentage(config[key])
                if value is not None:
                    return value

    value = _coerce_percentage(default)
    return value if value is not None else DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC


def get_solar_surplus_max_export_price_cents(
    config: Mapping[str, Any] | None,
) -> float:
    """Return the maximum export value allowed for automatic solar charging."""
    raw = (
        config.get(
            "max_export_price_cents",
            DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS,
        )
        if config
        else DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS
    )
    if isinstance(raw, bool):
        return DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS
    if not math.isfinite(value):
        return DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS
    return max(0.0, min(200.0, value))


def solar_surplus_price_allows_charging(
    export_price_cents: float | None,
    config: Mapping[str, Any] | None,
    *,
    deadline_override: bool = False,
) -> bool:
    """Return whether export opportunity cost permits automatic solar charging."""
    if deadline_override:
        return True
    if export_price_cents is None or isinstance(export_price_cents, bool):
        return False
    try:
        export_price = float(export_price_cents)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(export_price):
        return False
    return export_price <= get_solar_surplus_max_export_price_cents(config)


def normalize_solar_surplus_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge solar-surplus config with defaults and keep threshold aliases aligned."""
    min_battery_soc = get_solar_surplus_min_battery_soc(config)
    normalized = dict(DEFAULT_SOLAR_SURPLUS_CONFIG)
    if config:
        normalized.update(dict(config))

    normalized["home_battery_minimum"] = min_battery_soc
    normalized["min_battery_soc"] = min_battery_soc
    normalized["max_export_price_cents"] = (
        get_solar_surplus_max_export_price_cents(config)
    )
    return normalized


def get_stored_solar_surplus_config(entry_data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return normalized solar-surplus config from runtime entry data."""
    stored_config: Mapping[str, Any] | None = None
    if entry_data:
        automation_store = entry_data.get("automation_store")
        if automation_store:
            stored_data = getattr(automation_store, "_data", {}) or {}
            stored_config = stored_data.get("solar_surplus_config")
        if not stored_config:
            stored_config = entry_data.get("solar_surplus_config")

    return normalize_solar_surplus_config(stored_config)
