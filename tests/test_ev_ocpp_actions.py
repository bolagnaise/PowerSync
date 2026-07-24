"""Tests for OCPP EV action fallbacks and ownership guards."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_ha_stubs() -> None:
    ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha_config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    ha_exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    ha_storage = sys.modules.setdefault(
        "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
    )
    ha_update = sys.modules.setdefault(
        "homeassistant.helpers.update_coordinator",
        types.ModuleType("homeassistant.helpers.update_coordinator"),
    )
    ha_er = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    ha_dr = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    ha_event = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
    ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_er.async_get = lambda hass: hass.entity_registry
    ha_dr.async_get = lambda hass: SimpleNamespace(devices={})
    ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
    ha_update.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "__init__": lambda self, *args, **kwargs: None,
        },
    )
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = getattr(ha_dt, "now", lambda *args, **kwargs: None)
    ha_dt.utcnow = getattr(ha_dt, "utcnow", lambda *args, **kwargs: None)

    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util


_install_ha_stubs()

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "CONF_EV_PROVIDER"):
    sys.modules.pop("power_sync.const", None)
sys.modules.pop("power_sync.automations.actions", None)
actions = importlib.import_module("power_sync.automations.actions")


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

    def async_entity_ids(self, domain: str | None = None):
        if domain is None:
            return list(self._states)
        return [entity_id for entity_id in self._states if entity_id.startswith(f"{domain}.")]

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        return [
            state for entity_id, state in self._states.items()
            if entity_id.startswith(f"{domain}.")
        ]


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(self, states: list[_State], registry_entities: dict[str, object] | None = None) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.states = _States(states)
        self.services = _Services()
        self.entity_registry = SimpleNamespace(entities=registry_entities or {})


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


def _tesla_entry():
    return SimpleNamespace(
        entry_id="entry-1",
        data={"battery_system": "tesla"},
        options={},
    )


def test_tesla_preserve_charge_holds_current_soc_with_backup_reserve():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 63.4}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 63, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_caps_unsupported_mid_80s_soc_to_80_percent():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 91}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 80, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_uses_100_percent_when_already_full():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 99}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 100, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_fails_without_home_battery_soc():
    hass = _Hass([])

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is False
    assert hass.services.calls == []


def test_tesla_grid_export_supports_legacy_entry_without_battery_system():
    """Pre-multi-brand Tesla entries implicitly use the Tesla battery system."""
    hass = _Hass([])
    legacy_entry = SimpleNamespace(entry_id="entry-1", data={}, options={})

    result = asyncio.run(
        actions._action_set_grid_export(
            hass,
            legacy_entry,
            {"rule": "pv_only"},
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_grid_export",
            {"rule": "pv_only", "source": "automation"},
        )
    ]


def test_tesla_grid_export_respects_explicit_non_tesla_option():
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"battery_system": "sungrow"},
    )

    result = asyncio.run(
        actions._action_set_grid_export(
            hass,
            entry,
            {"rule": "pv_only"},
        )
    )

    assert result is None
    assert hass.services.calls == []


def test_tesla_stop_accepts_numbered_teslemetry_charge_switch(monkeypatch):
    vin = "LRWYHCEKXTC687964"
    device = SimpleNamespace(
        id="device-yf88",
        name="",
        identifiers={("teslemetry", vin)},
    )
    hass = _Hass(
        [_State("switch.charge_2", "on")],
        registry_entities={
            "switch.charge_2": SimpleNamespace(
                entity_id="switch.charge_2",
                device_id="device-yf88",
            ),
            "binary_sensor.charge_cable": SimpleNamespace(
                entity_id="binary_sensor.charge_cable",
                device_id="device-yf88",
            ),
        },
    )

    monkeypatch.setattr(
        actions.dr,
        "async_get",
        lambda hass: SimpleNamespace(devices={"device-yf88": device}),
    )

    async def wake_success(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_wake_tesla_ev", wake_success)

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _tesla_entry(),
            {"charger_type": "tesla", "vehicle_vin": vin},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charge_2"})
    ]


class _ZaptecClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_installation_current(self, installation_id: str, amps: int):
        self.calls.append(("set_installation_current", installation_id, amps))

    async def resume_charging(self, charger_id: str):
        self.calls.append(("resume_charging", charger_id))

    async def stop_charging(self, charger_id: str):
        self.calls.append(("stop_charging", charger_id))


class _SessionManager:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def update_session(self, **kwargs):
        self.updates.append(kwargs)


class _OcppCentralSystem:
    def __init__(self, accepted: bool, state_accepted: bool = True) -> None:
        self.accepted = accepted
        self.state_accepted = state_accepted
        self.calls: list[tuple[str, float, int]] = []
        self.state_calls: list[tuple[str, str, bool, int]] = []

    async def set_max_charge_rate_amps(self, charger_id: str, amps: float, connector_id: int = 0):
        self.calls.append((charger_id, amps, connector_id))
        return self.accepted

    async def set_charger_state(
        self,
        charger_id: str,
        service_name: str,
        state: bool = True,
        connector_id: int = 1,
    ):
        self.state_calls.append((charger_id, service_name, state, connector_id))
        return self.state_accepted


def _zaptec_entry(installation_id: str = ""):
    return SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
            "zaptec_installation_id_cloud": installation_id,
        },
    )


def _zaptec_hass(cached_state: dict):
    client = _ZaptecClient()
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"].update({
        "zaptec_client": client,
        "zaptec_cached_state": cached_state,
    })
    return hass, client


def _install_solar_surplus_runtime_stubs(
    monkeypatch,
    live_status: dict,
    ev_soc: float | None = None,
):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def get_ev_location(*args, **kwargs):
        return "home"

    async def get_ev_battery_level(*args, **kwargs):
        return ev_soc

    ev_planner.get_ev_location = get_ev_location
    ev_planner.get_ev_battery_level = get_ev_battery_level
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_session", ev_session)

    async def fake_live_status(*args, **kwargs):
        return live_status

    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_observed_ev_power_kw(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_get_observed_ev_power_kw", fake_observed_ev_power_kw)
    return set_amps_calls


def _solar_surplus_state(current_amps: int = 8) -> dict:
    return {
        "active": True,
        "current_amps": current_amps,
        "target_amps": current_amps,
        "charging_started": True,
        "entity_max_rechecked": True,
        "params": {
            "dynamic_mode": "solar_surplus",
            "charger_type": "tesla",
            "min_charge_amps": 1,
            "max_charge_amps": 32,
            "voltage": 240,
            "phases": 1,
            "household_buffer_kw": 0.5,
            "surplus_calculation": "grid_based",
            "sustained_surplus_minutes": 3,
            "stop_delay_minutes": 5,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        },
    }


def test_solar_surplus_parallel_reserve_blocks_sigenergy_battery_charge_surplus():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": 30,
            "battery_power": -4530,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=1.68,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_parallel_reserve_allows_excess_above_battery_rate():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": -3000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 2.0


def test_active_solar_surplus_refreshes_threshold_with_hysteresis(monkeypatch):
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = types.SimpleNamespace(
        _data={
            "solar_surplus_config": {
                "enabled": True,
                "home_battery_minimum": 90,
                "allow_parallel_charging": False,
                "household_buffer_kw": 0.5,
            }
        }
    )
    vehicle_id = "generic_ev"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=10)
    state["params"].update(
        {
            "charger_type": "generic",
            "min_battery_soc": 80,
            "pause_below_soc": 70,
            "notify_on_error": False,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}

    live_status = {
        "battery_soc": 81,
        "grid_power": -2500,
        "battery_power": -500,
        "solar_power": 5000,
        "load_power": 2000,
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(monkeypatch, live_status)

    async def not_unplugged(*args, **kwargs):
        return False

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["params"]["min_battery_soc"] == 90
    assert state["params"]["pause_below_soc"] == 80
    assert state.get("paused") is not True
    assert 0 not in set_amps_calls

    live_status["battery_soc"] = 79
    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert state["paused"] is True
    assert set_amps_calls[-1] == 0


def test_solar_surplus_curtailed_full_battery_keeps_active_ev_headroom():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
        current_ev_power_kw=3.31,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 5.61


def test_solar_surplus_curtailed_full_battery_probes_idle_ev_start():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 0,
            "solar_power": 1200,
            "load_power": 1200,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 1.2


def test_solar_surplus_curtailed_full_battery_idle_probe_requires_solar():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_curtailed_full_battery_idle_probe_blocks_grid_import():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 300,
            "battery_power": 0,
            "solar_power": 1200,
            "load_power": 1500,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "grid_import_tolerance_kw": 0.1,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_full_battery_topoff_does_not_reserve_battery_charge_rate():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=3.31,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 4.11


def test_observed_wall_connector_power_does_not_probe_vehicle_sensor(monkeypatch):
    async def fail_tesla_entity_lookup(*args, **kwargs):
        raise AssertionError("Tesla vehicle power lookup should not run")

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fail_tesla_entity_lookup)

    hass = _Hass([
        _State("sensor.tesla_wall_connector_power", "2.2", {"unit_of_measurement": "kW"}),
        _State("sensor.tesla_wall_connector_phase_a_current", "9.1", {"unit_of_measurement": "A"}),
        _State("sensor.tesla_wall_connector_energy", "12.3", {"unit_of_measurement": "kWh"}),
    ])

    power_kw = asyncio.run(
        actions._get_observed_ev_power_kw(
            hass,
            "LRW3F7FS1NC484342",
            {"charger_type": "tesla"},
            allow_wall_connector_fallback=True,
        )
    )

    assert power_kw == 2.2


def test_optional_tesla_power_probe_does_not_warn_when_sensor_missing(caplog):
    caplog.set_level("WARNING")
    hass = _Hass([])

    power_kw = asyncio.run(
        actions._get_observed_ev_power_kw(
            hass,
            "LRW3F7FS1NC484342",
            {"charger_type": "tesla"},
        )
    )

    assert power_kw == 0.0
    assert "No Tesla EV devices found" not in caplog.text
    assert "No entity matching pattern" not in caplog.text


def test_observed_wall_connector_power_is_counted_for_solar_surplus_stop(monkeypatch):
    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_soc": 55,
            "grid_power": 700,
            "battery_power": -700,
            "solar_power": 9200,
            "load_power": 9100,
        }

    stop_calls = []

    async def fake_set_amps(hass, config_entry, vehicle_id, amps, params):
        stop_calls.append((vehicle_id, amps))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_amps)

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def home_location(*args, **kwargs):
        return "home"

    ev_planner.get_ev_location = home_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "LRW3F7FS1NC484342": {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "low_surplus_start": datetime.now() - timedelta(minutes=10),
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 2.0,
                "surplus_calculation": "grid_based",
                "allow_parallel_charging": True,
                "max_battery_charge_rate_kw": 5.0,
                "min_battery_soc": 20,
                "stop_delay_minutes": 5,
            },
        }
    }

    hass = _Hass([
        _State("sensor.tesla_wall_connector_power", "5.4", {"unit_of_measurement": "kW"}),
    ])

    asyncio.run(
        actions._dynamic_ev_update(
            hass,
            _Entry(),
            "entry-1",
            "LRW3F7FS1NC484342",
        )
    )

    assert stop_calls == [("LRW3F7FS1NC484342", 0)]
    assert actions._dynamic_ev_state["entry-1"]["LRW3F7FS1NC484342"]["current_amps"] == 0


def test_solar_surplus_direct_parallel_reserve_tops_up_existing_battery_charge():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": 0,
            "battery_power": -4000,
            "solar_power": 12000,
            "load_power": 1000,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "direct",
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 4.0


def test_dynamic_ocpp_update_leaves_energy_to_ocpp_session_poll(monkeypatch):
    manager = _SessionManager()
    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: manager
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_session", ev_session)

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 1500,
            "battery_soc": 50,
        }

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "ocpp",
                "target_battery_charge_kw": 10.5,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "ocpp_charger"))

    assert manager.updates == []


def test_dynamic_battery_target_uses_grid_headroom_when_powerwall_tapers(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -10000,
            "grid_power": 12000,
            "battery_soc": 95.2,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16.0,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [22]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 22


def test_dynamic_scheduled_full_battery_grid_cap_holds_min_amps(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 18400,
            "solar_power": 4000,
            "load_power": 500,
            "ev_power": 2400,
            "battery_soc": 95.1,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "tesla",
                "target_battery_charge_kw": 0,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [6]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 6


def test_dynamic_scheduled_grid_shortfall_holds_min_amps(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 17400,
            "solar_power": 0,
            "ev_power": 2400,
            "battery_soc": 70,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "tesla",
                "target_battery_charge_kw": 5,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [6]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 6


def test_dynamic_full_battery_grid_cap_can_stop_non_scheduled_session(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 18400,
            "solar_power": 4000,
            "load_power": 500,
            "ev_power": 2400,
            "battery_soc": 95.1,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 0,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [0]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 0


def test_dynamic_battery_target_uses_solar_and_home_load_to_preserve_grid_charge(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -8200,
            "grid_power": 15900,
            "solar_power": 3000,
            "load_power": 3600,
            "ev_power": 7100,
            "battery_soc": 88.0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16.0,
                "no_grid_import": True,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [5]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 5


def test_non_ev_home_load_uses_site_balance_when_load_power_is_already_adjusted():
    live_status = {
        "solar_power": 3000,
        "grid_power": 15900,
        "battery_power": -8200,
        "load_power": 3600,
    }

    assert round(actions._non_ev_home_load_kw(live_status, 7.1), 3) == 3.6


def test_ocpp_amps_falls_back_to_hacs_number_entity():
    entity_id = "number.evse_1_maximum_current"
    hass = _Hass(
        [
            _State(entity_id, "16", {"min": 6, "max": 32}),
        ],
        {
            entity_id: SimpleNamespace(entity_id=entity_id, platform="ocpp"),
        },
    )

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 40)) is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": entity_id, "value": 32})
    ]


def test_ocpp_amps_uses_hacs_api_when_available():
    central = _OcppCentralSystem(accepted=True)
    hass = _Hass([
        _State("number.evse_1_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 16)) is True

    assert central.calls == [("evse_1", 16.0, 0)]
    assert hass.services.calls == []


def test_ocpp_amps_uses_hacs_api_connector_id_for_multi_connector_prefix():
    central = _OcppCentralSystem(accepted=True)
    hass = _Hass([
        _State("number.evse_1_connector_2_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(
        actions._set_ocpp_charging_amps(hass, "evse_1_connector_2", 16)
    ) is True

    assert central.calls == [("evse_1", 16.0, 2)]
    assert hass.services.calls == []


def test_ocpp_amps_reports_hacs_api_rejection_without_optimistic_number_fallback():
    central = _OcppCentralSystem(accepted=False)
    hass = _Hass([
        _State("number.evse_1_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 16)) is False

    assert central.calls == [("evse_1", 16.0, 0)]
    assert hass.services.calls == []


def test_ocpp_amps_rejects_hacs_number_entity_capped_below_evse_minimum():
    entity_id = "number.evse_1_maximum_current"
    hass = _Hass(
        [
            _State(entity_id, "5", {"min": 0, "max": 5}),
        ],
        {
            entity_id: SimpleNamespace(entity_id=entity_id, platform="ocpp"),
        },
    )

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 7)) is False
    assert hass.services.calls == []


def test_ocpp_effective_minimum_amps_is_six():
    assert actions._effective_min_charge_amps({
        "charger_type": "ocpp",
        "min_charge_amps": 5,
    }) == 6


def test_ocpp_managed_start_fails_when_only_switch_control_exists():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "ocpp_evse_1",
            16,
            {"charger_type": "ocpp", "ocpp_charger_id": "evse_1"},
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_ocpp_direct_start_uses_hacs_api_result():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True

    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == []


def test_ocpp_direct_start_reports_hacs_rejection():
    central = _OcppCentralSystem(accepted=True, state_accepted=False)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is False

    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == []


def test_ocpp_start_skips_duplicate_remote_start_when_switch_already_on():
    hass = _Hass(
        [
            _State("switch.evse_1_charge_control", "on"),
            _State("sensor.evse_1_status_connector", "Charging"),
        ]
    )

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True
    assert hass.services.calls == []


def test_ocpp_start_still_resets_when_switch_on_but_connector_finishing():
    hass = _Hass(
        [
            _State("switch.evse_1_charge_control", "on"),
            _State("sensor.evse_1_status_connector", "Finishing"),
        ]
    )

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.evse_1_charge_control"}),
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"}),
    ]


def test_ocpp_current_limit_rejection_is_cached_for_session(monkeypatch):
    calls = []

    async def reject_current_limit(hass, charger_id, amps):
        calls.append((charger_id, amps))
        return False

    monkeypatch.setattr(actions, "_set_ocpp_charging_amps", reject_current_limit)

    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    params = {"charger_type": "ocpp", "ocpp_charger_id": "evse_1"}

    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 7, params)) is False
    assert params["_ocpp_current_limit_unsupported"] is True
    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 5, params)) is False

    assert calls == [("evse_1", 7)]
    assert hass.services.calls == []


def test_ocpp_loadpoint_id_does_not_double_prefix():
    assert actions._ev_action_loadpoint_id({
        "charger_type": "ocpp",
        "ocpp_charger_id": "evse_1",
    }) == "ocpp_evse_1"

    assert actions._ev_action_loadpoint_id({
        "charger_type": "ocpp",
        "ocpp_charger_id": "ocpp_evse_1",
    }) == "ocpp_evse_1"


def test_generic_start_blocks_when_status_available_and_no_connector_present():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Available"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "charger_status_entity": "sensor.garage_ev_status",
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_generic_start_allows_available_status_when_connector_has_car():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Available"),
        _State("sensor.garage_ev_status_connector", "Preparing"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": " switch.garage_ev ",
                "charger_status_entity": "sensor.garage_ev_status",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"})
    ]


def test_generic_start_runs_pre_charge_wake_before_switch_on():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Preparing"),
        _State("switch.byd_aircon", "off"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "charger_status_entity": "sensor.garage_ev_status",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_off", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"}),
    ]


def test_generic_direct_start_skips_switch_that_is_already_on():
    hass = _Hass([_State("switch.charger_charge_control", "on")])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == []


def test_generic_ocpp_wrapper_resets_finishing_switch_before_start(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(actions.asyncio, "sleep", fake_sleep)
    hass = _Hass([
        _State("switch.charger_charge_control", "on"),
        _State("sensor.charger_status_connector", "Finishing"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charger_charge_control"}),
        ("switch", "turn_on", {"entity_id": "switch.charger_charge_control"}),
    ]
    assert sleeps == [1]


def test_generic_set_vehicle_amps_uses_input_number_and_skips_duplicate_start():
    hass = _Hass([
        _State("input_number.smart_charge_set_amps", "16"),
        _State("switch.charger_charge_control", "on"),
    ])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            12,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 12},
        )
    ]


def test_generic_direct_set_amps_uses_configured_entity_domain():
    hass = _Hass([_State("input_number.smart_charge_set_amps", "16")])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "amps": 10,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 10},
        )
    ]


def test_generic_switch_stop_does_not_require_zero_amp_write():
    hass = _Hass([
        _State("input_number.smart_charge_set_amps", "6", {"min": 6, "max": 32}),
        _State("switch.charger_charge_control", "on"),
    ])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            0,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charger_charge_control"})
    ]


def test_generic_amps_only_stop_sets_input_number_to_zero():
    hass = _Hass([_State("input_number.smart_charge_set_amps", "6")])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            0,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 0},
        )
    ]


def test_ocpp_pre_charge_wake_blocks_when_connector_available():
    hass = _Hass([
        _State("switch.evse_1_charge_control", "off"),
        _State("sensor.evse_1_status_connector", "Available"),
        _State("switch.byd_aircon", "off"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_ocpp_set_vehicle_amps_runs_pre_charge_wake_before_start():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([
        _State("switch.evse_1_charge_control", "off"),
        _State("sensor.evse_1_status_connector", "Preparing"),
        _State("switch.byd_aircon", "off"),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "ocpp_evse_1",
            16,
            {
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_off", {"entity_id": "switch.byd_aircon"}),
    ]


def test_direct_ev_start_action_records_manual_ownership(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
            },
        )
    )

    assert result is True
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"
    assert lease["last_command"]["command"] == "start"


def test_direct_manual_start_preempts_solar_surplus_ownership(monkeypatch):
    from power_sync.automations import ev_ownership

    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "generic",
                "notify_on_complete": False,
            },
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        "generic_ev",
        owner_mode="solar_surplus",
    )

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "vehicle_id": "generic_ev",
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
            },
        )
    )

    assert result is True
    assert cancelled == [True]
    state = actions._dynamic_ev_state["entry-1"]["generic_ev"]
    assert state["params"]["owner_mode"] == "manual"
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"


def test_solar_surplus_disable_cannot_release_concurrent_manual_takeover(
    monkeypatch,
):
    from power_sync.automations import ev_ownership

    async def run_race():
        hass = _Hass([])
        actions._dynamic_ev_state.clear()
        actions._dynamic_ev_state["entry-1"] = {
            "generic_ev": {
                "active": True,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "owner_mode": "solar_surplus",
                    "charger_type": "generic",
                    "notify_on_complete": False,
                },
                "cancel_timer": lambda: None,
                "session_id": None,
            }
        }
        ev_ownership.claim_ev_ownership(
            hass,
            _Entry(),
            "generic_ev",
            owner_mode="solar_surplus",
        )

        start_entered = asyncio.Event()
        allow_start = asyncio.Event()

        async def delayed_start(*args, **kwargs):
            start_entered.set()
            await allow_start.wait()
            return True

        monkeypatch.setattr(
            actions,
            "_action_start_ev_charging",
            delayed_start,
        )

        manual_task = asyncio.create_task(
            actions._execute_single_action(
                hass,
                _Entry(),
                "start_ev_charging",
                {
                    "vehicle_id": "generic_ev",
                    "charger_type": "generic",
                    "charger_switch_entity": "switch.garage_ev",
                },
            )
        )
        await start_entered.wait()
        disable_task = asyncio.create_task(
            actions.stop_solar_surplus_ev_charging(hass, _Entry())
        )
        await asyncio.sleep(0)
        assert not disable_task.done()

        allow_start.set()
        assert await manual_task is True
        assert await disable_task is True
        return hass

    hass = asyncio.run(run_race())

    state = actions._dynamic_ev_state["entry-1"]["generic_ev"]
    assert state["params"]["owner_mode"] == "manual"
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"


def test_solar_surplus_disable_preserves_manual_solar_policy(monkeypatch):
    from power_sync.automations import ev_ownership

    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "automatic_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
            },
        },
        "manual_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "manual_solar_surplus",
            },
        },
    }
    for vehicle_id, owner_mode in (
        ("automatic_ev", "solar_surplus"),
        ("manual_ev", "manual_solar_surplus"),
    ):
        ev_ownership.claim_ev_ownership(
            hass,
            _Entry(),
            vehicle_id,
            owner_mode=owner_mode,
        )

    stopped = []

    async def fake_stop(hass, entry, params):
        vehicle_id = params["vehicle_id"]
        stopped.append(vehicle_id)
        actions._dynamic_ev_state["entry-1"].pop(vehicle_id)
        ev_ownership.release_ev_ownership(hass, entry, vehicle_id)
        return True

    monkeypatch.setattr(
        actions,
        "_action_stop_ev_charging_dynamic",
        fake_stop,
    )

    result = asyncio.run(
        actions.stop_solar_surplus_ev_charging(hass, _Entry())
    )

    assert result is True
    assert stopped == ["automatic_ev"]
    assert set(actions._dynamic_ev_state["entry-1"]) == {"manual_ev"}
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["manual_ev"]
    assert lease["owner_mode"] == "manual_solar_surplus"


def test_direct_ev_start_action_can_skip_ownership(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "skip_ownership": True,
            },
        )
    )

    assert result is True
    assert "ev_ownership" not in hass.data["power_sync"]["entry-1"]


def test_untracked_dynamic_stop_is_passive_by_default():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "ocpp_evse_1",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
        )
    )

    assert result is True
    assert hass.services.calls == []


def test_explicit_untracked_dynamic_stop_controls_ocpp_charger():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "ocpp_evse_1",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "stop_untracked": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.evse_1_charge_control"})
    ]
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]["command"]
        == "stop"
    )


def test_dynamic_start_claims_business_owner_mode():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "max_charge_amps": 16,
            },
            context=None,
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["dynamic_mode"] == "battery_target"
    assert state["params"]["owner_mode"] == "price_level_recovery"
    assert state["ownership"]["owner_mode"] == "price_level_recovery"
    assert (
        hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]["last_command"]["command"]
        == "start_price_level_recovery"
    )


def test_solar_surplus_dynamic_start_uses_home_power_max_over_idle_tesla_cap(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def plugged_in(*args, **kwargs):
        return True

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = plugged_in
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "max_charge_speed_enabled": True,
                "max_amps_per_phase": 30,
            }
        }
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_charge_amps"] == 30
    assert params["max_charge_amps_source"] == "home_power"
    assert params["allow_stale_entity_max_override"] is True


def test_app_solar_surplus_start_rechecks_disabled_persisted_toggle():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={"solar_surplus_config": {"enabled": False}}
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert command["success"] is False
    assert "disabled before the session started" in command["reason"]


def test_solar_surplus_dynamic_start_blocks_full_ev(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def plugged_in(*args, **kwargs):
        return True

    async def full_ev_soc(*args, **kwargs):
        return 100.0

    ev_planner.is_ev_plugged_in = plugged_in
    ev_planner.get_ev_battery_level = full_ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert last_command["command"] == "start_solar_surplus"
    assert last_command["success"] is False
    assert last_command["reason"] == "EV 100.0% >= 100%, already full"


def test_solar_surplus_dynamic_start_blocks_unplugged_ev(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unplugged(*args, **kwargs):
        return False

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = unplugged
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert last_command["command"] == "start_solar_surplus"
    assert last_command["success"] is False
    assert last_command["reason"] == "vehicle is not plugged in"


def test_solar_surplus_active_default_session_debounces_resolved_vin_unplug(
    monkeypatch,
):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unplugged(*args, **kwargs):
        return False

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = unplugged
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        actions.DEFAULT_VEHICLE_ID: {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    assert set(actions._dynamic_ev_state["entry-1"]) == {
        actions.DEFAULT_VEHICLE_ID
    }


def test_dynamic_start_uses_home_power_grid_import_limit(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "phase_type": "single",
                "max_grid_import_amps": 80,
                "default_voltage": 240,
            }
        }
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_grid_import_kw"] == 19.2


def test_dynamic_start_prefers_tesla_site_meter_limit_over_home_power(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "phase_type": "single",
                "max_grid_import_amps": 80,
                "default_voltage": 240,
            }
        }
    )
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        _site_info_cache={"max_site_meter_power_ac": 16.1}
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_grid_import_kw"] == 16.1


def test_dynamic_deadline_start_uses_configured_max_over_idle_tesla_cap(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_start(*args, **kwargs):
        return True

    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    hass = _Hass([
        _State("number.car_charging_amps", "5", {"min": 5, "max": 5}),
    ])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
                "allow_stale_entity_max_override": True,
            },
            context=None,
        )
    )

    assert result is True
    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert state["params"]["max_charge_amps"] == 32
    assert set_amps_calls == [32]


def test_dynamic_sigenergy_start_uses_charger_abstraction(monkeypatch):
    set_amps_calls: list[tuple[str, int, str]] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps, params["charger_type"]))
        return True

    async def fail_tesla_start(*args, **kwargs):
        raise AssertionError("Sigenergy dynamic starts must not use Tesla discovery")

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fail_tesla_start)
    actions._dynamic_ev_state.clear()
    hass = _Hass([])

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "sigenergy_charger",
                "vehicle_vin": "sigenergy_charger",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "sigenergy",
                "target_battery_charge_kw": 0,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
            },
            context=None,
        )
    )

    assert result is True
    assert set_amps_calls == [("sigenergy_charger", 32, "sigenergy")]
    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert state["current_amps"] == 32
    assert state["params"]["target_battery_charge_kw"] == 0


def test_dynamic_update_holds_fixed_deadline_rate(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "VIN123"))

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert set_amps_calls == [32]


def test_dynamic_update_skips_sigenergy_evdc_rate_adjustment(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == []
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert "rate control is unsupported" in state["reason"]


def test_dynamic_update_uses_detected_sigenergy_evdc_rate_entity(monkeypatch):
    set_amps_calls: list[tuple[int, dict]] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((amps, dict(params)))
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([
        _State("number.sigen_inverter_dc_charger_max_charging_power_limit", "25")
    ])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == [
        (
            16,
            {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": True,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
                "supports_restart_while_plugged": False,
                "control_strategy": "one_shot",
                "solar_control_strategy": "dynamic_rate",
                "charger_capabilities": {
                    "charger_type": "evdc",
                    "supports_start_stop": True,
                    "supports_rate_control": True,
                    "supports_restart_while_plugged": False,
                    "control_strategy": "one_shot",
                    "solar_control_strategy": "dynamic_rate",
                    "sigenergy_charger_charge_power_limit_entity": (
                        "number.sigen_inverter_dc_charger_max_charging_power_limit"
                    ),
                    "sigenergy_charger_discharge_power_limit_entity": "",
                },
                "sigenergy_charger_charge_power_limit_entity": (
                    "number.sigen_inverter_dc_charger_max_charging_power_limit"
                ),
            },
        )
    ]
    assert state["current_amps"] == 16
    assert state["target_amps"] == 16


def test_dynamic_update_clears_unplugged_ble_session(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    plug_checks: list[str | None] = []

    async def is_ev_plugged_in(*args, vehicle_vin=None, **kwargs):
        plug_checks.append(vehicle_vin)
        return False

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_ble_slater": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": "ble_ble_slater",
                "vehicle_name": "Slater",
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "ble_ble_slater"))

    assert actions._dynamic_ev_state == {}
    assert cancelled == [True]
    assert plug_checks == ["ble_ble_slater"]
    assert hass.services.calls == []
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ble_ble_slater"]["command"]
        == "release"
    )
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ble_ble_slater"]["reason"]
        == "vehicle unplugged"
    )


def test_dynamic_update_keeps_plugged_ble_session(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(*args, **kwargs):
        return True

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_ble_phoenix": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": "ble_ble_phoenix",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "fixed_charge_amps": 10,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "ble_ble_phoenix"))

    assert "ble_ble_phoenix" in actions._dynamic_ev_state["entry-1"]
    assert actions._dynamic_ev_state["entry-1"]["ble_ble_phoenix"]["current_amps"] == 10
    assert hass.services.calls == []


def test_tesla_set_amps_clamps_to_entity_max_by_default(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {"vehicle_vin": "VIN123", "amps": 30},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 16})
    ]


def test_solar_surplus_tesla_set_amps_uses_configured_max_over_idle_entity_cap(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "amps": 30,
                "max_charge_amps": 30,
                "allow_stale_entity_max_override": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 30})
    ]


def test_solar_surplus_tesla_set_amps_falls_back_after_range_rejection(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    class _RejectFirstRangeServices(_Services):
        async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
            self.calls.append((domain, service, data))
            if len(self.calls) == 1:
                raise Exception("out_of_range")

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])
    hass.services = _RejectFirstRangeServices()
    params = {
        "vehicle_vin": "VIN123",
        "amps": 30,
        "max_charge_amps": 30,
        "allow_stale_entity_max_override": True,
    }

    result = asyncio.run(actions._action_set_ev_charging_amps(hass, _Entry(), params))

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 30}),
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 16}),
    ]
    assert params["max_charge_amps"] == 16


def test_dynamic_start_is_blocked_by_manual_owner():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {"owner": "powersync", "owner_mode": "manual"}
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is False
    assert hass.services.calls == []
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]
    assert last_command["command"] == "start_price_level_recovery"
    assert last_command["success"] is False
    assert "manual already owns" in last_command["reason"]


def test_dynamic_start_updates_same_owner_family_without_restarting():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
            },
            "session_id": "sess-1",
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {
            "owner": "powersync",
            "owner_mode": "price_level_recovery",
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_opportunity",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is True
    assert hass.services.calls == []
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["owner_mode"] == "price_level_opportunity"
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "price_level_opportunity"
    assert ownership["last_command"]["command"] == "update_price_level_opportunity"


def test_dynamic_start_is_blocked_by_legacy_foreign_state():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "ocpp",
            },
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is False
    assert hass.services.calls == []
    assert "ocpp_evse_1" in actions._dynamic_ev_state["entry-1"]
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]
    assert last_command["success"] is False
    assert "solar_surplus already owns" in last_command["reason"]


def test_dynamic_start_takes_over_legacy_solar_surplus_when_allowed():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "max_charge_amps": 16,
            },
            context=None,
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [
        ("evse_1", "service_charge_stop", False, 1),
        ("evse_1", "service_charge_start", True, 1),
    ]
    assert cancelled == [True]
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["dynamic_mode"] == "battery_target"
    assert state["params"]["owner_mode"] == "price_level_recovery"
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "price_level_recovery"


def test_dynamic_start_updates_solar_surplus_owner_when_same_mode_allowed():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            "session_id": "sess-1",
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {
            "owner": "powersync",
            "owner_mode": "solar_surplus",
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "smart_schedule_solar_surplus",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is True
    assert hass.services.calls == []
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["owner_mode"] == "smart_schedule_solar_surplus"
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "smart_schedule_solar_surplus"
    assert ownership["last_command"]["command"] == "update_smart_schedule_solar_surplus"


def test_manual_session_replaces_existing_owner_without_physical_stop():
    hass = _Hass([_State("switch.ev_charge", "on")])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "params": {"dynamic_mode": "solar_surplus"},
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }

    asyncio.run(
        actions.record_manual_ev_charging_session(
            hass,
            _Entry(),
            "VIN123",
            {"charger_type": "tesla"},
        )
    )

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["active"] is True
    assert state["charging_started"] is True
    assert state["params"]["dynamic_mode"] == "manual"
    assert state["ownership"]["owner_mode"] == "manual"
    assert state["ownership"]["last_command"]["command"] == "start"
    assert hass.data["power_sync"]["entry-1"]["ev_ownership"]["VIN123"]["owner_mode"] == "manual"
    assert cancelled == [True]
    assert hass.services.calls == []


def test_manual_session_records_quick_control_metadata():
    hass = _Hass([_State("switch.ev_charge", "on")])
    actions._dynamic_ev_state.clear()

    asyncio.run(
        actions.record_manual_ev_charging_session(
            hass,
            _Entry(),
            "VIN123",
            {
                "charger_type": "tesla",
                "source_mode": "grid_allowed",
                "duration_minutes": 90,
                "expires_at": "2026-05-01T01:30:00+00:00",
                "quick_control": True,
            },
        )
    )

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["VIN123"]
    assert state["params"]["source_mode"] == "grid_allowed"
    assert state["params"]["duration_minutes"] == 90
    assert state["params"]["expires_at"] == "2026-05-01T01:30:00+00:00"
    assert state["params"]["quick_control"] is True
    assert ownership["source_mode"] == "grid_allowed"
    assert ownership["duration_minutes"] == 90
    assert ownership["expires_at"] == "2026-05-01T01:30:00+00:00"
    assert ownership["quick_control"] is True


def test_solar_surplus_stop_delay_holds_current_amps_on_first_low_sample(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: _solar_surplus_state(current_amps=8),
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": 2000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 8
    assert state["target_amps"] == 8
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == []


def test_solar_surplus_update_stops_full_ev(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=8)
    state["params"].update({
        "charger_type": "generic",
        "notify_on_complete": False,
    })
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": -5000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
        ev_soc=100.0,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert actions._dynamic_ev_state == {}
    assert set_amps_calls == [0]
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"][vehicle_id]
    assert last_command["command"] == "stop"
    assert last_command["reason"] == "already full"


def test_solar_surplus_parallel_reserve_prevents_ramp_while_battery_charging(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["params"].update(
        {
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 38,
            "grid_power": 30,
            "battery_power": -4530,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 7
    assert state["low_surplus_start"] is not None


def test_solar_surplus_full_battery_curtailment_ramps_from_visible_headroom(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["params"].update(
        {
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 17
    assert state.get("low_surplus_start") is None
    assert set_amps_calls == [17]


def test_solar_surplus_below_floor_can_start_with_strict_surplus(monkeypatch):
    start_calls = []

    async def fake_start(*args, **kwargs):
        start_calls.append((args, kwargs))
        return True

    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["charging_started"] = False
    state["high_surplus_start"] = datetime.now() - timedelta(minutes=4)
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
            "notify_on_start": False,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -4000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_is_vehicle_charge_complete", lambda *args, **kwargs: False)

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["charging_started"] is True
    assert state["parallel_charging_mode"] is True
    assert start_calls
    assert set_amps_calls[-1] > 0


def test_solar_surplus_below_floor_continues_with_strict_surplus(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -4000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state.get("paused") is not True
    assert state["parallel_charging_mode"] is True
    assert state["current_amps"] > 0
    assert set_amps_calls[-1] > 0


def test_solar_surplus_below_floor_no_reserved_surplus_uses_stop_delay(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=6)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.2,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
            "stop_delay_minutes": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 8,
            "grid_power": 0,
            "battery_power": -1110,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 6
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == []


def test_solar_surplus_below_floor_pauses_when_battery_discharges(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -2000,
            "battery_power": 800,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["paused"] is True
    assert "battery is discharging" in state["paused_reason"]
    assert state["current_amps"] == 0
    assert set_amps_calls == [0]


def test_solar_surplus_below_floor_pauses_on_grid_import(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "grid_import_tolerance_kw": 0.1,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": 900,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["paused"] is True
    assert "grid import" in state["paused_reason"]
    assert state["current_amps"] == 0
    assert set_amps_calls == [0]


def test_solar_surplus_stop_delay_uses_tesla_hardware_minimum(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: _solar_surplus_state(current_amps=8),
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": -10,
            "battery_power": 380,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 8
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == []


def test_solar_surplus_stop_delay_stops_after_elapsed_delay(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=8)
    state["low_surplus_start"] = datetime.now() - timedelta(minutes=6)
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": 2000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"] == 0
    assert set_amps_calls == [0]


def test_clear_tracked_session_does_not_send_physical_stop():
    hass = _Hass([_State("switch.ev_charge", "on")])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "params": {"dynamic_mode": "manual", "charger_type": "tesla"},
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }

    asyncio.run(actions.clear_tracked_ev_charging_session(hass, _Entry(), "VIN123"))

    assert actions._dynamic_ev_state == {}
    assert cancelled == [True]
    assert hass.data["power_sync"]["entry-1"]["ev_ownership"] == {}
    assert hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]["command"] == "release"
    assert hass.services.calls == []


def test_zaptec_waiting_without_installation_current_fails_start():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry(),
            {"charger_type": "zaptec"},
        )
    )

    assert result is False
    assert client.calls == []


def test_zaptec_waiting_sets_current_without_resume():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec"},
        )
    )

    assert result is True
    assert client.calls == [("set_installation_current", "installation-1", 16)]


def test_zaptec_already_charging_updates_current_without_resume():
    hass, client = _zaptec_hass({"charger_operation_mode": "charging"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec", "amps": 12},
        )
    )

    assert result is True
    assert client.calls == [("set_installation_current", "installation-1", 12)]


def test_zaptec_idle_stop_is_passive_success():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec"},
        )
    )

    assert result is True
    assert client.calls == []


def test_sigenergy_charger_set_vehicle_amps_sets_limit_then_starts(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def set_charging_amps(self, amps: int):
            calls.append(("set_charging_amps", amps))
            return True

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.10",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            16,
            {"charger_type": "sigenergy"},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.10",
                "port": 502,
                "slave_id": 1,
                "charger_type": "evac",
            },
        ),
        ("set_charging_amps", 16),
        ("disconnect",),
        (
            "init",
            {
                "host": "192.0.2.10",
                "port": 502,
                "slave_id": 1,
                "charger_type": "evac",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_charger_stop_routes_to_modbus_controller(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def stop_charging(self):
            calls.append(("stop_charging",))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"sigenergy_modbus_host": "192.0.2.20"},
        options={
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            entry,
            {"charger_type": "sigenergy"},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.20",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("stop_charging",),
        ("disconnect",),
    ]


def test_sigenergy_evdc_dynamic_start_skips_unsupported_current_limit(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def set_charging_amps(self, amps: int):
            calls.append(("set_charging_amps", amps))
            return False

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.30",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            24,
            {"charger_type": "sigenergy", "_sigenergy_start_after_rate_limit": True},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.30",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_evdc_one_shot_blocks_restart_until_unplug(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyState:
        def __init__(self, connected: bool) -> None:
            self.is_connected = connected
            self.is_charging = False

    class _SigenergyController:
        connected = True

        def __init__(self, **config):
            calls.append(("init", config))

        async def read_state(self):
            calls.append(("read_state", self.connected))
            return _SigenergyState(self.connected)

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def stop_charging(self):
            calls.append(("stop_charging",))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.31",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )
    params = {"charger_type": "sigenergy", "_sigenergy_start_after_rate_limit": True}

    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))
    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 0, params))
    assert not asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))

    _SigenergyController.connected = False
    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))

    assert [call[0] for call in calls].count("start_charging") == 2
    assert ("read_state", True) in calls
    assert ("read_state", False) in calls


def test_sigenergy_evdc_rate_entity_sets_kw_then_starts(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([
        _State(
            "number.sigen_inverter_dc_charger_max_charging_power_limit",
            "25",
            {"min": 0, "max": 25.0},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.40",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            24,
            {
                "charger_type": "sigenergy",
                "voltage": 230,
                "phases": 1,
                "_sigenergy_start_after_rate_limit": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {
                "entity_id": "number.sigen_inverter_dc_charger_max_charging_power_limit",
                "value": 5.52,
            },
        )
    ]
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.40",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_evdc_rate_entity_clamps_to_entity_max(monkeypatch):
    hass = _Hass([
        _State(
            "number.evdc_charge_limit",
            "7",
            {"min": 0, "max": 7.0},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.41",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
            "sigenergy_charger_charge_power_limit_entity": "number.evdc_charge_limit",
        },
    )

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {
                "charger_type": "sigenergy",
                "amps": 40,
                "voltage": 230,
                "phases": 1,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.evdc_charge_limit", "value": 7.0},
        )
    ]


def test_sigenergy_evdc_rate_entity_uses_default_25kw_cap_without_entity_max(monkeypatch):
    hass = _Hass([
        _State(
            "number.sigen_inverter_dc_charger_max_charging_power_limit",
            "25",
            {},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.42",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {
                "charger_type": "sigenergy",
                "amps": 200,
                "voltage": 240,
                "phases": 1,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {
                "entity_id": "number.sigen_inverter_dc_charger_max_charging_power_limit",
                "value": 25.0,
            },
        )
    ]


def test_sigenergy_evdc_solar_surplus_uses_native_handoff_without_amp_updates(monkeypatch):
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "battery_power": 0,
            "grid_power": -2000,
            "solar_power": 6000,
            "load_power": 2000,
        },
    )
    mode_calls: list[str] = []

    class _SigenergyController:
        async def set_self_consumption_mode(self):
            mode_calls.append("self_consumption")
            return True

        async def disconnect(self):
            mode_calls.append("disconnect")

    async def fake_controller(config_entry):
        return _SigenergyController()

    monkeypatch.setattr(actions, "_get_sigenergy_controller", fake_controller)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "charging_started": False,
            "entity_max_rechecked": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "solar_control_strategy": "native_handoff",
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 0.5,
                "min_battery_soc": 80,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == []
    assert mode_calls == ["self_consumption", "disconnect"]
    assert state["native_solar_mode_set"] is True
    assert state["target_amps"] == 0
    assert "native solar handoff" in state["reason"]


def test_sigenergy_evdc_solar_surplus_uses_dynamic_rate_when_entity_detected(monkeypatch):
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "battery_power": 0,
            "grid_power": -2000,
            "solar_power": 6000,
            "load_power": 2000,
        },
    )

    async def unexpected_controller(config_entry):
        raise AssertionError("EVDC with rate entity should not use native solar handoff")

    monkeypatch.setattr(actions, "_get_sigenergy_controller", unexpected_controller)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 6,
            "target_amps": 6,
            "charging_started": True,
            "entity_max_rechecked": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "solar_control_strategy": "native_handoff",
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 0.5,
                "surplus_calculation": "grid_based",
                "min_battery_soc": 80,
                "pause_below_soc": 70,
            },
        }
    }

    hass = _Hass([
        _State("number.sigen_inverter_dc_charger_max_charging_power_limit", "25")
    ])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == [12]
    assert state["target_amps"] == 12
    assert state["params"]["supports_rate_control"] is True
    assert state["params"]["solar_control_strategy"] == "dynamic_rate"
