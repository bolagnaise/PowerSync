"""Sensor currency metadata tests."""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
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
    ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_event = types.ModuleType("homeassistant.helpers.event")
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

    ha_sensor.SensorEntityDescription = SensorEntityDescription
    ha_sensor.SensorEntity = SensorEntity
    ha_sensor.SensorDeviceClass = SimpleNamespace(
        BATTERY="battery",
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
    ha_update.CoordinatorEntity = CoordinatorEntity
    ha_dispatcher.async_dispatcher_connect = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ha_dt.as_local = lambda value: value
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ha_util.dt = ha_dt
    ha_helpers.entity_platform = ha_entity_platform
    ha_helpers.update_coordinator = ha_update
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.event = ha_event
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
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.event"] = ha_event
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
