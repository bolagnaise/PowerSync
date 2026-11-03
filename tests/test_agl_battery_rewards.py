"""Tests for AGL Battery Rewards tariff modelling."""

from __future__ import annotations

from copy import deepcopy
import ast
import importlib
import json
import sys
import textwrap
import types
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_MISSING = object()


@pytest.fixture()
def agl_module():
    module_names = (
        "power_sync",
        "power_sync.const",
        "power_sync.agl",
        "power_sync.tariff_time",
    )
    saved = {
        name: sys.modules.get(name, _MISSING)
        for name in module_names
    }
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = package
    sys.modules.pop("power_sync.const", None)
    sys.modules.pop("power_sync.agl", None)
    sys.modules.pop("power_sync.tariff_time", None)
    try:
        yield importlib.import_module("power_sync.agl")
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _tariff(
    periods: dict[str, list[dict]],
    buy_rates: dict[str, float],
) -> dict:
    return {
        "name": "AGL Battery Rewards",
        "utility": "AGL",
        "currency": "AUD",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": periods,
            }
        },
        "energy_charges": {"All Year": buy_rates},
        "sell_tariff": {
            "energy_charges": {
                "All Year": {name: 0.03 for name in periods}
            }
        },
    }


def _legacy_ausgrid_tariff(agl, *, overlay: bool = False) -> dict:
    """Build the exact two-season tariff emitted before the calendar repair."""
    tariff = {
        "name": "AGL / Ausgrid Seasonal TOU",
        "utility": "AGL",
        "currency": "AUD",
        "description": "user metadata",
        "seasons": {
            "Summer": {
                "fromMonth": 11,
                "toMonth": 3,
                "tou_periods": {
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
            },
            "Winter": {
                "fromMonth": 4,
                "toMonth": 10,
                "tou_periods": {
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
            },
        },
        "energy_charges": {
            "Summer": {"PEAK": 0.51, "OFF_PEAK": 0.21},
            "Winter": {"PEAK": 0.47, "OFF_PEAK": 0.19},
        },
        "sell_tariff": {
            "energy_charges": {
                "Summer": {"PEAK": 0.0, "OFF_PEAK": 0.0},
                "Winter": {"PEAK": 0.0, "OFF_PEAK": 0.0},
            }
        },
        "power_sync_explicit_periods": [
            {
                "name": "PEAK",
                "season": "Summer",
                "start": 14,
                "end": 20,
                "days": "weekdays",
                "import_rate": 0.51,
                "export_rate": 0.0,
            },
            {
                "name": "OFF_PEAK",
                "season": "Summer",
                "start": 0,
                "end": 24,
                "days": "all_days",
                "import_rate": 0.21,
                "export_rate": 0.0,
            },
            {
                "name": "PEAK",
                "season": "Winter",
                "start": 17,
                "end": 21,
                "days": "weekdays",
                "import_rate": 0.47,
                "export_rate": 0.0,
            },
            {
                "name": "OFF_PEAK",
                "season": "Winter",
                "start": 0,
                "end": 24,
                "days": "all_days",
                "import_rate": 0.19,
                "export_rate": 0.0,
            },
        ],
    }
    if overlay:
        tariff = agl.apply_battery_rewards_export_rates(
            tariff,
            peak_export_rate=0.28,
            offpeak_export_rate=0.03,
            start_hour=17,
            end_hour=21,
        )
    return tariff


def test_all_day_tariff_is_split_at_reward_boundaries_without_mutation(
    agl_module,
):
    agl = agl_module
    original = _tariff(
        {
            "ALL": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ]
        },
        {"ALL": 0.31},
    )
    before = deepcopy(original)

    result = agl.apply_battery_rewards_export_rates(
        original,
        peak_export_rate=0.28,
        offpeak_export_rate=0.03,
    )

    assert original == before
    periods = result["seasons"]["All Year"]["tou_periods"]
    assert {item["fromHour"] for item in periods["ALL"]} == {0, 21}
    assert {item["toHour"] for item in periods["ALL"]} == {17, 24}
    assert {
        (item["fromHour"], item["toHour"])
        for item in periods["ALL_AGL_REWARD"]
    } == {(17, 21)}
    assert result["energy_charges"]["All Year"] == {
        "ALL": 0.31,
        "ALL_AGL_REWARD": 0.31,
    }
    assert result["sell_tariff"]["energy_charges"]["All Year"] == {
        "ALL": 0.03,
        "ALL_AGL_REWARD": 0.28,
    }

    tariff_time = importlib.import_module("power_sync.tariff_time")
    assert (
        tariff_time.find_matching_tou_period(
            periods, datetime(2026, 7, 20, 16, 59)
        )
        == "ALL"
    )
    assert (
        tariff_time.find_matching_tou_period(
            periods, datetime(2026, 7, 20, 17, 0)
        )
        == "ALL_AGL_REWARD"
    )
    assert (
        tariff_time.find_matching_tou_period(
            periods, datetime(2026, 7, 20, 21, 0)
        )
        == "ALL"
    )


def test_import_period_price_is_preserved_across_evening_split(agl_module):
    agl = agl_module
    original = _tariff(
        {
            "PEAK": [
                {
                    "fromDayOfWeek": 1,
                    "toDayOfWeek": 5,
                    "fromHour": 15,
                    "toHour": 22,
                }
            ]
        },
        {"PEAK": 0.51},
    )

    result = agl.apply_battery_rewards_export_rates(
        original,
        peak_export_rate=0.28,
        offpeak_export_rate=0.03,
    )

    assert result["energy_charges"]["All Year"]["PEAK"] == 0.51
    assert result["energy_charges"]["All Year"]["PEAK_AGL_REWARD"] == 0.51
    assert {
        (item["fromHour"], item["toHour"])
        for item in result["seasons"]["All Year"]["tou_periods"]["PEAK"]
    } == {(15, 17), (21, 22)}


def test_overnight_period_keeps_next_day_import_coverage(agl_module):
    agl = agl_module
    original = _tariff(
        {
            "OFF_PEAK": [
                {
                    "fromDayOfWeek": 1,
                    "toDayOfWeek": 5,
                    "fromHour": 22,
                    "toHour": 7,
                }
            ]
        },
        {"OFF_PEAK": 0.18},
    )

    result = agl.apply_battery_rewards_export_rates(
        original,
        peak_export_rate=0.28,
        offpeak_export_rate=0.03,
    )
    ranges = result["seasons"]["All Year"]["tou_periods"]["OFF_PEAK"]

    assert any(
        item["fromDayOfWeek"] == 1
        and item["fromHour"] == 22
        and item["toHour"] == 24
        for item in ranges
    )
    assert any(
        item["fromDayOfWeek"] == 6
        and item["fromHour"] == 0
        and item["toHour"] == 7
        for item in ranges
    )
    assert "OFF_PEAK_AGL_REWARD" not in result["seasons"]["All Year"]["tou_periods"]


def test_reapplying_rates_is_idempotent_and_uses_original_import_tariff(
    agl_module,
):
    agl = agl_module
    original = _tariff(
        {
            "ALL": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ]
        },
        {"ALL": 0.31},
    )
    first = agl.apply_battery_rewards_export_rates(
        original,
        peak_export_rate=0.28,
        offpeak_export_rate=0.03,
    )

    updated = agl.apply_battery_rewards_export_rates(
        first,
        peak_export_rate=0.30,
        offpeak_export_rate=0.04,
    )

    periods = updated["seasons"]["All Year"]["tou_periods"]
    assert set(periods) == {"ALL", "ALL_AGL_REWARD"}
    assert updated["sell_tariff"]["energy_charges"]["All Year"] == {
        "ALL": 0.04,
        "ALL_AGL_REWARD": 0.30,
    }
    assert updated["agl_base_tariff"] == original


def test_opt_in_ausgrid_schedule_preserves_each_seasons_import_prices(
    agl_module,
):
    seasons, periods = agl_module.build_ausgrid_seasonal_import_periods(
        summer_offpeak_rate=0.21,
        summer_peak_rate=0.51,
        winter_offpeak_rate=0.19,
        winter_peak_rate=0.47,
    )

    assert seasons == [
        {"name": "Summer", "from_month": 11, "to_month": 3},
        {"name": "Autumn", "from_month": 4, "to_month": 5},
        {"name": "Winter", "from_month": 6, "to_month": 8},
        {"name": "Spring", "from_month": 9, "to_month": 10},
    ]
    assert [
        (period["season"], period["name"], period["import_rate"])
        for period in periods
    ] == [
        ("Summer", "PEAK", 0.51),
        ("Summer", "OFF_PEAK", 0.21),
        ("Autumn", "OFF_PEAK", 0.19),
        ("Winter", "PEAK", 0.47),
        ("Winter", "OFF_PEAK", 0.19),
        ("Spring", "OFF_PEAK", 0.19),
    ]

    # The same user-entered import values survive the Battery Rewards overlay.
    tariff = {
        "seasons": {
            "Summer": {"fromMonth": 11, "toMonth": 3, "tou_periods": {
                "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}],
            }},
            "Autumn": {"fromMonth": 4, "toMonth": 5, "tou_periods": {
                "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}],
            }},
            "Winter": {"fromMonth": 6, "toMonth": 8, "tou_periods": {
                "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}],
            }},
            "Spring": {"fromMonth": 9, "toMonth": 10, "tou_periods": {
                "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}],
            }},
        },
        "energy_charges": {
            "Summer": {"OFF_PEAK": 0.21},
            "Autumn": {"OFF_PEAK": 0.19},
            "Winter": {"OFF_PEAK": 0.19},
            "Spring": {"OFF_PEAK": 0.19},
        },
        "sell_tariff": {"energy_charges": {
            "Summer": {"OFF_PEAK": 0.03},
            "Autumn": {"OFF_PEAK": 0.03},
            "Winter": {"OFF_PEAK": 0.03},
            "Spring": {"OFF_PEAK": 0.03},
        }},
    }
    overlaid = agl_module.apply_battery_rewards_export_rates(
        tariff, peak_export_rate=0.28, offpeak_export_rate=0.03,
    )
    assert overlaid["energy_charges"]["Summer"]["OFF_PEAK"] == 0.21
    assert overlaid["energy_charges"]["Winter"]["OFF_PEAK"] == 0.19


def test_ausgrid_calendar_covers_all_months_and_local_peak_boundaries(agl_module):
    agl = agl_module
    seasons, periods = agl.build_ausgrid_seasonal_import_periods(
        summer_offpeak_rate=0.21,
        summer_peak_rate=0.51,
        winter_offpeak_rate=0.19,
        winter_peak_rate=0.47,
    )
    tariff_time = importlib.import_module("power_sync.tariff_time")
    season_map = {
        season["name"]: {
            "fromMonth": season["from_month"],
            "toMonth": season["to_month"],
        }
        for season in seasons
    }
    assert [
        tariff_time.find_season_for_month(season_map, month)
        for month in range(1, 13)
    ] == [
        "Summer",
        "Summer",
        "Summer",
        "Autumn",
        "Autumn",
        "Winter",
        "Winter",
        "Winter",
        "Spring",
        "Spring",
        "Summer",
        "Summer",
    ]

    # Evaluate the actual authored rows, including a full week in every month.
    # Handwritten expected windows here would miss a wrong builder hour/day.
    for month in range(1, 13):
        season = tariff_time.find_season_for_month(season_map, month)
        rows = [row for row in periods if row["season"] == season]
        windows = {
            row["name"]: [{
                "fromDayOfWeek": 0 if row["days"] == "all_days" else 1,
                "toDayOfWeek": 6 if row["days"] == "all_days" else 5,
                "fromHour": row["start"], "toHour": row["end"],
            }]
            for row in rows
        }
        rates = {row["name"]: row["import_rate"] for row in rows}
        for day in range(1, 8):
            for hour, minute in [(14, 59), (15, 0), (20, 59), (21, 0)]:
                when = datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo("Australia/Sydney"))
                peak = month in (11, 12, 1, 2, 3, 6, 7, 8) and 15 <= hour < 21
                matched = tariff_time.find_matching_tou_period(windows, when, buy_rates=rates)
                assert matched == ("PEAK" if peak else "OFF_PEAK"), when
                expected = (0.51 if peak else 0.21) if month in (11, 12, 1, 2, 3) else (0.47 if peak else 0.19)
                assert rates[matched] == expected, when


def test_repair_legacy_raw_ausgrid_tariff_preserves_rates_metadata_and_identity(
    agl_module,
):
    original = _legacy_ausgrid_tariff(agl_module)
    before = deepcopy(original)

    repaired = agl_module.repair_legacy_ausgrid_seasonal_import_tariff(original)

    assert repaired is not original
    assert original == before
    assert repaired["description"] == "user metadata"
    assert list(repaired["seasons"]) == ["Summer", "Autumn", "Winter", "Spring"]
    assert {
        name: (season["fromMonth"], season["toMonth"])
        for name, season in repaired["seasons"].items()
    } == {
        "Summer": (11, 3),
        "Autumn": (4, 5),
        "Winter": (6, 8),
        "Spring": (9, 10),
    }
    assert repaired["energy_charges"] == {
        "Summer": {"PEAK": 0.51, "OFF_PEAK": 0.21},
        "Autumn": {"OFF_PEAK": 0.19},
        "Winter": {"PEAK": 0.47, "OFF_PEAK": 0.19},
        "Spring": {"OFF_PEAK": 0.19},
    }
    assert repaired["sell_tariff"]["energy_charges"] == {
        "Summer": {"PEAK": 0.0, "OFF_PEAK": 0.0},
        "Autumn": {"OFF_PEAK": 0.0},
        "Winter": {"PEAK": 0.0, "OFF_PEAK": 0.0},
        "Spring": {"OFF_PEAK": 0.0},
    }
    assert repaired["power_sync_explicit_periods"] == [
        {
            "name": "PEAK",
            "season": "Summer",
            "start": 15,
            "end": 21,
            "days": "all_days",
            "import_rate": 0.51,
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Summer",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": 0.21,
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Autumn",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": 0.19,
            "export_rate": 0.0,
        },
        {
            "name": "PEAK",
            "season": "Winter",
            "start": 15,
            "end": 21,
            "days": "all_days",
            "import_rate": 0.47,
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Winter",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": 0.19,
            "export_rate": 0.0,
        },
        {
            "name": "OFF_PEAK",
            "season": "Spring",
            "start": 0,
            "end": 24,
            "days": "all_days",
            "import_rate": 0.19,
            "export_rate": 0.0,
        },
    ]


def test_repair_legacy_overlay_preserves_rewards_and_rejects_edits(agl_module):
    original = _legacy_ausgrid_tariff(agl_module, overlay=True)
    before = deepcopy(original)

    repaired = agl_module.repair_legacy_ausgrid_seasonal_import_tariff(original)

    assert repaired is not original
    assert original == before
    assert repaired["agl_battery_rewards"] == {
        "peak_export_rate": 0.28,
        "offpeak_export_rate": 0.03,
        "start_hour": 17,
        "end_hour": 21,
    }
    assert repaired["power_sync_explicit_periods"] == repaired["agl_base_tariff"]["power_sync_explicit_periods"]
    assert repaired["agl_base_tariff"]["seasons"]["Summer"]["fromMonth"] == 11
    assert set(repaired["agl_base_tariff"]["seasons"]) == {
        "Summer",
        "Autumn",
        "Winter",
        "Spring",
    }
    expected = agl_module.apply_battery_rewards_export_rates(
        repaired["agl_base_tariff"],
        peak_export_rate=0.28,
        offpeak_export_rate=0.03,
    )
    assert repaired["seasons"] == expected["seasons"]
    assert repaired["energy_charges"] == expected["energy_charges"]
    assert repaired["sell_tariff"] == expected["sell_tariff"]
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(repaired) is repaired

    edited = deepcopy(original)
    edited["seasons"]["Summer"]["tou_periods"]["PEAK"][0]["fromHour"] = 13
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(edited) is edited
    edited = deepcopy(original)
    edited["agl_base_tariff"]["power_sync_explicit_periods"][0]["start"] = 13
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(edited) is edited
    edited = deepcopy(original)
    edited["agl_battery_rewards"]["start_hour"] = 16
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(edited) is edited


def test_repair_skips_manual_or_ineligible_tariffs(agl_module):
    for tariff in (
        {"name": "AGL / Ausgrid Seasonal TOU", "utility": "Other"},
        {"name": "AGL / Ausgrid Seasonal TOU", "utility": "AGL", "seasons": {}},
    ):
        assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(tariff) is tariff

    edited = _legacy_ausgrid_tariff(agl_module)
    edited["power_sync_explicit_periods"][0]["start"] = 13
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(edited) is edited
    missing_rows = _legacy_ausgrid_tariff(agl_module)
    del missing_rows["power_sync_explicit_periods"]
    repaired = agl_module.repair_legacy_ausgrid_seasonal_import_tariff(missing_rows)
    assert repaired is not missing_rows
    assert repaired["power_sync_explicit_periods"][0]["start"] == 15


@pytest.mark.asyncio
async def test_async_migrate_uses_saved_source_and_persists_repair(agl_module):
    saved = _legacy_ausgrid_tariff(agl_module)
    initial = _legacy_ausgrid_tariff(agl_module)
    saved["energy_charges"]["Summer"]["PEAK"] = 0.61
    saved["power_sync_explicit_periods"][0]["import_rate"] = 0.61
    initial["energy_charges"]["Summer"]["PEAK"] = 0.71
    initial["power_sync_explicit_periods"][0]["import_rate"] = 0.71
    entry = SimpleNamespace(
        options={"agl_import_schedule": "ausgrid_seasonal"},
        data={
            "agl_import_schedule": "manual",
            "initial_custom_tariff": initial,
        },
    )

    class Store:
        def __init__(self, tariff):
            self.tariff = tariff
            self.saved = 0
            self.set_values = []

        def get_custom_tariff(self):
            return self.tariff

        def set_custom_tariff(self, tariff):
            self.set_values.append(tariff)
            self.tariff = tariff

        async def async_save(self):
            self.saved += 1

    store = Store(saved)
    original_entry = deepcopy(entry.__dict__)
    assert await agl_module.async_migrate_ausgrid_seasonal_import_tariff(entry, store)
    assert store.saved == 1
    assert len(store.set_values) == 1
    assert store.tariff["energy_charges"]["Summer"]["PEAK"] == 0.61
    assert entry.__dict__ == original_entry


@pytest.mark.asyncio
async def test_async_migrate_falls_back_to_initial_and_respects_selection(agl_module):
    initial = _legacy_ausgrid_tariff(agl_module)

    class Store:
        def __init__(self):
            self.saved = 0
            self.value = None

        def get_custom_tariff(self):
            return None

        def set_custom_tariff(self, tariff):
            self.value = tariff

        async def async_save(self):
            self.saved += 1

    store = Store()
    seasonal_entry = SimpleNamespace(
        options={},
        data={
            "agl_import_schedule": "ausgrid_seasonal",
            "initial_custom_tariff": initial,
        },
    )
    assert await agl_module.async_migrate_ausgrid_seasonal_import_tariff(
        seasonal_entry, store
    )
    assert store.saved == 1
    assert set(store.value["seasons"]) == {"Summer", "Autumn", "Winter", "Spring"}

    for entry in (
        SimpleNamespace(options={"agl_import_schedule": "manual"}, data={}),
        SimpleNamespace(options={}, data={}),
        SimpleNamespace(
            options={"agl_import_schedule": "manual"},
            data={"agl_import_schedule": "ausgrid_seasonal", "initial_custom_tariff": initial},
        ),
    ):
        assert not await agl_module.async_migrate_ausgrid_seasonal_import_tariff(
            entry, store
        )


@pytest.mark.parametrize(
    ("peak", "offpeak"),
    [(-0.01, 0.03), (2.01, 0.03), (0.28, -0.01), (0.28, 2.01)],
)
def test_invalid_rates_fail_closed(agl_module, peak: float, offpeak: float):
    agl = agl_module
    original = _tariff({}, {})

    with pytest.raises(ValueError):
        agl.apply_battery_rewards_export_rates(
            original,
            peak_export_rate=peak,
            offpeak_export_rate=offpeak,
        )


@pytest.mark.parametrize("day_field", ["fromDay", "toDay"])
def test_ausgrid_repair_preserves_edited_season_day_boundaries(agl_module, day_field):
    tariff = _legacy_ausgrid_tariff(agl_module)
    tariff["seasons"]["Summer"][day_field] = 2
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(tariff) is tariff


@pytest.mark.parametrize("invalid", [17.5, "17", True, float("inf"), float("nan")])
def test_ausgrid_repair_rejects_edited_reward_hours(agl_module, invalid):
    tariff = _legacy_ausgrid_tariff(agl_module, overlay=True)
    tariff["agl_battery_rewards"]["start_hour"] = invalid
    assert agl_module.repair_legacy_ausgrid_seasonal_import_tariff(tariff) is tariff


@pytest.mark.asyncio
async def test_ausgrid_migration_accepts_immutable_entry_mappings(agl_module):
    source = _legacy_ausgrid_tariff(agl_module, overlay=True)
    class Store:
        def get_custom_tariff(self):
            return source
        def set_custom_tariff(self, tariff):
            self.value = tariff
        async def async_save(self):
            self.saved = True
    store = Store()
    entry = SimpleNamespace(
        options=MappingProxyType({"agl_import_schedule": "ausgrid_seasonal"}),
        data=MappingProxyType({}),
    )
    assert await agl_module.async_migrate_ausgrid_seasonal_import_tariff(entry, store)
    assert store.saved is True
    assert store.value["agl_base_tariff"]["seasons"]["Winter"]["fromMonth"] == 6


@pytest.mark.asyncio
async def test_ausgrid_migration_does_not_overwrite_unreadable_saved_tariff(agl_module):
    class Store:
        def get_custom_tariff(self):
            raise RuntimeError("unreadable saved tariff")
    entry = SimpleNamespace(options={"agl_import_schedule": "ausgrid_seasonal"},
                            data={"initial_custom_tariff": _legacy_ausgrid_tariff(agl_module)})
    assert not await agl_module.async_migrate_ausgrid_seasonal_import_tariff(entry, Store())


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["manual", "ausgrid_seasonal"])
async def test_agl_startup_honors_fresh_setup_over_retained_store(agl_module, selection):
    """Removing/readding an entry retains AutomationStore, not its old tariff choice."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    prepare = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.ImportFrom) and child.module == "agl"
            and any(alias.name.startswith("async_") for alias in child.names)
            for child in node.body
        )
    )
    function = ast.AsyncFunctionDef(
        name="prepare_tariff", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[prepare], decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    initial = agl_module.repair_legacy_ausgrid_seasonal_import_tariff(
        _legacy_ausgrid_tariff(agl_module, overlay=True)
    )
    if selection == "manual":
        initial["name"] = "Fresh manual tariff"
    old = {"name": "Tariff retained from previous installation"}
    class Store:
        def __init__(self):
            self.tariff, self.saved = old, 0
        def get_custom_tariff(self):
            return self.tariff
        def set_custom_tariff(self, tariff):
            self.tariff = tariff
        async def async_save(self):
            self.saved += 1
    store = Store()
    namespace = {
        "__package__": "power_sync", "electricity_provider": "agl",
        "entry": SimpleNamespace(options={"agl_import_schedule": selection},
                                 data={"initial_custom_tariff": initial}),
        "automation_store": store,
        "_LOGGER": SimpleNamespace(info=lambda *_: None),
    }
    exec(compile(module, "<agl-startup-preparation>", "exec"), namespace)
    await namespace["prepare_tariff"]()
    assert store.tariff == initial
    assert store.saved == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_agl_startup_preparation_keeps_legacy_upgrade_guards(agl_module, edited):
    initial = _legacy_ausgrid_tariff(agl_module, overlay=True)
    saved = deepcopy(initial)
    if edited:
        saved["seasons"]["Summer"]["tou_periods"]["PEAK"][0]["fromHour"] = 16
    before = deepcopy(saved)
    class Store:
        def __init__(self):
            self.tariff, self.saves = saved, 0
        def get_custom_tariff(self):
            return self.tariff
        def set_custom_tariff(self, tariff):
            self.tariff = tariff
        async def async_save(self):
            self.saves += 1
    store = Store()
    entry = SimpleNamespace(options=MappingProxyType({"agl_import_schedule": "ausgrid_seasonal"}),
                            data=MappingProxyType({"initial_custom_tariff": initial}))
    migrated = await agl_module.async_prepare_agl_startup_tariff(entry, store)
    assert saved == before
    assert migrated is (not edited)
    assert store.saves == (0 if edited else 1)
    if edited:
        assert store.tariff is saved
    else:
        assert set(store.tariff["seasons"]) == {"Summer", "Autumn", "Winter", "Spring"}
    assert entry.data["initial_custom_tariff"] is initial


def test_agl_is_wired_as_a_first_class_static_provider():
    const_source = (COMPONENT_ROOT / "const.py").read_text()
    config_source = (COMPONENT_ROOT / "config_flow.py").read_text()
    runtime_source = (COMPONENT_ROOT / "__init__.py").read_text()
    optimizer_source = (
        COMPONENT_ROOT / "optimization" / "coordinator.py"
    ).read_text()
    sensor_source = (COMPONENT_ROOT / "sensor.py").read_text()

    assert '"agl": "AGL Battery Rewards' in const_source
    assert 'elif provider == "agl":' in config_source
    assert "return await self.async_step_agl()" in config_source
    assert "CONF_DAILY_SUPPLY_CHARGE: daily_supply_charge" in config_source
    assert "CONF_MONTHLY_SUPPLY_CHARGE: monthly_supply_charge" in config_source
    assert 'provider == "agl"' in config_source
    assert '"agl", "globird", "aemo_vpp", "other", "tou_only", "nz"' in runtime_source
    assert '"agl",' in optimizer_source
    assert '"agl",' in sensor_source


def test_mobile_custom_tariff_api_reapplies_agl_overlay():
    init_path = COMPONENT_ROOT / "__init__.py"
    source = init_path.read_text()
    tree = ast.parse(source)
    post_method = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CustomTariffView"
        for item in node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "post"
    )
    post_source = ast.get_source_segment(source, post_method)

    assert post_source is not None
    assert "tariff_season_validation_error(data.get(\"seasons\"))" in post_source
    assert 'if provider == "agl":' in post_source
    assert "data = apply_battery_rewards_export_rates(" in post_source
    assert "store.set_custom_tariff(data)" in post_source


def test_config_tariff_builder_applies_agl_overlay(agl_module):
    config_path = COMPONENT_ROOT / "config_flow.py"
    tree = ast.parse(config_path.read_text())
    method = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PowerSyncConfigFlow"
        for item in node.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_build_tariff_from_periods"
    )
    namespace = {
        "__name__": "power_sync.config_flow_builder_test",
        "__package__": "power_sync",
        "normalize_currency": lambda value, fallback: value or fallback,
        "currency_for_provider": lambda provider, hass: "AUD",
        "DEFAULT_AGL_BATTERY_REWARDS_PEAK_EXPORT_RATE": 28.0,
        "DEFAULT_AGL_BATTERY_REWARDS_OFFPEAK_EXPORT_RATE": 3.0,
    }
    exec(textwrap.dedent(ast.get_source_segment(config_path.read_text(), method)), namespace)
    ctx = type(
        "Ctx",
        (),
        {
            "_tariff_offpeak_rate": 0.0663,
            "_tariff_fit_rate": 0.03,
            "_tariff_plan_name": "AGL Battery Rewards",
            "_selected_electricity_provider": "agl",
            "_tariff_currency": "AUD",
            "_agl_peak_export_rate": 28.0,
            "_agl_offpeak_export_rate": 3.0,
            "hass": None,
        },
    )()

    tariff = namespace["_build_tariff_from_periods"](
        ctx,
        [
            {
                "name": "PEAK",
                "start": 15,
                "end": 22,
                "days": "all_days",
                "import_rate": 0.50,
                "export_rate": 0.03,
            }
        ],
    )

    rates = tariff["sell_tariff"]["energy_charges"]["All Year"]
    assert rates["PEAK"] == 0.03
    assert rates["PEAK_AGL_REWARD"] == 0.28
    assert tariff["energy_charges"]["All Year"]["OFF_PEAK"] == 0.0663
    assert (
        tariff["agl_base_tariff"]["energy_charges"]["All Year"]["OFF_PEAK"]
        == 0.0663
    )
    assert tariff["agl_base_tariff"]["power_sync_explicit_periods"] == [
        {
            "name": "PEAK",
            "start": 15,
            "end": 22,
            "days": "all_days",
            "import_rate": 0.50,
            "export_rate": 0.03,
        }
    ]

    reopened_offpeak = round(
        tariff["energy_charges"]["All Year"]["OFF_PEAK"] * 100,
        2,
    )
    ctx._tariff_offpeak_rate = reopened_offpeak / 100
    reopened = namespace["_build_tariff_from_periods"](
        ctx,
        [
            {
                "name": "PEAK",
                "start": 15,
                "end": 22,
                "days": "all_days",
                "import_rate": 0.50,
                "export_rate": 0.03,
            }
        ],
    )

    assert reopened_offpeak == 6.63
    assert reopened["energy_charges"]["All Year"]["OFF_PEAK"] == 0.0663
    assert tariff["utility"] == "AGL"


def test_agl_setup_and_options_strings_stay_aligned():
    strings = json.loads((COMPONENT_ROOT / "strings.json").read_text())
    translations = json.loads(
        (COMPONENT_ROOT / "translations" / "en.json").read_text()
    )

    for payload in (strings, translations):
        setup = payload["config"]["step"]["agl"]
        options = payload["options"]["step"]["agl_options"]
        assert setup["data"] == options["data"]
        assert setup["data_description"] == options["data_description"]
        assert setup["data"]["daily_supply_charge"] == "Daily supply charge ($)"
        assert setup["data"]["monthly_supply_charge"] == "Monthly supply charge ($)"
        assert "supply charge" in setup["description"]
        assert "supply charge" in options["description"]
        assert "17:00" in setup["description"]
        assert "21:00" in setup["description"]
        assert "VPP" in setup["description"]
