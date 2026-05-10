"""Time-of-use tariff period matching helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


def tesla_day_of_week(when: datetime) -> int:
    """Return Tesla day of week for a datetime: Sunday=0, Monday=1."""
    return (when.weekday() + 1) % 7


def _day_range(start: int, end: int) -> set[int]:
    """Return inclusive Tesla day range, allowing week wrap."""
    start %= 7
    end %= 7
    if start <= end:
        return set(range(start, end + 1))
    return set(range(start, 7)) | set(range(0, end + 1))


def _time_minutes(period: Mapping[str, Any], prefix: str, fallback_hour: int) -> int:
    hour = int(period.get(f"{prefix}Hour", fallback_hour) or 0)
    minute = int(period.get(f"{prefix}Minute", 0) or 0)
    return min(24 * 60, max(0, hour * 60 + minute))


def _period_duration_minutes(period: Mapping[str, Any]) -> int:
    """Return one-day duration for a TOU period, allowing midnight wrap."""
    start_minute = _time_minutes(period, "from", 0)
    end_minute = _time_minutes(period, "to", 24)

    if start_minute == 0 and end_minute == 0:
        return 24 * 60
    if end_minute == 0 and start_minute > 0:
        end_minute = 24 * 60
    if start_minute < end_minute:
        return end_minute - start_minute
    if start_minute == end_minute:
        return 24 * 60
    return (24 * 60 - start_minute) + end_minute


def tou_period_matches(period: Mapping[str, Any], when: datetime) -> bool:
    """Return true if a Tesla tariff period matches the given local datetime."""
    today = tesla_day_of_week(when)
    yesterday = (today - 1) % 7
    now_minute = when.hour * 60 + when.minute

    from_day = int(period.get("fromDayOfWeek", 0) or 0)
    to_day = int(period.get("toDayOfWeek", 6) or 6)
    active_start_days = _day_range(from_day, to_day)

    start_minute = _time_minutes(period, "from", 0)
    end_minute = _time_minutes(period, "to", 24)

    # Tesla often represents an all-day tariff as 00:00 -> 00:00.
    if start_minute == 0 and end_minute == 0:
        end_minute = 24 * 60

    # 21:00 -> 00:00 means "until midnight", not an empty interval.
    if end_minute == 0 and start_minute > 0:
        end_minute = 24 * 60

    if start_minute < end_minute:
        return today in active_start_days and start_minute <= now_minute < end_minute

    if start_minute == end_minute:
        return today in active_start_days

    # Overnight period.  The day range applies to the start day, so early
    # morning belongs to yesterday's started period.
    return (
        (today in active_start_days and now_minute >= start_minute)
        or (yesterday in active_start_days and now_minute < end_minute)
    )


def period_entries(period_data: Any) -> Sequence[Mapping[str, Any]]:
    """Normalize Tesla/custom TOU period entry shapes."""
    if isinstance(period_data, Mapping) and isinstance(period_data.get("periods"), list):
        return [p for p in period_data["periods"] if isinstance(p, Mapping)]
    if isinstance(period_data, list):
        return [p for p in period_data if isinstance(p, Mapping)]
    return []


def tariff_period_priority(name: str) -> tuple[int, str]:
    """Sort common TOU names from most-specific to least-specific."""
    return (
        0 if name.startswith("SUPER_OFF_PEAK") else
        1 if name.startswith("PEAK_") else
        2 if name == "PEAK" else
        3 if name.startswith("SHOULDER") else
        4 if name.startswith("PARTIAL_PEAK") else
        5,
        name,
    )


def find_matching_tou_period(
    tou_periods: Mapping[str, Any],
    when: datetime,
    default: str = "OFF_PEAK",
    buy_rates: Mapping[str, float] | None = None,
    sell_rates: Mapping[str, float] | None = None,
) -> str:
    """Find the current TOU period name for a local datetime."""
    matches: list[tuple[str, Mapping[str, Any]]] = []
    for period_name, period_data in tou_periods.items():
        for period in period_entries(period_data):
            if tou_period_matches(period, when):
                matches.append((period_name, period))

    if not matches:
        return default

    if buy_rates is None and sell_rates is None:
        return sorted((name for name, _period in matches), key=tariff_period_priority)[0]

    def _rate(rates: Mapping[str, float] | None, name: str, fallback: float) -> float:
        if rates is None:
            return fallback
        value = rates.get(name)
        return float(value) if isinstance(value, (int, float)) else fallback

    return min(
        matches,
        key=lambda match: (
            _period_duration_minutes(match[1]),
            -_rate(sell_rates, match[0], 0.0),
            _rate(buy_rates, match[0], 0.0),
            match[0],
        ),
    )[0]
