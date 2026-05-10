"""Tests for TOU tariff period matching."""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

from power_sync.tariff_time import (  # noqa: E402
    find_matching_tou_period,
    tesla_day_of_week,
    tou_period_matches,
)


def test_tesla_day_of_week_maps_sunday_to_zero():
    assert tesla_day_of_week(datetime(2026, 5, 3)) == 0
    assert tesla_day_of_week(datetime(2026, 5, 4)) == 1


def test_matches_minutes_inside_half_hour_period():
    period = {
        "fromDayOfWeek": 0,
        "toDayOfWeek": 6,
        "fromHour": 17,
        "fromMinute": 30,
        "toHour": 18,
    }

    assert tou_period_matches(period, datetime(2026, 5, 1, 17, 29)) is False
    assert tou_period_matches(period, datetime(2026, 5, 1, 17, 30)) is True
    assert tou_period_matches(period, datetime(2026, 5, 1, 18, 0)) is False


def test_matches_overnight_period_on_next_morning():
    periods = {
        "OFF_PEAK": {
            "periods": [{
                "fromDayOfWeek": 1,
                "toDayOfWeek": 5,
                "fromHour": 21,
                "toHour": 7,
            }]
        },
        "PEAK": {
            "periods": [{
                "fromDayOfWeek": 1,
                "toDayOfWeek": 5,
                "fromHour": 15,
                "toHour": 21,
            }]
        },
    }

    # Saturday morning belongs to the Friday 21:00 overnight period.
    assert find_matching_tou_period(periods, datetime(2026, 5, 2, 1, 0)) == "OFF_PEAK"


def test_midnight_zero_to_zero_represents_all_day():
    periods = {
        "ALL": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 0,
            }]
        }
    }

    assert find_matching_tou_period(periods, datetime(2026, 5, 1, 12, 0)) == "ALL"


def test_matching_uses_local_datetime_timezone():
    periods = {
        "PEAK": {
            "periods": [{
                "fromDayOfWeek": 1,
                "toDayOfWeek": 5,
                "fromHour": 15,
                "toHour": 21,
            }]
        },
        "OFF_PEAK": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 24,
            }]
        },
    }

    melbourne = ZoneInfo("Australia/Melbourne")
    when = datetime(2026, 5, 1, 16, 0, tzinfo=melbourne)

    assert find_matching_tou_period(periods, when) == "PEAK"


def test_rate_aware_matching_prefers_specific_window_not_name():
    periods = {
        "PEAK": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 24,
            }]
        },
        "OFF_PEAK": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 18,
                "toHour": 21,
            }]
        },
    }

    assert find_matching_tou_period(
        periods,
        datetime(2026, 5, 1, 19, 0),
        buy_rates={"PEAK": 0.31, "OFF_PEAK": 0.51},
        sell_rates={"PEAK": 0.0, "OFF_PEAK": 0.0},
    ) == "OFF_PEAK"


def test_rate_aware_matching_handles_nested_free_import_override():
    periods = {
        "PARTIAL_PEAK": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 24,
            }]
        },
        "WINDOW_2": {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 10,
                "toHour": 14,
            }]
        },
    }

    assert find_matching_tou_period(
        periods,
        datetime(2026, 5, 1, 11, 0),
        buy_rates={"PARTIAL_PEAK": 0.31, "WINDOW_2": 0.0},
        sell_rates={"PARTIAL_PEAK": 0.0, "WINDOW_2": 0.0},
    ) == "WINDOW_2"
