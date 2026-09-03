"""Tests for PowerSync Tesla vehicle status normalization."""

from __future__ import annotations

import asyncio
import importlib
import math
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
    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: dict | None = None,
        last_updated: datetime | None = None,
        last_reported: datetime | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        if last_updated is not None:
            self.last_updated = last_updated
        if last_reported is not None:
            self.last_reported = last_reported


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
        config_entries: list[object] | None = None,
    ) -> None:
        self.states = _States(states)
        self.entity_registry = SimpleNamespace(entities=registry_entities or {})
        self.device_registry = SimpleNamespace(devices=devices or {})
        self.data = {"power_sync": {"entry-1": entry_data or {}}}
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: config_entries or [],
            async_domains=lambda: [],
        )


def _fake_site_snapshot(hass) -> dict:
    """Mirror ``EVLoadpointStatusView._site_snapshot`` for the stub views.

    The display loader refreshes the site projection from the view *after*
    normalizing the energy coordinator, so the stub has to read the
    coordinator live rather than hand back a frozen dict.
    """
    entry_data = hass.data.get("power_sync", {}).get("entry-1", {})
    data = getattr(entry_data.get("tesla_coordinator"), "data", None) or {}
    try:
        load_power_kw = float(data.get("load_power"))
    except (TypeError, ValueError):
        load_power_kw = None
    if load_power_kw is not None and not math.isfinite(load_power_kw):
        load_power_kw = None
    return {
        "battery_soc": data.get("battery_level", 0) or 0,
        "solar_power_kw": data.get("solar_power", 0) or 0,
        "grid_power_kw": data.get("grid_power", 0) or 0,
        "battery_power_kw": data.get("battery_power", 0) or 0,
        "load_power_kw": load_power_kw,
        "is_curtailed": data.get("is_curtailed", False) is True,
    }


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


def test_ble_steady_power_uses_its_own_last_reported_timestamp():
    """A steady source report stays fresh without borrowing metadata time."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    stale_change = now - timedelta(minutes=5)
    hass = _Hass([
        _State("binary_sensor.yf88_status", "on", last_updated=now),
        _State("sensor.yf88_charging_state", "Charging", last_updated=now),
        _State("binary_sensor.yf88_charge_flap", "on", last_updated=now),
        _State(
            "sensor.yf88_charge_power",
            "11.0",
            {"unit_of_measurement": "kW"},
            last_updated=stale_change,
            last_reported=now,
        ),
        _State("sensor.yf88_charge_level", "30", last_updated=now),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": "yf88"},
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    vehicle = next(item for item in vehicles if item["vehicle_id"] == "ble_yf88")
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(
        [
            ev_load.EvLoadObservation(
                physical_load_key="vehicle:ble_yf88",
                source_key="ble_yf88",
                power_kw=vehicle["ev_power_kw"],
                observed_at=vehicle["_observed_at"],
                active=vehicle["is_charging"],
            )
        ],
        at=now,
    )
    normalized = ev_load.normalize_energy_data(
        {"load_power": 13.77},
        battery_system="sungrow",
        ev_load=snapshot,
        at=now,
    )

    assert vehicle["ev_power_kw"] == 11.0
    assert vehicle["is_charging"] is True
    assert vehicle["_observed_at"] == now
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert snapshot.power_kw == 11.0
    assert round(normalized["load_power"], 2) == 2.77


def test_ble_metadata_does_not_refresh_stale_power_measurement():
    """Connection and charging state updates cannot freshen measured power."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime.now(timezone.utc)
    stale_power = now - timedelta(minutes=5)
    hass = _Hass([
        _State("binary_sensor.yf88_status", "on", last_updated=now),
        _State("sensor.yf88_charging_state", "Charging", last_updated=now),
        _State("binary_sensor.yf88_charge_flap", "on", last_updated=now),
        _State(
            "sensor.yf88_charge_power",
            "11.0",
            {"unit_of_measurement": "kW"},
            last_updated=stale_power,
        ),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": "yf88"},
    )

    vehicle = next(
        item
        for item in power_sync._get_ev_vehicles_status(hass, entry)
        if item["vehicle_id"] == "ble_yf88"
    )
    snapshot = ev_load.aggregate_ev_load(
        [
            ev_load.EvLoadObservation(
                physical_load_key="vehicle:ble_yf88",
                source_key="ble_yf88",
                power_kw=vehicle["ev_power_kw"],
                observed_at=vehicle["_observed_at"],
                active=vehicle["is_charging"],
            )
        ],
        at=now,
    )

    assert vehicle["_observed_at"] == stale_power
    assert snapshot.quality == ev_load.EvLoadQuality.INCOMPLETE
    assert snapshot.unavailable_active_keys == ("vehicle:ble_yf88",)


def test_ble_unknown_power_reaches_the_loadpoint_as_unknown_not_idle_zero():
    """Ticket 36: unavailable BLE power must not masquerade as an observation."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    hass = _Hass([
        _State("binary_sensor.yf88_status", "on"),
        _State("sensor.yf88_charging_state", "unknown"),
        _State("sensor.yf88_charge_power", "unknown"),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": "yf88"},
    )

    vehicle = next(
        item
        for item in power_sync._get_ev_vehicles_status(hass, entry)
        if item["vehicle_id"] == "ble_yf88"
    )
    vehicle["include_idle"] = True
    loadpoint = loadpoint_status.build_loadpoint_status({}, [vehicle])[0]

    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["power_available"] is False
    assert loadpoint["current_power_kw"] is None
    assert loadpoint["status"] == "unknown"


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


def test_overlapping_ble_prefixes_keep_both_vehicle_observations():
    """A longer BLE prefix must not be hidden by a shorter substring prefix."""
    power_sync = _power_sync_module()
    tessy_vin = "5YJTEST0000000001"
    w3_vin = "5YJTEST0000000002"
    stale = datetime(2026, 8, 20, 3, 28, tzinfo=timezone.utc)
    fresh = stale + timedelta(seconds=30)
    short_prefix = "teslable"
    long_prefix = "tesla_ble_second_car"
    states = [
        _State("sensor.tessy_battery_level", "62", last_updated=stale),
        _State("sensor.tessy_charging_state", "charging", last_updated=stale),
        _State("binary_sensor.tessy_charge_cable", "on", last_updated=stale),
        _State(
            "sensor.tessy_charger_power",
            "7.2",
            {"unit_of_measurement": "kW"},
            last_updated=stale,
        ),
        _State("sensor.w3_battery_level", "70", last_updated=stale),
        _State("sensor.w3_charging_state", "charging", last_updated=stale),
        _State("binary_sensor.w3_charge_cable", "on", last_updated=stale),
        _State(
            "sensor.w3_charger_power",
            "7.2",
            {"unit_of_measurement": "kW"},
            last_updated=stale,
        ),
        _State(f"binary_sensor.{short_prefix}_status", "on", last_updated=fresh),
        _State(
            f"sensor.{short_prefix}_charging_state",
            "Charging",
            last_updated=fresh,
        ),
        _State(
            f"sensor.{short_prefix}_charge_power",
            "7.2",
            {"unit_of_measurement": "kW"},
            last_updated=fresh,
        ),
        _State(f"binary_sensor.{long_prefix}_status", "on", last_updated=fresh),
        _State(
            f"sensor.{long_prefix}_charging_state",
            "Disconnected",
            last_updated=fresh,
        ),
        _State(
            f"binary_sensor.{long_prefix}_charge_flap",
            "on",
            last_updated=fresh,
        ),
        _State(
            f"sensor.{long_prefix}_charge_power",
            "0",
            {"unit_of_measurement": "kW"},
            last_updated=fresh,
        ),
    ]
    registry_entities = {
        state.entity_id: _entity(
            state.entity_id,
            "tessy-device" if "tessy_" in state.entity_id else "w3-device",
        )
        for state in states[:8]
    }
    hass = _Hass(
        states,
        registry_entities,
        {
            "tessy-device": SimpleNamespace(
                id="tessy-device",
                name="TESSY",
                identifiers={("teslemetry", tessy_vin)},
            ),
            "w3-device": SimpleNamespace(
                id="w3-device",
                name="W3",
                identifiers={("tesla_fleet", w3_vin)},
            ),
        },
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_BOTH,
            "tesla_ble_entity_prefix": f"{short_prefix},{long_prefix}",
            "tesla_ble_vehicle_mapping": (
                f"{tessy_vin}={short_prefix},{w3_vin}={long_prefix}"
            ),
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    by_id = {vehicle["vehicle_id"]: vehicle for vehicle in vehicles}

    assert set(by_id) == {tessy_vin, w3_vin}
    assert by_id[tessy_vin]["ev_power_kw"] == 7.2
    assert by_id[tessy_vin]["is_charging"] is True
    assert by_id[w3_vin]["ev_power_kw"] == 0.0
    assert by_id[w3_vin]["is_connected"] is False
    assert by_id[w3_vin]["is_charging"] is False


def test_ble_disconnected_state_overrides_open_charge_flap():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    prefix = "garage_ble"
    hass = _Hass([
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
        _State(
            f"sensor.{prefix}_charging_state",
            "Disconnected",
            last_updated=now,
        ),
        _State(
            f"binary_sensor.{prefix}_charge_flap",
            "on",
            last_updated=now,
        ),
        _State(
            f"sensor.{prefix}_charge_power",
            "0",
            {"unit_of_measurement": "kW"},
            last_updated=now,
        ),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": prefix},
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["is_connected"] is False
    assert vehicle["is_charging"] is False
    assert vehicle["ev_power_kw"] == 0.0


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


def test_mobile_ble_views_merge_legacy_entry_data_with_options():
    """A legacy BLE provider setting must reach every mobile EV entry point."""
    power_sync = _power_sync_module()
    entry = SimpleNamespace(
        data={
            "ev_provider": power_sync.EV_PROVIDER_TESLA_BLE,
            "tesla_ble_entity_prefix": "legacy_ble",
        },
        options={"tesla_ble_entity_prefix": "current_ble"},
    )
    hass = _Hass(
        [_State("binary_sensor.current_ble_status", "on")],
        config_entries=[entry],
    )

    expected_config = {
        "ev_provider": power_sync.EV_PROVIDER_TESLA_BLE,
        "tesla_ble_entity_prefix": "current_ble",
    }
    assert power_sync.EVStatusView(hass)._get_powersync_config() == expected_config
    assert power_sync.EVVehiclesView(hass)._get_powersync_config() == expected_config
    assert power_sync.EVVehicleCommandView(hass)._get_powersync_config() == expected_config
    assert power_sync._get_available_ev_vehicles(hass) == [{
        "id": "ble_current_ble",
        "display_name": "Tesla BLE (Current Ble)",
        "source": "tesla_ble",
    }]


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
        "site_presence": "home",
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
        "site_presence": "home",
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


def test_generic_and_native_wall_connector_same_device_are_one_loadpoint():
    """A generic entity sourced from a Wall Connector must not be summed twice."""
    power_sync = _power_sync_module()
    generic_power = "sensor.tesla_wall_connector_single_phase_load"
    generic_status = "sensor.tesla_wall_connector_vehicle"
    native_power = "sensor.tesla_wall_connector_total_power"
    device_id = "wall-connector-1"
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_power_entity": generic_power,
            "generic_charger_status_entity": generic_status,
        },
    )
    states = [
        _State(generic_power, "6.96", {"unit_of_measurement": "kW"}),
        _State(generic_status, "connected"),
        _State(native_power, "6.96", {"unit_of_measurement": "kW"}),
    ]
    hass = _Hass(
        states,
        {state.entity_id: _entity(state.entity_id, device_id) for state in states},
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert vehicles == [{
        "vehicle_id": "generic_ev",
        "vehicle_name": "EV",
        "ev_power_kw": 6.96,
        "ev_soc": None,
        "is_connected": True,
        "is_charging": True,
    }]


def test_generic_and_native_wall_connector_same_device_stay_one_idle_loadpoint():
    """The duplicate must not return when the connected charger is idle."""
    power_sync = _power_sync_module()
    generic_power = "sensor.tesla_wall_connector_single_phase_load"
    generic_status = "sensor.tesla_wall_connector_vehicle"
    native_power = "sensor.tesla_wall_connector_total_power"
    device_id = "wall-connector-1"
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_power_entity": generic_power,
            "generic_charger_status_entity": generic_status,
        },
    )
    states = [
        _State(generic_power, "0", {"unit_of_measurement": "kW"}),
        _State(generic_status, "connected"),
        _State(native_power, "0", {"unit_of_measurement": "kW"}),
    ]
    hass = _Hass(
        states,
        {state.entity_id: _entity(state.entity_id, device_id) for state in states},
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert vehicles == [{
        "vehicle_id": "generic_ev",
        "vehicle_name": "EV",
        "ev_power_kw": 0.0,
        "ev_soc": None,
        "is_connected": True,
        "is_charging": False,
    }]


def test_generic_and_native_wall_connector_distinct_devices_remain_separate():
    """Device identity, not matching power or connection text, joins a charger."""
    power_sync = _power_sync_module()
    generic_power = "sensor.generic_ev_power"
    generic_status = "sensor.generic_ev_status"
    native_power = "sensor.tesla_wall_connector_total_power"
    native_status = "sensor.tesla_wall_connector_vehicle"
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_power_entity": generic_power,
            "generic_charger_status_entity": generic_status,
        },
    )
    states = [
        _State(generic_power, "6.96", {"unit_of_measurement": "kW"}),
        _State(generic_status, "connected"),
        _State(native_power, "6.96", {"unit_of_measurement": "kW"}),
        _State(native_status, "connected"),
    ]
    hass = _Hass(
        states,
        {
            generic_power: _entity(generic_power, "generic-charger"),
            generic_status: _entity(generic_status, "generic-charger"),
            native_power: _entity(native_power, "wall-connector"),
            native_status: _entity(native_status, "wall-connector"),
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert [(vehicle["vehicle_id"], vehicle["ev_power_kw"]) for vehicle in vehicles] == [
        ("generic_ev", 6.96),
        ("wall_connector_ha", 6.96),
    ]


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
            self._hass = hass

        def _site_snapshot(self):
            return _fake_site_snapshot(self._hass)

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


def test_wall_connector_same_vehicle_edges_follow_source_timestamp():
    """Ticket #204: source time decides same-VIN stop/restart ordering."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)

    stale_vehicle = {
        "vehicle_id": vehicle_id,
        "ev_power_kw": 11.0,
        "is_connected": True,
        "is_charging": True,
        "_charging_state_known": True,
        "_observed_at": current - timedelta(seconds=60),
    }
    stale_duplicate = dict(stale_vehicle)
    assert power_sync._apply_wall_connector_observation(
        [stale_vehicle, stale_duplicate],
        0.0,
        True,
        False,
        vehicle_id,
        current,
    )
    assert stale_vehicle["ev_power_kw"] == 0.0
    assert stale_vehicle["is_charging"] is False
    assert stale_vehicle["_observed_at"] == current
    assert stale_duplicate["ev_power_kw"] == 0.0
    assert stale_duplicate["is_charging"] is False
    assert stale_duplicate["_observed_at"] == current

    current_vehicle = {
        "vehicle_id": vehicle_id,
        "ev_power_kw": 0.0,
        "is_connected": True,
        "is_charging": False,
        "_charging_state_known": True,
        "_observed_at": current,
    }
    assert power_sync._apply_wall_connector_observation(
        [current_vehicle],
        11.0,
        True,
        True,
        vehicle_id,
        current - timedelta(seconds=60),
    )
    assert current_vehicle["ev_power_kw"] == 0.0
    assert current_vehicle["is_charging"] is False
    assert current_vehicle["_observed_at"] == current


def test_ev_vehicle_status_rejects_older_same_vin_wall_connector_power():
    """Ticket #204: stale site telemetry cannot revive stopped EV power."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power_2",
            "0",
            {"unit_of_measurement": "kW"},
            current,
        ),
        _State("sensor.primary_ev_charging_2", "stopped", last_updated=current),
        _State("binary_sensor.primary_ev_charge_cable_2", "on", last_updated=current),
        _State("device_tracker.primary_ev_location_2", "home", last_updated=current),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [
                {
                    "wall_connector_state": 2,
                    "wall_connector_power": 11000,
                    "vin": vehicle_id,
                }
            ],
            "last_update": current - timedelta(seconds=60),
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert len(vehicles) == 1
    assert vehicles[0]["ev_power_kw"] == 0.0
    assert vehicles[0]["is_charging"] is False
    assert vehicles[0]["_observed_at"] == current


def test_ev_vehicle_status_home_metadata_does_not_retimestamp_power_edges():
    """Ticket #204: unrelated home/cable updates cannot hide direct edges."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)

    for vehicle_power, charging_state, direct_power in (
        (11.0, "charging", 0.0),
        (0.0, "stopped", 11.0),
    ):
        stale_at = current - timedelta(seconds=60)
        direct_at = current - timedelta(seconds=30)
        hass = _tesla_hass([
            _State(
                "sensor.primary_ev_charger_power_2",
                str(vehicle_power),
                {"unit_of_measurement": "kW"},
                stale_at,
            ),
            _State(
                "sensor.primary_ev_charging_2",
                charging_state,
                last_updated=stale_at,
            ),
            _State(
                "binary_sensor.primary_ev_charge_cable_2",
                "on",
                last_updated=current,
            ),
            _State(
                "device_tracker.primary_ev_location_2",
                "home",
                last_updated=current,
            ),
        ])
        hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
            data={
                "wall_connectors_raw": [{
                    "wall_connector_state": 11 if direct_power == 0.0 else 2,
                    "wall_connector_power": direct_power * 1000,
                    "vin": vehicle_id,
                }],
                "last_update": direct_at,
            }
        )

        vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

        assert len(vehicles) == 1
        assert vehicles[0]["ev_power_kw"] == direct_power
        assert vehicles[0]["is_charging"] is (direct_power > 0.05)
        assert vehicles[0]["_observed_at"] == direct_at


def test_same_vin_coalescing_uses_newer_stop_and_restart_in_both_orders():
    """Ticket #204: duplicate providers cannot revive stale same-VIN state."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)

    for newer_power, older_power in ((0.0, 11.0), (11.0, 0.0)):
        newer = {
            "vehicle_id": vehicle_id,
            "charger_type": "tesla",
            "ev_power_kw": newer_power,
            "is_connected": True,
            "is_charging": newer_power > 0.05,
            "_observed_at": current,
        }
        older = {
            "vehicle_id": vehicle_id,
            "charger_type": "tesla",
            "ev_power_kw": older_power,
            "is_connected": True,
            "is_charging": older_power > 0.05,
            "_observed_at": current - timedelta(seconds=60),
        }
        for observations in ([newer, older], [older, newer]):
            coalesced = loadpoint_status.coalesce_vehicle_observations(
                observations
            )
            assert len(coalesced) == 1
            assert coalesced[0]["ev_power_kw"] == newer_power
            assert coalesced[0]["is_charging"] is (newer_power > 0.05)
            assert coalesced[0]["_observed_at"] == current


def test_same_vin_coalescing_orders_power_and_charging_state_separately():
    """A newer state edge must not restamp an older same-VIN power sample."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    older = current - timedelta(seconds=60)
    current_power = {
        "vehicle_id": vehicle_id,
        "charger_type": "tesla",
        "ev_power_kw": 11.0,
        "is_connected": True,
        "is_charging": True,
        "_observed_at": current,
        "_charging_observed_at": older,
    }
    current_stop = {
        "vehicle_id": vehicle_id,
        "charger_type": "tesla",
        "ev_power_kw": 0.0,
        "is_connected": True,
        "is_charging": False,
        "_observed_at": older,
        "_charging_observed_at": current,
    }

    for observations in (
        [current_power, current_stop],
        [current_stop, current_power],
    ):
        coalesced = loadpoint_status.coalesce_vehicle_observations(observations)
        assert len(coalesced) == 1
        assert coalesced[0]["ev_power_kw"] == 11.0
        assert coalesced[0]["is_charging"] is False
        assert coalesced[0]["_observed_at"] == current
        assert coalesced[0]["_charging_observed_at"] == current


def test_same_vin_equal_timestamp_positive_power_clears_stale_auxiliary():
    """Replacing zero with measured charging power cannot double-count aux load."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    coalesced = loadpoint_status.coalesce_vehicle_observations([
        {
            "vehicle_id": vehicle_id,
            "charger_type": "tesla",
            "ev_power_kw": 0.0,
            "auxiliary_power_kw": 0.5,
            "is_connected": True,
            "is_charging": False,
            "_observed_at": current,
        },
        {
            "vehicle_id": vehicle_id,
            "charger_type": "tesla",
            "ev_power_kw": 11.0,
            "is_connected": True,
            "is_charging": True,
            "_observed_at": current,
        },
    ])

    assert len(coalesced) == 1
    assert coalesced[0]["ev_power_kw"] == 11.0
    assert "auxiliary_power_kw" not in coalesced[0]


def test_multiple_vinless_wall_connectors_keep_distinct_physical_loads():
    """Distinct unidentified Wall Connectors must not overwrite one Tesla."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    hass = _tesla_hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [
                {
                    "wall_connector_state": 2,
                    "wall_connector_power": 7000,
                    "wall_connector_id": "alpha",
                },
                {
                    "wall_connector_state": 2,
                    "wall_connector_power": 5000,
                    "wall_connector_id": "beta",
                },
            ],
            "last_update": current,
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    connector_rows = [
        vehicle for vehicle in vehicles
        if str(vehicle.get("charger_id") or "").startswith("wall_connector_")
    ]

    assert {vehicle["charger_id"] for vehicle in connector_rows} == {
        "wall_connector_alpha",
        "wall_connector_beta",
    }
    assert sum(vehicle["ev_power_kw"] for vehicle in connector_rows) == 12.0


def test_away_vin_match_keeps_home_wall_connector_as_separate_loadpoint():
    """A named-zone vehicle cannot be revived by conflicting site telemetry."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power_2",
            "11",
            {"unit_of_measurement": "kW"},
            current - timedelta(seconds=60),
        ),
        _State(
            "sensor.primary_ev_charging_2",
            "charging",
            last_updated=current - timedelta(seconds=60),
        ),
        _State(
            "device_tracker.primary_ev_location_2",
            "work",
            last_updated=current,
        ),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [{
                "wall_connector_state": 2,
                "wall_connector_power": 11000,
                "wall_connector_id": "garage",
                "vin": vehicle_id,
            }],
            "last_update": current + timedelta(seconds=30),
        }
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    away = next(vehicle for vehicle in vehicles if vehicle.get("site_presence") == "away")
    connector = next(
        vehicle for vehicle in vehicles
        if vehicle.get("charger_id") == "wall_connector_garage"
    )

    assert away["ev_power_kw"] == 0.0
    assert away["is_charging"] is False
    assert connector["ev_power_kw"] == 11.0
    assert connector["is_charging"] is True


def test_duplicate_same_vin_away_tracker_fences_exact_wall_connector():
    """Ticket #204: an unlocated duplicate cannot bypass the away VIN fence."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)

    for device_order in (("away", "duplicate"), ("duplicate", "away")):
        for wall_connector_state, wall_connector_power in (
            (11, 0),
            (2, 0),
            (2, 11000),
        ):
            devices = {}
            for kind in device_order:
                device_id = f"device-{kind}"
                devices[device_id] = SimpleNamespace(
                    id=device_id,
                    name="TL",
                    identifiers={("teslemetry", vehicle_id)},
                )
            tracker = _State(
                "device_tracker.tl_location",
                "work",
                last_updated=current,
            )
            hass = _Hass(
                [tracker],
                {
                    tracker.entity_id: _entity(
                        tracker.entity_id,
                        "device-away",
                    )
                },
                devices,
                entry_data={
                    "tesla_coordinator": SimpleNamespace(data={
                        "wall_connectors_raw": [{
                            "wall_connector_state": wall_connector_state,
                            "wall_connector_power": wall_connector_power,
                            "wall_connector_id": "garage",
                            "vin": vehicle_id,
                        }],
                        "last_update": current + timedelta(seconds=30),
                    })
                },
            )

            vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
            away = next(
                vehicle
                for vehicle in vehicles
                if vehicle.get("vehicle_id") == vehicle_id
            )
            connector = next(
                vehicle
                for vehicle in vehicles
                if vehicle.get("charger_id") == "wall_connector_garage"
            )

            assert away["site_presence"] == "away"
            assert away["_site_presence_observed_at"] == current
            assert away["ev_power_kw"] == 0.0
            assert away["is_connected"] is False
            assert away["is_charging"] is False
            assert connector["ev_power_kw"] == wall_connector_power / 1000
            assert connector["is_connected"] is (wall_connector_power > 0)
            assert connector["is_charging"] is (wall_connector_power > 0)


def test_away_vehicle_fences_zero_power_charging_connector_display_contract():
    """Ticket #204: a stale state-2 connector cannot render an away EV onsite."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    ev_display = importlib.import_module("power_sync.ev_display")
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 26, tzinfo=timezone.utc)

    tracker = _State(
        "device_tracker.tl_location",
        "work",
        last_updated=current,
    )
    battery = _State(
        "sensor.tl_battery_level",
        "64",
        last_updated=current,
    )
    hass = _Hass(
        [tracker, battery],
        {
            tracker.entity_id: _entity(tracker.entity_id, "device-away"),
            battery.entity_id: _entity(battery.entity_id, "device-away"),
        },
        {
            "device-away": SimpleNamespace(
                id="device-away",
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            ),
            "device-duplicate": SimpleNamespace(
                id="device-duplicate",
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            ),
        },
        entry_data={
            "tesla_coordinator": SimpleNamespace(data={
                "wall_connectors_raw": [{
                    "wall_connector_state": 2,
                    "wall_connector_power": 0,
                    "wall_connector_id": "garage",
                    "vin": vehicle_id,
                }],
                "last_update": current + timedelta(seconds=30),
            })
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    loadpoints = loadpoint_status.build_loadpoint_status({}, vehicles)
    snapshot = {
        "site": {
            "ev_power_kw": 0.0,
            "observation_quality": "complete",
        },
        "loadpoints": loadpoints,
    }
    sensor = ev_display.display_snapshot_to_sensor_data(snapshot)

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == ["TL"]
    assert loadpoints[0]["site_presence"] == "away"
    assert loadpoints[0]["connected"] is False
    assert loadpoints[0]["actual_charging"] is False
    assert sensor["vehicle_name"] == "TL"
    assert sensor["site_presence"] == "away"
    assert sensor["is_connected"] is False
    assert sensor["is_charging"] is False


def test_vinless_zero_power_connector_cannot_revive_only_away_vehicle():
    """Ticket #204: a DIN-only idle connector cannot invent onsite presence."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    ev_display = importlib.import_module("power_sync.ev_display")
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)

    for device_order in (
        ("away",),
        ("away", "duplicate"),
        ("duplicate", "away"),
    ):
        devices = {}
        for kind in device_order:
            device_id = f"device-{kind}"
            devices[device_id] = SimpleNamespace(
                id=device_id,
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            )
        tracker = _State(
            "device_tracker.tl_location",
            "work",
            last_updated=current,
        )
        battery = _State(
            "sensor.tl_battery_level",
            "64",
            last_updated=current,
        )
        hass = _Hass(
            [tracker, battery],
            {
                tracker.entity_id: _entity(tracker.entity_id, "device-away"),
                battery.entity_id: _entity(battery.entity_id, "device-away"),
            },
            devices,
            entry_data={
                "tesla_coordinator": SimpleNamespace(data={
                    "wall_connectors_raw": [{
                        "wall_connector_state": 2,
                        "wall_connector_power": 0,
                        "din": "1529455-42-H--PGT26089015211",
                    }],
                    "last_update": current,
                })
            },
        )

        vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
        loadpoints = loadpoint_status.build_loadpoint_status({}, vehicles)
        sensor = ev_display.display_snapshot_to_sensor_data({
            "site": {
                "ev_power_kw": 0.0,
                "observation_quality": "complete",
            },
            "loadpoints": loadpoints,
        })

        assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == ["TL"]
        assert loadpoints[0]["site_presence"] == "away"
        assert loadpoints[0]["connected"] is False
        assert loadpoints[0]["actual_charging"] is False
        assert sensor["vehicle_id"] == vehicle_id
        assert sensor["vehicle_count"] == 1
        assert sensor["loadpoint_count"] == 1
        assert sensor["site_presence"] == "away"
        assert sensor["is_connected"] is False
        assert sensor["is_charging"] is False


def test_vinless_positive_connector_remains_distinct_from_away_vehicle():
    """A measured unidentified connector remains a separate physical load."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)
    tracker = _State(
        "device_tracker.tl_location",
        "work",
        last_updated=current,
    )
    hass = _Hass(
        [tracker],
        {tracker.entity_id: _entity(tracker.entity_id, "device-away")},
        {
            "device-away": SimpleNamespace(
                id="device-away",
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            )
        },
        entry_data={
            "tesla_coordinator": SimpleNamespace(data={
                "wall_connectors_raw": [{
                    "wall_connector_state": 2,
                    "wall_connector_power": 7000,
                    "din": "1529455-42-H--PGT26089015211",
                }],
                "last_update": current,
            })
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    connector = next(
        vehicle for vehicle in vehicles
        if vehicle.get("charger_id")
        == "wall_connector_1529455-42-H--PGT26089015211"
    )

    assert connector["ev_power_kw"] == 7.0
    assert connector["is_connected"] is True
    assert connector["is_charging"] is True


def test_vinless_connector_accepts_newer_home_presence_for_same_vehicle():
    """A newer home transition must reopen the DIN-only attribution path."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)
    states = []
    entities = {}
    devices = {}
    for kind, presence, observed_at in (
        ("away", "work", current),
        ("home", "home", current + timedelta(seconds=60)),
    ):
        device_id = f"device-{kind}"
        tracker = _State(
            f"device_tracker.tl_location_{kind}",
            presence,
            last_updated=observed_at,
        )
        states.append(tracker)
        entities[tracker.entity_id] = _entity(tracker.entity_id, device_id)
        devices[device_id] = SimpleNamespace(
            id=device_id,
            name="TL",
            identifiers={("teslemetry", vehicle_id)},
        )
    hass = _Hass(
        states,
        entities,
        devices,
        entry_data={
            "tesla_coordinator": SimpleNamespace(data={
                "wall_connectors_raw": [{
                    "wall_connector_state": 2,
                    "wall_connector_power": 0,
                    "din": "1529455-42-H--PGT26089015211",
                }],
                "last_update": current + timedelta(seconds=90),
            })
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert len(vehicles) == 1
    assert vehicles[0]["vehicle_id"] == vehicle_id
    assert vehicles[0]["site_presence"] == "home"
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is True


def test_duplicate_same_vin_newer_home_tracker_allows_exact_wall_connector():
    """A newer home observation restores exact-VIN attribution after travel."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)

    for device_order in (("away", "home"), ("home", "away")):
        devices = {}
        states = []
        entities = {}
        for kind in device_order:
            device_id = f"device-{kind}"
            devices[device_id] = SimpleNamespace(
                id=device_id,
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            )
            tracker = _State(
                f"device_tracker.tl_{kind}_location",
                "work" if kind == "away" else "home",
                last_updated=(
                    current
                    if kind == "away"
                    else current + timedelta(seconds=60)
                ),
            )
            states.append(tracker)
            entities[tracker.entity_id] = _entity(tracker.entity_id, device_id)
        hass = _Hass(
            states,
            entities,
            devices,
            entry_data={
                "tesla_coordinator": SimpleNamespace(data={
                    "wall_connectors_raw": [{
                        "wall_connector_state": 11,
                        "wall_connector_power": 0,
                        "wall_connector_id": "garage",
                        "vin": vehicle_id,
                    }],
                    "last_update": current + timedelta(seconds=90),
                })
            },
        )

        vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

        assert len(vehicles) == 1
        assert vehicles[0]["vehicle_id"] == vehicle_id
        assert vehicles[0]["site_presence"] == "home"
        assert vehicles[0]["_site_presence_observed_at"] == (
            current + timedelta(seconds=60)
        )
        assert vehicles[0]["is_connected"] is True
        assert vehicles[0]["is_charging"] is False
        assert vehicles[0]["ev_power_kw"] == 0.0


def test_duplicate_same_vin_untimestamped_away_presence_fails_closed():
    """An unknown-age away observation needs a comparable home transition."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    away_vehicle = {
        "vehicle_id": vehicle_id,
        "site_presence": "away",
        "ev_power_kw": 0.0,
        "is_connected": False,
        "is_charging": False,
    }
    home_vehicle = {
        "vehicle_id": vehicle_id,
        "site_presence": "home",
        "_site_presence_observed_at": current,
        "ev_power_kw": 0.0,
        "is_connected": False,
        "is_charging": False,
    }

    matched = power_sync._apply_wall_connector_observation(
        [home_vehicle, away_vehicle],
        11.0,
        True,
        True,
        vehicle_id,
        current + timedelta(seconds=30),
    )

    assert matched is False
    assert home_vehicle["ev_power_kw"] == 0.0
    assert home_vehicle["is_connected"] is False


def test_legacy_ev_status_applies_away_tracker_to_duplicate_physical_vin():
    """The aggregate fallback cannot count an unlocated duplicate as home."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)

    for device_order in (("away", "duplicate"), ("duplicate", "away")):
        devices = {}
        for kind in device_order:
            device_id = f"device-{kind}"
            devices[device_id] = SimpleNamespace(
                id=device_id,
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            )
        states = [
            _State(
                "device_tracker.tl_location",
                "work",
                last_updated=current,
            ),
            _State(
                "sensor.tl_duplicate_charging_state",
                "charging",
                last_updated=current + timedelta(seconds=30),
            ),
            _State(
                "sensor.tl_duplicate_charger_power",
                "7",
                {"unit_of_measurement": "kW"},
                current + timedelta(seconds=30),
            ),
        ]
        entities = {
            states[0].entity_id: _entity(states[0].entity_id, "device-away"),
            states[1].entity_id: _entity(states[1].entity_id, "device-duplicate"),
            states[2].entity_id: _entity(states[2].entity_id, "device-duplicate"),
        }
        hass = _Hass(states, entities, devices)

        status = power_sync._get_ev_vehicle_status(hass, _Entry())

        assert status["ev_power_kw"] == 0.0


def test_legacy_ev_status_accepts_newer_home_for_duplicate_physical_vin():
    """A newer home tracker re-enables physical-VIN aggregate telemetry."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)

    for device_order in (("away", "home"), ("home", "away")):
        devices = {}
        states = []
        entities = {}
        for kind in device_order:
            device_id = f"device-{kind}"
            devices[device_id] = SimpleNamespace(
                id=device_id,
                name="TL",
                identifiers={("teslemetry", vehicle_id)},
            )
            tracker = _State(
                f"device_tracker.tl_{kind}_location",
                "work" if kind == "away" else "home",
                last_updated=(
                    current
                    if kind == "away"
                    else current + timedelta(seconds=60)
                ),
            )
            states.append(tracker)
            entities[tracker.entity_id] = _entity(tracker.entity_id, device_id)
        stale_charging = _State(
            "sensor.tl_away_charging_state",
            "charging",
            last_updated=current + timedelta(seconds=30),
        )
        stale_power = _State(
            "sensor.tl_away_charger_power",
            "20",
            {"unit_of_measurement": "kW"},
            current + timedelta(seconds=30),
        )
        charging = _State(
            "sensor.tl_home_charging_state",
            "charging",
            last_updated=current + timedelta(seconds=90),
        )
        power = _State(
            "sensor.tl_home_charger_power",
            "7",
            {"unit_of_measurement": "kW"},
            current + timedelta(seconds=90),
        )
        states.extend((stale_charging, stale_power, charging, power))
        entities[stale_charging.entity_id] = _entity(
            stale_charging.entity_id,
            "device-away",
        )
        entities[stale_power.entity_id] = _entity(
            stale_power.entity_id,
            "device-away",
        )
        entities[charging.entity_id] = _entity(charging.entity_id, "device-home")
        entities[power.entity_id] = _entity(power.entity_id, "device-home")
        hass = _Hass(states, entities, devices)

        status = power_sync._get_ev_vehicle_status(hass, _Entry())

        assert status["ev_power_kw"] == 7.0


def test_unmatched_explicit_vin_never_falls_through_to_another_vehicle():
    """A connector's VIN cannot be heuristically assigned to a different car."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    away_vehicle = {
        "vehicle_id": "5YJTEST0000000001",
        "site_presence": "away",
        "ev_power_kw": 0.0,
        "is_connected": False,
        "is_charging": False,
        "_observed_at": current,
    }
    home_vehicle = {
        "vehicle_id": "5YJTEST0000000002",
        "ev_power_kw": 0.0,
        "is_connected": True,
        "is_charging": False,
        "_observed_at": current,
    }

    matched = power_sync._apply_wall_connector_observation(
        [away_vehicle, home_vehicle],
        11.0,
        True,
        True,
        away_vehicle["vehicle_id"],
        current + timedelta(seconds=30),
    )

    assert matched is False
    assert away_vehicle["ev_power_kw"] == 0.0
    assert home_vehicle["ev_power_kw"] == 0.0


def test_exact_vin_zero_state_clears_stale_power_and_connection():
    """An explicit VIN-scoped state-0 sample is still authoritative telemetry."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    vehicle = {
        "vehicle_id": "5YJTEST0000000001",
        "ev_power_kw": 11.0,
        "is_connected": True,
        "is_charging": True,
        "_observed_at": current - timedelta(seconds=60),
    }

    matched = power_sync._apply_wall_connector_observation(
        [vehicle],
        0.0,
        False,
        False,
        vehicle["vehicle_id"],
        current,
    )

    assert matched is True
    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["is_connected"] is False
    assert vehicle["is_charging"] is False
    assert vehicle["_observed_at"] == current


def test_unmatched_disconnected_wall_connector_stays_disconnected():
    """An idle connector row must retain its real disconnected state."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    hass = _Hass([], entry_data={
        "tesla_coordinator": SimpleNamespace(data={
            "wall_connectors_raw": [{
                "wall_connector_state": 0,
                "wall_connector_power": 0,
                "wall_connector_id": "garage",
            }],
            "last_update": current,
        })
    })

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert len(vehicles) == 1
    assert vehicles[0]["charger_id"] == "wall_connector_garage"
    assert vehicles[0]["is_connected"] is False
    assert vehicles[0]["is_charging"] is False
    assert vehicles[0]["ev_power_kw"] == 0.0


def test_display_snapshot_reconciles_direct_same_vehicle_stop_and_restart(monkeypatch):
    """Ticket #204: the canonical cache follows direct VIN-scoped edges."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: now)
    vehicle_key = "vehicle:5yjtest0000000001"
    case = {}

    async def get_ev_load_observations(hass, entry, vehicles):
        observed_at = power_sync.dt_util.utcnow()
        return [
            ev_load.EvLoadObservation(
                physical_load_key=vehicle_key,
                source_key="cached_vehicle",
                power_kw=case["cached_power_kw"],
                observed_at=observed_at,
                active=case["cached_power_kw"] > 0.05,
                measurement_kind=ev_load.EvMeasurementKind.VEHICLE,
            )
        ]

    class LoadpointStatusView:
        def __init__(self, hass, entry):
            self._hass = hass

        def _site_snapshot(self):
            return _fake_site_snapshot(self._hass)

        async def _async_build_response(self, request, observed_vehicle_sink):
            observed_vehicle_sink.append(
                {
                    "vehicle_id": "5YJTEST0000000001",
                    "ev_power_kw": case["cached_power_kw"],
                    "is_charging": case["cached_power_kw"] > 0.05,
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

    for case in (
        {
            "cached_power_kw": 1.420654,
            "direct_power_kw": 0.0,
            "raw_load_kw": 1.395,
            "expected_home_load_kw": 1.395,
        },
        {
            "cached_power_kw": 0.0,
            "direct_power_kw": 2.182647,
            "raw_load_kw": 3.569,
            "expected_home_load_kw": 1.386353,
        },
    ):
        tesla_coordinator = SimpleNamespace(
            data={
                "load_power": case["expected_home_load_kw"],
                "raw_home_load_power": case["raw_load_kw"],
                "ev_power": case["direct_power_kw"],
                "ev_power_fallback_by_physical_key": {
                    vehicle_key: case["direct_power_kw"]
                },
                "last_update": now,
            }
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

        assert observed.power_kw == case["direct_power_kw"]
        assert observed.quality == ev_load.EvLoadQuality.COMPLETE
        assert snapshot["site"]["observed_ev_load_kw"] == case["direct_power_kw"]
        assert abs(
            tesla_coordinator.data["load_power"]
            - case["expected_home_load_kw"]
        ) < 1e-9


def test_display_snapshot_direct_meter_keeps_distinct_missing_ev_incomplete(
    monkeypatch,
):
    """A direct Tesla meter must not hide a different unmeasured charger."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    observed_at = power_sync.dt_util.utcnow()
    vehicle_key = "vehicle:5yjtest0000000001"

    async def get_ev_load_observations(hass, entry, vehicles):
        return [
            ev_load.EvLoadObservation(
                vehicle_key,
                "cached_vehicle",
                10.8,
                observed_at,
                True,
                ev_load.EvMeasurementKind.VEHICLE,
            ),
            ev_load.EvLoadObservation(
                "ocpp:garage:1",
                "ocpp_meter",
                None,
                observed_at,
                True,
                ev_load.EvMeasurementKind.LOADPOINT_METER,
            ),
        ]

    class LoadpointStatusView:
        def __init__(self, hass, entry):
            self._hass = hass

        def _site_snapshot(self):
            return _fake_site_snapshot(self._hass)

        async def _async_build_response(self, request, observed_vehicle_sink):
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
        data={
            "load_power": None,
            "raw_home_load_power": 19.734,
            "ev_power": 10.8,
            "ev_power_fallback_by_physical_key": {vehicle_key: 10.8},
            "last_update": observed_at,
        }
    )
    hass = _Hass([], entry_data={"tesla_coordinator": tesla_coordinator})

    asyncio.run(
        power_sync._get_ev_display_coordinator(
            hass,
            _Entry(),
        ).async_refresh(force=True)
    )
    observed = hass.data["power_sync"]["entry-1"][
        "observed_ev_load_snapshot"
    ]

    assert observed.power_kw == 10.8
    assert observed.quality == ev_load.EvLoadQuality.INCOMPLETE
    assert observed.unavailable_active_keys == ("ocpp:garage:1",)
    assert tesla_coordinator.data["load_power"] is None
    assert tesla_coordinator.data["home_load_normalization_quality"] == "incomplete"


def _dual_registered_devices():
    """One car registered by two Tesla integrations, plus a Powerwall.

    Exactly the shape of a site running Tesla Fleet and Teslemetry side by
    side: both publish a device for the same VIN, and Teslemetry additionally
    publishes the energy site under a non-VIN identifier.
    """
    return {
        "device-fleet": SimpleNamespace(
            id="device-fleet",
            name="TESSY",
            name_by_user=None,
            identifiers={("tesla_fleet", "LRWYHCEK3PC907290")},
        ),
        "device-teslemetry": SimpleNamespace(
            id="device-teslemetry",
            name="TESSY",
            name_by_user=None,
            identifiers={("teslemetry", "LRWYHCEK3PC907290")},
        ),
        "device-second-car": SimpleNamespace(
            id="device-second-car",
            name="W3RT1E",
            name_by_user=None,
            identifiers={("tesla_fleet", "LRWYHCEK8TC828420")},
        ),
        "device-powerwall": SimpleNamespace(
            id="device-powerwall",
            name="POWERSYNC",
            name_by_user="18 Parkside Drive-Teslemetry",
            identifiers={("teslemetry", "2252397099082264")},
        ),
    }


def test_one_car_registered_by_two_integrations_is_one_vehicle():
    """Two provider rows for the same VIN are one car, not two loadpoints.

    Running Tesla Fleet alongside Teslemetry registers each vehicle twice.
    Discovery yielded both rows, so a two-car household reported three
    vehicles and the duplicate became a phantom Smart Schedule loadpoint.
    """
    power_sync = _power_sync_module()
    planner = importlib.import_module(
        "power_sync.automations.ev_charging_planner"
    )
    hass = _Hass([], {}, _dual_registered_devices())

    vehicles = asyncio.run(
        planner.discover_all_tesla_vehicles(hass, _Entry())
    )

    vins = sorted(vehicle["vin"] for vehicle in vehicles)
    assert vins == ["LRWYHCEK3PC907290", "LRWYHCEK8TC828420"]


def test_a_powerwall_never_supplies_an_ev_battery_level():
    """A Tesla-domain device with no VIN is not a vehicle.

    The energy site registers under the same integration as the cars but with
    a non-VIN identifier. It was mapped in with an empty VIN, which then read
    as "matches every vehicle", so the home battery's state of charge could be
    returned as a car's SoC.
    """
    power_sync = _power_sync_module()
    planner = importlib.import_module(
        "power_sync.automations.ev_charging_planner"
    )
    devices = _dual_registered_devices()
    states = [
        _State("sensor.18_parkside_drive_teslemetry_battery_charged", "37.619"),
    ]
    hass = _Hass(
        states,
        {
            "sensor.18_parkside_drive_teslemetry_battery_charged": _entity(
                "sensor.18_parkside_drive_teslemetry_battery_charged",
                "device-powerwall",
            ),
        },
        devices,
    )

    executor = planner.PriceLevelChargingExecutor.__new__(
        planner.PriceLevelChargingExecutor
    )
    executor.hass = hass
    executor.config_entry = _Entry()
    executor._domain = "power_sync"

    soc = asyncio.run(executor._get_ev_soc("LRWYHCEK3PC907290"))

    assert soc != 37, "the home battery's SoC was returned as the car's"
    assert soc is None
