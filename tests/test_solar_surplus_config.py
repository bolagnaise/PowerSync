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


# --- Shared surplus reserves -----------------------------------------------------

from power_sync.solar_surplus_config import (  # noqa: E402
    forecast_ev_surplus_kw,
    get_household_buffer_kw,
    get_parallel_charge_rate_kw,
    parallel_battery_reserve_kw,
)


def test_household_buffer_defaults_and_rejects_bad_values():
    assert get_household_buffer_kw(None) == 0.5
    assert get_household_buffer_kw({"household_buffer_kw": 1.2}) == 1.2
    assert get_household_buffer_kw({"household_buffer_kw": -3}) == 0.0
    assert get_household_buffer_kw({"household_buffer_kw": "bad"}) == 0.5
    assert get_household_buffer_kw({"household_buffer_kw": float("nan")}) == 0.5


def test_parallel_charge_rate_only_applies_when_enabled():
    assert get_parallel_charge_rate_kw({"max_battery_charge_rate_kw": 3}) == 0.0
    on = {"allow_parallel_charging": True}
    assert get_parallel_charge_rate_kw(on) == 5.0
    assert get_parallel_charge_rate_kw({**on, "max_battery_charge_rate_kw": 3}) == 3.0
    assert get_parallel_charge_rate_kw({**on, "max_battery_charge_rate_kw": 0}) == 0.0
    assert get_parallel_charge_rate_kw({**on, "max_battery_charge_rate_kw": None}) == 0.0
    assert get_parallel_charge_rate_kw({**on, "max_battery_charge_rate_kw": "bad"}) == 5.0


def test_live_parallel_reserve_follows_battery_state():
    config = {"allow_parallel_charging": True, "max_battery_charge_rate_kw": 4.0, "min_battery_soc": 80}
    assert parallel_battery_reserve_kw(config, 50, 0.0, "grid_based") == 4.0  # below minimum
    assert parallel_battery_reserve_kw(config, 90, 0.0, "grid_based") == 0.0  # idle above minimum
    assert parallel_battery_reserve_kw(config, 90, 1.0, "grid_based") == 4.0  # charging
    assert parallel_battery_reserve_kw(config, 90, 1.0, "direct") == 3.0      # tops up measured charge
    assert parallel_battery_reserve_kw(config, 99, 1.0, "grid_based") == 0.0  # full
    assert parallel_battery_reserve_kw({}, 50, 1.0, "grid_based") == 0.0      # disabled


def test_forecast_surplus_withholds_buffer_and_parallel_rate():
    assert forecast_ev_surplus_kw(4.0, 1.0, None) == 2.5
    config = {"household_buffer_kw": 0.0, "allow_parallel_charging": True, "max_battery_charge_rate_kw": 2.0}
    assert forecast_ev_surplus_kw(4.0, 1.0, config) == 1.0
    assert forecast_ev_surplus_kw(1.0, 2.0, None) == 0.0
