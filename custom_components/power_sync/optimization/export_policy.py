"""Provider-neutral battery export price policy.

This module deliberately contains only the global minimum export-price rule.
Callers must pass the real settlement/effective export price for each slot;
provider-specific overlays (for example Amber Export Boost, Chip, or
curtailment) do not belong here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
from typing import Any

from ..const import (
    CONF_OPTIMIZATION_MIN_EXPORT_PRICE,
    DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE,
)


def normalize_min_export_price(value: Any) -> float:
    """Return a finite, non-negative minimum export price in $/kWh.

    The setting is persisted and passed around in dollars per kWh.  Invalid
    values, including booleans and non-finite numbers, normalize to the
    disabled/default value.  Applying this function to an already normalized
    value is therefore idempotent.
    """
    if isinstance(value, bool):
        return float(DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE)
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE)
    if not math.isfinite(normalized):
        return float(DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE)
    return max(0.0, normalized)


def _configured_value(config: Any) -> Any:
    """Extract the setting from a mapping, config-entry-like object, or scalar."""
    if config is None:
        return DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE

    if isinstance(config, Mapping):
        if CONF_OPTIMIZATION_MIN_EXPORT_PRICE in config:
            return config[CONF_OPTIMIZATION_MIN_EXPORT_PRICE]

        # Supporting an entry-shaped mapping keeps this helper useful for
        # serialized config data while retaining options-over-data precedence.
        options = config.get("options")
        if (
            isinstance(options, Mapping)
            and CONF_OPTIMIZATION_MIN_EXPORT_PRICE in options
        ):
            return options[CONF_OPTIMIZATION_MIN_EXPORT_PRICE]
        data = config.get("data")
        if isinstance(data, Mapping) and CONF_OPTIMIZATION_MIN_EXPORT_PRICE in data:
            return data[CONF_OPTIMIZATION_MIN_EXPORT_PRICE]
        return DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE

    options = getattr(config, "options", None)
    if (
        isinstance(options, Mapping)
        and CONF_OPTIMIZATION_MIN_EXPORT_PRICE in options
    ):
        return options[CONF_OPTIMIZATION_MIN_EXPORT_PRICE]
    data = getattr(config, "data", None)
    if isinstance(data, Mapping) and CONF_OPTIMIZATION_MIN_EXPORT_PRICE in data:
        return data[CONF_OPTIMIZATION_MIN_EXPORT_PRICE]

    # A scalar is convenient for pure callers and means the same thing as the
    # setting value itself.  Unknown objects still safely normalize to zero.
    return config


def get_min_export_price(config: Any = None) -> float:
    """Return the normalized configured minimum export price in $/kWh."""
    return normalize_min_export_price(_configured_value(config))


def export_price_allows_battery_export(
    real_export_price: Any,
    min_export_price: Any = DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE,
) -> bool:
    """Return whether a real slot price permits intentional battery export.

    Battery export always requires a strictly positive, finite real price.
    A configured floor adds a second boundary: the price must be at least the
    normalized floor.  Consequently an invalid/non-finite price fails closed
    whether the floor is enabled or disabled, while an invalid floor safely
    normalizes to the legacy disabled setting.
    """
    if isinstance(real_export_price, bool):
        return False
    try:
        price = float(real_export_price)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(price) or price <= 0.0:
        return False

    floor = normalize_min_export_price(min_export_price)
    return price >= floor


def battery_export_allowed_slots(
    real_export_prices: Iterable[Any] | None,
    min_export_price: Any = DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE,
) -> list[bool]:
    """Return the export-permission mask for real settlement/effective prices."""
    if real_export_prices is None:
        return []
    return [
        export_price_allows_battery_export(price, min_export_price)
        for price in real_export_prices
    ]


__all__ = [
    "battery_export_allowed_slots",
    "export_price_allows_battery_export",
    "get_min_export_price",
    "normalize_min_export_price",
]
