"""Shared configuration helpers for export-price curtailment."""

from __future__ import annotations

import math
from typing import Any

from .const import (
    CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
    DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
)
from .tariff_utils import with_hysteresis


CURTAILMENT_HYSTERESIS_CENTS = 0.2
MIN_CURTAILMENT_EXPORT_THRESHOLD_CENTS = -100.0
MAX_CURTAILMENT_EXPORT_THRESHOLD_CENTS = 200.0


def normalize_curtailment_export_threshold_cents(value: Any) -> float:
    """Return a finite, bounded curtailment entry threshold in c/kWh."""
    if isinstance(value, bool):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    if not math.isfinite(parsed):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    return max(
        MIN_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        min(MAX_CURTAILMENT_EXPORT_THRESHOLD_CENTS, parsed),
    )


def get_curtailment_price_thresholds(entry: Any) -> tuple[float, float]:
    """Return configured enter/exit thresholds in c/kWh.

    The user selects the economic entry boundary. The existing 0.2 c/kWh
    deadband follows that boundary so changing the entry threshold cannot
    silently retain the old 1.2 c/kWh release point.
    """
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    raw = options.get(
        CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        data.get(
            CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
            DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        ),
    )
    enter = normalize_curtailment_export_threshold_cents(raw)
    return enter, enter + CURTAILMENT_HYSTERESIS_CENTS


def export_earnings_are_uneconomic(
    export_earnings_cents: float,
    was_active: bool,
    entry: Any,
) -> bool:
    """Apply the configured low-price threshold with hysteresis."""
    enter, exit_ = get_curtailment_price_thresholds(entry)
    return with_hysteresis(
        export_earnings_cents,
        was_active,
        enter_threshold=enter,
        exit_threshold=exit_,
    )
