"""Normalization helpers for Tesla Powerwall local readback values."""

from __future__ import annotations

from typing import Any


# Powerwall keeps a hidden low-SOE reserve. Local hardware/config values include
# that reserve, while Tesla app/cloud UI presents the user-facing reserve target.
DEFAULT_LOW_SOE_RESERVE_PCT = 5.0
MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT = 20.0


def _coerce_percent(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _coerce_low_soe_reserve_percent(value: Any) -> float:
    reserve = _coerce_percent(value)
    if reserve is None:
        return DEFAULT_LOW_SOE_RESERVE_PCT
    return min(reserve, MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT)


def normalize_local_soc_percent(value: Any) -> float | None:
    """Map raw full-pack SOE percent to Tesla app/cloud SOC percent."""
    try:
        raw_soc = float(value)
    except (TypeError, ValueError):
        return None
    raw_soc = max(0.0, min(100.0, raw_soc))
    return max(
        0.0,
        (raw_soc - DEFAULT_LOW_SOE_RESERVE_PCT)
        / (100.0 - DEFAULT_LOW_SOE_RESERVE_PCT)
        * 100.0,
    )


def detect_local_backup_reserve_offset(
    local_value: Any,
    user_facing_value: Any,
) -> float | None:
    """Infer the local hidden reserve offset from paired local/cloud readbacks."""
    local_reserve = _coerce_percent(local_value)
    user_reserve = _coerce_percent(user_facing_value)
    if local_reserve is None or user_reserve is None:
        return None
    if local_reserve >= 100 or user_reserve >= 100:
        return None
    offset = local_reserve - user_reserve
    if offset < 0 or offset > MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT:
        return None
    return round(offset, 3)


def normalize_local_backup_reserve_percent(
    value: Any,
    low_soe_reserve_pct: Any = DEFAULT_LOW_SOE_RESERVE_PCT,
) -> int | None:
    """Map local Powerwall backup reserve readback to the user-facing target."""
    local_reserve = _coerce_percent(value)
    if local_reserve is None:
        return None
    low_soe_reserve = _coerce_low_soe_reserve_percent(low_soe_reserve_pct)
    if local_reserve >= 100:
        return 100
    if local_reserve <= low_soe_reserve:
        return 0
    return int(
        round(
            max(0.0, min(100.0, local_reserve - low_soe_reserve))
        )
    )


def local_backup_reserve_write_percent(
    value: Any,
    low_soe_reserve_pct: Any = DEFAULT_LOW_SOE_RESERVE_PCT,
) -> int | None:
    """Map a user-facing reserve target to the local Powerwall config value."""
    reserve = _coerce_percent(value)
    if reserve is None:
        return None
    low_soe_reserve = _coerce_low_soe_reserve_percent(low_soe_reserve_pct)
    if reserve >= 100:
        return 100
    return int(
        round(
            max(0.0, min(100.0, reserve + low_soe_reserve))
        )
    )
