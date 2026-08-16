"""Tests for PowerSync Tesla vehicle status normalization."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
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
    ha_core.callback = lambda func: func
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    ha_http.HomeAssistantView = type("HomeAssistantView", (), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_device_registry.async_get = lambda hass: hass.device_registry
    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_event.async_track_utc_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
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
    optimization_coordinator.OptimizationConfig = type("OptimizationConfig", (), {})
    optimization_coordinator.sigenergy_capped_optimizer_limit_w = (
        lambda *args, **kwargs: None
    )
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
        "CustomEntityEnergyCoordinator",
        "DiscoveredEntityEnergyCoordinator",
        "FoxESSCloudEnergyCoordinator",
        "GoodWeEnergyCoordinator",
        "AlphaESSEnergyCoordinator",
        "ESYSunhomeEnergyCoordinator",
        "SolaxBatteryEnergyCoordinator",
        "SajH2EnergyCoordinator",
        "FroniusReservaEnergyCoordinator",
        "NeovoltEnergyCoordinator",
        "SolarEdgeEnergyCoordinator",
        "AnkerSolixEnergyCoordinator",
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
                name="PRIMARY EV",
                identifiers={("teslemetry", "5YJTEST0000000001")},
            )
        },
    )


def test_ev_vehicle_status_ignores_stale_power_when_tesla_is_away_and_disconnected():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power_2", "0.4", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_2", "disconnected"),
        _State("binary_sensor.primary_ev_charge_cable_2", "off"),
        _State("device_tracker.primary_ev_location_2", "not_home"),
        _State("sensor.primary_ev_battery_level_2", "72.887", {"unit_of_measurement": "%"}),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": "5YJTEST0000000001",
        "vehicle_name": "PRIMARY EV",
        "ev_power_kw": 0.0,
        "ev_soc": 72,
        "is_connected": False,
        "is_charging": False,
        "site_presence": "away",
    }]
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 0.0


def test_ev_vehicle_status_excludes_named_zone_charging_from_home_site():
    """Ticket #284: a named HA zone is away, not a home EV load."""
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power_2", "2.0", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "amma and appa's"),
        _State("sensor.primary_ev_battery_level_2", "86", {"unit_of_measurement": "%"}),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [{
                "din": "1529455-02-E--TEST",
                "wall_connector_state": 2,
                "wall_connector_power": 0,
            }]
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    away_vehicle = next(
        vehicle for vehicle in vehicles
        if vehicle.get("vehicle_id") == "5YJTEST0000000001"
    )
    assert away_vehicle == {
        "vehicle_id": "5YJTEST0000000001",
        "vehicle_name": "PRIMARY EV",
        "ev_power_kw": 0.0,
        "ev_soc": 86,
        "is_connected": False,
        "is_charging": False,
        "site_presence": "away",
    }
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 0.0


def test_named_zone_also_excludes_paired_ble_bridge_power():
    """A paired BLE view of the remote charge must not restore site power."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    states = [
        _State("sensor.primary_ev_charger_power", "2.0", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_state", "charging"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("device_tracker.primary_ev_location", "amma and appa's"),
        _State("sensor.primary_ev_battery_level", "86"),
        _State("binary_sensor.remote_ble_status", "on"),
        _State("sensor.remote_ble_charging_state", "Charging"),
        _State("binary_sensor.remote_ble_charge_flap", "on"),
        _State("sensor.remote_ble_charge_power", "2.0", {"unit_of_measurement": "kW"}),
        _State("sensor.remote_ble_charge_level", "86"),
    ]
    hass = _tesla_hass(states)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_BOTH,
            "tesla_ble_entity_prefix": "remote_ble",
            "tesla_ble_vehicle_mapping": f"{vin}=remote_ble",
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert len(vehicles) == 1
    assert vehicles[0]["vehicle_id"] == vin
    assert vehicles[0]["ev_power_kw"] == 0.0
    assert vehicles[0]["is_connected"] is False
    assert vehicles[0]["is_charging"] is False
    assert vehicles[0]["site_presence"] == "away"
    assert power_sync._get_ev_vehicle_status(hass, entry) == {
        "ev_power_kw": 0.0,
        "ev_soc": 86,
    }


def test_named_zone_does_not_suppress_unpaired_ble_outside_both_mode():
    """A stale Fleet tracker cannot claim an unrelated BLE-only vehicle."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power",
            "2.0",
            {"unit_of_measurement": "kW"},
        ),
        _State("sensor.primary_ev_charging_state", "charging"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("device_tracker.primary_ev_location", "amma and appa's"),
        _State("sensor.primary_ev_battery_level", "86"),
        _State("binary_sensor.remote_ble_status", "on"),
        _State("sensor.remote_ble_charging_state", "Charging"),
        _State("binary_sensor.remote_ble_charge_flap", "on"),
        _State(
            "sensor.remote_ble_charge_power",
            "3.0",
            {"unit_of_measurement": "kW"},
        ),
        _State("sensor.remote_ble_charge_level", "64"),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_FLEET_API,
            "tesla_ble_entity_prefix": "remote_ble",
            "tesla_ble_vehicle_mapping": f"{vin}=remote_ble",
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert len(vehicles) == 1
    assert vehicles[0]["ev_power_kw"] == 3.0
    assert power_sync._get_ev_vehicle_status(hass, entry)["ev_power_kw"] == 3.0
    assert power_sync._get_external_tesla_ev_power_kw(hass, entry) == 3.0


def test_ev_vehicle_status_keeps_real_charging_power_when_charging():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power_2", "6.8", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "home"),
        _State("sensor.primary_ev_battery_level_2", "73", {"unit_of_measurement": "%"}),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles[0]["ev_power_kw"] == 6.8
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is True
    assert vehicles[0]["ev_soc"] == 73


def test_both_provider_ble_bridge_coalesces_without_hiding_fleet_only_vehicle():
    power_sync = _power_sync_module()
    primary_vin = "5YJTEST0000000001"
    secondary_vin = "5YJTEST0000000002"
    states = [
        _State("sensor.primary_ev_battery_level", "78"),
        _State("sensor.primary_ev_charging_state", "stopped"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("sensor.secondary_ev_battery_level", "69"),
        _State("sensor.secondary_ev_charging_state", "charging"),
        _State("binary_sensor.secondary_ev_charge_cable", "on"),
        _State("sensor.secondary_ev_charger_power", "2.4", {"unit_of_measurement": "kW"}),
        _State("binary_sensor.primary_ev_bridge_status", "on"),
        _State("sensor.primary_ev_bridge_charge_level", "78"),
        _State("sensor.primary_ev_bridge_charging_state", "stopped"),
        _State("binary_sensor.primary_ev_bridge_charge_flap", "on"),
    ]
    registry_entities = {
        state.entity_id: _entity(
            state.entity_id,
            "primary_ev-device" if "primary_ev_" in state.entity_id else "secondary_ev-device",
        )
        for state in states[:7]
    }
    hass = _Hass(
        states,
        registry_entities,
        {
            "primary_ev-device": SimpleNamespace(
                id="primary_ev-device",
                name="PRIMARY EV",
                identifiers={("teslemetry", primary_vin)},
            ),
            "secondary_ev-device": SimpleNamespace(
                id="secondary_ev-device",
                name="Secondary EV",
                identifiers={("tesla_fleet", secondary_vin)},
            ),
        },
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_BOTH,
            "tesla_ble_entity_prefix": "primary_ev_bridge",
            "tesla_ble_vehicle_mapping": f"{primary_vin}=primary_ev_bridge",
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert [vehicle["vehicle_name"] for vehicle in vehicles] == ["PRIMARY EV", "Secondary EV"]
    assert [vehicle["vehicle_id"] for vehicle in vehicles] == [primary_vin, secondary_vin]
    assert vehicles[0]["ev_soc"] == 78
    assert vehicles[1]["ev_soc"] == 69
    assert vehicles[1]["ev_power_kw"] == 2.4


def test_autodetected_ble_bridge_pairs_with_single_fleet_vehicle_and_commands():
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    states = [
        _State("sensor.primary_ev_battery_level", "75"),
        _State("sensor.primary_ev_charging_state", "stopped"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("sensor.garage_ble_charging_state", "Stopped"),
        _State("binary_sensor.garage_ble_ble_status", "on"),
        _State("sensor.garage_ble_charge_level", "81"),
        _State("binary_sensor.garage_ble_charge_flap", "on"),
    ]
    hass = _Hass(
        states,
        {
            state.entity_id: _entity(state.entity_id, "primary_ev-device")
            for state in states[:3]
        },
        {
            "primary_ev-device": SimpleNamespace(
                id="primary_ev-device",
                name="PRIMARY EV",
                identifiers={("tesla_fleet", vin)},
            )
        },
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_BOTH,
            "tesla_ble_entity_prefix": "tesla_ble",
        },
    )
    config = {**entry.data, **entry.options}

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert [vehicle["vehicle_id"] for vehicle in vehicles] == [vin]
    assert power_sync._ble_prefix_for_vehicle(hass, config, vin) == "garage_ble"


def test_mobile_command_identity_and_ble_pairing_deduplicate_provider_devices():
    power_sync = _power_sync_module()
    primary_vin = "5YJTEST0000000001"
    secondary_vin = "5YJTEST0000000002"
    hass = _Hass(
        [],
        devices={
            "fleet-primary_ev": SimpleNamespace(
                id="fleet-primary_ev",
                name="PRIMARY EV",
                identifiers={("tesla_fleet", primary_vin)},
            ),
            "teslemetry-primary_ev": SimpleNamespace(
                id="teslemetry-primary_ev",
                name="PRIMARY EV",
                identifiers={("teslemetry", primary_vin)},
            ),
            "fleet-secondary_ev": SimpleNamespace(
                id="fleet-secondary_ev",
                name="Secondary EV",
                identifiers={("tesla_fleet", secondary_vin)},
            ),
        },
    )
    one_bridge = {
        "ev_provider": power_sync.EV_PROVIDER_BOTH,
        "tesla_ble_entity_prefix": "primary_ev_bridge",
        "tesla_ble_vehicle_mapping": f"{primary_vin}=primary_ev_bridge",
    }
    two_bridges = {
        **one_bridge,
        "tesla_ble_entity_prefix": "bridge_alpha,bridge_beta",
        "tesla_ble_vehicle_mapping": (
            f"{primary_vin}=bridge_beta,{secondary_vin}=bridge_alpha"
        ),
    }
    view = power_sync.EVVehicleCommandView(hass)
    view._get_powersync_config = lambda: one_bridge

    assert view._get_vin_from_vehicle_id("1") == primary_vin
    assert view._get_vin_from_vehicle_id("2") == secondary_vin
    assert power_sync._ble_prefix_for_vehicle(hass, one_bridge, primary_vin) == "primary_ev_bridge"
    assert power_sync._ble_prefix_for_vehicle(hass, one_bridge, secondary_vin) is None
    assert power_sync._ble_prefix_for_vehicle(hass, two_bridges, primary_vin) == "bridge_beta"
    assert power_sync._ble_prefix_for_vehicle(hass, two_bridges, secondary_vin) == "bridge_alpha"

    partially_mapped = {
        **two_bridges,
        "tesla_ble_vehicle_mapping": f"{primary_vin}=bridge_beta",
    }
    view._get_powersync_config = lambda: partially_mapped
    assert view._get_vin_from_vehicle_id("3") == "ble_bridge_alpha"


def test_mobile_ble_telemetry_merges_by_vin_mapping_not_prefix_order():
    power_sync = _power_sync_module()
    vin_a = "5YJTEST0000000001"
    vin_b = "5YJTEST0000000002"
    view = power_sync.EVVehiclesView(_Hass([]))
    ble_vehicles = {
        "bridge_alpha": {
            "battery_level": 41,
            "charging_state": "Stopped",
            "is_online": True,
        },
        "bridge_beta": {
            "battery_level": 82,
            "charging_state": "Charging",
            "is_online": True,
        },
    }
    view._get_tesla_ble_vehicle = lambda prefix, vehicle_index=1: dict(
        ble_vehicles[prefix]
    )
    vehicles = [
        {"vin": vin_a, "battery_level": 60, "charging_state": "Stopped"},
        {"vin": vin_b, "battery_level": 70, "charging_state": "Stopped"},
    ]
    config = {
        "ev_provider": power_sync.EV_PROVIDER_BOTH,
        "tesla_ble_entity_prefix": "bridge_alpha,bridge_beta",
        "tesla_ble_vehicle_mapping": (
            f"{vin_a}=bridge_beta,{vin_b}=bridge_alpha"
        ),
    }

    view._merge_tesla_ble_vehicles(
        vehicles,
        config,
        ["bridge_alpha", "bridge_beta"],
    )

    assert len(vehicles) == 2
    assert vehicles[0]["battery_level"] == 82
    assert vehicles[0]["charging_state"] == "Charging"
    assert vehicles[1]["battery_level"] == 41


def test_external_tesla_power_uses_coalesced_charging_vehicle():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power_2", "7.0", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "home"),
    ])

    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 7.0


def test_external_tesla_power_excludes_other_charger_types():
    power_sync = _power_sync_module()
    power_sync._get_ev_vehicles_status = lambda hass, entry: [
        {
            "vehicle_id": "generic_ev",
            "brand": "generic",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "sigenergy_charger",
            "charger_type": "evdc",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "5YJTEST0000000001",
            "brand": "tesla",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "5YJTEST0000000002",
            "brand": "tesla",
            "ev_power_kw": 3.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "wall_connector",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
    ]

    assert power_sync._get_external_tesla_ev_power_kw(_Hass([]), _Entry()) == 10.0


def test_external_tesla_power_honors_fleet_provider_with_ble_duplicate():
    power_sync = _power_sync_module()
    power_sync._get_ev_vehicles_status = lambda hass, entry: [
        {
            "vehicle_id": "5YJTEST0000000001",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "5YJTEST0000000002",
            "ev_power_kw": 0.0,
            "is_charging": False,
        },
        {
            "vehicle_id": "ble_primary_ev",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
    ]

    assert power_sync._get_external_tesla_ev_power_kw(_Hass([]), _Entry()) == 7.0


def test_external_tesla_power_coalesces_reversed_fleet_and_ble_in_both_mode():
    power_sync = _power_sync_module()
    power_sync._get_ev_vehicles_status = lambda hass, entry: [
        {
            "vehicle_id": "5YJTEST0000000001",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "5YJTEST0000000002",
            "ev_power_kw": 3.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "ble_primary_ev",
            "ev_power_kw": 3.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "ble_theo",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "wall_connector",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
    ]
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": power_sync.EV_PROVIDER_BOTH},
    )

    assert power_sync._get_external_tesla_ev_power_kw(_Hass([]), entry) == 10.0


def test_external_tesla_power_uses_conservative_total_for_partial_both_mode():
    power_sync = _power_sync_module()
    power_sync._get_ev_vehicles_status = lambda hass, entry: [
        {
            "vehicle_id": "5YJTEST0000000001",
            "ev_power_kw": 3.0,
            "is_charging": True,
        },
        {
            "vehicle_id": "ble_primary_ev",
            "ev_power_kw": 7.0,
            "is_charging": True,
        },
    ]
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": power_sync.EV_PROVIDER_BOTH},
    )

    assert power_sync._get_external_tesla_ev_power_kw(_Hass([]), entry) == 7.0


def test_mobile_ble_vehicle_accepts_connection_status_without_optional_node_status():
    """Mobile Sync must show a configured BLE bridge that exposes BLE Status."""
    power_sync = _power_sync_module()
    hass = _Hass([
        _State("binary_sensor.tesla_flinn_ble_status", "off"),
        _State("sensor.tesla_flinn_charging_state", "Unknown"),
        _State("sensor.tesla_flinn_charge_level", "79"),
    ])

    vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("tesla_flinn")

    assert vehicle is not None
    assert vehicle["id"] == "ble_tesla_flinn"
    assert vehicle["battery_level"] == 79
    assert vehicle["is_online"] is False

    canonical_hass = _Hass([
        _State("binary_sensor.tesla_flinn_status", "off"),
        _State("binary_sensor.tesla_flinn_ble_status", "on"),
    ])
    canonical_vehicle = power_sync.EVVehiclesView(
        canonical_hass
    )._get_tesla_ble_vehicle("tesla_flinn")

    assert canonical_vehicle is not None
    assert canonical_vehicle["is_online"] is False


def test_ev_vehicle_status_prefers_wall_connector_power_for_single_charging_tesla():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power_2", "7.0", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "home"),
        _State("sensor.primary_ev_battery_level_2", "70", {"unit_of_measurement": "%"}),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [
                {
                    "wall_connector_state": 2,
                    "wall_connector_power": 3400,
                }
            ]
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": "5YJTEST0000000001",
        "vehicle_name": "PRIMARY EV",
        "ev_power_kw": 3.4,
        "ev_soc": 70,
        "is_connected": True,
        "is_charging": True,
    }]
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 3.4


def test_ev_vehicle_status_drops_stale_power_for_connected_idle_state():
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power", "0.4", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging", "stopped"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("device_tracker.primary_ev_location", "home"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles[0]["ev_power_kw"] == 0.0
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is False


def test_ev_vehicle_status_keeps_wall_connector_auxiliary_draw_idle():
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power", "1", {"unit_of_measurement": "kW"}),
        _State("sensor.primary_ev_charging", "stopped"),
        _State("binary_sensor.primary_ev_charge_cable", "on"),
        _State("device_tracker.primary_ev_location", "home"),
        _State("sensor.primary_ev_battery_level", "72", {"unit_of_measurement": "%"}),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [
                {
                    "wall_connector_state": 11,
                    "wall_connector_power": 579.5,
                    "vin": vin,
                }
            ]
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": vin,
        "vehicle_name": "PRIMARY EV",
        "ev_power_kw": 0.0,
        "auxiliary_power_kw": 0.5795,
        "ev_soc": 72,
        "is_connected": True,
        "is_charging": False,
    }]

    async def no_sigenergy_charger(*args, **kwargs):
        return None

    power_sync._read_sigenergy_charger_state_for_entry = no_sigenergy_charger
    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, _Entry(), vehicles)
    )
    assert len(observations) == 1
    assert observations[0].power_kw == 0.5795
    assert observations[0].active is False
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 0.5795


def test_ev_vehicle_status_uses_wall_connector_power_without_vehicle_sensors():
    power_sync = _power_sync_module()
    hass = _Hass([
        _State("sensor.tesla_wall_connector_total_power", "3.4", {"unit_of_measurement": "kW"}),
        _State("sensor.wall_connector_vehicle_2", "connected"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert vehicles == [{
        "vehicle_id": "wall_connector_ha",
        "charger_id": "wall_connector_ha",
        "vehicle_name": "Wall Connector",
        "ev_power_kw": 3.4,
        "ev_soc": None,
        "is_connected": True,
        "is_charging": True,
    }]
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 3.4


def test_aggregate_ev_status_ignores_teslemetry_bt_power_when_not_charging():
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    hass = _Hass([
        _State(f"sensor.{vin}_charging_state", "Stopped"),
        _State(f"switch.{vin}_charge", "off"),
        _State(f"sensor.{vin}_charger_power", "7.2", {"unit_of_measurement": "kW"}),
        _State(f"sensor.{vin}_battery_level", "72", {"unit_of_measurement": "%"}),
    ])

    status = power_sync._get_ev_vehicle_status(hass, _Entry())

    assert status == {"ev_power_kw": 0.0, "ev_soc": 72}


def test_aggregate_ev_status_excludes_named_zone_power_from_site_fallback():
    """Ticket #284: the no-Wall-Connector fallback must retain Home Load."""
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power_2",
            "2.0",
            {"unit_of_measurement": "kW"},
        ),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "amma and appa's"),
        _State(
            "sensor.primary_ev_battery_level_2",
            "86",
            {"unit_of_measurement": "%"},
        ),
    ])

    status = power_sync._get_ev_vehicle_status(hass, _Entry())

    assert status == {"ev_power_kw": 0.0, "ev_soc": None}
    assert max(0.0, 0.206 - status["ev_power_kw"]) == 0.206


def test_aggregate_ev_status_keeps_home_zone_power_in_site_fallback():
    """The shared presence gate must retain literal-home charging power."""
    power_sync = _power_sync_module()
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power_2",
            "2.0",
            {"unit_of_measurement": "kW"},
        ),
        _State("sensor.primary_ev_charging_2", "charging"),
        _State("binary_sensor.primary_ev_charge_cable_2", "on"),
        _State("device_tracker.primary_ev_location_2", "home"),
        _State(
            "sensor.primary_ev_battery_level_2",
            "86",
            {"unit_of_measurement": "%"},
        ),
    ])

    status = power_sync._get_ev_vehicle_status(hass, _Entry())

    assert status == {"ev_power_kw": 2.0, "ev_soc": 86}


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


def test_aggregate_ev_status_uses_configured_generic_charger_power():
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_power_entity": "sensor.generic_ev_power",
            "generic_charger_soc_entity": "sensor.generic_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.generic_ev_power", "3500", {"unit_of_measurement": "W"}),
        _State("sensor.generic_ev_soc", "64"),
    ])

    status = power_sync._get_ev_vehicle_status(hass, entry)

    assert status == {"ev_power_kw": 3.5, "ev_soc": 64}


def test_generic_charger_vehicle_reports_connected_idle_status():
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "sensor.generic_ev_status",
            "generic_charger_power_entity": "sensor.generic_ev_power",
            "generic_charger_soc_entity": "sensor.generic_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.generic_ev_status", "connected"),
        _State("sensor.generic_ev_power", "0", {"unit_of_measurement": "W"}),
        _State("sensor.generic_ev_soc", "64"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert vehicles == [{
        "vehicle_id": "generic_ev",
        "vehicle_name": "EV",
        "ev_power_kw": 0.0,
        "ev_soc": 64,
        "is_connected": True,
        "is_charging": False,
    }]


def test_generic_charger_vehicle_reports_measured_charging_power():
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "sensor.generic_ev_status",
            "generic_charger_power_entity": "sensor.generic_ev_power",
            "generic_charger_soc_entity": "sensor.generic_ev_soc",
        },
    )
    hass = _Hass([
        _State("sensor.generic_ev_status", "connected"),
        _State("sensor.generic_ev_power", "3.4", {"unit_of_measurement": "kW"}),
        _State("sensor.generic_ev_soc", "64"),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert vehicles == [{
        "vehicle_id": "generic_ev",
        "vehicle_name": "EV",
        "ev_power_kw": 3.4,
        "ev_soc": 64,
        "is_connected": True,
        "is_charging": True,
    }]


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
    vin = "5YJTEST0000000001"
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


def test_app_managed_sequential_tesla_id_deduplicates_with_vehicle_vin(monkeypatch):
    power_sync = _power_sync_module()
    planner = importlib.import_module("power_sync.automations.ev_charging_planner")

    async def no_sigenergy_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync,
        "_read_sigenergy_charger_state_for_entry",
        no_sigenergy_state,
    )
    vin = "5YJTEST0000000001"
    settings = SimpleNamespace(
        vehicle_id="1",
        charger_type="tesla",
        charger_power_entity="sensor.sequential_tesla_power",
    )
    executor = SimpleNamespace(
        _settings={"1": settings},
        _resolve_vehicle_vin=lambda vehicle_id: vin if vehicle_id == "1" else None,
    )
    monkeypatch.setattr(planner, "get_auto_schedule_executor", lambda: executor)
    hass = _Hass([
        _State(
            "sensor.sequential_tesla_power",
            "7.2",
            {"unit_of_measurement": "kW"},
        )
    ])
    vehicles = [{
        "vehicle_id": vin,
        "vehicle_name": "W3RT1E",
        "ev_power_kw": 7.2,
        "is_charging": True,
    }]

    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, _Entry(), vehicles)
    )
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(observations)

    assert snapshot.power_kw == 7.2
    assert len(snapshot.components) == 1
    assert snapshot.components[0].physical_load_key == f"vehicle:{vin.lower()}"


def test_app_managed_tesla_power_is_zeroed_when_vehicle_is_in_named_zone(monkeypatch):
    """Ticket #284: configured vehicle power cannot restore a remote EV draw."""
    power_sync = _power_sync_module()
    planner = importlib.import_module("power_sync.automations.ev_charging_planner")

    async def no_sigenergy_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync,
        "_read_sigenergy_charger_state_for_entry",
        no_sigenergy_state,
    )
    vin = "5YJTEST0000000001"
    settings = SimpleNamespace(
        vehicle_id="1",
        charger_type="tesla",
        charger_power_entity="sensor.sequential_tesla_power",
    )
    executor = SimpleNamespace(
        _settings={"1": settings},
        _resolve_vehicle_vin=lambda vehicle_id: vin if vehicle_id == "1" else None,
    )
    monkeypatch.setattr(planner, "get_auto_schedule_executor", lambda: executor)
    hass = _Hass([
        _State(
            "sensor.sequential_tesla_power",
            "2.0",
            {"unit_of_measurement": "kW"},
        )
    ])
    vehicles = [{
        "vehicle_id": vin,
        "vehicle_name": "W3RT1E",
        "ev_power_kw": 0.0,
        "is_charging": False,
        "site_presence": "away",
    }]

    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, _Entry(), vehicles)
    )
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(observations)

    assert snapshot.power_kw == 0.0
    assert len(snapshot.components) == 1
    assert snapshot.components[0].physical_load_key == f"vehicle:{vin.lower()}"


def test_display_snapshot_timestamp_follows_active_zero_power_observation(monkeypatch):
    """Fresh active telemetry must not become unavailable due to clock ordering."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    base = datetime(2026, 8, 15, 5, 42, 14, tzinfo=timezone.utc)
    clock_calls = 0

    def utcnow():
        nonlocal clock_calls
        current = base + timedelta(microseconds=clock_calls)
        clock_calls += 1
        return current

    monkeypatch.setattr(power_sync.dt_util, "utcnow", utcnow)

    async def get_ev_load_observations(hass, entry, vehicles):
        return [
            ev_load.EvLoadObservation(
                physical_load_key="vehicle:5yjtest0000000001",
                source_key="5YJTEST0000000001",
                power_kw=0.0,
                observed_at=power_sync.dt_util.utcnow(),
                active=True,
                measurement_kind=ev_load.EvMeasurementKind.VEHICLE,
            )
        ]

    class LoadpointStatusView:
        def __init__(self, hass, entry):
            pass

        async def _async_build_response(self, request, observed_vehicle_sink):
            observed_vehicle_sink.append(
                {
                    "vehicle_id": "5YJTEST0000000001",
                    "ev_power_kw": 0.0,
                    "is_charging": True,
                }
            )
            return SimpleNamespace(
                status=200,
                body=b'{"success": true, "site": {}, "loadpoints": []}',
            )

    monkeypatch.setattr(
        power_sync,
        "_get_ev_load_observations",
        get_ev_load_observations,
    )
    monkeypatch.setattr(power_sync, "EVLoadpointStatusView", LoadpointStatusView)
    tesla_coordinator = SimpleNamespace(
        data={"load_power": 0.617, "ev_power": 0.0}
    )
    hass = _Hass([], entry_data={"tesla_coordinator": tesla_coordinator})

    snapshot = asyncio.run(
        power_sync._get_ev_display_coordinator(
            hass,
            _Entry(),
        ).async_refresh(force=True)
    )

    observed = hass.data["power_sync"]["entry-1"][
        "observed_ev_load_snapshot"
    ]
    assert snapshot["site"]["ev_power_kw"] == 0.0
    assert snapshot["site"]["observation_quality"] == "complete"
    assert observed.unavailable_active_keys == ()
    assert tesla_coordinator.data["load_power"] == 0.617
    assert tesla_coordinator.data["home_load_normalization_quality"] == "complete"
