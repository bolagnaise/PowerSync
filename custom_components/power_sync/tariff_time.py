"""Time-of-use tariff period matching helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math
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
    raw_to_day = period.get("toDayOfWeek", 6)
    to_day = 6 if raw_to_day is None else int(raw_to_day)
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


def find_season_for_month(seasons: Mapping[str, Any], month: int) -> str:
    """Return the configured tariff season for a calendar month."""
    fallback = next(
        (str(name) for name in seasons if str(name).casefold() == "all year"),
        None,
    )
    for season_name, season_data in seasons.items():
        if str(season_name).casefold() == "all year":
            continue
        if not isinstance(season_data, Mapping):
            continue
        from_month = int(season_data.get("fromMonth", 1) or 1)
        to_month = int(season_data.get("toMonth", 12) or 12)
        if from_month <= to_month:
            if from_month <= month <= to_month:
                return str(season_name)
        elif month >= from_month or month <= to_month:
            return str(season_name)
    return fallback or next(iter(seasons), "All Year")


def season_rate_maps(energy_charges: Any) -> dict[str, dict[str, float]]:
    """Normalize direct and Tesla-nested tariff rates for every season."""
    if not isinstance(energy_charges, Mapping):
        return {}
    normalized: dict[str, dict[str, float]] = {}
    for season_name, season_data in energy_charges.items():
        if not isinstance(season_data, Mapping):
            continue
        rates = season_data.get("rates", season_data)
        if not isinstance(rates, Mapping):
            continue
        normalized[str(season_name)] = {
            str(period): float(rate)
            for period, rate in rates.items()
            if isinstance(rate, (int, float))
            and not isinstance(rate, bool)
            and math.isfinite(float(rate))
        }
    return normalized


def tariff_components_for_datetime(
    tariff: Mapping[str, Any],
    when: datetime,
) -> tuple[Mapping[str, Any], Mapping[str, float], Mapping[str, float], str]:
    """Return season-correct TOU periods and rates for one local timestamp."""
    seasons = tariff.get("seasons", {})
    if not isinstance(seasons, Mapping):
        seasons = {}
    season_name = find_season_for_month(seasons, when.month)
    season_data = seasons.get(season_name, {})
    if not isinstance(season_data, Mapping):
        season_data = {}

    tou_periods = season_data.get("tou_periods")
    if not isinstance(tou_periods, Mapping) or not tou_periods:
        tou_periods = tariff.get("tou_periods", {})
    if not isinstance(tou_periods, Mapping):
        tou_periods = {}

    season_buy_rates = tariff.get("season_buy_rates", {})
    season_sell_rates = tariff.get("season_sell_rates", {})
    buy_rates = (
        season_buy_rates.get(season_name)
        if isinstance(season_buy_rates, Mapping)
        else None
    )
    sell_rates = (
        season_sell_rates.get(season_name)
        if isinstance(season_sell_rates, Mapping)
        else None
    )
    if not isinstance(buy_rates, Mapping):
        buy_rates = tariff.get("buy_rates", {})
    if not isinstance(sell_rates, Mapping):
        sell_rates = tariff.get("sell_rates", {})
    return (
        tou_periods,
        buy_rates if isinstance(buy_rates, Mapping) else {},
        sell_rates if isinstance(sell_rates, Mapping) else {},
        season_name,
    )


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
