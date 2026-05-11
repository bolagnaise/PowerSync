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
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
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
    ha_er.async_get = lambda hass: hass.entity_registry
    ha_dr.async_get = lambda hass: SimpleNamespace(devices={})
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = getattr(ha_dt, "now", lambda *args, **kwargs: None)
    ha_dt.utcnow = getattr(ha_dt, "utcnow", lambda *args, **kwargs: None)

    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
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


class _ZaptecClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_installation_current(self, installation_id: str, amps: int):
        self.calls.append(("set_installation_current", installation_id, amps))

    async def resume_charging(self, charger_id: str):
        self.calls.append(("resume_charging", charger_id))

    async def stop_charging(self, charger_id: str):
        self.calls.append(("stop_charging", charger_id))


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


def _install_solar_surplus_runtime_stubs(monkeypatch, live_status: dict):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def get_ev_location(*args, **kwargs):
        return "home"

    ev_planner.get_ev_location = get_ev_location
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

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
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


def test_ocpp_vehicle_start_succeeds_when_only_switch_control_exists():
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

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"})
    ]


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

    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 7, params)) is True
    assert params["_ocpp_current_limit_unsupported"] is True
    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 5, params)) is True

    assert calls == [("evse_1", 7)]
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"}),
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"}),
    ]


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
    hass = _Hass([
        _State("switch.evse_1_charge_control", "off"),
        _State("sensor.evse_1_status_connector", "Preparing"),
        _State("switch.byd_aircon", "off"),
    ])

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
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_off", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"}),
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
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
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
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["dynamic_mode"] == "battery_target"
    assert state["params"]["owner_mode"] == "price_level_recovery"
    assert state["ownership"]["owner_mode"] == "price_level_recovery"
    assert (
        hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]["last_command"]["command"]
        == "start_price_level_recovery"
    )


def test_solar_surplus_dynamic_start_uses_home_power_max_over_idle_tesla_cap(monkeypatch):
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
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
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
