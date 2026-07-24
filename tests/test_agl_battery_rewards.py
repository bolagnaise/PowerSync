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
    assert 'provider == "agl"' in config_source
    assert '"agl", "globird", "aemo_vpp", "other", "tou_only", "nz"' in runtime_source
    assert '"agl",' in optimizer_source
    assert '"agl",' in sensor_source


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
            "_tariff_offpeak_rate": 0.20,
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
        assert "17:00" in setup["description"]
        assert "21:00" in setup["description"]
        assert "VPP" in setup["description"]
