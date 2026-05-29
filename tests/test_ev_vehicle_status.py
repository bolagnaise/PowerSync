"""Tests for PowerSync Tesla vehicle status normalization."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components"


def _install_import_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_http = types.ModuleType("homeassistant.components.http")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    ha_const = types.ModuleType("homeassistant.const")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_config_entries.ConfigEntryState = SimpleNamespace(LOADED="loaded")
    ha_config_validation.config_entry_only_config_schema = lambda domain: {}
    ha_const.Platform = SimpleNamespace(
        SENSOR="sensor",
        SWITCH="switch",
        SELECT="select",
        NUMBER="number",
        BINARY_SENSOR="binary_sensor",
        BUTTON="button",
        UPDATE="update",
    )
    ha_const.CONF_ACCESS_TOKEN = "access_token"
    ha_const.CONF_TOKEN = "token"
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_core.ServiceCall = type("ServiceCall", (), {})
    ha_core.SupportsResponse = SimpleNamespace(ONLY="only", OPTIONAL="optional", NONE="none")
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_http.HomeAssistantView = type("HomeAssistantView", (), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_device_registry.async_get = lambda hass: hass.device_registry
    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_event.async_track_utc_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_utc_time = lambda *args, **kwargs: (lambda: None)
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_storage.Store = type("Store", (), {})
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_dispatcher.async_dispatcher_connect = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime.now(timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime.now(timezone.utc)
    ha_util.dt = ha_dt

    ha_helpers.aiohttp_client = ha_aiohttp_client
    ha_helpers.config_validation = ha_config_validation
    ha_helpers.device_registry = ha_device_registry
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.entity_registry = ha_entity_registry
    ha_helpers.event = ha_event
    ha_helpers.storage = ha_storage
    ha_root.components = ha_components
    ha_root.config_entries = ha_config_entries
    ha_root.const = ha_const
    ha_root.core = ha_core
    ha_root.exceptions = ha_exceptions
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util
    ha_components.http = ha_http

    for name, module in {
        "homeassistant": ha_root,
        "homeassistant.components": ha_components,
        "homeassistant.components.http": ha_http,
        "homeassistant.config_entries": ha_config_entries,
        "homeassistant.const": ha_const,
        "homeassistant.core": ha_core,
        "homeassistant.exceptions": ha_exceptions,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.aiohttp_client": ha_aiohttp_client,
        "homeassistant.helpers.config_validation": ha_config_validation,
        "homeassistant.helpers.device_registry": ha_device_registry,
        "homeassistant.helpers.dispatcher": ha_dispatcher,
        "homeassistant.helpers.entity_registry": ha_entity_registry,
        "homeassistant.helpers.event": ha_event,
        "homeassistant.helpers.storage": ha_storage,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
    }.items():
        sys.modules[name] = module

    currency = types.ModuleType("power_sync.currency")
    currency.DEFAULT_CURRENCY = "AUD"
    currency.currency_for_entry = lambda *args, **kwargs: "AUD"
    currency.currency_metadata = lambda *args, **kwargs: {}
    currency.normalize_currency = lambda value=None, *args, **kwargs: value or "AUD"
    sys.modules["power_sync.currency"] = currency

    inverters = types.ModuleType("power_sync.inverters")
    inverters.get_inverter_controller = lambda *args, **kwargs: None
    sys.modules["power_sync.inverters"] = inverters

    optimization_coordinator = types.ModuleType("power_sync.optimization.coordinator")
    optimization_coordinator.OptimizationCoordinator = type("OptimizationCoordinator", (), {})
    sys.modules["power_sync.optimization.coordinator"] = optimization_coordinator

    coordinator = types.ModuleType("power_sync.coordinator")
    for class_name in (
        "AmberPriceCoordinator",
        "AmberUsageCoordinator",
        "TeslaEnergyCoordinator",
        "SigenergyEnergyCoordinator",
        "SungrowEnergyCoordinator",
        "DualSungrowCoordinator",
        "FoxESSEnergyCoordinator",
        "FoxESSEntityEnergyCoordinator",
        "FoxESSCloudEnergyCoordinator",
        "GoodWeEnergyCoordinator",
        "AlphaESSEnergyCoordinator",
        "ESYSunhomeEnergyCoordinator",
        "SolaxBatteryEnergyCoordinator",
        "SajH2EnergyCoordinator",
        "FroniusReservaEnergyCoordinator",
        "NeovoltEnergyCoordinator",
        "SolarEdgeEnergyCoordinator",
        "DemandChargeCoordinator",
        "AEMOSensorCoordinator",
        "OctopusPriceCoordinator",
        "LocalvoltsPriceCoordinator",
    ):
        setattr(coordinator, class_name, type(class_name, (), {}))
    sys.modules["power_sync.coordinator"] = coordinator


def _power_sync_module():
    _install_import_stubs()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.modules.pop("power_sync", None)
    return importlib.import_module("power_sync")


class _State:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        return [
            state for entity_id, state in self._states.items()
            if entity_id.startswith(f"{domain}.")
        ]


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Hass:
    def __init__(
        self,
        states: list[_State],
        registry_entities: dict[str, object] | None = None,
        devices: dict[str, object] | None = None,
        entry_data: dict | None = None,
    ) -> None:
        self.states = _States(states)
        self.entity_registry = SimpleNamespace(entities=registry_entities or {})
        self.device_registry = SimpleNamespace(devices=devices or {})
        self.data = {"power_sync": {"entry-1": entry_data or {}}}


def _entity(entity_id: str, device_id: str):
    return SimpleNamespace(
        entity_id=entity_id,
        device_id=device_id,
        domain=entity_id.split(".", 1)[0],
    )


def _tesla_hass(states: list[_State]) -> _Hass:
    device_id = "device-1"
    entity_ids = [state.entity_id for state in states]
    return _Hass(
        states,
        {entity_id: _entity(entity_id, device_id) for entity_id in entity_ids},
        {
            device_id: SimpleNamespace(
                id=device_id,
                name="TESSY",
                identifiers={("teslemetry", "LRWYHCEK3PC907290")},
            )
        },
    )


def test_ev_vehicle_status_ignores_stale_power_when_tesla_is_away_and_disconnected():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.tessy_charger_power_2", "0.4", {"unit_of_measurement": "kW"}),
        _State("sensor.tessy_charging_2", "disconnected"),
        _State("binary_sensor.tessy_charge_cable_2", "off"),
        _State("device_tracker.tessy_location_2", "not_home"),
        _State("sensor.tessy_battery_level_2", "72.887", {"unit_of_measurement": "%"}),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": "LRWYHCEK3PC907290",
        "vehicle_name": "TESSY",
        "ev_power_kw": 0.0,
        "ev_soc": 72,
        "is_connected": False,
        "is_charging": False,
    }]


def test_ev_vehicle_status_keeps_real_charging_power_when_charging():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.tessy_charger_power_2", "6.8", {"unit_of_measurement": "kW"}),
        _State("sensor.tessy_charging_2", "charging"),
        _State("binary_sensor.tessy_charge_cable_2", "on"),
        _State("device_tracker.tessy_location_2", "home"),
        _State("sensor.tessy_battery_level_2", "73", {"unit_of_measurement": "%"}),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles[0]["ev_power_kw"] == 6.8
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is True
    assert vehicles[0]["ev_soc"] == 73


def test_ev_vehicle_status_drops_stale_power_for_connected_idle_state():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.tessy_charger_power", "0.4", {"unit_of_measurement": "kW"}),
        _State("sensor.tessy_charging", "stopped"),
        _State("binary_sensor.tessy_charge_cable", "on"),
        _State("device_tracker.tessy_location", "home"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles[0]["ev_power_kw"] == 0.0
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is False


def test_ev_vehicle_status_uses_wall_connector_power_without_vehicle_sensors():
    power_sync = _power_sync_module()
    hass = _Hass([
        _State("sensor.tesla_wall_connector_total_power", "3.4", {"unit_of_measurement": "kW"}),
        _State("sensor.wall_connector_vehicle_2", "connected"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": "wall_connector",
        "vehicle_name": "Wall Connector",
        "ev_power_kw": 3.4,
        "ev_soc": None,
        "is_connected": True,
        "is_charging": True,
    }]


def test_aggregate_ev_status_ignores_teslemetry_bt_power_when_not_charging():
    power_sync = _power_sync_module()
    vin = "LRWYHCEK3PC907290"
    hass = _Hass([
        _State(f"sensor.{vin}_charging_state", "Stopped"),
        _State(f"switch.{vin}_charge", "off"),
        _State(f"sensor.{vin}_charger_power", "7.2", {"unit_of_measurement": "kW"}),
        _State(f"sensor.{vin}_battery_level", "72", {"unit_of_measurement": "%"}),
    ])

    status = power_sync._get_ev_vehicle_status(hass, _Entry())

    assert status == {"ev_power_kw": 0.0, "ev_soc": 72}


def test_aggregate_ev_status_uses_configured_generic_charger_soc():
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_soc_entity": "sensor.solaredge_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.solaredge_ev_soc", "64"),
    ])

    status = power_sync._get_ev_vehicle_status(hass, entry)

    assert status == {"ev_power_kw": 0.0, "ev_soc": 64}


def test_aggregate_ev_status_uses_generic_charger_fallback_soc():
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_soc_entity": "sensor.primary_ev_soc",
            "generic_charger_soc_entity_2": "sensor.fallback_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.primary_ev_soc", "unknown"),
        _State("sensor.fallback_ev_soc", "68"),
    ])

    status = power_sync._get_ev_vehicle_status(hass, entry)

    assert status == {"ev_power_kw": 0.0, "ev_soc": 68}


def test_aggregate_ev_status_prefers_configured_generic_soc_over_vehicle_fallback():
    power_sync = _power_sync_module()
    vin = "LRWYHCEK3PC907290"
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_soc_entity": "sensor.solaredge_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.solaredge_ev_soc", "64"),
        _State(f"sensor.{vin}_charging_state", "Stopped"),
        _State(f"switch.{vin}_charge", "off"),
        _State(f"sensor.{vin}_battery_level", "72", {"unit_of_measurement": "%"}),
    ])

    status = power_sync._get_ev_vehicle_status(hass, entry)

    assert status == {"ev_power_kw": 0.0, "ev_soc": 64}
