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
    season_rate_maps,
    tariff_components_for_datetime,
    tesla_day_of_week,
    tou_period_matches,
)


def test_tesla_day_of_week_maps_sunday_to_zero():
    assert tesla_day_of_week(datetime(2026, 5, 3)) == 0
    assert tesla_day_of_week(datetime(2026, 5, 4)) == 1


def test_tariff_components_reselect_year_spanning_season_per_timestamp():
    all_day = {
        "OFF_PEAK": {"periods": [{
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
            "fromHour": 0,
            "toHour": 24,
        }]},
    }
    summer = {
        **all_day,
        "PEAK": {"periods": [{
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
            "fromHour": 15,
            "toHour": 21,
        }]},
    }
    tariff = {
        "tou_periods": all_day,
        "buy_rates": {"OFF_PEAK": 0.21},
        "sell_rates": {"OFF_PEAK": 0.05},
        "seasons": {
            "Shoulder": {"fromMonth": 9, "toMonth": 10, "tou_periods": all_day},
            "Summer": {"fromMonth": 11, "toMonth": 3, "tou_periods": summer},
        },
        "season_buy_rates": {
            "Shoulder": {"OFF_PEAK": 0.21},
            "Summer": {"OFF_PEAK": 0.21, "PEAK": 0.54},
        },
        "season_sell_rates": {
            "Shoulder": {"OFF_PEAK": 0.05},
            "Summer": {"OFF_PEAK": 0.05, "PEAK": 0.05},
        },
    }

    _, october_buy, _, october_season = tariff_components_for_datetime(
        tariff,
        datetime(2026, 10, 31, 15, 0, tzinfo=ZoneInfo("Australia/Sydney")),
    )
    november_tou, november_buy, _, november_season = tariff_components_for_datetime(
        tariff,
        datetime(2026, 11, 1, 15, 0, tzinfo=ZoneInfo("Australia/Sydney")),
    )

    assert october_season == "Shoulder"
    assert october_buy["OFF_PEAK"] == 0.21
    assert november_season == "Summer"
    assert find_matching_tou_period(
        november_tou,
        datetime(2026, 11, 1, 15, 0, tzinfo=ZoneInfo("Australia/Sydney")),
        buy_rates=november_buy,
    ) == "PEAK"
    assert november_buy["PEAK"] == 0.54


def test_specific_season_precedes_all_year_catch_all():
    tariff = {
        "seasons": {
            "All Year": {"fromMonth": 1, "toMonth": 12},
            "Winter": {"fromMonth": 6, "toMonth": 8},
        },
        "season_buy_rates": {
            "All Year": {"OFF_PEAK": 0.21},
            "Winter": {"OFF_PEAK": 0.44},
        },
    }

    _, buy_rates, _, season = tariff_components_for_datetime(
        tariff,
        datetime(2026, 7, 1, 12, 0),
    )

    assert season == "Winter"
    assert buy_rates["OFF_PEAK"] == 0.44


def test_season_rate_maps_accepts_direct_and_nested_tesla_rates():
    assert season_rate_maps({
        "Summer": {"rates": {"PEAK": 0.54}},
        "Shoulder": {"OFF_PEAK": 0.21},
    }) == {
        "Summer": {"PEAK": 0.54},
        "Shoulder": {"OFF_PEAK": 0.21},
    }


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


def test_sunday_only_period_does_not_match_friday():
    period = {
        "fromDayOfWeek": 0,
        "toDayOfWeek": 0,
        "fromHour": 16,
        "toHour": 17,
    }

    assert tou_period_matches(period, datetime(2026, 5, 3, 16, 30)) is True
    assert tou_period_matches(period, datetime(2026, 5, 1, 16, 30)) is False


def test_rate_aware_agl_reward_matching_keeps_weekday_peak_rate():
    periods = {
        "PEAK_AGL_REWARD": [{
            "fromDayOfWeek": 1,
            "toDayOfWeek": 5,
            "fromHour": 17,
            "toHour": 20,
        }],
        "OFF_PEAK_AUTO_AGL_REWARD": [{
            "fromDayOfWeek": 0,
            "toDayOfWeek": 0,
            "fromHour": 17,
            "toHour": 20,
        }],
    }

    assert find_matching_tou_period(
        periods,
        datetime(2026, 8, 7, 17, 30),
        buy_rates={
            "PEAK_AGL_REWARD": 0.4242,
            "OFF_PEAK_AUTO_AGL_REWARD": 0.3212,
        },
        sell_rates={
            "PEAK_AGL_REWARD": 0.28,
            "OFF_PEAK_AUTO_AGL_REWARD": 0.28,
        },
    ) == "PEAK_AGL_REWARD"
