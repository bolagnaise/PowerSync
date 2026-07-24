"""AGL Battery Rewards tariff helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .const import (
    AGL_BATTERY_REWARDS_END_HOUR,
    AGL_BATTERY_REWARDS_START_HOUR,
)

_REWARD_SUFFIX = "_AGL_REWARD"
_BASE_TARIFF_KEY = "agl_base_tariff"
_METADATA_KEY = "agl_battery_rewards"


def _days_in_range(start: int, end: int) -> list[int]:
    """Expand an inclusive Home Assistant weekday range."""
    start %= 7
    end %= 7
    if start <= end:
        return list(range(start, end + 1))
    return list(range(start, 7)) + list(range(0, end + 1))


def _non_wrapping_ranges(period: dict[str, Any]) -> list[dict[str, int]]:
    """Expand a TOU range into per-day, non-wrapping ranges."""
    start_hour = int(period.get("fromHour", 0))
    end_hour = int(period.get("toHour", 24))
    days = _days_in_range(
        int(period.get("fromDayOfWeek", 0)),
        int(period.get("toDayOfWeek", 6)),
    )
    result: list[dict[str, int]] = []

    for day in days:
        if start_hour == end_hour:
            result.append(
                {
                    "fromDayOfWeek": day,
                    "toDayOfWeek": day,
                    "fromHour": 0,
                    "toHour": 24,
                }
            )
        elif start_hour < end_hour:
            result.append(
                {
                    "fromDayOfWeek": day,
                    "toDayOfWeek": day,
                    "fromHour": start_hour,
                    "toHour": end_hour,
                }
            )
        else:
            result.append(
                {
                    "fromDayOfWeek": day,
                    "toDayOfWeek": day,
                    "fromHour": start_hour,
                    "toHour": 24,
                }
            )
            next_day = (day + 1) % 7
            result.append(
                {
                    "fromDayOfWeek": next_day,
                    "toDayOfWeek": next_day,
                    "fromHour": 0,
                    "toHour": end_hour,
                }
            )

    return result


def _split_reward_window(
    period: dict[str, int],
    *,
    start_hour: int,
    end_hour: int,
) -> list[tuple[dict[str, int], bool]]:
    """Split one non-wrapping range at the daily reward boundaries."""
    period_start = period["fromHour"]
    period_end = period["toHour"]
    boundaries = sorted(
        {
            period_start,
            period_end,
            max(period_start, min(period_end, start_hour)),
            max(period_start, min(period_end, end_hour)),
        }
    )
    result: list[tuple[dict[str, int], bool]] = []
    for segment_start, segment_end in zip(boundaries, boundaries[1:]):
        if segment_start == segment_end:
            continue
        segment = {
            **period,
            "fromHour": segment_start,
            "toHour": segment_end,
        }
        is_reward = (
            segment_start >= start_hour and segment_end <= end_hour
        )
        result.append((segment, is_reward))
    return result


def apply_battery_rewards_export_rates(
    tariff: dict[str, Any],
    *,
    peak_export_rate: float,
    offpeak_export_rate: float,
    start_hour: int = AGL_BATTERY_REWARDS_START_HOUR,
    end_hour: int = AGL_BATTERY_REWARDS_END_HOUR,
) -> dict[str, Any]:
    """Overlay AGL's daily evening feed-in rates on an import tariff.

    Rates are dollars per kWh. The helper preserves the original tariff so
    applying updated AGL rates is idempotent and the options editor can recover
    the user's unsplit import periods.
    """
    if not isinstance(tariff, dict):
        raise TypeError("tariff must be a dictionary")
    if not 0 <= peak_export_rate <= 2:
        raise ValueError("peak_export_rate must be between 0 and 2 $/kWh")
    if not 0 <= offpeak_export_rate <= 2:
        raise ValueError("offpeak_export_rate must be between 0 and 2 $/kWh")
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("reward window must be within one day")

    source = tariff.get(_BASE_TARIFF_KEY, tariff)
    base = deepcopy(source)
    base.pop(_BASE_TARIFF_KEY, None)
    base.pop(_METADATA_KEY, None)
    result = deepcopy(base)

    seasons = result.get("seasons")
    energy_charges = result.get("energy_charges")
    sell_tariff = result.setdefault("sell_tariff", {})
    sell_charges = sell_tariff.setdefault("energy_charges", {})
    if not isinstance(seasons, dict) or not isinstance(energy_charges, dict):
        raise ValueError("tariff is missing seasons or energy charges")

    for season_name, season in seasons.items():
        if not isinstance(season, dict):
            continue
        tou_periods = season.get("tou_periods")
        season_buy_rates = energy_charges.get(season_name)
        if not isinstance(tou_periods, dict) or not isinstance(
            season_buy_rates, dict
        ):
            continue

        overlaid_periods: dict[str, list[dict[str, int]]] = {}
        overlaid_buy_rates: dict[str, float] = {}
        overlaid_sell_rates: dict[str, float] = {}

        for original_name, ranges in tou_periods.items():
            if not isinstance(ranges, list):
                continue
            base_name = str(original_name)
            if base_name.endswith(_REWARD_SUFFIX):
                base_name = base_name[: -len(_REWARD_SUFFIX)]
            buy_rate = float(season_buy_rates.get(original_name, 0.0))

            for raw_range in ranges:
                if not isinstance(raw_range, dict):
                    continue
                for expanded in _non_wrapping_ranges(raw_range):
                    for segment, is_reward in _split_reward_window(
                        expanded,
                        start_hour=start_hour,
                        end_hour=end_hour,
                    ):
                        period_name = (
                            f"{base_name}{_REWARD_SUFFIX}"
                            if is_reward
                            else base_name
                        )
                        overlaid_periods.setdefault(period_name, []).append(
                            segment
                        )
                        overlaid_buy_rates[period_name] = buy_rate
                        overlaid_sell_rates[period_name] = (
                            peak_export_rate
                            if is_reward
                            else offpeak_export_rate
                        )

        season["tou_periods"] = overlaid_periods
        energy_charges[season_name] = overlaid_buy_rates
        sell_charges[season_name] = overlaid_sell_rates

    result[_BASE_TARIFF_KEY] = base
    result[_METADATA_KEY] = {
        "peak_export_rate": peak_export_rate,
        "offpeak_export_rate": offpeak_export_rate,
        "start_hour": start_hour,
        "end_hour": end_hour,
    }
    return result
