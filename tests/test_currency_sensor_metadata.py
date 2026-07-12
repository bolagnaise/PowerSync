"""Sensor currency metadata tests."""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_sensor_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_const = types.ModuleType("homeassistant.const")
    ha_core = types.ModuleType("homeassistant.core")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_restore_state = types.ModuleType("homeassistant.helpers.restore_state")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    @dataclass
    class SensorEntityDescription:
        key: str
        name: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: Any | None = None
        state_class: Any | None = None
        suggested_display_precision: int | None = None
        icon: str | None = None

    class SensorEntity:
        pass

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class RestoreEntity:
        async def async_get_last_state(self):
            return None

    ha_sensor.SensorEntityDescription = SensorEntityDescription
    ha_sensor.SensorEntity = SensorEntity
    ha_sensor.SensorDeviceClass = SimpleNamespace(
        BATTERY="battery",
        CURRENT="current",
        DURATION="duration",
        ENERGY="energy",
        ENERGY_STORAGE="energy_storage",
        MONETARY="monetary",
        POWER="power",
        TEMPERATURE="temperature",
        TIMESTAMP="timestamp",
        VOLTAGE="voltage",
    )
    ha_sensor.SensorStateClass = SimpleNamespace(
        MEASUREMENT="measurement",
        TOTAL="total",
        TOTAL_INCREASING="total_increasing",
    )
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_const.UnitOfEnergy = SimpleNamespace(KILO_WATT_HOUR="kWh")
    ha_const.UnitOfPower = SimpleNamespace(KILO_WATT="kW")
    ha_const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C")
    ha_const.UnitOfTime = SimpleNamespace(HOURS="h")
    ha_const.PERCENTAGE = "%"
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_core.callback = lambda func: func
    ha_entity_platform.AddEntitiesCallback = Any
    ha_device_registry.async_get = lambda hass: getattr(
        hass, "device_registry", SimpleNamespace(devices={})
    )
    ha_entity_registry.async_get = lambda hass: getattr(
        hass, "entity_registry", SimpleNamespace(entities={})
    )
    ha_update.CoordinatorEntity = CoordinatorEntity
    ha_dispatcher.async_dispatcher_connect = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_restore_state.RestoreEntity = RestoreEntity
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ha_dt.as_local = lambda value: value
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ha_util.dt = ha_dt
    ha_helpers.entity_platform = ha_entity_platform
    ha_helpers.device_registry = ha_device_registry
    ha_helpers.entity_registry = ha_entity_registry
    ha_helpers.update_coordinator = ha_update
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.event = ha_event
    ha_helpers.restore_state = ha_restore_state
    ha_components.sensor = ha_sensor
    ha_root.components = ha_components
    ha_root.config_entries = ha_config_entries
    ha_root.const = ha_const
    ha_root.core = ha_core
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.sensor"] = ha_sensor
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.const"] = ha_const
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_platform"] = ha_entity_platform
    sys.modules["homeassistant.helpers.device_registry"] = ha_device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.event"] = ha_event
    sys.modules["homeassistant.helpers.restore_state"] = ha_restore_state
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(ROOT)]
    ps_module.get_current_price_from_tariff_schedule = lambda tariff: (25.0, 8.0, "PEAK")
    sys.modules["power_sync"] = ps_module

    coordinator = types.ModuleType("power_sync.coordinator")
    for name in (
        "AmberPriceCoordinator",
        "LocalvoltsPriceCoordinator",
        "OctopusPriceCoordinator",
        "TeslaEnergyCoordinator",
        "DemandChargeCoordinator",
        "SolcastForecastCoordinator",
    ):
        setattr(coordinator, name, type(name, (), {}))
    sys.modules["power_sync.coordinator"] = coordinator


def _sensor_module():
    _install_sensor_stubs()
    sys.modules.pop("power_sync.sensor", None)
    return importlib.import_module("power_sync.sensor")


def _entry(provider: str):
    return SimpleNamespace(entry_id="entry-1", data={}, options={"electricity_provider": provider})


def _hass(currency: str):
    return SimpleNamespace(config=SimpleNamespace(currency=currency), data={})


def test_gbp_price_sensor_uses_rate_unit_without_monetary_device_class():
    sensor = _sensor_module()
    desc = next(d for d in sensor.PRICE_SENSORS if d.key == "current_import_price")
    entity = sensor.AmberPriceSensor(
        SimpleNamespace(data={"current": [{"channelType": "general", "perKwh": 25.0}]}),
        desc,
        _entry("octopus"),
    )
    entity.hass = _hass("AUD")

    assert entity.native_value == 0.25
    assert entity.native_unit_of_measurement == "GBP/kWh"
    assert entity.device_class is None
    assert entity.extra_state_attributes["currency"] == "GBP"
    assert entity.extra_state_attributes["minor_price_unit"] == "p/kWh"


def test_aud_monetary_total_keeps_monetary_device_class_and_value():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "daily_import_cost")
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"energy_summary": {"import_cost_today": 1.23}}),
        desc,
        _entry("amber"),
    )
    entity.hass = _hass("GBP")

    assert entity.native_value == 1.23
    assert entity.native_unit_of_measurement == "AUD"
    assert entity.device_class == "monetary"
    assert entity.extra_state_attributes["currency"] == "AUD"


def test_daily_cost_uses_restored_numeric_state_while_energy_summary_is_missing():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "daily_import_cost")
    entity = sensor.TeslaEnergySensor(SimpleNamespace(data={}), desc, _entry("amber"))
    entity.hass = _hass("AUD")
    entity._restored_native_value = 4.56

    assert entity.native_value == 4.56


def test_flow_power_import_price_uses_restored_state_before_coordinator_data():
    sensor = _sensor_module()
    entity = sensor.FlowPowerPriceSensor(
        SimpleNamespace(data=None),
        _entry("flow_power"),
        "current_import_price",
    )
    entity.hass = _hass("AUD")
    entity._restored_native_value = 0.321

    assert entity.native_value == 0.321


def test_tariff_schedule_attributes_convert_high_tesla_rates_to_cents():
    sensor = _sensor_module()
    entity = sensor.TariffScheduleSensor(_hass("AUD"), _entry("globird"))

    entity._rebuild_schedule_cache(
        {
            "last_sync": "2026-07-10 16:53:49",
            "utility": "GloBird",
            "plan_name": "Zero Hero",
            "current_season": "Summer",
            "buy_rates": {
                "ON_PEAK": 10.0,
                "OFF_PEAK": 0.52,
            },
            "sell_rates": {
                "ON_PEAK": 0.10,
                "OFF_PEAK": 0.0,
            },
            "tou_periods": {
                "ON_PEAK": [{"fromHour": 15, "toHour": 23}],
                "OFF_PEAK": [{"fromHour": 0, "toHour": 15}],
            },
        }
    )

    assert entity._schedule_cache["buy_rates"]["ON_PEAK"] == 1000.0
    assert entity._schedule_cache["sell_rates"]["ON_PEAK"] == 10.0
    assert entity._schedule_cache["tou_schedule"][0]["buy"] == 1000.0
    assert entity._schedule_cache["tou_schedule"][0]["sell"] == 10.0


def test_flow_power_current_import_price_prefers_tariff_schedule():
    sensor = _sensor_module()
    entity = sensor.FlowPowerPriceSensor(
        SimpleNamespace(data={"current": [{"channelType": "general", "perKwh": 44.1}]}),
        _entry("flow_power"),
        "current_import_price",
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "tariff_schedule": {
                        "currency": "AUD",
                        "buy_prices": {"PEAK": 0.25},
                        "sell_prices": {"PEAK": 0.08},
                        "price_source": "flow_power_kwatch",
                        "utility": "Flow Power",
                        "plan_name": "PowerSync Flow Power",
                    }
                }
            }
        },
    )

    assert entity.native_value == 0.25
    attrs = entity.extra_state_attributes
    assert attrs["source"] == "tariff_schedule"
    assert attrs["current_period"] == "PEAK"
    assert attrs["final_rate_cents"] == 25.0
    assert attrs["price_source"] == "flow_power_kwatch"
    assert attrs["price_spike"] is None


def test_dedicated_flow_power_import_price_keeps_coordinator_calculation():
    sensor = _sensor_module()
    entity = sensor.FlowPowerPriceSensor(
        SimpleNamespace(data={"current": [{"channelType": "general", "perKwh": 44.1}]}),
        _entry("flow_power"),
        "flow_power_price",
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "tariff_schedule": {
                        "currency": "AUD",
                        "buy_prices": {"PEAK": 0.25},
                        "sell_prices": {"PEAK": 0.08},
                    }
                }
            }
        },
    )

    assert entity.native_value != 0.25


def test_daily_load_uses_total_state_class():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "daily_load")

    assert desc.state_class == "total"


def test_foxess_sensor_descriptions_include_pv4_power():
    sensor = _sensor_module()
    keys = {description.key for description in sensor.FOXESS_SENSORS}

    assert {"pv1_power", "pv2_power", "pv3_power", "pv4_power", "pv5_power", "pv6_power"} <= keys
    assert sensor.SENSOR_KEY_TO_FAMILY["pv4_power"] == sensor.SENSOR_FAMILY_SOLAR_INVERTER


def test_foxess_battery_energy_sensor_names_are_distinct_from_generic_totals():
    sensor = _sensor_module()
    generic_names = {
        description.key: description.name
        for description in sensor.ENERGY_SENSORS
        if description.key in {"daily_battery_charge", "daily_battery_discharge"}
    }
    foxess_names = {
        description.key: description.name
        for description in sensor.FOXESS_SENSORS
        if description.key
        in {"daily_battery_charge_foxess", "daily_battery_discharge_foxess"}
    }

    assert foxess_names["daily_battery_charge_foxess"] != generic_names["daily_battery_charge"]
    assert (
        foxess_names["daily_battery_discharge_foxess"]
        != generic_names["daily_battery_discharge"]
    )
    assert foxess_names["daily_battery_charge_foxess"] == "FoxESS Daily Battery Charge"
    assert foxess_names["daily_battery_discharge_foxess"] == "FoxESS Daily Battery Discharge"


def test_sungrow_solar_sensor_adds_configured_ac_inverter_output():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "solar_power")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={
            "battery_system": sensor.BATTERY_SYSTEM_SUNGROW,
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": "sungrow",
        },
        options={},
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"solar_power": 4.2}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "inverter_attributes": {
                        "power_output_w": 5100,
                    },
                },
            },
        },
    )

    assert round(entity.native_value, 3) == 9.3
    assert entity.extra_state_attributes["battery_inverter_solar_power_kw"] == 4.2
    assert entity.extra_state_attributes["ac_inverter_solar_power_kw"] == 5.1
    assert entity.extra_state_attributes["total_solar_power_kw"] == 9.3


def test_energy_sensor_unavailable_when_coordinator_data_is_stale():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "solar_power")
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(
            data={"solar_power": 3.131},
            last_update_success=True,
            last_update_success_time=datetime(2026, 5, 3, 11, 55, tzinfo=timezone.utc),
            update_interval=timedelta(seconds=15),
        ),
        desc,
        _entry("amber"),
    )
    entity.hass = _hass("AUD")

    assert entity.available is False


def test_energy_sensor_available_when_coordinator_data_is_recent():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "solar_power")
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(
            data={"solar_power": 3.131},
            last_update_success=True,
            last_update_success_time=datetime(2026, 5, 3, 11, 59, 30, tzinfo=timezone.utc),
            update_interval=timedelta(seconds=15),
        ),
        desc,
        _entry("amber"),
    )
    entity.hass = _hass("AUD")

    assert entity.available is True


def test_local_powerwall_home_load_excludes_observed_ev_power():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
        options={},
    )
    local_coord = SimpleNamespace(
        data=SimpleNamespace(load_w=10_700.0),
        last_success_ts=time.time(),
        last_success_monotonic=time.monotonic(),
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"load_power": 3.6, "ev_power": 7.1}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "powerwall_local": {
                        "coordinator": local_coord,
                    },
                },
            },
        },
    )

    assert round(entity.native_value, 3) == 3.6


def test_local_powerwall_home_load_never_goes_negative_after_ev_subtraction():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
        options={},
    )
    local_coord = SimpleNamespace(
        data=SimpleNamespace(load_w=2_000.0),
        last_success_ts=time.time(),
        last_success_monotonic=time.monotonic(),
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"load_power": 0.0, "ev_power": 7.1}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "powerwall_local": {
                        "coordinator": local_coord,
                    },
                },
            },
        },
    )

    assert entity.native_value == 0.0


def test_home_load_sensor_never_publishes_negative_history_value():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")

    assert desc.value_fn({"load_power": -19.519}) == 0.0
    assert desc.value_fn({"load_power": 1.234}) == 1.234


def test_neovolt_surplus_balancer_sensor_exposes_status_and_attributes():
    sensor = _sensor_module()
    desc = next(d for d in sensor.NEOVOLT_SENSORS if d.key == "neovolt_surplus_balancer")
    payload = {
        "status": "balancing_low_stack",
        "soc_delta_percent": 32.8,
        "lowest_soc_index": 0,
        "highest_soc_index": 1,
    }
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"neovolt_surplus_balancer": payload}),
        desc,
        _entry("amber"),
    )
    entity.hass = _hass("AUD")

    assert entity.native_value == "balancing_low_stack"
    assert entity.extra_state_attributes["soc_delta_percent"] == 32.8
    assert entity.extra_state_attributes["lowest_soc_index"] == 0


def test_optimizer_force_discharge_windows_include_discharge_and_export():
    sensor = _sensor_module()
    desc = next(
        d
        for d in sensor.OPTIMIZER_ACTION_SENSORS
        if d.key == "optimization_force_discharge_windows"
    )
    payload = {
        "next_actions": [
            {
                "action": "discharge",
                "timestamp": "2026-05-03T10:00:00+00:00",
                "end_time": "2026-05-03T10:30:00+00:00",
                "power_w": 3200,
                "soc": 0.76,
            },
            {
                "action": "charge",
                "timestamp": "2026-05-03T12:30:00+00:00",
                "end_time": "2026-05-03T13:00:00+00:00",
                "power_w": 5000,
                "soc": 0.78,
            },
            {
                "action": "export",
                "timestamp": "2026-05-03T17:00:00+00:00",
                "end_time": "2026-05-03T18:30:00+00:00",
                "power_w": 4200,
                "soc": 0.82,
            },
            {
                "action": "discharge",
                "timestamp": "2026-05-03T19:00:00+00:00",
                "end_time": "2026-05-03T19:30:00+00:00",
                "power_w": 2500,
                "soc": 0.55,
            },
        ],
    }
    entity = sensor.OptimizerActionSensor(SimpleNamespace(data=payload), desc, _entry("amber"))

    assert entity.native_value == "17:00-18:30, 19:00-19:30"
    attrs = entity.extra_state_attributes
    assert attrs["actions"] == ["discharge", "export"]
    assert attrs["count"] == 2
    assert attrs["total_minutes"] == 120
    assert [w["action"] for w in attrs["windows"]] == ["export", "discharge"]
    assert attrs["next_power_w"] == 4200


def test_optimizer_force_charge_window_uses_active_command_power():
    sensor = _sensor_module()
    desc = next(
        d
        for d in sensor.OPTIMIZER_ACTION_SENSORS
        if d.key == "optimization_force_charge_windows"
    )
    payload = {
        "current_action": "charge",
        "effective_current_action": "charge",
        "current_power_w": 1019,
        "next_actions": [
            {
                "action": "charge",
                "timestamp": "2026-05-03T11:30:00+00:00",
                "end_time": "2026-05-03T13:00:00+00:00",
                "power_w": 10000,
                "soc": 0.52,
            },
            {
                "action": "charge",
                "timestamp": "2026-05-03T14:00:00+00:00",
                "end_time": "2026-05-03T14:30:00+00:00",
                "power_w": 5000,
                "soc": 0.64,
            },
        ],
    }
    entity = sensor.OptimizerActionSensor(SimpleNamespace(data=payload), desc, _entry("amber"))

    attrs = entity.extra_state_attributes
    assert attrs["next_power_w"] == 1019
    assert attrs["windows"][0]["power_w"] == 1019
    assert attrs["windows"][0]["planned_power_w"] == 10000
    assert attrs["windows"][1]["power_w"] == 5000
    assert attrs["windows"][1]["planned_power_w"] == 5000


def test_optimizer_current_action_exposes_reserve_recommendation():
    sensor = _sensor_module()
    desc = next(
        d
        for d in sensor.OPTIMIZER_ACTION_SENSORS
        if d.key == "optimization_status"
    )
    recommendation = {
        "suggested_optimizer_reserve_percent": 59,
        "minimum_forecast_soc_percent": 59.1,
        "next_charge_reason": "forecast_solar_surplus",
    }
    payload = {
        "current_action": "self_consumption",
        "current_power_w": 1000,
        "actual_battery_power_w": 950,
        "status": "active",
        "current_action_end_time": "2026-05-04T00:05:00+00:00",
        "lp_stats": {"solver_used": "highs"},
        "reserve_recommendation": recommendation,
        "idle_hold_active": True,
        "idle_hold_reserve": 1.0,
        "idle_hold_reserve_percent": 100,
    }
    entity = sensor.OptimizerActionSensor(SimpleNamespace(data=payload), desc, _entry("amber"))

    assert entity.native_value == "self_consumption"
    attrs = entity.extra_state_attributes
    assert attrs["reserve_recommendation"] == recommendation
    assert attrs["lp_stats"]["solver_used"] == "highs"
    assert attrs["idle_hold_active"] is True
    assert attrs["idle_hold_reserve_percent"] == 100


def test_eur_price_forecast_uses_major_rate_and_ct_minor_attributes():
    sensor = _sensor_module()
    desc = next(d for d in sensor.LP_FORECAST_SENSORS if d.key == "lp_import_price_forecast")
    entity = sensor.LPForecastSensor(
        SimpleNamespace(get_forecast_data=lambda: {"available": True, "import_price_avg": 0.25}),
        desc,
        _entry("epex"),
    )
    entity.hass = _hass("AUD")

    assert entity.native_value == 0.25
    assert entity.native_unit_of_measurement == "EUR/kWh"
    assert entity.device_class is None
    assert entity.extra_state_attributes["minor_price_unit"] == "ct/kWh"


def test_nzd_tariff_schedule_prefers_tariff_currency_metadata():
    sensor = _sensor_module()
    hass = _hass("GBP")
    entry = _entry("other")
    hass.data = {
        "power_sync": {
            entry.entry_id: {
                "tariff_schedule": {
                    "currency": "NZD",
                    "buy_rates": {"PEAK": 0.25},
                    "sell_rates": {"PEAK": 0.08},
                    "tou_periods": {},
                    "last_sync": "now",
                }
            }
        }
    }
    entity = sensor.TariffScheduleSensor(hass, entry)

    assert entity.native_value == "PEAK (25.0c/kWh)"
    attrs = entity.extra_state_attributes
    assert attrs["currency"] == "NZD"
    assert attrs["price_unit"] == "NZD/kWh"
    assert attrs["minor_price_unit"] == "c/kWh"


def test_tariff_schedule_wall_clock_periods_do_not_roll_elapsed_slots_to_tomorrow(monkeypatch):
    sensor = _sensor_module()
    hass = _hass("AUD")
    entry = _entry("other")
    monkeypatch.setattr(
        sensor.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
    )
    hass.data = {
        "power_sync": {
            entry.entry_id: {
                "tariff_schedule": {
                    "buy_prices": {
                        "PERIOD_12_30": 0.19,
                        "PERIOD_13_30": 0.19,
                    },
                    "sell_prices": {
                        "PERIOD_12_30": 0.0,
                        "PERIOD_13_30": 0.0,
                    },
                    "last_sync": "2026-07-02T13:26:00+10:00",
                }
            }
        }
    }
    entity = sensor.TariffScheduleSensor(hass, entry)

    schedule = entity.extra_state_attributes["schedule"]

    assert schedule[0]["time"] == "12:30"
    assert schedule[0]["date"] == "2026-07-02"
    assert schedule[0]["date_label"] == "Today"
    assert schedule[1]["date_label"] == "Today"


def test_tariff_price_sensor_unit_prefers_tariff_currency_metadata():
    sensor = _sensor_module()
    hass = _hass("GBP")
    entry = _entry("other")
    hass.data = {
        "power_sync": {
            entry.entry_id: {
                "tariff_schedule": {
                    "currency": "NZD",
                    "buy_rates": {"PEAK": 0.25},
                    "sell_rates": {"PEAK": 0.08},
                    "tou_periods": {},
                    "last_sync": "now",
                }
            }
        }
    }
    entity = sensor.TariffPriceSensor(
        hass,
        entry,
        "current_import_price",
        "Current Import Price",
    )

    assert entity.native_value == 0.25
    assert entity.native_unit_of_measurement == "NZD/kWh"
    assert entity.extra_state_attributes["currency"] == "NZD"


def test_powerwall_pack_sensors_use_bms_health_and_parent_device():
    sensor = _sensor_module()
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    health = {
        "source": "ha_local_tedapi",
        "individual_batteries": [
            {
                "nominalFullPackEnergyWh": 14380.0,
                "nominalEnergyRemainingWh": 5440.0,
                "serialNumber": "LEADER",
                "isExpansion": False,
                "isFollower": False,
            },
            {
                "nominalFullPackEnergyWh": 14290.0,
                "nominalEnergyRemainingWh": 6820.0,
                "serialNumber": "EXPANSION",
                "isExpansion": True,
                "isFollower": False,
            },
        ],
    }
    hass = SimpleNamespace(data={"power_sync": {"entry-1": {"battery_health": health}}})

    soc = sensor.PowerwallBlockSocSensor(hass, entry, 1)
    current_energy = sensor.PowerwallBlockCurrentEnergySensor(hass, entry, 1)
    capacity = sensor.PowerwallBlockCapacitySensor(hass, entry, 1)
    soh = sensor.PowerwallBlockSohSensor(hass, entry, 1)

    assert soc.device_info == sensor.powerwall_device_info("entry-1")
    assert soc._attr_name == "Expansion Pack 1 SOC"
    assert soc.native_value == 47.7
    assert current_energy.native_value == 6.82
    assert current_energy._attr_name == "Expansion Pack 1 Current Energy"
    assert capacity.native_value == 14.29
    assert soh.native_value == 105.9
    assert soc.extra_state_attributes["pack_label"] == "Expansion Pack 1"
    assert soc.extra_state_attributes["serial_number"] == "EXPANSION"
    assert soc.extra_state_attributes["pack_role"] == "expansion"
    assert soc.extra_state_attributes["is_expansion"] is True
    assert soc.extra_state_attributes["source"] == "ha_local_tedapi"


def test_powerwall_solar_string_voltage_sensor_metadata_and_value():
    sensor = _sensor_module()
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    diagnostics = {
        "source": "pw3_components",
        "transport_source": "ha_fleet_api_relay",
        "last_scan": "2026-05-30T10:00:00+10:00",
        "strings": [
            {
                "id": "pch:A",
                "label": "A",
                "mppt": "A",
                "voltage_v": 295.24,
                "current_a": 3.1,
                "power_w": 915.244,
                "state": "PV_Active",
                "connected": True,
            }
        ],
        "groups": [
            {
                "id": "gateway:A+B",
                "label": "MPPT A+B",
                "string_ids": ["pch:A", "pch:B"],
                "total_power_w": 1800.0,
            }
        ],
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {"solar_string_diagnostics": diagnostics}}}
    )

    entity = sensor.PowerwallSolarStringVoltageSensor(hass, entry, "pch_a", "pch:A", "A")

    assert entity.device_info == sensor.powerwall_device_info("entry-1")
    assert entity.native_value == 295.2
    assert entity.available is True
    assert entity._attr_name == "Solar String A Voltage"
    assert entity.extra_state_attributes["source"] == "pw3_components"
    assert entity.extra_state_attributes["transport_source"] == "ha_fleet_api_relay"
    assert entity.extra_state_attributes["group_label"] == "MPPT A+B"


def test_powerwall_pack_builder_skips_missing_optional_metrics():
    sensor = _sensor_module()
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    packs = [
        {
            "nominalFullPackEnergyWh": 14380.0,
            "nominalEnergyRemainingWh": 5440.0,
            "isExpansion": False,
            "isFollower": False,
        },
        {
            "nominalFullPackEnergyWh": 14290.0,
            "nominalEnergyRemainingWh": 6820.0,
            "isExpansion": True,
            "isFollower": False,
        },
    ]
    hass = SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "battery_health": {
                        "individual_batteries": packs,
                    },
                },
            },
        },
    )

    entities = sensor._build_powerwall_pack_sensors(hass, entry, packs, set())

    assert [entity.metric_key for entity in entities] == [
        "soc",
        "current_energy",
        "capacity",
        "soh",
        "soc",
        "current_energy",
        "capacity",
        "soh",
    ]


def test_powerwall_pack_labels_leader_follower_and_expansions():
    sensor = _sensor_module()
    packs = [
        {"role": "leader", "isExpansion": False, "isFollower": False},
        {"role": "follower", "isExpansion": False, "isFollower": True},
        {"role": "expansion", "isExpansion": True, "isFollower": False},
        {"role": "expansion", "isExpansion": True, "isFollower": False},
    ]

    assert [sensor._pack_label(packs, index) for index in range(len(packs))] == [
        "Leader Powerwall",
        "Follower Powerwall",
        "Expansion Pack 1",
        "Expansion Pack 2",
    ]


def test_powerwall_pack_labels_pw2_units_as_powerwalls():
    sensor = _sensor_module()
    packs = [
        {"role": "powerwall", "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "isExpansion": False, "isFollower": False},
    ]

    assert [sensor._pack_label(packs, index) for index in range(len(packs))] == [
        "Powerwall 1",
        "Powerwall 2",
        "Powerwall 3",
        "Powerwall 4",
    ]


def test_battery_health_attributes_publish_pack_labels():
    sensor = _sensor_module()
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    entity = sensor.BatteryHealthSensor(entry)
    entity._original_capacity_wh = 54000.0
    entity._current_capacity_wh = 22950.0
    entity._battery_count = 4
    entity._source = "ha_local_tedapi"
    entity._individual_batteries = [
        {"role": "powerwall", "nominalFullPackEnergyWh": 14290.0, "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "nominalFullPackEnergyWh": 14290.0, "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "nominalFullPackEnergyWh": 14420.0, "isExpansion": False, "isFollower": False},
        {"role": "powerwall", "nominalFullPackEnergyWh": 14470.0, "isExpansion": False, "isFollower": False},
    ]

    attrs = entity.extra_state_attributes

    assert [attrs[f"battery_{index}_label"] for index in range(1, 5)] == [
        "Powerwall 1",
        "Powerwall 2",
        "Powerwall 3",
        "Powerwall 4",
    ]
    assert attrs["battery_1_role"] == "powerwall"


def test_powerwall_pack_registry_cleanup_tolerates_legacy_identifier_shape(monkeypatch):
    sensor = _sensor_module()
    entry = SimpleNamespace(entry_id="entry-1")
    removed_entities = []
    updated_devices = []

    device_registry_module = types.ModuleType("homeassistant.helpers.device_registry")
    entity_registry_module = types.ModuleType("homeassistant.helpers.entity_registry")

    device_registry = SimpleNamespace(
        devices={
            "legacy-device": SimpleNamespace(
                id="legacy-device",
                identifiers={
                    ("power_sync", "entry-1_pw_1", "legacy-extra"),
                    ("other", "ignored"),
                },
            ),
        },
        async_update_device=lambda **kwargs: updated_devices.append(kwargs),
    )
    entity_registry = SimpleNamespace(
        entities={
            "sensor.legacy_temperature": SimpleNamespace(
                platform="power_sync",
                device_id="legacy-device",
                unique_id="entry-1_pw1_temperature",
                entity_id="sensor.legacy_temperature",
            ),
        },
        async_remove=lambda entity_id: removed_entities.append(entity_id),
    )

    device_registry_module.async_get = lambda hass: device_registry
    entity_registry_module.async_get = lambda hass: entity_registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry_module)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", entity_registry_module)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "device_registry",
        device_registry_module,
        raising=False,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        entity_registry_module,
        raising=False,
    )

    sensor._cleanup_legacy_powerwall_pack_registry(SimpleNamespace(), entry)

    assert removed_entities == ["sensor.legacy_temperature"]
    assert updated_devices == [
        {"device_id": "legacy-device", "remove_config_entry_id": "entry-1"}
    ]


def test_has_tesla_ev_device_tolerates_extended_identifier_shape():
    sensor = _sensor_module()
    hass = SimpleNamespace(
        device_registry=SimpleNamespace(
            devices={
                "tesla-device": SimpleNamespace(
                    identifiers={("teslemetry", "LRWYHCEK3PC907290", "vehicle")},
                ),
                "ignored-device": SimpleNamespace(
                    identifiers={"not-a-valid-identifier"},
                ),
            },
        ),
    )

    assert sensor._has_tesla_ev_device(hass) is True


def test_has_solaredge_ev_power_detects_reported_charger_entity():
    sensor = _sensor_module()
    state = SimpleNamespace(
        entity_id="sensor.ev_charger_power",
        attributes={"friendly_name": "SolarEdge EV Charger EV Charger Power"},
    )
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain=None: [state]))

    assert sensor._has_solaredge_ev_power(hass) is True


def test_has_solaredge_ev_power_ignores_unrelated_charger_power():
    sensor = _sensor_module()
    state = SimpleNamespace(
        entity_id="sensor.tessy_charger_power",
        attributes={"friendly_name": "Tessy Charger Power"},
    )
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain=None: [state]))

    assert sensor._has_solaredge_ev_power(hass) is False


def test_ev_status_sensor_labels_solaredge_coordinator_power():
    sensor = _sensor_module()
    desc = next(d for d in sensor.EV_SENSORS if d.key == "ev_power")
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    entity = sensor.EVStatusSensor(SimpleNamespace(data={}), entry, desc)
    entity.async_write_ha_state = lambda: None
    entity.hass = SimpleNamespace(
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "solaredge_coordinator": SimpleNamespace(
                        data={
                            "ev_power": 7.4,
                            "ev_charger_type": "solaredge",
                            "ev_charger_connected": True,
                            "ev_charger_charging": True,
                        }
                    )
                }
            }
        }
    )

    entity._handle_coordinator_update()

    assert entity.native_value == 7.4
    assert entity.extra_state_attributes["vehicle_name"] == "SolarEdge EV Charger"
    assert entity.extra_state_attributes["vehicle_id"] == "solaredge_ev_charger"


def test_ev_status_sensor_exposes_idle_sigenergy_evac_presence():
    sensor = _sensor_module()
    power_sync = sys.modules["power_sync"]
    power_sync._get_ev_vehicle_status = lambda hass, entry: {"ev_power_kw": 0.0}
    power_sync._get_ev_vehicles_status = lambda hass, entry: []

    async def read_sigenergy_charger_state(entry):
        return SimpleNamespace(
            charger_type="evac",
            power_kw=0.0,
            vehicle_soc=None,
            is_connected=True,
            is_charging=False,
            is_discharging=False,
        )

    power_sync._read_sigenergy_charger_state_for_entry = read_sigenergy_charger_state
    desc = next(d for d in sensor.EV_SENSORS if d.key == "ev_power")
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    entity = sensor.EVStatusSensor(SimpleNamespace(data={}), entry, desc)
    entity.async_write_ha_state = lambda: None
    entity.hass = SimpleNamespace(data={sensor.DOMAIN: {"entry-1": {}}})

    asyncio.run(entity._async_update_ev())

    assert entity.native_value == 0.0
    assert entity.extra_state_attributes["vehicle_name"] == "Sigenergy EVAC"
    assert entity.extra_state_attributes["vehicle_id"] == "sigenergy_charger"
    assert entity.extra_state_attributes["is_connected"] is True
    assert entity.extra_state_attributes["is_charging"] is False
