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
    ha_const.UnitOfPower = SimpleNamespace(KILO_WATT="kW", WATT="W")
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


def test_tariff_schedule_native_value_preserves_two_decimal_cents():
    sensor = _sensor_module()
    sensor.get_current_price_from_tariff_schedule = lambda tariff: (6.63, 1.25, "PEAK")
    entity = sensor.TariffScheduleSensor(_hass("AUD"), _entry("globird"))
    entity.hass.data = {
        sensor.DOMAIN: {
            "entry-1": {
                "tariff_schedule": {
                    "currency": "AUD",
                    "last_sync": "2026-07-10 16:53:49",
                }
            }
        }
    }

    assert entity.native_value == "PEAK (6.63c/kWh)"


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


def test_covau_price_and_quota_sensors_use_live_provider_contract():
    sensor = _sensor_module()
    entry = _entry("covau")
    contract = {
        "plan": {
            "plan_id": "COV1117616MRE2@EME",
            "display_name": "SolarMax SA Residential TOU",
        },
        "prices": {
            "import": {
                "c_per_kwh": 0.0,
                "base_c_per_kwh": 35.17,
                "period": "covau_solarmax_free_import",
            },
            "export": {
                "c_per_kwh": 15.0,
                "base_c_per_kwh": 5.0,
                "period": "covau_solarmax_premium_export",
            },
        },
        "tariff_day": "2026-05-03",
        "settlement_confidence": "authoritative",
        "settlement_reason": None,
        "quotas": {
            "import": {
                "rule_id": "covau_solarmax_free_import",
                "remaining_kwh": 42.5,
            },
            "export": {
                "rule_id": "covau_solarmax_premium_export",
                "remaining_kwh": 21.25,
            },
        },
        "tariff_schedule": {
            "currency": "AUD",
            "buy_prices": {"PERIOD_12_00": 0.0},
            "sell_prices": {"PERIOD_12_00": 0.05},
            "utility": "CovaU",
            "plan_name": "SolarMax SA Residential TOU",
            "price_source": "covau_aer_cdr",
            "last_sync": "2026-05-03:authoritative",
        },
    }
    coordinator = SimpleNamespace(get_provider_contract=lambda: contract)
    hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                entry.entry_id: {
                    "covau_quota_runtime": SimpleNamespace(
                        contract=lambda: (_ for _ in ()).throw(RuntimeError("stale"))
                    ),
                    "optimization_coordinator": coordinator,
                    "tariff_schedule": {
                        "buy_prices": {"PERIOD_12_00": 9.99},
                        "sell_prices": {"PERIOD_12_00": 9.99},
                        "last_sync": "stale-generic-schedule",
                    },
                }
            }
        },
    )

    import_price = sensor.TariffPriceSensor(
        hass,
        entry,
        sensor.SENSOR_TYPE_CURRENT_IMPORT_PRICE,
        "Current Import Price",
    )
    export_price = sensor.TariffPriceSensor(
        hass,
        entry,
        sensor.SENSOR_TYPE_CURRENT_EXPORT_PRICE,
        "Current Export Price",
    )
    free_remaining = sensor.CovaUProviderSensor(
        hass,
        entry,
        sensor.COVAU_SENSOR_IMPORT_REMAINING,
    )
    premium_remaining = sensor.CovaUProviderSensor(
        hass,
        entry,
        sensor.COVAU_SENSOR_EXPORT_REMAINING,
    )
    tariff_schedule = sensor.TariffScheduleSensor(hass, entry)

    assert import_price.native_value == 0.0
    assert export_price.native_value == 0.15
    assert import_price.extra_state_attributes["current_period"] == "covau_solarmax_free_import"
    assert export_price.extra_state_attributes["current_period"] == "covau_solarmax_premium_export"
    assert import_price.extra_state_attributes["quota"]["remaining_kwh"] == 42.5
    assert free_remaining.native_value == 42.5
    assert premium_remaining.native_value == 21.25
    assert tariff_schedule.native_value == "PEAK (25.00c/kWh)"
    assert tariff_schedule.extra_state_attributes["schedule"] == [
        {
            "time": "12:00",
            "date": "2026-05-03",
            "date_label": "Today",
            "buy": 0.0,
            "sell": 0.05,
        }
    ]

    # An incomplete runtime contract must not hide a valid optimizer contract.
    hass.data[sensor.DOMAIN][entry.entry_id]["covau_quota_runtime"] = (
        SimpleNamespace(contract=lambda: {"prices": contract["prices"]})
    )
    assert tariff_schedule.native_value == "PEAK (25.00c/kWh)"
    assert import_price.native_value == 0.0

    # Malformed rates fail closed and never use a stale generic tariff.
    malformed = {
        **contract,
        "prices": {
            **contract["prices"],
            "import": {"c_per_kwh": "oops"},
        },
    }
    hass.data[sensor.DOMAIN][entry.entry_id]["covau_quota_runtime"] = (
        SimpleNamespace(contract=lambda: malformed)
    )
    hass.data[sensor.DOMAIN][entry.entry_id]["optimization_coordinator"] = (
        SimpleNamespace(get_provider_contract=lambda: malformed)
    )
    assert import_price.native_value is None
    assert import_price.extra_state_attributes == {}
    assert free_remaining.extra_state_attributes["settlement_confidence"] == "authoritative"
    for quota_sensor in (free_remaining, premium_remaining):
        assert quota_sensor._attr_device_class == "energy"
        assert quota_sensor._attr_native_unit_of_measurement == "kWh"
        assert getattr(quota_sensor, "_attr_state_class", None) is None


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


def test_sungrow_solar_sensor_does_not_double_count_coordinator_ac_output():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "solar_power")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={
            "battery_system": sensor.BATTERY_SYSTEM_SUNGROW,
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": "sungrow",
            "inverter_host": "192.0.2.20",
            "sungrow_host": "192.0.2.10",
        },
        options={},
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(
            data={
                "solar_power": 9.3,
                "battery_inverter_solar_power": 4.2,
                "ac_inverter_solar_power": 5.1,
            }
        ),
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


def test_local_powerwall_home_load_uses_site_ev_snapshot_when_coordinator_is_zero():
    """W3 regression: the EV sensor can be fresher than Tesla site telemetry."""
    sensor = _sensor_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
        options={},
    )
    local_coord = SimpleNamespace(
        data=SimpleNamespace(load_w=5_670.0),
        last_success_ts=time.time(),
        last_success_monotonic=time.monotonic(),
    )
    observed = ev_load.ObservedEvLoadSnapshot(
        power_kw=1.0,
        components=(),
        observed_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        quality=ev_load.EvLoadQuality.COMPLETE,
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"load_power": 5.67, "ev_power": 0.0}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {
                    "powerwall_local": {"coordinator": local_coord},
                    "observed_ev_load_snapshot": observed,
                }
            }
        },
    )

    assert entity.native_value == 4.67


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


def test_tesla_home_load_sensor_keeps_direct_wall_connector_reconciliation():
    """Ticket #204: the sensor must not undo the coordinator's VIN fallback."""
    sensor = _sensor_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    vin_key = "vehicle:5yjtest0000000001"
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    coordinator = SimpleNamespace(
        data={
            "load_power": 6.80154,
            "raw_home_load_power": 17.334,
            "ev_power": 10.53246,
            "ev_power_fallback_by_physical_key": {vin_key: 10.53246},
            "last_update": datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
            "home_load_basis": "excludes_ev",
            "home_load_normalization_quality": "complete",
        }
    )
    incomplete = ev_load.ObservedEvLoadSnapshot(
        power_kw=0.0,
        components=(),
        observed_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        quality=ev_load.EvLoadQuality.INCOMPLETE,
        unavailable_active_keys=(vin_key,),
    )
    entity = sensor.TeslaEnergySensor(coordinator, desc, entry)
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {"observed_ev_load_snapshot": incomplete}
            }
        },
    )

    assert abs(entity.native_value - 6.80154) < 1e-9
    assert entity.extra_state_attributes["normalization_quality"] == "complete"
    assert abs(
        entity.extra_state_attributes["observed_ev_power_kw"] - 10.53246
    ) < 1e-9


def test_tesla_home_load_sensor_tracks_direct_same_vehicle_stop_and_restart():
    """Ticket #204: paired-local Home Load follows direct VIN-scoped edges."""
    sensor = _sensor_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    vehicle_key = "vehicle:5yjtest0000000001"
    observed_at = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)

    for cached_power_kw, direct_power_kw, raw_load_kw, expected_home_load_kw in (
        (1.420654, 0.0, 1.395, 1.395),
        (0.0, 2.182647, 3.569, 1.386353),
    ):
        entry = SimpleNamespace(
            entry_id="entry-1",
            data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
            options={},
        )
        local_coord = SimpleNamespace(
            data=SimpleNamespace(load_w=raw_load_kw * 1000.0),
            last_success_ts=time.time(),
            last_success_monotonic=time.monotonic(),
        )
        coordinator = SimpleNamespace(
            data={
                "load_power": expected_home_load_kw,
                "raw_home_load_power": raw_load_kw,
                "ev_power": direct_power_kw,
                "ev_power_fallback_by_physical_key": {
                    vehicle_key: direct_power_kw
                },
                "last_update": observed_at,
                "home_load_basis": "excludes_ev",
                "home_load_normalization_quality": "complete",
            }
        )
        cached = ev_load.ObservedEvLoadSnapshot(
            power_kw=cached_power_kw,
            components=(
                ev_load.EvLoadObservation(
                    physical_load_key=vehicle_key,
                    source_key="cached_vehicle",
                    power_kw=cached_power_kw,
                    observed_at=observed_at,
                    active=cached_power_kw > 0.05,
                    measurement_kind=ev_load.EvMeasurementKind.VEHICLE,
                ),
            ),
            observed_at=observed_at,
            quality=ev_load.EvLoadQuality.COMPLETE,
        )
        entity = sensor.TeslaEnergySensor(coordinator, desc, entry)
        entity.hass = SimpleNamespace(
            config=SimpleNamespace(currency="AUD"),
            data={
                sensor.DOMAIN: {
                    "entry-1": {
                        "powerwall_local": {"coordinator": local_coord},
                        "observed_ev_load_snapshot": cached,
                    }
                }
            },
        )

        assert abs(entity.native_value - expected_home_load_kw) < 1e-9
        assert entity.extra_state_attributes["normalization_quality"] == "complete"
        assert entity.extra_state_attributes["observed_ev_power_kw"] == direct_power_kw


def test_tesla_home_load_sensor_still_fails_closed_for_distinct_missing_ev():
    """A Tesla direct meter must not cover a separate unavailable charger."""
    sensor = _sensor_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "home_load")
    vin_key = "vehicle:5yjtest0000000001"
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    coordinator = SimpleNamespace(
        data={
            "load_power": None,
            "raw_home_load_power": 19.734,
            "ev_power": 13.2,
            "ev_power_fallback_by_physical_key": {vin_key: 10.8},
            "last_update": datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        }
    )
    incomplete = ev_load.ObservedEvLoadSnapshot(
        power_kw=10.8,
        components=(
            ev_load.EvLoadObservation(
                physical_load_key=vin_key,
                source_key="tesla_wall_connector",
                power_kw=10.8,
                observed_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
                active=True,
                measurement_kind=ev_load.EvMeasurementKind.LOADPOINT_METER,
            ),
        ),
        observed_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        quality=ev_load.EvLoadQuality.INCOMPLETE,
        unavailable_active_keys=("ocpp:garage:1",),
    )
    entity = sensor.TeslaEnergySensor(coordinator, desc, entry)
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {"observed_ev_load_snapshot": incomplete}
            }
        },
    )

    assert entity.native_value is None
    assert entity.extra_state_attributes["normalization_quality"] == "incomplete"


def test_grid_status_sensor_requires_provider_reported_status():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "grid_status")

    assert desc.value_fn({"grid_power": 0.001, "solar_power": 0.0}) is None
    unknown_statuses = (
        None,
        "",
        "unavailable",
        "unexpected-status",
        "connected",
        "on-grid",
        "off grid",
        "SystemIslandedReady",
        "SystemTransitionToGrid",
        "SystemTransitionToIsland",
        "SystemMicroGridFaulted",
        "SystemWaitForUser",
    )
    for status in unknown_statuses:
        assert desc.value_fn({"grid_status": status}) is None


def test_grid_status_sensor_preserves_provider_reported_status():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "grid_status")

    recognized_statuses = (
        "Active",
        "SystemGridConnected",
        "Inactive",
        "Islanded",
        "Off-Grid",
        "SystemIslandedActive",
    )
    for status in recognized_statuses:
        assert desc.value_fn({"grid_status": status}) == status


def test_fresh_local_grid_status_does_not_fall_back_to_cloud_when_unknown():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "grid_status")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
        options={},
    )
    local_coord = SimpleNamespace(
        data=SimpleNamespace(grid_status="SystemIslandedReady"),
        last_success_monotonic=time.monotonic(),
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"grid_status": "Active"}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {"powerwall_local": {"coordinator": local_coord}},
            },
        },
    )

    for status in (
        "SystemIslandedReady",
        "SystemTransitionToGrid",
        "SystemTransitionToIsland",
        "SystemMicroGridFaulted",
        "SystemWaitForUser",
        "Unknown",
        None,
    ):
        local_coord.data.grid_status = status
        assert entity.native_value is None
    local_coord.data = SimpleNamespace()
    assert entity.native_value is None


def test_fresh_local_grid_status_maps_recognized_states_explicitly():
    sensor = _sensor_module()
    desc = next(d for d in sensor.ENERGY_SENSORS if d.key == "grid_status")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={sensor.CONF_POWERWALL_LOCAL_PAIRED: True},
        options={},
    )
    local_coord = SimpleNamespace(
        data=SimpleNamespace(grid_status="SystemGridConnected"),
        last_success_monotonic=time.monotonic(),
    )
    entity = sensor.TeslaEnergySensor(
        SimpleNamespace(data={"grid_status": "Inactive"}),
        desc,
        entry,
    )
    entity.hass = SimpleNamespace(
        config=SimpleNamespace(currency="AUD"),
        data={
            sensor.DOMAIN: {
                "entry-1": {"powerwall_local": {"coordinator": local_coord}},
            },
        },
    )

    expected_by_status = {
        "Active": "Active",
        "SystemGridConnected": "Active",
        "Inactive": "Off-Grid",
        "Islanded": "Off-Grid",
        "Off-Grid": "Off-Grid",
        "SystemIslandedActive": "Off-Grid",
    }
    for status, expected in expected_by_status.items():
        local_coord.data.grid_status = status
        assert entity.native_value == expected


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


def test_optimizer_current_action_exposes_profit_max_solar_export_diagnostics():
    sensor = _sensor_module()
    desc = next(
        d
        for d in sensor.OPTIMIZER_ACTION_SENSORS
        if d.key == "optimization_status"
    )
    solar_export = {
        "capability": {
            "supported": True,
            "reason": "supported",
            "export_limit_kw": 5.0,
            "selected_slots": 0,
            "current_slot": {
                "selected": False,
                "reason": "insufficient_cheaper_replenishment",
            },
            "rejection_counts": {"insufficient_cheaper_replenishment": 1},
        },
        "planned_slots": 0,
        "hold": {"active": False},
    }
    payload = {
        "current_action": "self_consumption",
        "current_action_detail": None,
        "planned_current_action": "self_consumption",
        "planned_current_action_detail": None,
        "effective_current_action": "self_consumption",
        "effective_current_action_detail": None,
        "monitoring_mode": False,
        "profit_max_enabled": True,
        "profit_max_solar_export": solar_export,
    }
    entity = sensor.OptimizerActionSensor(
        SimpleNamespace(data=payload),
        desc,
        _entry("amber"),
    )

    attrs = entity.extra_state_attributes
    assert attrs["current_action_detail"] is None
    assert attrs["planned_action_detail"] is None
    assert attrs["effective_action_detail"] is None
    assert attrs["monitoring_mode"] is False
    assert attrs["profit_max_enabled"] is True
    assert attrs["profit_max_solar_export"] == solar_export


def test_optimizer_current_action_exposes_charge_by_time_settings():
    sensor = _sensor_module()
    desc = next(
        d
        for d in sensor.OPTIMIZER_ACTION_SENSORS
        if d.key == "optimization_status"
    )
    payload = {
        "current_action": "charge",
        "charge_by_time_enabled": True,
        "config": {
            "charge_by_time_target_time": "17:15",
            "charge_by_time_target_soc": 100,
        },
    }
    entity = sensor.OptimizerActionSensor(
        SimpleNamespace(data=payload),
        desc,
        _entry("amber"),
    )

    attrs = entity.extra_state_attributes
    assert attrs["charge_by_time_enabled"] is True
    assert attrs["charge_by_time_target_time"] == "17:15"
    assert attrs["charge_by_time_target_soc"] == 100


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


def test_lp_battery_forecast_exposes_home_and_export_split_attributes():
    sensor = _sensor_module()
    desc = next(d for d in sensor.LP_FORECAST_SENSORS if d.key == "lp_battery_power_forecast")
    entity = sensor.LPForecastSensor(
        SimpleNamespace(get_forecast_data=lambda: {
            "battery_schedule_available": True,
            "battery_power_now_kw": 7.5,
            "battery_charge_peak_kw": 0.0,
            "battery_discharge_peak_kw": 7.5,
            "battery_charge_forecast": [0.0],
            "battery_discharge_forecast": [7.5],
            "battery_home_consumption_forecast": [2.0],
            "battery_export_forecast": [5.5],
            "battery_power_forecast": [7.5],
        }),
        desc,
        _entry("amber"),
    )
    entity.hass = _hass("AUD")

    attrs = entity.extra_state_attributes

    assert attrs["discharge_values_kw"] == [7.5]
    assert attrs["home_consumption_values_kw"] == [2.0]
    assert attrs["export_values_kw"] == [5.5]


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

    assert entity.native_value == "PEAK (25.00c/kWh)"
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


def test_rolling_tariff_schedule_labels_elapsed_slots_as_tomorrow(monkeypatch):
    sensor = _sensor_module()
    hass = _hass("AUD")
    entry = _entry("amber")
    monkeypatch.setattr(
        sensor.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 7, 29, 8, 53, tzinfo=timezone.utc),
    )
    hass.data = {
        "power_sync": {
            entry.entry_id: {
                "tariff_schedule": {
                    "buy_prices": {
                        "PERIOD_18_00": 2.15,
                        "PERIOD_18_30": 0.39,
                        "PERIOD_19_00": 0.37,
                    },
                    "sell_prices": {
                        "PERIOD_18_00": 1.73,
                        "PERIOD_18_30": 0.13,
                        "PERIOD_19_00": 0.11,
                    },
                    "rolling_24h": True,
                    "rolling_anchor": "2026-07-29T18:50:00+10:00",
                    "last_sync": "2026-07-29 18:50:00",
                }
            }
        }
    }
    entity = sensor.TariffScheduleSensor(hass, entry)

    schedule = entity.extra_state_attributes["schedule"]

    assert schedule[0] == {
        "time": "18:00",
        "date": "2026-07-30",
        "date_label": "Tomorrow",
        "buy": 2.15,
        "sell": 1.73,
    }
    assert schedule[1]["date"] == "2026-07-29"
    assert schedule[1]["date_label"] == "Today"
    assert schedule[2]["date_label"] == "Today"


def test_rolling_tariff_schedule_relabels_cached_absolute_dates_after_midnight(monkeypatch):
    sensor = _sensor_module()
    hass = _hass("AUD")
    entry = _entry("amber")
    monkeypatch.setattr(
        sensor.dt_util,
        "now",
        lambda *args, **kwargs: datetime(
            2026,
            7,
            30,
            0,
            5,
            tzinfo=timezone(timedelta(hours=10)),
        ),
    )
    hass.data = {
        "power_sync": {
            entry.entry_id: {
                "tariff_schedule": {
                    "buy_prices": {
                        "PERIOD_00_00": 0.16,
                        "PERIOD_23_00": 0.20,
                        "PERIOD_23_30": 0.40,
                    },
                    "sell_prices": {},
                    "rolling_24h": True,
                    "rolling_anchor": "2026-07-29T23:50:00+10:00",
                    "last_sync": "2026-07-29 23:50:00",
                }
            }
        }
    }
    entity = sensor.TariffScheduleSensor(hass, entry)

    schedule = entity.extra_state_attributes["schedule"]

    assert schedule[0]["date"] == "2026-07-30"
    assert schedule[0]["date_label"] == "Today"
    assert schedule[1]["date"] == "2026-07-30"
    assert schedule[1]["date_label"] == "Today"
    assert schedule[2]["date"] == "2026-07-29"
    assert schedule[2]["date_label"] == "Yesterday"


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
                    identifiers={("teslemetry", "5YJTEST0000000001", "vehicle")},
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
        entity_id="sensor.primary_ev_charger_power",
        attributes={"friendly_name": "Primary EV Charger Power"},
    )
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain=None: [state]))

    assert sensor._has_solaredge_ev_power(hass) is False


def test_ev_status_sensor_labels_solaredge_coordinator_power():
    sensor = _sensor_module()
    power_sync = sys.modules["power_sync"]

    class DisplayCoordinator:
        snapshot = {
            "site": {"ev_power_kw": 7.4},
            "loadpoints": [{
                "loadpoint_id": "solaredge_ev_charger",
                "vehicle_name": "SolarEdge EV Charger",
                "current_power_kw": 7.4,
                "connected": True,
                "actual_charging": True,
                "status": "charging",
            }],
        }

    power_sync._get_ev_display_coordinator = lambda hass, entry: DisplayCoordinator()
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

    entity._handle_display_update(DisplayCoordinator.snapshot)

    assert entity.native_value == 7.4
    assert entity.extra_state_attributes["vehicle_name"] == "SolarEdge EV Charger"
    assert entity.extra_state_attributes["vehicle_id"] == "solaredge_ev_charger"


def test_ev_status_sensor_exposes_idle_sigenergy_evac_presence():
    sensor = _sensor_module()
    power_sync = sys.modules["power_sync"]

    class DisplayCoordinator:
        async def async_refresh(self, *, force=False):
            return {
                "site": {"ev_power_kw": 0.0},
                "loadpoints": [{
                    "loadpoint_id": "sigenergy_charger",
                    "vehicle_name": "Sigenergy EVAC",
                    "charger_type": "sigenergy",
                    "current_power_kw": 0.0,
                    "soc": None,
                    "connected": True,
                    "actual_charging": False,
                    "status": "connected_idle",
                }],
            }

    power_sync._get_ev_display_coordinator = lambda hass, entry: DisplayCoordinator()
    power_sync._get_ev_vehicles_status = lambda hass, entry: []

    async def read_sigenergy_charger_state(entry, hass):
        return SimpleNamespace(
            charger_type="evac",
            power_kw=0.0,
            vehicle_soc=None,
            is_connected=True,
            is_charging=False,
            is_discharging=False,
        )

    power_sync._read_sigenergy_charger_state_for_entry = read_sigenergy_charger_state
    async def get_ev_load_observations(hass, entry, vehicles):
        state = await read_sigenergy_charger_state(entry, hass)
        hass.data[sensor.DOMAIN][entry.entry_id][
            "observed_sigenergy_charger_state"
        ] = state
        ev_load = importlib.import_module("power_sync.ev_load")
        return [
            ev_load.EvLoadObservation(
                physical_load_key="sigenergy:evac",
                source_key="sigenergy_evac",
                power_kw=0.0,
                observed_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
                active=False,
                measurement_kind=ev_load.EvMeasurementKind.INTEGRATED_CHARGER,
            )
        ]

    power_sync._get_ev_load_observations = get_ev_load_observations
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
