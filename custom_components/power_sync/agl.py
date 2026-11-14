"""AGL Battery Rewards tariff helpers."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import math
from typing import Any

from .const import (
    AGL_BATTERY_REWARDS_END_HOUR,
    AGL_BATTERY_REWARDS_START_HOUR,
    AGL_IMPORT_SCHEDULE_AUSGRID_SEASONAL,
)

_REWARD_SUFFIX = "_AGL_REWARD"
_BASE_TARIFF_KEY = "agl_base_tariff"
_METADATA_KEY = "agl_battery_rewards"


def build_ausgrid_seasonal_import_periods(
    *,
    summer_offpeak_rate: float,
    summer_peak_rate: float,
    winter_offpeak_rate: float,
    winter_peak_rate: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return authored seasonal import periods for the opt-in Ausgrid plan.

    AGL offers are account-specific, so this helper deliberately accepts the
    prices from the user's configured plan instead of publishing a rate table.
    The returned rows use the existing custom-tariff schema and can therefore
    be edited or persisted through the normal tariff path.
    """
    rates = (
        summer_offpeak_rate,
        summer_peak_rate,
        winter_offpeak_rate,
        winter_peak_rate,
    )
    if any(not 0 <= float(rate) <= 2 for rate in rates):
        raise ValueError("Ausgrid import rates must be between 0 and 2 $/kWh")

    seasons = [
        {"name": "Summer", "from_month": 11, "to_month": 3},
        {"name": "Autumn", "from_month": 4, "to_month": 5},
        {"name": "Winter", "from_month": 6, "to_month": 8},
        {"name": "Spring", "from_month": 9, "to_month": 10},
    ]
    # Ausgrid's peak window is the same on every day in the two seasons that
    # have a peak.  Autumn and Spring retain the winter off-peak rate; there is
    # no additional shoulder-rate input in the AGL setup flow.
    periods = [
        {
            "name": "PEAK",
            "season": "Summer",
            "start": 15,
            "end": 21,
            "days": "all_days",
            "import_rate": float(summer_peak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Summer",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(summer_offpeak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Autumn",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(winter_offpeak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "PEAK",
            "season": "Winter",
            "start": 15,
            "end": 21,
            "days": "all_days",
            "import_rate": float(winter_peak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Winter",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(winter_offpeak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Spring",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(winter_offpeak_rate),
            "export_rate": 0.0,
        },
    ]
    return seasons, periods


def _legacy_ausgrid_period_rows(
    summer_offpeak_rate: float,
    summer_peak_rate: float,
    winter_offpeak_rate: float,
    winter_peak_rate: float,
) -> list[dict[str, Any]]:
    """Return the exact explicit rows emitted by the former two-season builder."""
    return [
        {
            "name": "PEAK",
            "season": "Summer",
            "start": 14,
            "end": 20,
            "days": "weekdays",
            "import_rate": float(summer_peak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Summer",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(summer_offpeak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "PEAK",
            "season": "Winter",
            "start": 17,
            "end": 21,
            "days": "weekdays",
            "import_rate": float(winter_peak_rate),
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Winter",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": float(winter_offpeak_rate),
            "export_rate": 0.0,
        },
    ]


def _legacy_ausgrid_base_rates(
    tariff: Any,
) -> tuple[dict[str, float], list[dict[str, Any]]] | None:
    """Validate and extract the former generated two-season tariff."""
    if not isinstance(tariff, dict):
        return None
    if tariff.get("name") != "AGL / Ausgrid Seasonal TOU":
        return None
    if tariff.get("utility") != "AGL":
        return None
    if _BASE_TARIFF_KEY in tariff or _METADATA_KEY in tariff:
        return None

    seasons = tariff.get("seasons")
    if not isinstance(seasons, dict) or set(seasons) != {"Summer", "Winter"}:
        return None
    expected_seasons = {
        "Summer": (11, 3, 14, 20),
        "Winter": (4, 10, 17, 21),
    }
    expected_ranges = {
        "Summer": {
            "PEAK": [
                {
                    "fromDayOfWeek": 1,
                    "toDayOfWeek": 5,
                    "fromHour": 14,
                    "toHour": 20,
                }
            ],
            "OFF_PEAK": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ],
        },
        "Winter": {
            "PEAK": [
                {
                    "fromDayOfWeek": 1,
                    "toDayOfWeek": 5,
                    "fromHour": 17,
                    "toHour": 21,
                }
            ],
            "OFF_PEAK": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ],
        },
    }
    for season_name, (from_month, to_month, _start, _end) in expected_seasons.items():
        season = seasons.get(season_name)
        if not isinstance(season, dict):
            return None
        if set(season) != {"fromMonth", "toMonth", "tou_periods"}:
            return None
        if season.get("fromMonth") != from_month or season.get("toMonth") != to_month:
            return None
        if season.get("tou_periods") != expected_ranges[season_name]:
            return None

    energy_charges = tariff.get("energy_charges")
    sell_tariff = tariff.get("sell_tariff")
    if not isinstance(energy_charges, dict) or set(energy_charges) != set(seasons):
        return None
    if not isinstance(sell_tariff, dict):
        return None
    sell_charges = sell_tariff.get("energy_charges")
    if not isinstance(sell_charges, dict) or set(sell_charges) != set(seasons):
        return None

    rates: dict[str, float] = {}
    for season_name in ("Summer", "Winter"):
        season_rates = energy_charges.get(season_name)
        season_sell_rates = sell_charges.get(season_name)
        if not isinstance(season_rates, dict) or set(season_rates) != {
            "PEAK",
            "OFF_PEAK",
        }:
            return None
        if not isinstance(season_sell_rates, dict) or set(season_sell_rates) != {
            "PEAK",
            "OFF_PEAK",
        }:
            return None
        for period_name in ("PEAK", "OFF_PEAK"):
            if isinstance(season_rates[period_name], bool) or isinstance(
                season_sell_rates[period_name], bool
            ):
                return None
            try:
                import_rate = float(season_rates[period_name])
                export_rate = float(season_sell_rates[period_name])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(import_rate) or not math.isfinite(export_rate):
                return None
            if export_rate != 0.0:
                return None
            rates[f"{season_name.lower()}_{period_name.lower()}"] = import_rate

    rows = _legacy_ausgrid_period_rows(
        rates["summer_off_peak"],
        rates["summer_peak"],
        rates["winter_off_peak"],
        rates["winter_peak"],
    )
    if (
        "power_sync_explicit_periods" in tariff
        and tariff.get("power_sync_explicit_periods") != rows
    ):
        return None
    return rates, rows


def _reauthor_ausgrid_base(
    tariff: dict[str, Any],
    rates: dict[str, float],
) -> dict[str, Any]:
    """Rebuild only the calendar and explicit rows for a validated base."""
    season_defs, explicit_rows = build_ausgrid_seasonal_import_periods(
        summer_offpeak_rate=rates["summer_off_peak"],
        summer_peak_rate=rates["summer_peak"],
        winter_offpeak_rate=rates["winter_off_peak"],
        winter_peak_rate=rates["winter_peak"],
    )
    old_seasons = tariff["seasons"]
    new_seasons: dict[str, dict[str, Any]] = {}
    new_energy: dict[str, dict[str, float]] = {}
    new_sell_energy: dict[str, dict[str, float]] = {}
    for season_def in season_defs:
        season_name = str(season_def["name"])
        source_name = "Summer" if season_name == "Summer" else "Winter"
        season = deepcopy(old_seasons[source_name])
        season["fromMonth"] = int(season_def["from_month"])
        season["toMonth"] = int(season_def["to_month"])
        season_periods: dict[str, list[dict[str, int]]] = {}
        season_rates: dict[str, float] = {}
        season_sell_rates: dict[str, float] = {}
        for period in explicit_rows:
            if period["season"] != season_name:
                continue
            period_name = str(period["name"])
            season_periods[period_name] = [
                {
                    "fromDayOfWeek": 0 if period["days"] == "all_days" else 1,
                    "toDayOfWeek": 6 if period["days"] == "all_days" else 5,
                    "fromHour": int(period["start"]),
                    "toHour": int(period["end"]),
                }
            ]
            season_rates[period_name] = float(period["import_rate"])
            season_sell_rates[period_name] = float(period["export_rate"])
        season["tou_periods"] = season_periods
        new_seasons[season_name] = season
        new_energy[season_name] = season_rates
        new_sell_energy[season_name] = season_sell_rates

    result = deepcopy(tariff)
    result["seasons"] = new_seasons
    result["energy_charges"] = new_energy
    sell_tariff = deepcopy(tariff["sell_tariff"])
    sell_tariff["energy_charges"] = new_sell_energy
    result["sell_tariff"] = sell_tariff
    result["power_sync_explicit_periods"] = deepcopy(explicit_rows)
    return result


def _overlay_values(metadata: Any) -> tuple[float, float, int, int] | None:
    """Read the required Battery Rewards metadata without accepting guesses."""
    if not isinstance(metadata, dict):
        return None
    if any(type(metadata.get(key)) is not int for key in ("start_hour", "end_hour")):
        return None
    if any(
        isinstance(metadata.get(key), bool)
        for key in ("peak_export_rate", "offpeak_export_rate", "start_hour", "end_hour")
    ):
        return None
    try:
        peak = float(metadata["peak_export_rate"])
        offpeak = float(metadata["offpeak_export_rate"])
        start = metadata["start_hour"]
        end = metadata["end_hour"]
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(peak) or not math.isfinite(offpeak):
        return None
    if not 0 <= peak <= 2 or not 0 <= offpeak <= 2:
        return None
    if not 0 <= start < end <= 24:
        return None
    return peak, offpeak, start, end


def repair_legacy_ausgrid_seasonal_import_tariff(
    tariff: Any,
) -> dict[str, Any] | Any:
    """Repair the pre-November AGL/Ausgrid calendar, preserving user data.

    The migration is deliberately exact.  A tariff is changed only when it is
    the generated two-season tariff, including any generated overlay.  A
    hand-edited calendar, explicit row list, base tariff, or reward overlay is
    returned by identity so a user change cannot be overwritten on startup.
    """
    if not isinstance(tariff, dict):
        return tariff
    if tariff.get("name") != "AGL / Ausgrid Seasonal TOU" or tariff.get(
        "utility"
    ) != "AGL":
        return tariff

    has_base = _BASE_TARIFF_KEY in tariff
    has_metadata = _METADATA_KEY in tariff
    if has_base != has_metadata:
        return tariff

    if not has_base:
        validated = _legacy_ausgrid_base_rates(tariff)
        if validated is None:
            return tariff
        rates, _rows = validated
        try:
            return _reauthor_ausgrid_base(tariff, rates)
        except (KeyError, TypeError, ValueError):
            return tariff

    base = tariff.get(_BASE_TARIFF_KEY)
    metadata = tariff.get(_METADATA_KEY)
    validated = _legacy_ausgrid_base_rates(base)
    overlay = _overlay_values(metadata)
    if validated is None or overlay is None:
        return tariff
    rates, legacy_rows = validated
    if "power_sync_explicit_periods" in tariff and tariff.get(
        "power_sync_explicit_periods"
    ) != legacy_rows:
        return tariff
    try:
        expected_overlay = _apply_battery_rewards_export_rates(
            base,
            peak_export_rate=overlay[0],
            offpeak_export_rate=overlay[1],
            start_hour=overlay[2],
            end_hour=overlay[3],
            resolve_import_overlaps=False,
        )
    except (TypeError, ValueError, KeyError):
        return tariff
    sections = ("seasons", "energy_charges", "sell_tariff")
    if any(tariff.get(section) != expected_overlay.get(section) for section in sections):
        expected_overlay = apply_battery_rewards_export_rates(
            base, peak_export_rate=overlay[0], offpeak_export_rate=overlay[1],
            start_hour=overlay[2], end_hour=overlay[3],
        )
        if any(tariff.get(section) != expected_overlay.get(section) for section in sections):
            return tariff

    try:
        repaired_base = _reauthor_ausgrid_base(base, rates)
    except (KeyError, TypeError, ValueError):
        return tariff
    repaired = apply_battery_rewards_export_rates(
        repaired_base,
        peak_export_rate=overlay[0],
        offpeak_export_rate=overlay[1],
        start_hour=overlay[2],
        end_hour=overlay[3],
    )
    # Keep the stored metadata byte-for-byte, including future fields unknown
    # to this helper, and retain any non-tariff metadata on the overlay.
    repaired[_METADATA_KEY] = deepcopy(metadata)
    for key, value in tariff.items():
        if key not in {
            "seasons",
            "energy_charges",
            "sell_tariff",
            _BASE_TARIFF_KEY,
            _METADATA_KEY,
            "power_sync_explicit_periods",
        }:
            repaired[key] = deepcopy(value)
    return repaired


async def async_migrate_ausgrid_seasonal_import_tariff(
    entry: Any,
    store: Any,
) -> bool:
    """Persist the strict AGL calendar repair when the user opted in."""
    options = getattr(entry, "options", None)
    data = getattr(entry, "data", None)
    if not isinstance(options, Mapping):
        options = {}
    if not isinstance(data, Mapping):
        data = {}
    schedule = (
        options["agl_import_schedule"]
        if "agl_import_schedule" in options
        else data.get("agl_import_schedule")
    )
    if schedule != AGL_IMPORT_SCHEDULE_AUSGRID_SEASONAL:
        return False
    if store is None:
        return False
    try:
        source = store.get_custom_tariff()
    except Exception:
        return False
    if not isinstance(source, dict) or not source:
        source = data.get("initial_custom_tariff")
    if not isinstance(source, dict) or not source:
        return False
    repaired = repair_legacy_ausgrid_seasonal_import_tariff(source)
    if repaired is source:
        return False
    setter = getattr(store, "set_custom_tariff", None)
    saver = getattr(store, "async_save", None)
    if not callable(setter) or not callable(saver):
        return False
    setter(repaired)
    await saver()
    return True


async def async_prepare_agl_startup_tariff(entry: Any, store: Any) -> bool:
    """Honor a fresh setup tariff before repairing existing automatic presets.

    AutomationStore survives entry removal. A current setup's pending tariff
    must replace that retained state before startup pricing reads it. Only an
    explicitly selected, exact legacy automatic snapshot defers to the saved
    tariff, so it cannot undo an upgrade repair or later user edits.
    """
    data = getattr(entry, "data", {})
    options = getattr(entry, "options", {})
    data = data if isinstance(data, Mapping) else {}
    options = options if isinstance(options, Mapping) else {}
    initial = data.get("initial_custom_tariff")
    selection = options.get("agl_import_schedule", data.get("agl_import_schedule"))
    if isinstance(initial, dict) and initial:
        legacy_snapshot = (
            selection == AGL_IMPORT_SCHEDULE_AUSGRID_SEASONAL
            and repair_legacy_ausgrid_seasonal_import_tariff(initial) is not initial
        )
        if not legacy_snapshot:
            repaired_initial = repair_battery_rewards_import_overlap(deepcopy(initial))
            store.set_custom_tariff(repaired_initial)
            await store.async_save()
            return False
    migrated = await async_migrate_ausgrid_seasonal_import_tariff(entry, store)
    if store is not None:
        try:
            source = store.get_custom_tariff()
        except Exception:
            return migrated
        repaired = repair_battery_rewards_import_overlap(source)
        if repaired is not source:
            store.set_custom_tariff(repaired)
            await store.async_save()
            return True
    return migrated


def repair_battery_rewards_import_overlap(tariff: Any) -> Any:
    """Rebuild an unchanged generated reward overlay, preserving user edits."""
    if not isinstance(tariff, dict) or not isinstance(tariff.get(_BASE_TARIFF_KEY), dict):
        return tariff
    overlay = _overlay_values(tariff.get(_METADATA_KEY))
    if overlay is None:
        return tariff
    base = tariff[_BASE_TARIFF_KEY]
    if tariff.get("power_sync_explicit_periods") != base.get("power_sync_explicit_periods"):
        return tariff
    kwargs = {
        "peak_export_rate": overlay[0],
        "offpeak_export_rate": overlay[1],
        "start_hour": overlay[2],
        "end_hour": overlay[3],
    }
    try:
        previous = _apply_battery_rewards_export_rates(base, **kwargs, resolve_import_overlaps=False)
        sections = ("seasons", "energy_charges", "sell_tariff")
        if any(tariff.get(section) != previous.get(section) for section in sections):
            return tariff
        corrected = apply_battery_rewards_export_rates(base, **kwargs)
    except (TypeError, ValueError, KeyError):
        return tariff
    if all(tariff.get(section) == corrected.get(section) for section in sections):
        return tariff
    result = deepcopy(tariff)
    for section in sections:
        result[section] = corrected[section]
    return result


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
    """Overlay daily export rewards without changing effective import prices."""
    return _apply_battery_rewards_export_rates(
        tariff,
        peak_export_rate=peak_export_rate,
        offpeak_export_rate=offpeak_export_rate,
        start_hour=start_hour,
        end_hour=end_hour,
        resolve_import_overlaps=True,
    )


def _resolved_import_ranges(
    periods: dict[str, Any],
    buy_rates: dict[str, float],
    sell_rates: dict[str, float],
) -> dict[str, list[dict[str, int]]]:
    """Resolve fallback overlaps before splitting away their original duration."""
    from .tariff_time import find_matching_tou_period

    expanded = [
        entry
        for ranges in periods.values() if isinstance(ranges, list)
        for raw in ranges if isinstance(raw, dict)
        for entry in _non_wrapping_ranges(raw)
    ]
    resolved: dict[str, list[dict[str, int]]] = {}
    for day in range(7):
        boundaries = sorted({
            entry[key]
            for entry in expanded if entry["fromDayOfWeek"] == day
            for key in ("fromHour", "toHour")
        })
        for start, end in zip(boundaries, boundaries[1:]):
            # 4 January 2026 is Sunday (Tesla day zero). Only the weekday and
            # local time are relevant; the caller already selected the season.
            name = find_matching_tou_period(
                periods, datetime(2026, 1, 4 + day, start), default="",
                buy_rates=buy_rates, sell_rates=sell_rates,
            )
            if name:
                resolved.setdefault(name, []).append({
                    "fromDayOfWeek": day, "toDayOfWeek": day,
                    "fromHour": start, "toHour": end,
                })
    return resolved


def _apply_battery_rewards_export_rates(
    tariff: dict[str, Any],
    *,
    peak_export_rate: float,
    offpeak_export_rate: float,
    start_hour: int,
    end_hour: int,
    resolve_import_overlaps: bool,
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

        if resolve_import_overlaps:
            tou_periods = _resolved_import_ranges(
                tou_periods, season_buy_rates, sell_charges.get(season_name, {}),
            )

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
