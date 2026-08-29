"""Tests for shared EV solar-surplus configuration helpers."""

from __future__ import annotations

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

from power_sync.solar_surplus_config import (  # noqa: E402
    DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS,
    DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
    get_stored_solar_surplus_config,
    get_solar_surplus_max_export_price_cents,
    get_solar_surplus_min_battery_soc,
    normalize_solar_surplus_config,
    solar_surplus_price_allows_charging,
)


def test_default_min_battery_soc_is_80_percent():
    assert get_solar_surplus_min_battery_soc({}) == DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC


def test_home_battery_minimum_sets_runtime_threshold():
    config = normalize_solar_surplus_config({"home_battery_minimum": 40})

    assert config["home_battery_minimum"] == 40
    assert config["min_battery_soc"] == 40
    assert get_solar_surplus_min_battery_soc(config) == 40


def test_min_battery_soc_alias_is_accepted_for_new_clients():
    config = normalize_solar_surplus_config({"min_battery_soc": "35"})

    assert config["home_battery_minimum"] == 35
    assert config["min_battery_soc"] == 35


def test_min_battery_soc_is_clamped_to_percentage_range():
    assert get_solar_surplus_min_battery_soc({"home_battery_minimum": -5}) == 0
    assert get_solar_surplus_min_battery_soc({"home_battery_minimum": 140}) == 100


def test_invalid_primary_threshold_falls_back_to_alias():
    assert get_solar_surplus_min_battery_soc({
        "home_battery_minimum": "nope",
        "min_battery_soc": 45,
    }) == 45


def test_stored_solar_config_prefers_automation_store():
    store = types.SimpleNamespace(
        _data={
            "solar_surplus_config": {
                "household_buffer_kw": 1.5,
                "home_battery_minimum": 20,
                "allow_parallel_charging": True,
                "max_battery_charge_rate_kw": 3.0,
            }
        }
    )
    config = get_stored_solar_surplus_config({
        "automation_store": store,
        "solar_surplus_config": {"household_buffer_kw": 0.5},
    })

    assert config["household_buffer_kw"] == 1.5
    assert config["home_battery_minimum"] == 20
    assert config["min_battery_soc"] == 20
    assert config["allow_parallel_charging"] is True
    assert config["max_battery_charge_rate_kw"] == 3.0


def test_stored_solar_config_falls_back_to_entry_data():
    config = get_stored_solar_surplus_config({
        "solar_surplus_config": {
            "household_buffer_kw": 1.0,
            "min_battery_soc": 35,
        }
    })

    assert config["household_buffer_kw"] == 1.0
    assert config["home_battery_minimum"] == 35
    assert config["min_battery_soc"] == 35


def test_default_export_opportunity_threshold_is_fifteen_cents():
    config = normalize_solar_surplus_config()

    assert config["max_export_price_cents"] == (
        DEFAULT_SOLAR_SURPLUS_MAX_EXPORT_PRICE_CENTS
    )
    assert get_solar_surplus_max_export_price_cents(config) == 15.0


def test_export_opportunity_threshold_is_normalized_and_clamped():
    assert get_solar_surplus_max_export_price_cents(
        {"max_export_price_cents": "20.5"}
    ) == 20.5
    assert get_solar_surplus_max_export_price_cents(
        {"max_export_price_cents": -5}
    ) == 0.0
    assert get_solar_surplus_max_export_price_cents(
        {"max_export_price_cents": 999}
    ) == 200.0


def test_solar_price_gate_blocks_high_or_missing_price_but_allows_deadline():
    config = {"max_export_price_cents": 15}

    assert solar_surplus_price_allows_charging(15.0, config) is True
    assert solar_surplus_price_allows_charging(20.0, config) is False
    assert solar_surplus_price_allows_charging(None, config) is False
    assert solar_surplus_price_allows_charging(
        20.0,
        config,
        deadline_override=True,
    ) is True
