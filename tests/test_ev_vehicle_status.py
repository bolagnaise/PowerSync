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

import pytest


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
    currency.presentation_currency_metadata_for_entry = lambda *args, **kwargs: {}
    sys.modules["power_sync.currency"] = currency

    inverters = types.ModuleType("power_sync.inverters")
    inverters.get_inverter_controller = lambda *args, **kwargs: None
    # Keep it a package: later files import real ``power_sync.inverters.*`` submodules.
    inverters.__path__ = [str(ROOT / "power_sync" / "inverters")]
    sys.modules["power_sync.inverters"] = inverters

    optimization_coordinator = types.ModuleType("power_sync.optimization.coordinator")
    optimization_coordinator.OptimizationCoordinator = type("OptimizationCoordinator", (), {})
    optimization_coordinator.OptimizationConfig = type("OptimizationConfig", (), {})
    optimization_coordinator.sigenergy_capped_optimizer_limit_w = (
        lambda *args, **kwargs: None
    )
    optimization_coordinator.battery_specs_source_by_field = (
        lambda *args, **kwargs: {}
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


def test_ocpp_status_endpoint_exposes_shared_soc_without_generic_charger():
    import json
    power_sync = _power_sync_module()
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ocpp_enabled": True, "generic_charger_soc_entity": "sensor.car_soc",
    })
    hass = _Hass([
        _State("sensor.beny_status_connector", "Preparing"),
        _State("sensor.car_soc", "64", {"unit_of_measurement": "%"}),
    ], registry_entities={"status": SimpleNamespace(platform="ocpp", entity_id="sensor.beny_status_connector")}, config_entries=[entry])
    response = asyncio.run(power_sync.OCPPChargersView(hass, entry).get(SimpleNamespace()))
    chargers = json.loads(response.body)["chargers"]
    assert len(chargers) == 1
    assert chargers[0]["current_soc"] == 64
    assert chargers[0]["is_vehicle_connected"] is True


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


@pytest.mark.parametrize("cable", ["unknown", "unavailable"])
def test_stale_fleet_home_and_stopped_do_not_prove_a_connected_vehicle(cable):
    """W3RT1E: unavailable native telemetry must not manufacture a home plug."""
    power_sync = _power_sync_module()
    stale = datetime.now(timezone.utc) - timedelta(hours=5)
    hass = _tesla_hass([
        _State("binary_sensor.primary_ev_status", "unknown", last_updated=stale),
        _State("binary_sensor.primary_ev_charge_cable", cable, last_updated=stale),
        _State("device_tracker.primary_ev_location", "home", last_updated=stale),
        _State("sensor.primary_ev_charging", "stopped", last_updated=stale),
        _State("sensor.primary_ev_battery_level", "75", last_updated=stale),
    ])

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    assert len(vehicles) == 1
    assert vehicles[0]["is_connected"] is False
    assert vehicles[0].get("site_presence") != "home"
    assert vehicles[0]["ev_soc"] == 75
    from power_sync.automations.loadpoint_status import build_loadpoint_status
    loadpoint = build_loadpoint_status({}, vehicles)[0]
    assert loadpoint["status"] == "unknown"
    assert loadpoint["actual_charging"] is False


@pytest.mark.parametrize("cable,age_seconds", [("on", 18000), ("unknown", 10)])
def test_known_plug_or_fresh_stopped_state_still_proves_connected(cable, age_seconds):
    power_sync = _power_sync_module()
    observed = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    hass = _tesla_hass([
        _State("binary_sensor.primary_ev_status", "off", last_updated=observed),
        _State("binary_sensor.primary_ev_charge_cable", cable, last_updated=observed),
        _State("device_tracker.primary_ev_location", "home", last_updated=observed),
        _State("sensor.primary_ev_charging", "stopped", last_updated=observed),
    ])
    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    assert vehicle["is_connected"] is True
    assert vehicle["site_presence"] == "home"


def _restored_charging_w3rt1e(status: str, *, restored_at, asleep_at):
    """W3RT1E 2026-11-02: last awake values were Charging / 2 kW."""
    return [
        _State(
            "binary_sensor.primary_ev_status", status,
            {"device_class": "connectivity"}, last_updated=asleep_at,
        ),
        _State("sensor.primary_ev_charging", "charging",
               last_updated=restored_at, last_reported=restored_at),
        _State("sensor.primary_ev_charger_power", "2", {"unit_of_measurement": "kW"},
               last_updated=restored_at, last_reported=restored_at),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=restored_at),
        _State("device_tracker.primary_ev_location", "home", last_updated=restored_at),
        _State("sensor.primary_ev_battery_level", "64", last_updated=restored_at),
    ]


def test_reload_restored_charging_with_unknown_status_is_offline_not_charging():
    """A reload re-stamps retained Charging/2 kW while the car status is unknown."""
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass(_restored_charging_w3rt1e(
        "unknown", restored_at=now, asleep_at=now,
    ))

    vehicle = power_sync._get_ev_vehicles_status(
        hass, _Entry(), include_control_evidence=True,
    )[0]
    assert vehicle["is_charging"] is False
    assert vehicle["is_connected"] is False
    assert vehicle["power_available"] is False
    assert vehicle["telemetry_state"] == "offline"
    assert vehicle["_boost_charging_state_evidence"]["charging"] is None
    assert power_sync._get_ev_vehicle_status(hass, _Entry())["ev_power_kw"] == 0.0

    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["status"] != "charging"
    assert loadpoint["actual_charging"] is False
    assert loadpoint["current_power_kw"] is None
    assert loadpoint["confidence"] == "unknown"
    assert loadpoint["telemetry_state"] == "offline"
    assert loadpoint["observed_at"] is None


def test_asleep_status_overrides_stale_charging_with_observed_zero():
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    asleep_at = now - timedelta(hours=5, minutes=34)
    hass = _tesla_hass(_restored_charging_w3rt1e(
        "off", restored_at=asleep_at, asleep_at=asleep_at,
    ))

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    assert vehicle["is_charging"] is False
    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["is_connected"] is True
    assert vehicle["telemetry_state"] == "asleep"
    assert power_sync._get_ev_vehicle_status(hass, _Entry())["ev_power_kw"] == 0.0

    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["status"] == "connected_idle"
    assert loadpoint["actual_charging"] is False
    assert loadpoint["current_power_kw"] == 0.0
    assert loadpoint["confidence"] == "observed"
    assert loadpoint["telemetry_state"] == "asleep"
    assert loadpoint["observed_at"] == asleep_at.isoformat()


def test_awake_charging_car_still_reports_live_charging():
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass(_restored_charging_w3rt1e(
        "on", restored_at=now, asleep_at=now,
    ))

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    assert vehicle["is_charging"] is True
    assert vehicle["ev_power_kw"] == 2.0
    assert vehicle["telemetry_state"] == "live"
    assert power_sync._get_ev_vehicle_status(hass, _Entry())["ev_power_kw"] == 2.0

    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["status"] == "charging"
    assert loadpoint["current_power_kw"] == 2.0
    assert loadpoint["confidence"] == "observed"
    assert loadpoint["telemetry_state"] == "live"
    assert loadpoint["observed_at"] == now.isoformat()


def test_disabled_native_status_does_not_hide_live_charging_power():
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    hass = _tesla_hass(_restored_charging_w3rt1e(
        "on", restored_at=now, asleep_at=now,
    ))
    status_id = "binary_sensor.primary_ev_status"
    hass.entity_registry.entities[status_id].disabled_by = "user"
    hass.states._states.pop(status_id)

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    assert vehicle["is_charging"] is True
    assert vehicle["ev_power_kw"] == 2.0
    assert power_sync._get_ev_vehicle_status(hass, _Entry())["ev_power_kw"] == 2.0
    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["actual_charging"] is True
    assert loadpoint["telemetry_state"] == "live"
    assert loadpoint["observed_at"] == now.isoformat()


@pytest.mark.parametrize("status", ["unknown", "off"])
@pytest.mark.parametrize("connector_age,expected_state,expected_power", [
    (timedelta(0), "live", 7.0),
    (timedelta(hours=2), None, None),
])
def test_wall_connector_refreshes_sleeping_or_offline_vehicle_telemetry(
    status, connector_age, expected_state, expected_power,
):
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    previous = now - timedelta(hours=1)
    hass = _tesla_hass(_restored_charging_w3rt1e(
        status, restored_at=previous, asleep_at=previous,
    ))
    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    connector_at = now - connector_age
    accepted = power_sync._apply_wall_connector_observation(
        [vehicle], 7.0, True, True, vehicle["vehicle_id"], connector_at,
    )
    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    if expected_state is not None:
        assert accepted is True
        assert loadpoint["actual_charging"] is True
        assert loadpoint["current_power_kw"] == expected_power
        assert loadpoint["telemetry_state"] == expected_state
        assert loadpoint["observed_at"] == connector_at.isoformat()
    else:
        assert accepted is False
        assert loadpoint["actual_charging"] is False
        assert loadpoint["telemetry_state"] == ("offline" if status == "unknown" else "asleep")
        assert loadpoint["observed_at"] == (None if status == "unknown" else previous.isoformat())


@pytest.mark.parametrize(
    "age,expected", [(timedelta(0), "live"), (timedelta(minutes=20), "stale")],
)
def test_car_without_status_sensor_keeps_charging_and_reports_freshness(age, expected):
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    power_sync = _power_sync_module()
    observed = datetime.now(timezone.utc) - age
    states = [
        state for state in _restored_charging_w3rt1e(
            "on", restored_at=observed, asleep_at=observed,
        )
        if not state.entity_id.endswith("_status")
    ]
    hass = _tesla_hass(states)

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    assert vehicle["is_charging"] is True
    assert vehicle["telemetry_state"] == expected

    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["actual_charging"] is True
    assert loadpoint["telemetry_state"] == expected
    assert loadpoint["observed_at"] == observed.isoformat()


@pytest.mark.parametrize("keep_status_sensor", [True, False])
def test_frozen_online_fleet_charging_claim_expires_to_unknown(keep_status_sensor):
    """W3RT1E recurrence: a Fleet device frozen since an HA restart.

    HA restored ``status=on`` and ``charging``/2 kW at 03:01 and Tesla Fleet
    never refreshed the car again (no BLE, no Wall Connector). A charging car
    is re-reported every poll, so 6.5 h of silence must not read as Charging
    -- but nor as a measured stop: the possible EV load stays unknown.
    """
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    power_sync = _power_sync_module()
    observed = datetime.now(timezone.utc) - timedelta(hours=6, minutes=30)
    states = [
        state for state in _restored_charging_w3rt1e(
            "on", restored_at=observed, asleep_at=observed,
        )
        if keep_status_sensor or not state.entity_id.endswith("_status")
    ]
    hass = _tesla_hass(states)

    vehicle = power_sync._get_ev_vehicles_status(
        hass, _Entry(), include_control_evidence=True,
    )[0]
    assert vehicle["is_charging"] is False
    assert vehicle["telemetry_state"] == "stale"
    assert vehicle["power_available"] is False
    assert vehicle["_power_activity_unknown"] is True
    evidence = vehicle.get("_boost_charging_state_evidence")
    assert evidence is None or evidence["charging"] is not True

    loadpoint = build_loadpoint_status({}, [vehicle])[0]
    assert loadpoint["actual_charging"] is False
    assert loadpoint["status"] != "charging"
    assert loadpoint["current_power_kw"] is None


def _w3rt1e_and_live_charging_hass(status: str, *, restored_at, now) -> _Hass:
    """W3RT1E restored as Charging/2 kW beside a second car live at 7 kW."""
    w3rt1e = [
        _State(
            state.entity_id.replace("primary_ev", "w3rt1e"), state.state,
            state.attributes,
            last_updated=getattr(state, "last_updated", None),
            last_reported=getattr(state, "last_reported", None),
        )
        for state in _restored_charging_w3rt1e(
            status, restored_at=restored_at, asleep_at=restored_at,
        )
    ]
    live = [
        _State("binary_sensor.second_ev_status", "on",
               {"device_class": "connectivity"}, last_updated=now),
        _State("sensor.second_ev_charging", "charging", last_updated=now),
        _State("sensor.second_ev_charger_power", "7", {"unit_of_measurement": "kW"},
               last_updated=now, last_reported=now),
        _State("binary_sensor.second_ev_charge_cable", "on", last_updated=now),
        _State("device_tracker.second_ev_location", "home", last_updated=now),
    ]
    entities = {
        state.entity_id: _entity(state.entity_id, "device-w3rt1e") for state in w3rt1e
    }
    entities.update({
        state.entity_id: _entity(state.entity_id, "device-second") for state in live
    })
    return _Hass(
        w3rt1e + live,
        entities,
        {
            "device-w3rt1e": SimpleNamespace(
                id="device-w3rt1e", name="W3RT1E",
                identifiers={("tesla_fleet", "LRWYHCEK8TC828420")},
            ),
            "device-second": SimpleNamespace(
                id="device-second", name="SECOND EV",
                identifiers={("tesla_fleet", "5YJTEST0000000001")},
            ),
        },
    )


@pytest.mark.parametrize("status,restored_age,expected_power_kw,home_load_known", [
    ("off", timedelta(hours=5, minutes=34), 0.0, True),
    ("unknown", timedelta(0), None, True),
    ("unavailable", timedelta(0), None, True),
    # A genuinely awake charging car whose watt reading is older than 90 s
    # must still leave Home Load unknown rather than guess its EV share.
    ("on", timedelta(seconds=120), None, False),
])
def test_asleep_or_offline_restored_charging_does_not_blank_home_load(
    monkeypatch, status, restored_age, expected_power_kw, home_load_known,
):
    """W3RT1E: a restored Charging state blanked an includes-EV Home Load.

    Pins the exact EvLoadObservation shapes that the Fleet, V1R, sensor and
    optimizer Home Load tests feed through their own consumers.
    """
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime.now(timezone.utc)
    hass = _w3rt1e_and_live_charging_hass(
        status, restored_at=now - restored_age, now=now,
    )

    async def no_sigenergy_charger(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    observations = {
        item.physical_load_key: item
        for item in asyncio.run(
            power_sync._get_ev_load_observations(hass, _Entry(), vehicles)
        )
    }
    w3rt1e = observations["vehicle:lrwyhcek8tc828420"]
    live = observations["vehicle:5yjtest0000000001"]
    snapshot = ev_load.aggregate_ev_load(observations.values(), at=now)
    home = ev_load.normalize_home_load(
        10.0, ev_load.HomeLoadBasis.INCLUDES_EV, snapshot, at=now,
    )

    assert w3rt1e.active is (not home_load_known)
    assert w3rt1e.power_kw == expected_power_kw
    assert (live.power_kw, live.active) == (7.0, True)
    if home_load_known:
        assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
        assert snapshot.power_kw == 7.0
        assert home.non_ev_home_load_kw == 3.0
        assert power_sync._get_ev_vehicle_status(hass, _Entry())["ev_power_kw"] == 7.0
    else:
        assert snapshot.unavailable_active_keys == ("vehicle:lrwyhcek8tc828420",)
        assert home.non_ev_home_load_kw is None


def test_offline_fleet_row_yields_to_fresh_ble_telemetry_in_merge():
    from power_sync.automations.loadpoint_status import coalesce_vehicle_observations

    now = datetime.now(timezone.utc)
    fleet = {
        "vehicle_id": "5YJTEST0000000001", "vehicle_name": "PRIMARY EV",
        "telemetry_state": "offline", "_telemetry_observed_at": None,
        "power_available": False, "is_charging": False,
    }
    ble = {
        "vehicle_id": "5YJTEST0000000001", "vehicle_name": "PRIMARY EV",
        "power_available": True, "ev_power_kw": 7.0, "is_charging": True,
        "_observed_at": now,
    }
    merged = coalesce_vehicle_observations([fleet, ble])
    assert len(merged) == 1
    assert merged[0]["telemetry_state"] == "live"
    assert merged[0]["_telemetry_observed_at"] == now


@pytest.mark.parametrize("fleet_soc_newer", [False, True])
@pytest.mark.parametrize("ble_connected", [False, True])
def test_mapped_ble_state_and_soc_have_independent_freshness(fleet_soc_newer, ble_connected):
    """W3RT1E: old Fleet/zero watts must not hide newer BLE state or SOC."""
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    native = now - timedelta(hours=9)
    power_at = now - timedelta(hours=4)
    state_at = now - timedelta(minutes=7)
    native_soc_at = now - timedelta(minutes=3) if fleet_soc_newer else native
    native_states = [
        _State("binary_sensor.primary_ev_status", "on", last_updated=native),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=native),
        _State("sensor.primary_ev_charging", "stopped", last_updated=native),
        _State("sensor.primary_ev_charger_power", "0", {"unit_of_measurement": "kW"}, last_updated=native),
        _State("sensor.primary_ev_battery_level", "70", last_updated=native_soc_at),
    ]
    ble_states = [
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("binary_sensor.home_ble_ble_status", "on" if ble_connected else "off", last_updated=now),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=state_at),
        _State("sensor.home_ble_charge_power", "0", {"unit_of_measurement": "kW"}, last_updated=power_at),
        _State("sensor.home_ble_charge_level", "67", last_updated=power_at),
    ]
    hass = _tesla_hass(native_states + ble_states)
    hass.entity_registry.entities = {
        key: value for key, value in hass.entity_registry.entities.items()
        if "primary_ev" in key
    }
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "both", "tesla_ble_entity_prefix": "home_ble",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=home_ble",
    })
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    assert len(vehicles) == 1
    vehicle = vehicles[0]
    expected_soc = 70 if fleet_soc_newer or not ble_connected else 67
    assert vehicle["ev_soc"] == expected_soc
    assert vehicle.get("_soc_observed_at") == (
        native_soc_at if expected_soc == 70 else power_at
    )
    row = build_loadpoint_status({}, vehicles)[0]
    assert row["soc"] == expected_soc
    if ble_connected:
        latest = native_soc_at if fleet_soc_newer else state_at
        assert row["telemetry_state"] == "live"
        assert row["observed_at"] == latest.isoformat()
        assert vehicle["_charging_observed_at"] == state_at
        assert row["current_power_kw"] is None
        assert row["confidence"] == "unknown"
        assert row["source"] == "unknown"
    else:
        assert row["telemetry_state"] == ("live" if fleet_soc_newer else "stale")


@pytest.mark.parametrize("asleep,ble_link,home_load_known", [
    # Live capture 2026-11-14: Fleet froze at 03:01 with W3RT1E "charging 2 kW";
    # the BLE bridge reported Stopped/0 kW at 06:00 and the car asleep at 06:00:53.
    ("on", "on", True),
    # Awake (or unknown sleep state): stale Fleet charging stays unknown.
    ("off", "on", False),
    # Bridge lost the car: its asleep value is no longer current evidence.
    ("on", "off", False),
])
def test_ble_asleep_resolves_stale_fleet_charging_claim(monkeypatch, asleep, ble_link, home_load_known):
    """A car the BLE bridge reports asleep is not charging; Home Load stays known."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime.now(timezone.utc)
    fleet_at = now - timedelta(hours=5)
    ble_at = now - timedelta(hours=2)
    native_states = [
        _State("binary_sensor.primary_ev_status", "on", {"device_class": "connectivity"}, last_updated=fleet_at),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=fleet_at),
        _State("sensor.primary_ev_charging", "charging", last_updated=fleet_at),
        _State("sensor.primary_ev_charger_power", "2", {"unit_of_measurement": "kW"}, last_updated=fleet_at),
        _State("device_tracker.primary_ev_location", "home", last_updated=fleet_at),
    ]
    ble_states = [
        _State("binary_sensor.home_ble_status", "on", last_updated=now - timedelta(hours=5)),
        _State("binary_sensor.home_ble_ble_status", ble_link, last_updated=now - timedelta(hours=5)),
        _State("binary_sensor.home_ble_asleep", asleep, last_updated=ble_at),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=ble_at),
        _State("sensor.home_ble_charge_power", "0", {"unit_of_measurement": "kW"}, last_updated=ble_at),
        _State("sensor.home_ble_charge_level", "73", last_updated=ble_at),
    ]
    hass = _tesla_hass(native_states + ble_states)
    hass.entity_registry.entities = {
        key: value for key, value in hass.entity_registry.entities.items()
        if "primary_ev" in key
    }
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "both", "tesla_ble_entity_prefix": "home_ble",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=home_ble",
    })

    async def no_sigenergy_charger(*_args, **_kwargs):
        return None

    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger)
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    observations = asyncio.run(power_sync._get_ev_load_observations(hass, entry, vehicles))
    at = datetime.now(timezone.utc)  # observations are stamped while building them
    snapshot = ev_load.aggregate_ev_load(observations, at=at)
    home = ev_load.normalize_home_load(1.731, ev_load.HomeLoadBasis.INCLUDES_EV, snapshot, at=at)

    if home_load_known:
        assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
        assert snapshot.power_kw == 0.0
        assert home.non_ev_home_load_kw == pytest.approx(1.731)
        assert not any(v.get("is_charging") for v in vehicles)
        assert not any(v.get("_power_activity_unknown") for v in vehicles)
    else:
        assert snapshot.quality == ev_load.EvLoadQuality.INCOMPLETE
        assert home.non_ev_home_load_kw is None


def test_ble_battery_receipt_dates_soc_without_renewing_old_watts():
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=4)
    received = now - timedelta(seconds=5)
    hass = _Hass([
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=old),
        _State("sensor.home_ble_charge_power", "0", {"unit_of_measurement": "kW"}, last_updated=old),
        _State("sensor.home_ble_charge_level", "67", last_updated=old),
        _State("sensor.home_ble_charge_level_received", received.isoformat(), last_updated=now),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "home_ble",
    })
    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]
    assert vehicle["_soc_observed_at"] == received
    assert vehicle["_observed_at"] == old
    assert vehicle["power_available"] is False
    row = build_loadpoint_status({}, [vehicle])[0]
    assert row["telemetry_state"] == "live"
    assert row["observed_at"] == received.isoformat()
    assert row["current_power_kw"] is None


@pytest.mark.parametrize("source", ["state_report", "state_receipt", "soc_report", "soc_receipt"])
def test_ble_fresh_reports_refresh_display_without_renewing_control_or_power(source):
    """W3RT1E: equal BLE reports have a display clock independent of watts."""
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    observed = now - timedelta(seconds=5)
    prefix = "second_car"
    states = [
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=old),
        _State(f"binary_sensor.{prefix}_ble_status", "on", last_updated=now),
        _State(f"sensor.{prefix}_charging_state", "Stopped", last_updated=old),
        _State(f"sensor.{prefix}_charge_power", "0", {"unit_of_measurement": "kW"}, last_updated=old),
        _State(f"sensor.{prefix}_charge_level", "67", last_updated=old),
    ]
    entity = states[2] if source.startswith("state") else states[4]
    if source.endswith("report"):
        entity.last_reported = observed
    else:
        states.append(_State(f"{entity.entity_id}_received", observed.isoformat(), last_updated=now))
    hass = _Hass(states)
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": prefix,
    })

    vehicle = power_sync._get_ev_vehicles_status(hass, entry, include_control_evidence=True)[0]
    row = build_loadpoint_status({}, [vehicle])[0]

    assert row["telemetry_state"] == "live"
    assert row["observed_at"] == observed.isoformat()
    assert row["current_power_kw"] is None
    assert vehicle["power_available"] is False
    assert vehicle["_observed_at"] == old
    assert vehicle["_charging_observed_at"] == old
    assert vehicle["_connected_observed_at"] == old
    assert vehicle["_boost_charging_state_evidence"]["observed_at"] == old


@pytest.mark.parametrize("invalid", ["unknown", "unavailable", "bad", "2036-01-01T00:00:00+00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00+00:00"])
def test_ble_expired_or_invalid_state_receipt_cannot_refresh_display_from_ha_replay(invalid):
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    hass = _Hass([
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Stopped", last_updated=now),
        _State("sensor.car_charging_state_received", invalid, last_updated=now),
        _State("sensor.car_charge_power", "0", last_updated=old),
        _State("sensor.car_charge_level", "67", last_updated=old),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "car",
    })
    row = build_loadpoint_status({}, power_sync._get_ev_vehicles_status(hass, entry))[0]
    assert row["telemetry_state"] == "stale"
    assert row["observed_at"] == old.isoformat()
    assert row["current_power_kw"] is None


def test_ble_display_receipts_stay_independent_between_two_bridges():
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    states = []
    for prefix, received in (("car", old), ("car_second", now)):
        states.extend([
            _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
            _State(f"sensor.{prefix}_charging_state", "Stopped", last_updated=old),
            _State(f"sensor.{prefix}_charging_state_received", received.isoformat(), last_updated=now),
            _State(f"sensor.{prefix}_charge_power", "0", last_updated=old),
            _State(f"sensor.{prefix}_charge_level", "67", last_updated=old),
        ])
    hass = _Hass(states)
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "car,car_second",
    })
    rows = {r["vehicle_id"]: r for r in build_loadpoint_status(
        {}, power_sync._get_ev_vehicles_status(hass, entry),
    )}
    assert rows["ble_car"]["telemetry_state"] == "stale"
    assert rows["ble_car"]["observed_at"] == old.isoformat()
    assert rows["ble_car_second"]["telemetry_state"] == "live"
    assert rows["ble_car_second"]["observed_at"] == now.isoformat()
    assert all(r["current_power_kw"] is None for r in rows.values())


def test_fresh_paired_ble_display_keeps_newer_fleet_power_and_control_evidence():
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    native_at = now - timedelta(seconds=30)
    received = now - timedelta(seconds=5)
    states = [
        _State("binary_sensor.primary_ev_status", "on", last_updated=native_at),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=native_at),
        _State("sensor.primary_ev_charging", "charging", last_updated=native_at),
        _State("sensor.primary_ev_charger_power", "7.4", {"unit_of_measurement": "kW"}, last_updated=native_at),
        _State("sensor.primary_ev_battery_level", "70", last_updated=native_at),
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Stopped", last_updated=old),
        _State("sensor.car_charge_power", "0", last_updated=old),
        _State("sensor.car_charge_level", "67", last_updated=old),
        _State("sensor.car_charge_level_received", received.isoformat(), last_updated=now),
    ]
    hass = _tesla_hass(states)
    hass.entity_registry.entities = {
        k: v for k, v in hass.entity_registry.entities.items() if "primary_ev" in k
    }
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "both", "tesla_ble_entity_prefix": "car",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=car",
    })
    vehicles = power_sync._get_ev_vehicles_status(hass, entry, include_control_evidence=True)
    assert len(vehicles) == 1
    vehicle = vehicles[0]
    row = build_loadpoint_status({}, vehicles)[0]
    assert row["telemetry_state"] == "live"
    assert row["observed_at"] == received.isoformat()
    assert row["soc"] == 67
    assert row["current_power_kw"] == 7.4
    assert row["actual_charging"] is True
    assert vehicle["_observed_at"] == native_at
    assert vehicle["_charging_observed_at"] == native_at
    assert vehicle["_boost_charging_state_evidence"] == {
        "known": True, "charging": True, "observed_at": native_at,
    }


def test_ble_connected_with_old_vehicle_reports_remains_stale():
    """Live W3RT1E capture: reconnecting BLE is not a vehicle-data receipt."""
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=1)
    hass = _Hass([
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("binary_sensor.car_ble_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Stopped", last_updated=old),
        _State("sensor.car_charge_power", "0", last_updated=old),
        _State("sensor.car_charge_level", "67", last_updated=old),
        _State("sensor.car_last_update", "Wed Nov  4 22:04:16 2026\n", last_updated=now),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "car",
    })
    row = build_loadpoint_status({}, power_sync._get_ev_vehicles_status(hass, entry))[0]
    assert row["telemetry_state"] == "stale"
    assert row["observed_at"] == old.isoformat()
    assert row["current_power_kw"] is None


@pytest.mark.parametrize("has_receipts", [False, True])
def test_ble_replayed_ha_values_need_raw_receipts_to_identify_native_age(has_receipts):
    """Keep the documented legacy fallback; only receipts can date a replay."""
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    states = [
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Stopped", last_updated=now),
        _State("sensor.car_charge_power", "0", last_updated=now),
        _State("sensor.car_charge_level", "67", last_updated=now),
        _State("sensor.car_last_update", "Wed Nov  4 22:04:16 2026\n", last_updated=now),
    ]
    if has_receipts:
        states.extend([
            _State(f"sensor.car_{field}_received", old.isoformat(), last_updated=now)
            for field in ("charging_state", "charge_power", "charge_level")
        ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "car",
    })
    row = build_loadpoint_status({}, power_sync._get_ev_vehicles_status(_Hass(states), entry))[0]
    assert row["telemetry_state"] == ("stale" if has_receipts else "live")
    assert row["observed_at"] == (old if has_receipts else now).isoformat()
    assert row["current_power_kw"] == (None if has_receipts else 0.0)


def test_quarantined_ble_cannot_renew_telemetry_from_soc_or_connection(monkeypatch):
    power_sync = _power_sync_module()
    from power_sync.automations.loadpoint_status import build_loadpoint_status

    monkeypatch.setattr(importlib.import_module("power_sync.tesla_ev_recovery"),
                        "ble_quarantined", lambda *_args: True)
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=now),
        _State("sensor.home_ble_charge_power", "0", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.home_ble_charge_level", "67", last_updated=now),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "home_ble",
    })
    vehicle = power_sync._get_ev_vehicles_status(hass, entry, include_control_evidence=True)[0]
    assert vehicle["ev_soc"] == 67
    assert vehicle.get("_soc_observed_at") is None
    assert vehicle["_boost_charging_state_evidence"]["known"] is False
    row = build_loadpoint_status({}, [vehicle])[0]
    assert row["telemetry_state"] == "offline"
    assert row["observed_at"] is None
    assert row["current_power_kw"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_native_battery_candidates_choose_newest_soc_report(reverse):
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=9)
    states = [
        _State("sensor.primary_ev_battery_level", "70", last_updated=old),
        _State("sensor.primary_ev_battery_level_2", "67", last_updated=now - timedelta(seconds=1)),
        _State("sensor.primary_ev_battery_temperature", "22", {"unit_of_measurement": "°C"}, last_updated=now),
        _State("sensor.primary_ev_battery_power", "5", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.primary_ev_battery_health", "99", {"unit_of_measurement": "%"}, last_updated=now),
    ]
    vehicle = power_sync._get_ev_vehicles_status(
        _tesla_hass(list(reversed(states)) if reverse else states), _Entry(),
    )[0]
    assert vehicle["ev_soc"] == 67
    assert vehicle["_soc_observed_at"] == now - timedelta(seconds=1)


def test_ble_node_online_does_not_make_disconnected_vehicle_online_or_plugged():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("binary_sensor.home_ble_ble_status", "off", last_updated=now),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=now),
        _State("binary_sensor.home_ble_charge_flap", "on", last_updated=now),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "home_ble",
    })
    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]
    assert vehicle["is_connected"] is False
    assert vehicle.get("site_presence") != "home"
    mobile = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("home_ble")
    assert mobile["is_online"] is False
    assert mobile["connection_usable"] is False
    assert mobile["is_plugged_in"] is False
    assert mobile["plugged_in_definitive"] is False


def test_disconnected_paired_ble_does_not_erase_known_native_plug():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
        _State("sensor.primary_ev_charging", "stopped", last_updated=now),
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("binary_sensor.home_ble_ble_status", "off", last_updated=now),
        _State("sensor.home_ble_charging_state", "Unknown", last_updated=now),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "both", "tesla_ble_entity_prefix": "home_ble",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=home_ble",
    })
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    assert len(vehicles) == 1
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["site_presence"] == "home"


def _stale_active_fleet_with_disconnected_ble(power_sync, *, native_age=10800):
    now = datetime.now(timezone.utc)
    native = now - timedelta(seconds=native_age)
    disconnected = now - timedelta(minutes=25)
    states = [
        _State("binary_sensor.primary_ev_status", "on", last_updated=native),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=native),
        _State("device_tracker.primary_ev_location", "home", last_updated=native),
        _State("sensor.primary_ev_charging", "charging", last_updated=native),
        _State("sensor.primary_ev_charger_power", "2", {"unit_of_measurement": "kW"}, last_updated=native),
        _State("sensor.primary_ev_battery_level", "66", last_updated=native),
        _State("binary_sensor.home_ble_status", "on", last_updated=native),
        _State("binary_sensor.home_ble_ble_status", "off", last_updated=disconnected),
        _State("sensor.home_ble_charging_state", "Unknown", last_updated=disconnected),
        _State("binary_sensor.home_ble_charge_flap", "unknown", last_updated=disconnected),
        _State("sensor.home_ble_charge_power", "unknown", last_updated=disconnected),
    ]
    hass = _tesla_hass(states)
    # The bridge is a separate ESPHome device, not a second set of Fleet sensors.
    hass.entity_registry.entities = {
        key: value for key, value in hass.entity_registry.entities.items()
        if "primary_ev" in key
    }
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": power_sync.EV_PROVIDER_BOTH,
        "tesla_ble_entity_prefix": "home_ble",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=home_ble",
    })
    return hass, entry, native, now


@pytest.mark.parametrize("cached_connector", [False, True])
def test_newer_ble_disconnect_invalidates_expired_active_fleet_status(cached_connector):
    """W3RT1E recurrence: cached cable ON/Charging must not survive departure."""
    power_sync = _power_sync_module()
    hass, entry, native, _now = _stale_active_fleet_with_disconnected_ble(power_sync)
    if cached_connector:
        hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(data={
            "last_update": native + timedelta(seconds=30),
            "wall_connectors_raw": [{
                "vin": "5YJTEST0000000001", "wall_connector_state": 2,
                "wall_connector_power": 2000,
            }],
        })
    vehicles = power_sync._get_ev_vehicles_status(hass, entry, include_control_evidence=True)
    assert len(vehicles) == 1
    vehicle = vehicles[0]
    assert vehicle["is_connected"] is False
    assert vehicle["is_charging"] is False
    assert vehicle.get("site_presence") != "home"
    assert vehicle["power_available"] is False
    assert vehicle["_power_activity_unknown"] is True
    assert vehicle["_boost_charging_state_evidence"]["known"] is False
    from power_sync.automations.loadpoint_status import build_loadpoint_status
    for dynamic in ({}, {vehicle["vehicle_id"]: {"active": False, "current_amps": 0}}):
        row = build_loadpoint_status(dynamic, vehicles)[0]
        assert row["connected"] is False
        assert row["actual_charging"] is False
        assert row["status"] == "unknown"
        assert row["current_power_kw"] is None
        assert row["soc"] == 66


def test_fresh_native_charging_survives_older_ble_disconnect():
    power_sync = _power_sync_module()
    hass, entry, _native, _now = _stale_active_fleet_with_disconnected_ble(power_sync, native_age=10)
    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]
    assert vehicle["is_connected"] is True
    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is True
    assert vehicle["ev_power_kw"] == 2
    assert vehicle["site_presence"] == "home"


def test_unrelated_bridge_disconnect_cannot_invalidate_another_vehicle():
    power_sync = _power_sync_module()
    hass, entry, _native, _now = _stale_active_fleet_with_disconnected_ble(power_sync)
    hass.device_registry.devices["other_fleet"] = SimpleNamespace(
        id="other_fleet", name="Other EV",
        identifiers={("tesla_fleet", "5YJTEST0000000002")},
    )
    entry.options["tesla_ble_vehicle_mapping"] = "5YJTEST0000000002=home_ble"
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    vehicle = next(v for v in vehicles if v["vehicle_id"] == "5YJTEST0000000001")
    assert vehicle["is_connected"] is True
    assert vehicle["site_presence"] == "home"


def test_vinless_connector_cannot_claim_vehicle_with_unconfirmed_presence():
    power_sync = _power_sync_module()
    hass, entry, _native, now = _stale_active_fleet_with_disconnected_ble(power_sync)
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    assert power_sync._apply_wall_connector_observation(
        vehicles, 4.0, True, True, wc_observed_at=now,
    ) is False
    assert vehicles[0]["is_connected"] is False
    assert vehicles[0]["power_available"] is False


def test_repeated_native_cable_report_is_fresh_even_when_value_has_not_changed():
    power_sync = _power_sync_module()
    hass, entry, native, now = _stale_active_fleet_with_disconnected_ble(power_sync)
    hass.states._states["binary_sensor.primary_ev_charge_cable"] = _State(
        "binary_sensor.primary_ev_charge_cable", "on", last_updated=native,
        last_reported=now,
    )
    assert power_sync._get_ev_vehicles_status(hass, entry)[0]["is_connected"] is True


@pytest.mark.parametrize("age,confirmed", [(0, True), (10800, False), (-30, False), (None, False)])
def test_only_fresh_exact_vehicle_connector_can_recover_unconfirmed_fleet(age, confirmed):
    power_sync = _power_sync_module()
    hass, entry, _native, now = _stale_active_fleet_with_disconnected_ble(power_sync)
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(data={
        "last_update": now - timedelta(seconds=age) if age is not None else None,
        "wall_connectors_raw": [{
            "vin": "5YJTEST0000000001", "wall_connector_state": 2,
            "wall_connector_power": 4000,
        }],
    })
    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]
    assert vehicle["is_connected"] is confirmed
    assert vehicle["is_charging"] is confirmed
    if confirmed:
        assert vehicle["ev_power_kw"] == 4
        assert vehicle.get("power_available", True) is True
        assert vehicle["site_presence"] == "home"
        assert not vehicle.get("_power_activity_unknown")
    else:
        assert vehicle["power_available"] is False
        assert vehicle.get("site_presence") != "home"


def test_mobile_stale_active_fleet_becomes_unknown_after_paired_ble_disconnect():
    power_sync = _power_sync_module()
    hass, _entry, native, _now = _stale_active_fleet_with_disconnected_ble(power_sync)
    mobile = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("home_ble")
    fleet = {
        "is_plugged_in": True, "charging_state": "Charging", "charger_power": None,
        "battery_level": 66, "data_updated_at": native.isoformat(),
        "is_plugged_in_updated_at": native.isoformat(),
        "charging_state_updated_at": native.isoformat(),
    }
    power_sync.EVVehiclesView._supplement_fleet_with_ble(fleet, mobile)
    assert fleet["is_plugged_in"] is False
    assert fleet["charging_state"] == "Unknown"
    assert fleet["charger_power"] is None
    assert fleet["battery_level"] == 66


def test_newer_ble_disconnect_keeps_possible_ev_load_unknown_in_canonical_api(monkeypatch):
    power_sync = _power_sync_module()
    hass, entry, _native, _now = _stale_active_fleet_with_disconnected_ble(power_sync)
    actions = importlib.import_module("power_sync.automations.actions")
    monkeypatch.setattr(actions, "_dynamic_ev_state", {})
    monkeypatch.setattr(actions, "_calculate_solar_surplus", lambda *_args: 0.0)
    monkeypatch.setattr(power_sync, "_configured_sigenergy_charger_state", lambda _entry: None)
    async def no_sigenergy(*_args):
        return None
    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy)
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(data={
        "load_power": 2.5, "load_power_raw": 2.5,
        "solar_power": 0.0, "grid_power": 2.5, "battery_power": 0.0,
    })
    snapshot = asyncio.run(
        power_sync._get_ev_display_coordinator(hass, entry).async_refresh(force=True)
    )
    assert snapshot["success"] is True
    row = snapshot["loadpoints"][0]
    assert row["connected"] is False
    assert row["actual_charging"] is False
    assert row["status"] == "unknown"
    assert row["current_power_kw"] is None
    assert snapshot["site"]["ev_power_kw"] is None
    assert snapshot["site"]["observation_quality"] == "incomplete"
    assert snapshot["site"]["load_power_kw"] is None


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
        "telemetry_state": "stale",
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
        "telemetry_state": "stale",
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


def test_newer_paired_ble_home_presence_overrides_stale_fleet_away():
    """Ticket #56: stale Fleet location cannot erase current BLE site power."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=5)
    states = [
        _State("sensor.primary_ev_charger_power", "0.0", last_updated=stale),
        _State("sensor.primary_ev_charging_state", "disconnected", last_updated=stale),
        _State("binary_sensor.primary_ev_charge_cable", "off", last_updated=stale),
        _State("device_tracker.primary_ev_location", "away", last_updated=stale),
        _State("sensor.primary_ev_battery_level", "86", last_updated=stale),
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("sensor.home_ble_charging_state", "Charging", last_updated=now),
        _State("binary_sensor.home_ble_charge_flap", "on", last_updated=now),
        _State(
            "sensor.home_ble_charge_power",
            "2.0",
            {"unit_of_measurement": "kW"},
            last_updated=now,
        ),
        _State("sensor.home_ble_charge_level", "86", last_updated=now),
    ]
    hass = _tesla_hass(states)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_BOTH,
            "tesla_ble_entity_prefix": "home_ble",
            "tesla_ble_vehicle_mapping": f"{vin}=home_ble",
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert len(vehicles) == 1
    assert vehicles[0]["vehicle_id"] == vin
    assert vehicles[0]["ev_power_kw"] == 2.0
    assert vehicles[0]["is_connected"] is True
    assert vehicles[0]["is_charging"] is True
    assert vehicles[0]["site_presence"] == "home"
    assert power_sync._get_ev_vehicle_status(hass, entry) == {
        "ev_power_kw": 2.0,
        "ev_soc": 86,
    }


def test_newer_fleet_away_presence_still_excludes_paired_ble_power():
    """Ticket #56: current remote-location evidence remains fail-closed."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=5)
    states = [
        _State("sensor.primary_ev_charger_power", "0.0", last_updated=now),
        _State("sensor.primary_ev_charging_state", "disconnected", last_updated=now),
        _State("binary_sensor.primary_ev_charge_cable", "off", last_updated=now),
        _State("device_tracker.primary_ev_location", "away", last_updated=now),
        _State("sensor.primary_ev_battery_level", "86", last_updated=now),
        _State("binary_sensor.remote_ble_status", "on", last_updated=stale),
        _State("sensor.remote_ble_charging_state", "Charging", last_updated=stale),
        _State("binary_sensor.remote_ble_charge_flap", "on", last_updated=stale),
        _State(
            "sensor.remote_ble_charge_power",
            "2.0",
            {"unit_of_measurement": "kW"},
            last_updated=stale,
        ),
        _State("sensor.remote_ble_charge_level", "86", last_updated=stale),
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
    assert vehicles[0]["ev_power_kw"] == 0.0
    assert vehicles[0]["is_connected"] is False
    assert vehicles[0]["is_charging"] is False
    assert vehicles[0]["site_presence"] == "away"
    assert power_sync._get_ev_vehicle_status(hass, entry)["ev_power_kw"] == 0.0


def test_named_zone_does_not_suppress_unpaired_ble_outside_both_mode():
    """A stale Fleet tracker cannot claim an unrelated BLE-only vehicle."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    # BLE power is only usable while fresh (ev_load.EV_POWER_MAX_AGE).
    now = datetime.now(timezone.utc)
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
            last_updated=now,
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


def test_site_ev_aggregate_does_not_become_a_phantom_wall_connector(monkeypatch):
    """#409: a prior BLE total must not feed back as a fresh charger meter."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: now)
    async def no_sigenergy_charger(*_args):
        return None
    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger)
    coordinator = SimpleNamespace(data={
        "wall_connectors_raw": [],
        "ev_power": 7.0,
        "last_update": now,
    })
    hass = _Hass([], entry_data={"tesla_coordinator": coordinator})
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": power_sync.EV_PROVIDER_TESLA_BLE,
        "tesla_ble_entity_prefix": "donkey,electric_bullet",
    })

    for charging_state, expected_power, expected_connected in (
        ("Charging", 7.0, True),
        ("Complete", 0.0, True),
        ("Disconnected", 0.0, False),
    ):
        states = []
        for prefix, state in (("donkey", "Disconnected"),
                              ("electric_bullet", charging_state)):
            states.extend([
                _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
                _State(f"sensor.{prefix}_charging_state", state, last_updated=now),
                _State(f"binary_sensor.{prefix}_charge_flap",
                       "off" if state == "Disconnected" else "on", last_updated=now),
                # The bridge keeps reporting the old watts after completion.
                _State(f"sensor.{prefix}_charge_power", "7.0",
                       {"unit_of_measurement": "kW"},
                       last_updated=now - timedelta(seconds=30), last_reported=now),
                _State(f"sensor.{prefix}_charge_level", "81", last_updated=now),
            ])
        hass.states = _States(states)
        # A refreshed site poll must never renew the prior derived total as
        # independent evidence, even across repeated status/aggregation reads.
        for _ in range(3):
            vehicles = power_sync._get_ev_vehicles_status(hass, entry)
            assert {v["vehicle_id"] for v in vehicles} == {
                "ble_donkey", "ble_electric_bullet",
            }
            target = next(v for v in vehicles if v["vehicle_id"] == "ble_electric_bullet")
            assert target["is_connected"] is expected_connected
            assert target["is_charging"] is (charging_state == "Charging")
            observations = asyncio.run(power_sync._get_ev_load_observations(hass, entry, vehicles))
            snapshot = ev_load.aggregate_ev_load(observations, at=now)
            assert snapshot.power_kw == expected_power
            assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
            normalized = ev_load.normalize_energy_data(
                {"raw_home_load_power": 1.475 + expected_power,
                 "load_power": 0.0, "ev_power": 7.0},
                battery_system="tesla", ev_load=snapshot, at=now,
            )
            assert abs(normalized["load_power"] - 1.475) < 1e-9


def test_real_wall_connector_meter_survives_unrelated_site_aggregate():
    """Only actual connector payloads or HA meters establish charger rows."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    for data, states, expected_id in (
        ({"wall_connectors_raw": [{"wall_connector_id": "garage",
                                    "wall_connector_power": 7000,
                                    "wall_connector_state": 2}]}, [], "wall_connector_garage"),
        ({"wall_connectors_raw": []},
         [_State("sensor.wall_connector_power", "7.0",
                 {"unit_of_measurement": "kW"}, last_updated=now)], "wall_connector_ha"),
    ):
        data.update({"ev_power": 99.0, "last_update": now})
        hass = _Hass(states, entry_data={"tesla_coordinator": SimpleNamespace(data=data)})
        vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
        assert len(vehicles) == 1
        assert vehicles[0]["vehicle_id"] == expected_id
        assert vehicles[0]["ev_power_kw"] == 7.0
        assert vehicles[0]["is_charging"] is True


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


def test_ble_only_provider_excludes_residual_fleet_registry_observation():
    """Ticket #56: BLE-only status must not absorb charging into Home Load."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    vin = "5YJTEST0000000001"
    states = [
        _State("sensor.primary_ev_charger_power", "0", {"unit_of_measurement": "kW"}, now),
        _State("sensor.primary_ev_charging_state", "disconnected", last_updated=now),
        _State("device_tracker.primary_ev_location", "away", last_updated=now),
        _State("binary_sensor.ble_status", "on", last_updated=now),
        _State("sensor.ble_charging_state", "Charging", last_updated=now),
        _State("binary_sensor.ble_charge_flap", "on", last_updated=now),
        _State("sensor.ble_charge_power", "3.84", {"unit_of_measurement": "kW"}, now),
        _State("sensor.ble_charge_level", "72", last_updated=now),
    ]
    hass = _tesla_hass(states)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": power_sync.EV_PROVIDER_TESLA_BLE,
            "tesla_ble_entity_prefix": "ble",
        },
    )

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)

    assert len(vehicles) == 1
    vehicle = vehicles[0]
    assert vehicle["vehicle_id"] == "ble_ble"
    assert vehicle["vehicle_name"] == "Tesla BLE (ble)"
    assert vehicle["ev_power_kw"] == 3.84
    assert vehicle["ev_soc"] == 72
    assert vehicle["is_connected"] is True
    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is True

    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(
        [
            ev_load.EvLoadObservation(
                physical_load_key="vehicle:ble_ble",
                source_key="ble_ble",
                power_kw=vehicle["ev_power_kw"],
                observed_at=vehicle["_observed_at"],
                active=vehicle["is_charging"],
            )
        ],
        at=now,
    )
    normalized = ev_load.normalize_energy_data(
        {"load_power": 5.30}, battery_system="sungrow", ev_load=snapshot, at=now
    )
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert snapshot.power_kw == 3.84
    assert round(normalized["load_power"], 2) == 1.46


def test_ble_power_is_not_replaced_by_vinless_wall_connector():
    """Ticket #409: a connector-wide refresh cannot identify a BLE vehicle."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.yf88_status", "on", last_updated=now),
        _State("sensor.yf88_charging_state", "Charging", last_updated=now),
        _State("binary_sensor.yf88_charge_flap", "on", last_updated=now),
        _State(
            "sensor.yf88_charge_power",
            "7.0",
            {"unit_of_measurement": "kW"},
            last_updated=now,
        ),
    ])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={
            "wall_connectors_raw": [{
                "wall_connector_state": 2,
                "wall_connector_power": 3000,
            }],
            "last_update": now + timedelta(seconds=1),
        }
    )
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

    assert vehicle["ev_power_kw"] == 7.0
    assert vehicle["is_charging"] is True


def test_ble_metadata_does_not_refresh_stale_power_measurement():
    """Stale BLE watts are withheld consistently from every status surface."""
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
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    observed_loadpoint = loadpoint_status.build_loadpoint_status({}, [vehicle])[0]
    dynamic_loadpoint = loadpoint_status.build_loadpoint_status(
        {
            "ble_yf88": {
                "active": True,
                "charging_started": True,
                "current_amps": 10,
                "target_amps": 10,
                "params": {"charger_type": "tesla", "voltage": 230},
            }
        },
        [vehicle],
    )[0]

    assert vehicle["_observed_at"] == stale_power
    assert vehicle["power_available"] is False
    assert snapshot.quality == ev_load.EvLoadQuality.INCOMPLETE
    assert snapshot.unavailable_active_keys == ("vehicle:ble_yf88",)
    assert observed_loadpoint["actual_charging"] is True
    assert observed_loadpoint["current_power_kw"] is None
    assert observed_loadpoint["confidence"] == "unknown"
    assert dynamic_loadpoint["actual_charging"] is True
    assert dynamic_loadpoint["current_power_kw"] is None
    assert dynamic_loadpoint["confidence"] == "unknown"


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


def test_fleet_stale_active_power_is_unavailable_across_status_surfaces():
    """Ticket #49: stale Fleet watts cannot be rendered as a live EV draw."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power", "7.0",
            {"unit_of_measurement": "kW"}, last_updated=now - timedelta(minutes=5),
        ),
        _State("sensor.primary_ev_charging_state", "charging", last_updated=now),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
    ])

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    loadpoint = loadpoint_status.build_loadpoint_status({}, [vehicle])[0]

    assert vehicle["ev_power_kw"] == 7.0
    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is False
    assert loadpoint["current_power_kw"] is None
    assert loadpoint["confidence"] == "unknown"


def test_fleet_active_unavailable_power_stays_active_and_incomplete(monkeypatch):
    """Ticket #49: Fleet charging state must not turn missing watts into idle."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State(
            "sensor.primary_ev_charger_power",
            "unavailable",
            {"unit_of_measurement": "kW"},
            last_updated=now,
        ),
        _State("sensor.primary_ev_charging_state", "charging", last_updated=now),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
    ])

    async def no_sigenergy_charger(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, _Entry())[0]
    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, _Entry(), [vehicle])
    )
    snapshot = ev_load.aggregate_ev_load(observations, at=now)
    loadpoint = loadpoint_status.build_loadpoint_status({}, [vehicle])[0]

    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is False
    assert observations[0].power_kw is None
    assert observations[0].active is True
    assert snapshot.quality == ev_load.EvLoadQuality.INCOMPLETE
    assert snapshot.unavailable_active_keys == ("vehicle:5yjtest0000000001",)
    assert loadpoint["actual_charging"] is True
    assert loadpoint["current_power_kw"] is None
    assert loadpoint["confidence"] == "unknown"


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


def test_overlapping_ble_prefixes_keep_both_vehicle_observations(monkeypatch):
    """A longer BLE prefix must not be hidden by a shorter substring prefix."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    tessy_vin = "5YJTEST0000000001"
    w3_vin = "5YJTEST0000000002"
    stale = datetime(2026, 8, 20, 3, 28, tzinfo=timezone.utc)
    fresh = stale + timedelta(seconds=30)
    # Freshness is judged against ev_load's wall clock; pin it to the fixture.
    monkeypatch.setattr(ev_load, "utc_now", lambda: fresh + timedelta(seconds=1))
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


def test_ble_newer_stopped_state_suppresses_re_reported_stale_power():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    prefix = "garage_ble"
    hass = _Hass([
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
        _State(
            f"sensor.{prefix}_charging_state",
            "Stopped",
            last_updated=now,
        ),
        _State(
            f"sensor.{prefix}_charge_power",
            "3.0",
            {"unit_of_measurement": "kW"},
            last_updated=now - timedelta(minutes=1),
            # A state event can be re-reported now without being a new watt
            # measurement; this is the Ticket #409 shape.
            last_reported=now,
        ),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": prefix},
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["is_connected"] is True
    assert vehicle["is_charging"] is False
    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["power_available"] is False


def _stopped_ble_vehicle(power_last_updated, state_last_updated, now):
    """Build the Ticket #409 bridge shape with the two stamps under test."""
    prefix = "garage_ble"
    hass = _Hass([
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
        _State(
            f"sensor.{prefix}_charging_state",
            "Stopped",
            last_updated=state_last_updated,
            # The bridge keeps re-reporting the unchanged Stopped state, so it
            # is demonstrably alive.
            last_reported=now,
        ),
        _State(
            f"sensor.{prefix}_charge_power",
            "7.0",
            {"unit_of_measurement": "kW"},
            last_updated=power_last_updated,
            last_reported=now,
        ),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": prefix},
    )
    return hass, entry


def test_ble_persisted_stopped_state_suppresses_newer_stale_power_value():
    """Ticket #409: the stop predates the last power value change.

    The ordering-only guard never fired for this shape, so 7.0 kW of phantom
    EV load survived every poll and pinned Home Load to 0.0 kW until the
    integration was reloaded.
    """
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=now - timedelta(minutes=5),
        state_last_updated=now - timedelta(minutes=10),
        now=now,
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["is_charging"] is False
    assert vehicle["power_available"] is False


def test_ble_equal_timestamp_stopped_state_suppresses_stale_power_value():
    """The strict ``>`` comparison also leaked when both stamps matched."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(minutes=10)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=stamp,
        state_last_updated=stamp,
        now=now,
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["is_charging"] is False
    assert vehicle["power_available"] is False


def test_ble_recent_stopped_state_still_allows_a_newer_power_value():
    """A stop inside the attribution window must not veto live watts.

    Guards against over-correcting: a bridge that reports the watt value a
    moment before flipping the state to Charging keeps its reading.
    """
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=now,
        state_last_updated=now - timedelta(seconds=30),
        now=now,
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 7.0
    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is True


def test_ble_complete_attribute_refresh_cannot_keep_phantom_power_active(monkeypatch):
    """#409: attribute updates are not new charging-state or watt values."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=now,
        state_last_updated=now - timedelta(seconds=1),
        now=now,
    )
    charging = hass.states.get("sensor.garage_ble_charging_state")
    charging.state = "Complete"
    charging.last_changed = now - timedelta(minutes=3)
    power = hass.states.get("sensor.garage_ble_charge_power")
    # Even a delayed positive value after Complete expires after 90 seconds
    # of a currently reporting, unchanged Complete state.
    power.last_changed = now - timedelta(minutes=2)

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["is_connected"] is True  # Cable remains plugged in.
    assert vehicle["is_charging"] is False
    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["power_available"] is False

    async def no_sigenergy_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_state,
    )
    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, entry, [vehicle])
    )
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(observations, at=now)
    normalized = ev_load.normalize_home_load(
        1.783, ev_load.HomeLoadBasis.INCLUDES_EV, snapshot, at=now,
    )
    assert snapshot.power_kw == 0.0
    assert snapshot.quality.value == "complete"
    assert normalized.non_ev_home_load_kw == 1.783


def test_ble_new_complete_state_beats_attributes_on_unchanged_power():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=now,
        state_last_updated=now - timedelta(seconds=1),
        now=now,
    )
    charging = hass.states.get("sensor.garage_ble_charging_state")
    charging.state = "Complete"
    charging.last_changed = now - timedelta(seconds=1)
    hass.states.get("sensor.garage_ble_charge_power").last_changed = (
        now - timedelta(minutes=2)
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["is_charging"] is False
    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["power_available"] is False


def test_ble_state_attributes_do_not_veto_a_genuinely_newer_power_value():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass, entry = _stopped_ble_vehicle(
        power_last_updated=now - timedelta(seconds=1),
        state_last_updated=now,
        now=now,
    )
    hass.states.get("sensor.garage_ble_charging_state").last_changed = (
        now - timedelta(seconds=30)
    )
    hass.states.get("sensor.garage_ble_charge_power").last_changed = (
        now - timedelta(seconds=1)
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 7.0
    assert vehicle["is_charging"] is True
    assert vehicle["power_available"] is True


def test_ble_silent_state_entity_cannot_veto_a_live_power_value():
    """A bridge that stopped reporting its state must not veto the meter."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    prefix = "garage_ble"
    hass = _Hass([
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
        _State(
            f"sensor.{prefix}_charging_state",
            "Stopped",
            last_updated=now - timedelta(minutes=10),
            last_reported=now - timedelta(minutes=10),
        ),
        _State(
            f"sensor.{prefix}_charge_power",
            "7.0",
            {"unit_of_measurement": "kW"},
            last_updated=now - timedelta(minutes=5),
            last_reported=now,
        ),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": prefix},
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 7.0
    assert vehicle["power_available"] is True


def test_ble_expired_power_does_not_assert_charging_activity(monkeypatch):
    """Expired watts cannot claim an active EV or external ownership."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    prefix = "garage_ble"
    hass = _Hass([
        _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
        _State(
            f"sensor.{prefix}_charging_state",
            "Stopped",
            last_updated=now - timedelta(minutes=10),
        ),
        _State(
            f"sensor.{prefix}_charge_power",
            "7.0",
            {"unit_of_measurement": "kW"},
            last_updated=now - timedelta(minutes=5),
        ),
        _State(f"sensor.{prefix}_charge_level", "78", last_updated=now),
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"tesla_ble_entity_prefix": prefix},
    )

    vehicle = power_sync._get_ev_vehicles_status(hass, entry)[0]

    assert vehicle["ev_power_kw"] == 0.0
    assert vehicle["power_available"] is False
    assert vehicle["is_charging"] is False
    assert vehicle["_power_activity_unknown"] is True

    async def no_sigenergy_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        power_sync,
        "_read_sigenergy_charger_state_for_entry",
        no_sigenergy_state,
    )
    observations = asyncio.run(
        power_sync._get_ev_load_observations(hass, entry, [vehicle])
    )
    assert observations[0].power_kw is None
    assert observations[0].active is True
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(
        observations,
        at=now,
    )
    assert snapshot.quality.value == "incomplete"
    assert snapshot.unavailable_active_keys == ("vehicle:blegarageble",)
    normalized = ev_load.normalize_home_load(
        1.783,
        ev_load.HomeLoadBasis.INCLUDES_EV,
        snapshot,
        at=now,
    )
    assert normalized.non_ev_home_load_kw is None
    assert normalized.normalization_quality.value == "incomplete"


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


@pytest.mark.parametrize("quarantined", [False, True])
def test_mobile_newer_fleet_stop_survives_retained_ble_charging(quarantined):
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=3)
    fleet = {
        "charging_state": "Stopped", "charger_power": 0.0,
        "is_plugged_in": True, "data_updated_at": now.isoformat(),
        "charging_state_updated_at": now.isoformat(),
    }
    ble = {
        "charging_state": "Charging", "charger_power": 2.0,
        "is_plugged_in": True, "plugged_in_definitive": True,
        "is_online": True, "transport_quarantined": quarantined,
        # A recent bridge/status/SoC report cannot renew old charge data.
        "data_updated_at": (now + timedelta(seconds=1)).isoformat(),
        "charging_state_updated_at": old.isoformat(),
        "charger_power_updated_at": old.isoformat(),
    }

    power_sync.EVVehiclesView._supplement_fleet_with_ble(fleet, ble)

    assert fleet["charging_state"] == "Stopped"
    assert fleet["charger_power"] == 0.0
    assert fleet["ble_connected"] is (not quarantined)


def test_mobile_fresher_ble_charging_still_replaces_older_fleet_stop():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    fleet = {"charging_state": "Stopped", "charger_power": 0.0,
             "data_updated_at": (now - timedelta(minutes=3)).isoformat()}
    ble = {"charging_state": "Charging", "charger_power": 2.0,
           "is_online": True, "data_updated_at": now.isoformat(),
           "charging_state_updated_at": now.isoformat(),
           "charger_power_updated_at": now.isoformat()}

    power_sync.EVVehiclesView._supplement_fleet_with_ble(fleet, ble)

    assert fleet["charging_state"] == "Charging"
    assert fleet["charger_power"] == 2.0


@pytest.mark.parametrize("radio_state", ["off", "unknown", "unavailable", "connecting"])
def test_mobile_known_ble_radio_state_blocks_stale_charge_override(radio_state):
    """A stale generic BLE status cannot make a disabled radio current."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        # This is the stale entity observed on the live bridge.
        _State("binary_sensor.car_status", "on", last_updated=now - timedelta(minutes=2)),
        _State("switch.car_ble_connection", radio_state, last_updated=now),
        _State("sensor.car_charging_state", "Charging", last_updated=now),
        _State(
            "sensor.car_charge_power",
            "2.0",
            {"unit_of_measurement": "kW"},
            last_updated=now,
        ),
    ])

    vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("car")

    assert vehicle["is_online"] is False
    assert vehicle.get("ble_connected") is not True
    assert vehicle["charging_state"] == "Unknown"
    assert vehicle["charger_power"] is None


def test_mobile_disabled_ble_radio_cannot_override_fleet_charge_state():
    """An unusable known radio cannot replace a newer native stopped report."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    fleet = {
        "charging_state": "Stopped",
        "charger_power": 0.0,
        "data_updated_at": now.isoformat(),
        "charging_state_updated_at": now.isoformat(),
    }
    ble = {
        "charging_state": "Charging",
        "charger_power": 2.0,
        "is_online": False,
        "connection_usable": False,
        "data_updated_at": (now + timedelta(seconds=1)).isoformat(),
        "charging_state_updated_at": (now + timedelta(seconds=1)).isoformat(),
        "charger_power_updated_at": (now + timedelta(seconds=1)).isoformat(),
    }

    power_sync.EVVehiclesView._supplement_fleet_with_ble(fleet, ble)

    assert fleet["charging_state"] == "Stopped"
    assert fleet["charger_power"] == 0.0
    assert fleet["ble_connected"] is False


def test_mobile_disabled_ble_radio_preserves_fleet_flap_and_charge_fields():
    """A newer BLE flap-off sample cannot turn native charging into Disconnected."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        # The generic status remains stale/on while the explicit radio is off.
        _State("binary_sensor.car_status", "on", last_updated=now - timedelta(minutes=2)),
        _State("switch.car_ble_connection", "off", last_updated=now),
        _State("sensor.car_charging_state", "Charging", last_updated=now),
        _State("sensor.car_charge_power", "2.0", last_updated=now),
        _State("binary_sensor.car_charge_flap", "off", last_updated=now),
    ])
    ble_vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("car")
    fleet = {
        "charging_state": "Charging",
        "charger_power": 2.0,
        "is_plugged_in": True,
        "data_updated_at": (now - timedelta(minutes=1)).isoformat(),
        "charging_state_updated_at": (now - timedelta(minutes=1)).isoformat(),
        "charger_power_updated_at": (now - timedelta(minutes=1)).isoformat(),
        "is_plugged_in_updated_at": (now - timedelta(minutes=1)).isoformat(),
    }

    power_sync.EVVehiclesView._supplement_fleet_with_ble(fleet, ble_vehicle)

    assert ble_vehicle["connection_usable"] is False
    assert ble_vehicle["charging_state"] == "Unknown"
    assert fleet["charging_state"] == "Charging"
    assert fleet["charger_power"] == 2.0
    assert fleet["is_plugged_in"] is True
    assert fleet["ble_connected"] is False


def test_mobile_disabled_ble_registry_row_blocks_radio_even_when_switch_is_on():
    """A disabled registry row is unusable despite its retained ON state."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    radio = SimpleNamespace(
        entity_id="switch.car_ble_connection",
        disabled_by="user",
    )
    hass = _Hass(
        [
            _State("binary_sensor.car_status", "on", last_updated=now),
            _State("switch.car_ble_connection", "on", last_updated=now),
            _State("sensor.car_charging_state", "Charging", last_updated=now),
            _State("sensor.car_charge_power", "2.0", last_updated=now),
        ],
        registry_entities={radio.entity_id: radio},
    )

    vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("car")

    assert vehicle["is_online"] is False
    assert vehicle["charger_power"] is None


def test_mobile_legacy_ble_without_radio_entity_remains_usable():
    """Older bridges without a connection switch keep their status behavior."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Charging", last_updated=now),
        _State("sensor.car_charge_power", "2.0", last_updated=now),
    ])

    vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("car")

    assert vehicle["is_online"] is True
    assert vehicle["charger_power"] == 2.0


def test_mobile_quarantined_ble_reports_unknown_power_and_keeps_vehicle():
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.car_status", "on", last_updated=now),
        _State("sensor.car_charging_state", "Charging", last_updated=now),
        _State("sensor.car_charge_power", "2.0", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.car_charge_level", "73", last_updated=now),
        _State("binary_sensor.car_charge_flap", "on", last_updated=now),
    ])
    hass.data["power_sync"]["entry-1"]["ev_transport_recovery"] = {
        "vehicles": {"VIN": {"route": {"kind": "ble", "prefix": "car"},
            "attempts": {"attempt": {"outcome": "sent_unknown",
                "route": {"kind": "ble", "prefix": "car"}}}, "hazards": {}}}}

    vehicle = power_sync.EVVehiclesView(hass)._get_tesla_ble_vehicle("car")

    assert vehicle["transport_quarantined"] is True
    assert vehicle["charging_state"] == "Unknown"
    assert vehicle["charger_power"] is None
    assert vehicle["is_online"] is False
    assert vehicle["battery_level"] == 73
    assert vehicle["is_plugged_in"] is True

    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "car",
    })
    status = power_sync._get_ev_vehicles_status(hass, entry, include_control_evidence=True)
    assert len(status) == 1
    assert status[0]["is_connected"] is True
    assert status[0]["is_charging"] is False
    assert status[0]["power_available"] is False
    assert status[0]["_boost_charging_state_evidence"]["known"] is False


@pytest.mark.parametrize("quarantined", [False, True])
def test_aggregate_newer_native_stop_cannot_retain_ble_charging_power(quarantined):
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=3)
    hass = _tesla_hass([
        _State("sensor.primary_ev_charging", "stopped", last_updated=now),
        _State("sensor.primary_ev_charger_power", "0", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.car_charging_state", "Charging", last_updated=old),
        _State("sensor.car_charge_power", "2", {"unit_of_measurement": "kW"}, last_updated=old),
    ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "both", "tesla_ble_entity_prefix": "car",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=car",
    })
    if quarantined:
        hass.data["power_sync"]["entry-1"]["ev_transport_recovery"] = {
            "vehicles": {"VIN": {"route": {"kind": "ble", "prefix": "car"},
                "attempts": {"attempt": {"outcome": "sent_unknown",
                    "route": {"kind": "ble", "prefix": "car"}}}, "hazards": {}}}}

    assert power_sync._get_ev_vehicle_status(hass, entry)["ev_power_kw"] == 0.0


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
        "telemetry_state": "stale",
        "site_presence": "home",
    }]
    assert power_sync._get_external_tesla_ev_power_kw(hass, _Entry()) == 3.4


@pytest.mark.parametrize("vehicle_current", ["0", "39.1"])
@pytest.mark.parametrize("vehicle_name", ["PRIMARY EV", "Tesla EV"])
@pytest.mark.parametrize("fleet_power,meter_power,raw_load", [(9.0, 8.7, 9.3), (5.1, 5.1, 5.7)])
def test_wall_connector_vehicle_current_does_not_duplicate_fleet_load(
    monkeypatch, vehicle_current, vehicle_name, fleet_power, meter_power, raw_load,
):
    """#454: HA's vehicle_current sensor is amperage, not a vehicle identifier."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power", str(fleet_power),
               {"unit_of_measurement": "kW"}, last_updated=now - timedelta(seconds=5)),
        _State("sensor.primary_ev_charging", "charging", last_updated=now),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
    ])
    hass.device_registry.devices["device-1"].name = vehicle_name
    for state in [
        _State("sensor.wall_connector_vehicle_current", vehicle_current,
               {"unit_of_measurement": "A", "device_class": "current"}, last_updated=now),
        _State("sensor.wall_connector_total_power", str(meter_power * 1000),
               {"unit_of_measurement": "W"}, last_updated=now),
    ]:
        hass.states._states[state.entity_id] = state
        hass.entity_registry.entities[state.entity_id] = _entity(state.entity_id, "wall-connector")

    async def no_sigenergy_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_state)
    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())
    observations = asyncio.run(power_sync._get_ev_load_observations(hass, _Entry(), vehicles))
    ev_load = importlib.import_module("power_sync.ev_load")
    snapshot = ev_load.aggregate_ev_load(observations)
    normalized = ev_load.normalize_energy_data(
        {"load_power": 0.0, "raw_home_load_power": raw_load},
        battery_system="sigenergy", ev_load=snapshot,
    )

    assert len(vehicles) == 1
    assert vehicles[0]["vehicle_id"] == "5YJTEST0000000001"
    assert snapshot.power_kw == pytest.approx(meter_power)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert normalized["load_power"] == pytest.approx(0.6)


def test_wall_connector_fresh_current_does_not_refresh_stale_power():
    """A fresh amperage reading cannot renew a stale connector watt sample."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power", "9", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.primary_ev_charging", "charging", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
    ])
    for state in [
        _State("sensor.wall_connector_vehicle_current", "39", {"unit_of_measurement": "A"}, last_updated=now),
        _State("sensor.wall_connector_total_power", "8700", {"unit_of_measurement": "W"},
               last_updated=now - timedelta(minutes=5)),
    ]:
        hass.states._states[state.entity_id] = state
        hass.entity_registry.entities[state.entity_id] = _entity(state.entity_id, "wall-connector")

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert len(vehicles) == 1
    assert vehicles[0]["ev_power_kw"] == 9.0


def test_wall_connector_current_does_not_merge_two_distinct_charging_vehicles():
    """Without an identity match, two active cars plus a meter stay distinct."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    hass = _tesla_hass([
        _State("sensor.primary_ev_charger_power", "9", {"unit_of_measurement": "kW"}, last_updated=now),
        _State("sensor.primary_ev_charging", "charging", last_updated=now),
        _State("device_tracker.primary_ev_location", "home", last_updated=now),
    ])
    hass.device_registry.devices["second-ev"] = SimpleNamespace(
        id="second-ev", name="SECOND EV", identifiers={("teslemetry", "5YJTEST0000000002")},
    )
    for state, device_id in [
        (_State("sensor.second_ev_charger_power", "3", {"unit_of_measurement": "kW"}, last_updated=now), "second-ev"),
        (_State("sensor.second_ev_charging", "charging", last_updated=now), "second-ev"),
        (_State("device_tracker.second_ev_location", "home", last_updated=now), "second-ev"),
        (_State("sensor.wall_connector_vehicle_current", "39", {"unit_of_measurement": "A"}, last_updated=now), "wall-connector"),
        (_State("sensor.wall_connector_total_power", "8700", {"unit_of_measurement": "W"}, last_updated=now), "wall-connector"),
    ]:
        hass.states._states[state.entity_id] = state
        hass.entity_registry.entities[state.entity_id] = _entity(state.entity_id, device_id)

    vehicles = power_sync._get_ev_vehicles_status(hass, _Entry())

    assert {vehicle["vehicle_id"]: vehicle["ev_power_kw"] for vehicle in vehicles} == {
        "5YJTEST0000000001": 9.0, "5YJTEST0000000002": 3.0, "wall_connector_ha": 8.7,
    }


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
        "telemetry_state": "stale",
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


def _sigenergy_load_snapshot(
    monkeypatch, *, charger_type="evac", row_power=6.956,
    native_power=6.956, native_available=True, row_available=True,
    row_age=0, auxiliary_power=0.0, extra_vehicles=(), alias=False,
):
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    planner = importlib.import_module("power_sync.automations.ev_charging_planner")
    now = datetime(2026, 10, 29, 2, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: now)
    # Shape emitted by sigenergy_charger_state_to_loadpoint_observation.
    row = {
        "vehicle_id": "sigenergy_charger", "charger_id": "sigenergy_charger",
        "charger_type": "sigenergy", "ev_power_kw": row_power,
        "is_charging": row_power > 0, "is_discharging": row_power < 0,
    }
    row.update(
        observed_at=now - timedelta(seconds=row_age),
        power_available=row_available, auxiliary_power_kw=auxiliary_power,
    )
    native_state = SimpleNamespace(
        charger_type=charger_type, power_kw=native_power, is_connected=True,
        is_charging=native_power > 0, is_discharging=native_power < 0,
    )

    async def read_state(*_args, **_kwargs):
        return native_state if native_available else None

    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", read_state)
    alias_entity = "sensor.sigenergy_charger_power"
    settings = {"sigenergy_charger": SimpleNamespace(
        vehicle_id="sigenergy_charger", charger_type="sigenergy",
        charger_power_entity=alias_entity,
    )} if alias else {}
    monkeypatch.setattr(
        planner, "get_auto_schedule_executor",
        lambda: SimpleNamespace(_settings=settings),
    )
    hass = _Hass([_State(alias_entity, str(row_power), {"unit_of_measurement": "kW"})])
    entry = _Entry()
    const = importlib.import_module("power_sync.const")
    entry.options = {const.CONF_SIGENERGY_CHARGER_TYPE: charger_type}
    observations = asyncio.run(power_sync._get_ev_load_observations(
        hass, entry, [row, *extra_vehicles],
    ))
    return ev_load, ev_load.aggregate_ev_load(observations, at=now), now


def test_sigenergy_evac_home_load_counts_loadpoint_and_native_meter_once(monkeypatch):
    """#135: 8.26 kW site load minus 6.956 kW EV must leave 1.304 kW home."""
    ev_load, snapshot, now = _sigenergy_load_snapshot(monkeypatch, alias=True)
    assert snapshot.power_kw == pytest.approx(6.956)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert [item.physical_load_key for item in snapshot.components] == ["sigenergy:evac"]
    raw = {"solar_power": 8.24, "grid_power": 0.02, "battery_power": 0, "load_power": 0}
    normalized = ev_load.normalize_energy_data(
        raw, battery_system="sigenergy", ev_load=snapshot, at=now,
    )
    assert normalized["raw_home_load_power"] == pytest.approx(8.26)
    assert normalized["observed_ev_power"] == pytest.approx(6.956)
    assert normalized["load_power"] == pytest.approx(1.304)
    assert ev_load.normalize_energy_data(
        normalized, battery_system="sigenergy", ev_load=snapshot, at=now,
    )["load_power"] == pytest.approx(1.304)


@pytest.mark.parametrize("charger_type,native_power", [("evac", 0.0), ("evdc", 4.0), ("evdc", -3.0)])
def test_sigenergy_native_readback_supersedes_duplicate_loadpoint(
    monkeypatch, charger_type, native_power,
):
    ev_load, snapshot, _ = _sigenergy_load_snapshot(
        monkeypatch, charger_type=charger_type, native_power=native_power,
    )
    assert snapshot.power_kw == native_power
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert len(snapshot.components) == 1
    assert snapshot.components[0].source_key == f"sigenergy_{charger_type}"


@pytest.mark.parametrize("charger_type,row_power,auxiliary", [("evac", 6.956, 0.1), ("evdc", -3.0, 0.0)])
def test_sigenergy_loadpoint_remains_fallback_when_native_read_unavailable(
    monkeypatch, charger_type, row_power, auxiliary,
):
    ev_load, snapshot, _ = _sigenergy_load_snapshot(
        monkeypatch, charger_type=charger_type, row_power=row_power,
        native_available=False, auxiliary_power=auxiliary,
    )
    assert snapshot.power_kw == pytest.approx(row_power + auxiliary)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert snapshot.components[0].physical_load_key == f"sigenergy:{charger_type}"


@pytest.mark.parametrize("row_available,row_age", [(False, 0), (True, 100)])
def test_sigenergy_missing_or_stale_active_power_withholds_home_load(
    monkeypatch, row_available, row_age,
):
    ev_load, snapshot, now = _sigenergy_load_snapshot(
        monkeypatch, native_available=False, row_available=row_available, row_age=row_age,
    )
    assert snapshot.quality == ev_load.EvLoadQuality.INCOMPLETE
    assert snapshot.unavailable_active_keys == ("sigenergy:evac",)
    assert ev_load.normalize_home_load(
        8.26, ev_load.HomeLoadBasis.INCLUDES_EV, snapshot, at=now,
    ).non_ev_home_load_kw is None


def test_sigenergy_dedup_preserves_distinct_vehicle_and_generic_auxiliary_load(monkeypatch):
    ev_load, snapshot, _ = _sigenergy_load_snapshot(monkeypatch, alias=True, extra_vehicles=[
        {"vehicle_id": "5YJTEST0000000001", "ev_power_kw": 2.0, "is_charging": True},
        {"vehicle_id": "generic_ev", "ev_power_kw": 0, "auxiliary_power_kw": 0.2},
    ])
    assert snapshot.power_kw == pytest.approx(9.156)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert len(snapshot.components) == 3


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


def test_display_snapshot_keeps_unknown_ev_power_out_of_surplus_arithmetic(
    monkeypatch,
):
    """An active unknown loadpoint must not make the canonical EV API fail."""
    power_sync = _power_sync_module()
    ev_load = importlib.import_module("power_sync.ev_load")
    observed_at = datetime(2026, 9, 9, 2, 55, tzinfo=timezone.utc)

    async def get_ev_load_observations(hass, entry, vehicles):
        return [
            ev_load.EvLoadObservation(
                "vehicle:unknown",
                "fleet_unknown",
                None,
                observed_at,
                True,
                ev_load.EvMeasurementKind.VEHICLE,
            ),
            ev_load.EvLoadObservation(
                "vehicle:known",
                "wall_connector",
                2.0,
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
                body=(
                    b'{"success": true, "site": {"ev_power_kw": null}, '
                    b'"loadpoints": ['
                    b'{"actual_charging": true, "current_power_kw": null}, '
                    b'{"actual_charging": true, "current_power_kw": 2.0}]}'
                ),
            )

    monkeypatch.setattr(
        power_sync,
        "_get_ev_load_observations",
        get_ev_load_observations,
    )
    monkeypatch.setattr(power_sync, "EVLoadpointStatusView", LoadpointStatusView)
    hass = _Hass(
        [],
        entry_data={
            "tesla_coordinator": SimpleNamespace(
                data={
                    "solar_power": 4.9,
                    "grid_power": -0.1,
                    "battery_power": 0.0,
                    "load_power": None,
                    "battery_level": 50,
                }
            )
        },
    )

    snapshot = asyncio.run(
        power_sync._get_ev_display_coordinator(hass, _Entry()).async_refresh(force=True)
    )

    assert snapshot["site"]["ev_power_kw"] is None
    assert snapshot["site"]["observation_quality"] == "incomplete"
    assert snapshot["site"]["surplus_kw"] == 1.6


def test_wall_connector_same_vehicle_edges_follow_source_timestamp(monkeypatch):
    """Ticket #204: source time decides same-VIN stop/restart ordering."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )

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


def test_ev_vehicle_status_rejects_older_same_vin_wall_connector_power(monkeypatch):
    """Ticket #204: stale site telemetry cannot revive stopped EV power."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )
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


def test_ev_vehicle_status_home_metadata_does_not_retimestamp_power_edges(monkeypatch):
    """Ticket #204: unrelated home/cable updates cannot hide direct edges."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )

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


def test_multiple_vinless_wall_connectors_keep_distinct_physical_loads(monkeypatch):
    """Distinct unidentified Wall Connectors must not overwrite one Tesla."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )
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


def test_away_vin_match_keeps_home_wall_connector_as_separate_loadpoint(monkeypatch):
    """A named-zone vehicle cannot be revived by conflicting site telemetry."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
    )
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


def test_duplicate_same_vin_away_tracker_fences_exact_wall_connector(monkeypatch):
    """Ticket #204: an unlocated duplicate cannot bypass the away VIN fence."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
    )

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


def test_away_vehicle_fences_zero_power_charging_connector_display_contract(monkeypatch):
    """Ticket #204: a stale state-2 connector cannot render an away EV onsite."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    ev_display = importlib.import_module("power_sync.ev_display")
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
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


def test_vinless_zero_power_connector_cannot_revive_only_away_vehicle(monkeypatch):
    """Ticket #204: a DIN-only idle connector cannot invent onsite presence."""
    power_sync = _power_sync_module()
    loadpoint_status = importlib.import_module(
        "power_sync.automations.loadpoint_status"
    )
    ev_display = importlib.import_module("power_sync.ev_display")
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )

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


def test_vinless_positive_connector_remains_distinct_from_away_vehicle(monkeypatch):
    """A measured unidentified connector remains a separate physical load."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )
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


def test_vinless_connector_accepts_newer_home_presence_for_same_vehicle(monkeypatch):
    """A newer home transition must reopen the DIN-only attribution path."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 5, 47, 19, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=90),
    )
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
                # State 2 at 0 W is Tesla's idle standby; attribution
                # needs measured connector power to prove charging.
                "wall_connectors_raw": [{
                    "wall_connector_state": 2,
                    "wall_connector_power": 7000,
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


def test_duplicate_same_vin_newer_home_tracker_allows_exact_wall_connector(monkeypatch):
    """A newer home observation restores exact-VIN attribution after travel."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=90),
    )

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


def test_duplicate_same_vin_untimestamped_away_presence_fails_closed(monkeypatch):
    """An unknown-age away observation needs a comparable home transition."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
    )
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


def test_legacy_ev_status_applies_away_tracker_to_duplicate_physical_vin(monkeypatch):
    """The aggregate fallback cannot count an unlocated duplicate as home."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
    )

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


def test_legacy_ev_status_accepts_newer_home_for_duplicate_physical_vin(monkeypatch):
    """A newer home tracker re-enables physical-VIN aggregate telemetry."""
    power_sync = _power_sync_module()
    vehicle_id = "5YJTEST0000000001"
    current = datetime(2026, 8, 17, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=90),
    )

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


def test_unmatched_explicit_vin_never_falls_through_to_another_vehicle(monkeypatch):
    """A connector's VIN cannot be heuristically assigned to a different car."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=30),
    )
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


def test_exact_vin_zero_state_clears_stale_power_and_connection(monkeypatch):
    """An explicit VIN-scoped state-0 sample is still authoritative telemetry."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )
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


def test_unmatched_disconnected_wall_connector_stays_disconnected(monkeypatch):
    """An idle connector row must retain its real disconnected state."""
    power_sync = _power_sync_module()
    current = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        importlib.import_module("power_sync.ev_load"), "utc_now",
        lambda: current + timedelta(seconds=0),
    )
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


def test_manual_start_uses_healthy_suffixed_provider_for_same_vehicle(monkeypatch):
    """TESSY: unknown Fleet entities must not mask Teslemetry's plugged car."""
    power_sync = _power_sync_module()
    # The lazily imported command helper may already hold another file's
    # registry stubs. Bind it to this probe's registries for this test only.
    actions = importlib.import_module("power_sync.automations.actions")
    monkeypatch.setattr(actions, "dr", power_sync.dr)
    monkeypatch.setattr(actions, "er", power_sync.er)
    vin, other_vin = "5YJTEST0000000001", "5YJTEST0000000002"
    devices = {
        name: SimpleNamespace(id=name, name=name, identifiers={(provider, identity)})
        for name, provider, identity in [
            ("fleet", "tesla_fleet", vin),
            ("teslemetry", "teslemetry", vin),
            ("other", "teslemetry", other_vin),
        ]
    }
    states, registry = [], {}
    for device, suffix, cable, charging, location in [
        ("fleet", "", "unknown", "unknown", "unknown"),
        ("teslemetry", "_2", "on", "stopped", "home"),
        ("other", "_3", "off", "disconnected", "not_home"),
    ]:
        for stem, value in [
            ("binary_sensor.car_charge_cable", cable),
            ("sensor.car_charging", charging),
            ("device_tracker.car_location", location),
        ]:
            entity_id = stem + suffix
            states.append(_State(entity_id, value))
            registry[entity_id] = SimpleNamespace(entity_id=entity_id, device_id=device)
    hass = _Hass(states, registry, devices)
    view = power_sync.EVVehicleCommandView(hass)
    view._get_powersync_config = lambda: {}
    assert asyncio.run(view._is_vehicle_plugged_in(vin)) is True
    assert asyncio.run(view._is_vehicle_at_home(vin)) is True
    assert asyncio.run(view._is_vehicle_plugged_in(other_vin)) is False
    assert asyncio.run(view._is_vehicle_at_home(other_vin)) is False


def test_manual_start_cannot_borrow_other_vehicles_ble_plug_cache():
    power_sync = _power_sync_module()
    vin, other_vin = "5YJTEST0000000001", "5YJTEST0000000002"
    hass = _Hass([])
    hass.data["power_sync"]["_ev_cache"] = {
        "ev_ble_plug_cache_other_bridge": {
            "is_plugged_in": True,
            "cached_at": datetime.now(timezone.utc),
        }
    }
    view = power_sync.EVVehicleCommandView(hass)
    view._get_powersync_config = lambda: {
        "ev_provider": "both",
        "tesla_ble_entity_prefix": "target_bridge,other_bridge",
        "tesla_ble_vehicle_mapping": f"{vin}=target_bridge,{other_vin}=other_bridge",
    }
    assert asyncio.run(view._is_vehicle_plugged_in(vin)) is False

    # The exact mapped bridge can supply fresh positive evidence, but neither
    # a stale entry nor a future timestamp may authorize a start.
    for age, expected in [(60, True), (7201, False), (-60, False)]:
        hass.data["power_sync"]["_ev_cache"]["ev_ble_plug_cache_target_bridge"] = {
            "is_plugged_in": True,
            "cached_at": datetime.now(timezone.utc) - timedelta(seconds=age),
        }
        assert asyncio.run(view._is_vehicle_plugged_in(vin)) is expected


def test_manual_start_accepts_current_exact_vin_wall_connector_status():
    """Ticket-57: manual readiness matches the connected loadpoint card."""
    power_sync = _power_sync_module()
    vin = "5YJTEST0000000001"
    observed_at = datetime.now(timezone.utc)
    charger = _State(
        "binary_sensor.primary_ev_charger",
        "off",
        last_updated=observed_at - timedelta(minutes=5),
    )
    states = [charger]
    hass = _Hass(
        states,
        {charger.entity_id: _entity(charger.entity_id, "fleet")},
        {
            "fleet": SimpleNamespace(
                id="fleet",
                name="Primary EV",
                identifiers={("tesla_fleet", vin)},
            ),
        },
        entry_data={
            "tesla_coordinator": SimpleNamespace(
                data={
                    "wall_connectors_raw": [{
                        "wall_connector_state": 11,
                        "wall_connector_power": 0,
                        "vin": vin,
                    }],
                    "last_update": observed_at,
                }
            )
        },
        config_entries=[_Entry()],
    )

    status = power_sync._get_ev_vehicles_status(hass, _Entry())
    assert status[0]["is_connected"] is True

    view = power_sync.EVVehicleCommandView(hass)
    view._get_powersync_config = lambda: {}
    assert asyncio.run(view._is_vehicle_plugged_in(vin)) is True


def test_ble_raw_receipts_keep_two_steady_vehicles_fresh_independently():
    """HA suppresses equal numeric reports; raw receipt values remain authoritative."""
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=5)
    states = []
    for prefix, age in (("first", 5), ("second", 120)):
        states.extend([
            _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
            _State(f"sensor.{prefix}_charging_state", "Charging", last_updated=now),
            _State(f"sensor.{prefix}_charge_power", "7.0", {"unit_of_measurement": "kW"},
                   last_updated=old, last_reported=now),
            _State(f"sensor.{prefix}_charge_power_received", (now - timedelta(seconds=age)).isoformat(),
                   last_updated=now, last_reported=now),
        ])
    hass = _Hass(states)
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "tesla_ble", "tesla_ble_entity_prefix": "first,second",
    })
    vehicles = {v["vehicle_id"]:v for v in power_sync._get_ev_vehicles_status(hass, entry)}
    assert vehicles["ble_first"]["power_available"] is True
    assert vehicles["ble_first"]["ev_power_kw"] == 7.0
    assert vehicles["ble_first"]["_observed_at"] == now - timedelta(seconds=5)
    assert vehicles["ble_second"]["power_available"] is False
    # Replaying a cached receipt into HA must not renew the second vehicle.
    assert vehicles["ble_second"]["_observed_at"] == now - timedelta(seconds=120)
    # A new raw equal sample renews only the second bridge.
    hass.states._states["sensor.second_charge_power_received"].state = now.isoformat()
    vehicles = {v["vehicle_id"]:v for v in power_sync._get_ev_vehicles_status(hass, entry)}
    assert vehicles["ble_first"]["_observed_at"] == now - timedelta(seconds=5)
    assert vehicles["ble_second"]["power_available"] is True
    assert vehicles["ble_second"]["ev_power_kw"] == 7.0


def test_ble_planner_requires_matching_current_raw_power_receipt():
    _power_sync_module()
    planner = importlib.import_module("power_sync.automations.ev_charging_planner")
    now = datetime.now(timezone.utc)
    for power_value, receipt_value, expected in [
        ("7.0", (now - timedelta(seconds=5)).isoformat(), 7.0),
        ("7.0", (now - timedelta(seconds=120)).isoformat(), 0.0),
        ("7.0", (now + timedelta(seconds=120)).isoformat(), 0.0),
        ("7.0", "unavailable", 0.0),
        ("unknown", now.isoformat(), 0.0),
    ]:
        hass = _Hass([
            _State("sensor.car_charge_power", power_value, {"unit_of_measurement": "kW"},
                   last_updated=now - timedelta(minutes=5), last_reported=now),
            _State("sensor.car_charge_power_received", receipt_value, last_updated=now),
        ])
        assert planner._tesla_ble_charge_power_kw(hass, "car") == expected


@pytest.mark.parametrize("provider", ["fleet_api", "both", "tesla_ble"])
@pytest.mark.parametrize("reverse_discovery", [False, True])
def test_two_mapped_vehicles_keep_one_loadpoint_and_power_source_per_car_for_each_provider(
    monkeypatch, provider, reverse_discovery,
):
    """Changing command provider retains explicit read-only physical identities."""
    power_sync = _power_sync_module()
    status_module = importlib.import_module("power_sync.automations.loadpoint_status")
    ev_load = importlib.import_module("power_sync.ev_load")
    mapping = importlib.import_module("power_sync.tesla_ble_mapping")
    now = datetime.now(timezone.utc)
    fleet_at = now - timedelta(seconds=5)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: now)

    async def no_sigenergy_charger(*_args):
        return None

    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger)
    vins = ("5YJTEST0000000001", "5YJTEST0000000002")
    prefixes = ("first_bridge", "second_bridge")
    states, entities, devices = [], {}, {}
    cars = list(zip(vins, prefixes, (4.0, 5.0)))
    if reverse_discovery:
        cars.reverse()
    for vin, prefix, power in cars:
        device_id = f"device-{vin[-1]}"
        fleet_states = [
            _State(f"sensor.car_{vin[-1]}_charging_state", "Charging", last_updated=fleet_at),
            _State(f"binary_sensor.car_{vin[-1]}_charge_cable", "on", last_updated=fleet_at),
            _State(f"sensor.car_{vin[-1]}_charger_power", str(power - 2),
                   {"unit_of_measurement": "kW"}, last_updated=fleet_at),
        ]
        states.extend(fleet_states)
        entities.update({state.entity_id: _entity(state.entity_id, device_id) for state in fleet_states})
        devices[device_id] = SimpleNamespace(id=device_id, name=f"Car {vin[-1]}",
                                             identifiers={("tesla_fleet", vin)})
        states.extend([
            _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
            _State(f"binary_sensor.{prefix}_charge_flap", "on", last_updated=now),
            _State(f"sensor.{prefix}_charging_state", "Charging", last_updated=now),
            _State(f"sensor.{prefix}_charge_power", str(power),
                   {"unit_of_measurement": "kW"}, last_updated=now),
        ])
    config = {"ev_provider": "both", "tesla_ble_entity_prefix": ",".join(prefixes),
              "tesla_ble_vehicle_mapping": ",".join(f"{vin}={prefix}" for vin, prefix in zip(vins, prefixes))}
    entry = SimpleNamespace(entry_id="entry-1", data=config, options={"ev_provider": provider})
    hass = _Hass(states, entities, devices)
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    expected = ({f"ble_{prefix}": power for _vin, prefix, power in cars}
                if provider == "tesla_ble" else {vin: power for vin, _prefix, power in cars})
    assert len(vehicles) == 2
    assert {vehicle["vehicle_id"]: vehicle["ev_power_kw"] for vehicle in vehicles} == expected
    rows = status_module.build_loadpoint_status({}, vehicles)
    assert len(rows) == 2 and {row["vehicle_id"] for row in rows} == set(expected)
    observations = asyncio.run(power_sync._get_ev_load_observations(hass, entry, vehicles))
    snapshot = ev_load.aggregate_ev_load(observations, at=now)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert snapshot.power_kw == 9.0
    # Telemetry coalescing must never switch the command provider, or make a
    # BLE alias command-addressable under Fleet-only control.
    assert entry.data["ev_provider"] == "both" and entry.options["ev_provider"] == provider
    effective = {**entry.data, **entry.options}
    expected_command_id = vins[0] if provider == "both" else f"ble_{prefixes[0]}"
    assert mapping.canonical_tesla_vehicle_id(effective, f"ble_{prefixes[0]}", vins, prefixes) == expected_command_id


def test_fleet_only_mapped_boosts_keep_fresh_fleet_power_over_old_or_unavailable_ble(monkeypatch):
    power_sync = _power_sync_module()
    status_module = importlib.import_module("power_sync.automations.loadpoint_status")
    ev_load = importlib.import_module("power_sync.ev_load")
    now = datetime.now(timezone.utc)
    older = now - timedelta(seconds=15)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: now)

    async def no_sigenergy_charger(*_args):
        return None

    monkeypatch.setattr(power_sync, "_read_sigenergy_charger_state_for_entry", no_sigenergy_charger)
    vins = ("5YJTEST0000000001", "5YJTEST0000000002")
    prefixes = ("first_bridge", "second_bridge")
    states, entities, devices = [], {}, {}
    for vin, prefix, power, ble_power, ble_state in zip(
        vins, prefixes, (1.18, 1.15), ("1.0", "unknown"), ("Charging", "Stopped")
    ):
        device_id = f"device-{vin[-1]}"
        fleet_states = [
            _State(f"sensor.car_{vin[-1]}_charging_state", "Charging", last_updated=now),
            _State(f"binary_sensor.car_{vin[-1]}_charge_cable", "on", last_updated=now),
            _State(f"sensor.car_{vin[-1]}_charger_power", str(power),
                   {"unit_of_measurement": "kW"}, last_updated=now),
        ]
        states.extend(fleet_states)
        entities.update({state.entity_id: _entity(state.entity_id, device_id) for state in fleet_states})
        devices[device_id] = SimpleNamespace(id=device_id, name=f"Car {vin[-1]}",
                                             identifiers={("tesla_fleet", vin)})
        states.extend([
            _State(f"binary_sensor.{prefix}_status", "on", last_updated=now),
            _State(f"binary_sensor.{prefix}_charge_flap", "on", last_updated=older),
            _State(f"sensor.{prefix}_charging_state", ble_state, last_updated=older),
            _State(f"sensor.{prefix}_charge_power", ble_power,
                   {"unit_of_measurement": "kW"}, last_updated=older if ble_power != "unknown" else now),
        ])
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": "fleet_api", "tesla_ble_entity_prefix": ",".join(prefixes),
        "tesla_ble_vehicle_mapping": ",".join(f"{vin}={prefix}" for vin, prefix in zip(vins, prefixes)),
    })
    hass = _Hass(states, entities, devices)
    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    assert len(vehicles) == 2
    assert {vehicle["vehicle_id"]: vehicle["ev_power_kw"] for vehicle in vehicles} == dict(zip(vins, (1.18, 1.15)))
    runtime = {vin: {"active": True, "charging_started": True, "current_amps": 5,
                     "target_amps": 5, "params": {"charger_type": "tesla", "owner_mode": "boost",
                                                   "dynamic_mode": "battery_target", "voltage": 230}}
               for vin in vins}
    rows = status_module.build_loadpoint_status(runtime, vehicles)
    assert len(rows) == 2 and {row["vehicle_id"] for row in rows} == set(vins)
    assert all(row["actual_charging"] is True and row["status"] == "charging" for row in rows)
    assert {row["vehicle_id"]: row["current_power_kw"] for row in rows} == dict(zip(vins, (1.18, 1.15)))
    observations = asyncio.run(power_sync._get_ev_load_observations(hass, entry, vehicles))
    snapshot = ev_load.aggregate_ev_load(observations, at=now)
    assert snapshot.quality == ev_load.EvLoadQuality.COMPLETE
    assert snapshot.power_kw == pytest.approx(2.33)


@pytest.mark.parametrize("standby_state", [2, 1, 0])
def test_idle_wall_connector_standby_cannot_revive_stale_fleet_charging(standby_state):
    """W3RT1E recurrence: an unplugged Wall Connector is not a charging car.

    Tesla's ``live_status`` reports an idle connector with no vehicle as
    ``wall_connector_state: 2`` and 0 W (HA's own Tesla Fleet/Teslemetry
    fixtures). That standby record must not re-stamp a Fleet ``Charging``
    reading restored at the last HA restart as live charging over the newer
    BLE ``Stopped`` 0 kW sample.
    """
    power_sync = _power_sync_module()
    now = datetime.now(timezone.utc)
    restored = now - timedelta(hours=6, minutes=30)
    ble_seen = now - timedelta(hours=1)
    states = [
        _State("binary_sensor.primary_ev_status", "on", last_updated=restored),
        _State("binary_sensor.primary_ev_charge_cable", "on", last_updated=restored),
        _State("device_tracker.primary_ev_location", "home", last_updated=restored),
        _State("sensor.primary_ev_charging", "charging", last_updated=restored),
        _State("sensor.primary_ev_charger_power", "2",
               {"unit_of_measurement": "kW"}, last_updated=restored),
        _State("sensor.primary_ev_battery_level", "62", last_updated=restored),
        _State("binary_sensor.second_car_status", "on", last_updated=ble_seen),
        _State("sensor.second_car_charging_state", "Stopped",
               last_updated=ble_seen, last_reported=ble_seen),
        _State("sensor.second_car_charge_power", "0.0",
               {"unit_of_measurement": "kW"},
               last_updated=ble_seen, last_reported=ble_seen),
        _State("sensor.second_car_charge_level", "68",
               last_updated=ble_seen, last_reported=ble_seen),
    ]
    hass = _tesla_hass(states)
    hass.entity_registry.entities = {
        key: value for key, value in hass.entity_registry.entities.items()
        if "primary_ev" in key
    }
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(data={
        "last_update": now - timedelta(seconds=5),
        "wall_connectors_raw": [{
            "din": "1529455-02-E--PGT00000000000",
            "wall_connector_state": standby_state,
            "wall_connector_fault_state": 2,
            "wall_connector_power": 0,
        }],
    })
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={
        "ev_provider": power_sync.EV_PROVIDER_BOTH,
        "tesla_ble_entity_prefix": "second_car",
        "tesla_ble_vehicle_mapping": "5YJTEST0000000001=second_car",
    })

    vehicles = power_sync._get_ev_vehicles_status(hass, entry)
    vehicle = next(
        item for item in vehicles if item.get("vehicle_id") == "5YJTEST0000000001"
    )
    assert vehicle["is_charging"] is False
    # The restored 2 kW is neither current nor measured: it may only survive
    # as an unavailable reading, never as live charging power.
    assert vehicle["ev_power_kw"] == 0.0 or vehicle["power_available"] is False
    assert vehicle.get("telemetry_state") != "live"
    assert not any(
        item.get("is_charging") or item.get("is_connected")
        for item in vehicles
        if str(item.get("charger_id") or "").startswith("wall_connector_")
    )

    from power_sync.automations.loadpoint_status import build_loadpoint_status
    rows = build_loadpoint_status({}, vehicles)
    assert not any(row["actual_charging"] for row in rows)
    assert not any(row["status"] == "charging" for row in rows)


def test_quarantined_ble_bridge_logs_hold_once_and_release(monkeypatch, caplog):
    """Ticket #158: a held bridge silently reported charging=Unknown/online=False."""
    import logging

    power_sync = _power_sync_module()
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    held = {"value": True}
    monkeypatch.setattr(recovery, "ble_quarantined", lambda *_args: held["value"])
    now = datetime.now(timezone.utc)
    hass = _Hass([
        _State("binary_sensor.home_ble_status", "on", last_updated=now),
        _State("sensor.home_ble_charging_state", "Stopped", last_updated=now),
        _State("binary_sensor.home_ble_charge_flap", "on", last_updated=now),
    ])
    view = power_sync.EVVehiclesView(hass)
    with caplog.at_level(logging.DEBUG):
        first = view._get_tesla_ble_vehicle("home_ble")
        view._get_tesla_ble_vehicle("home_ble")
    held_logs = [r for r in caplog.records if "held by retained Tesla command recovery" in r.getMessage()]
    assert len(held_logs) == 1 and held_logs[0].levelno == logging.WARNING
    assert "home_ble" in held_logs[0].getMessage()
    assert first["is_online"] is False and first["is_plugged_in"] is True

    caplog.clear()
    held["value"] = False
    with caplog.at_level(logging.DEBUG):
        view._get_tesla_ble_vehicle("home_ble")
        view._get_tesla_ble_vehicle("home_ble")
    released = [r for r in caplog.records if "recovery hold released" in r.getMessage()]
    assert len(released) == 1
