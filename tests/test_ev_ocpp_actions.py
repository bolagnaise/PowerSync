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
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "params": {"dynamic_mode": "manual", "charger_type": "tesla"},
            "cancel_timer": None,
            "session_id": None,
        }
    }

    asyncio.run(actions.clear_tracked_ev_charging_session(hass, _Entry(), "VIN123"))

    assert actions._dynamic_ev_state == {}
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
