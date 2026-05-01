"""Tests for price-level EV charging ownership guards."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_er = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)
_ha_dr = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
_ha_dr.async_get = lambda hass: SimpleNamespace(devices={})
_ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_dt.utcnow = getattr(_ha_dt, "utcnow", lambda *args, **kwargs: None)
_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_helpers.event = _ha_event
_ha_root.helpers = _ha_helpers
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


VIN = "LRWYHCEK3PC907290"


class _FakeConfigEntry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _FakeHass:
    def __init__(
        self,
        enabled: bool = True,
        price_settings: dict | None = None,
    ) -> None:
        settings = {"enabled": enabled}
        if price_settings:
            settings.update(price_settings)

        self.data = {
            "power_sync": {
                "entry-1": {
                    "automation_store": SimpleNamespace(
                        _data={"price_level_charging": settings}
                    )
                }
            }
        }


@pytest.fixture
def fake_actions(monkeypatch):
    actions = types.ModuleType("power_sync.automations.actions")
    actions.DEFAULT_VEHICLE_ID = "_default"
    actions._dynamic_ev_state = {}
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    return actions


async def _one_vehicle(*args, **kwargs):
    return [{"vin": VIN, "name": "Model 3"}]


def test_price_level_leaves_solar_surplus_owned_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {
        "entry-1": {
            VIN: {
                "active": True,
                "params": {"dynamic_mode": "solar_surplus"},
            }
        }
    }

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not probe or stop another owned session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "solar_surplus mode owns" in state.last_decision_reason


def test_price_level_leaves_manual_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {
        "entry-1": {
            VIN: {
                "active": True,
                "params": {"dynamic_mode": "manual"},
            }
        }
    }

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not stop a manual session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "manual mode owns" in state.last_decision_reason


def test_stop_guard_blocks_other_owner_family():
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="manual")

    allowed = ev_planner._can_stop_owned_loadpoint(
        hass,
        entry,
        VIN,
        expected_owner_mode="price_level_recovery",
    )

    assert allowed is False
    last_command = ev_ownership.get_ev_last_commands(hass, entry)[VIN]
    assert last_command["command"] == "stop"
    assert last_command["success"] is False
    assert "manual" in last_command["reason"]


def test_stop_guard_allows_same_owner_family():
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="price_level_recovery")

    allowed = ev_planner._can_stop_owned_loadpoint(
        hass,
        entry,
        VIN,
        expected_owner_mode="price_level_opportunity",
    )

    assert allowed is True


def test_price_level_stop_allows_unowned_high_price_stop(fake_actions):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    result = asyncio.run(executor._stop_charging("Price above threshold", vehicle_vin=VIN))

    assert result is True
    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == VIN
    assert params["vehicle_vin"] == VIN
    assert params["stop_untracked"] is True
    assert params["stop_reason"] == "Price above threshold"


def test_price_level_start_uses_vehicle_charger_config(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": "generic_ev",
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
        "min_amps": 6,
        "max_amps": 24,
        "voltage": 240,
        "phases": 3,
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(
        executor._start_charging(
            "price_level_recovery",
            "Cheap price",
            vehicle_vin="generic_ev",
        )
    )

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["charger_amps_entity"] == "number.garage_ev_current"
    assert params["charger_status_entity"] == "sensor.garage_ev_status"
    assert params["max_charge_amps"] == 24
    assert params["phases"] == 3


def test_price_level_stop_uses_vehicle_charger_config(fake_actions):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": "generic_ev",
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(
        executor._stop_charging("Price above threshold", vehicle_vin="generic_ev")
    )

    assert result is True
    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["charger_amps_entity"] == "number.garage_ev_current"
    assert params["charger_status_entity"] == "sensor.garage_ev_status"
    assert params["stop_untracked"] is True


def test_price_level_stop_blocks_manual_owned_dynamic_stop(fake_actions):
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="manual")

    executor = ev_planner.PriceLevelChargingExecutor(hass, entry)
    result = asyncio.run(executor._stop_charging("Price above threshold", vehicle_vin=VIN))

    assert result is False
    fake_actions._action_stop_ev_charging_dynamic.assert_not_awaited()


def test_price_level_leaves_ownership_lease_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {}

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not stop an owned session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        VIN: {"owner": "powersync", "owner_mode": "manual"}
    }
    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "manual mode owns" in state.last_decision_reason


def test_price_level_zaptec_start_is_blocked_by_manual_owner():
    class ZaptecEntry(_FakeConfigEntry):
        options = {
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
        }

    client = SimpleNamespace(resume_charging=AsyncMock(return_value=True))
    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "ev_ownership": {
                "zaptec_standalone": {
                    "owner": "powersync",
                    "owner_mode": "manual",
                }
            },
        }
    )

    executor = ev_planner.PriceLevelChargingExecutor(hass, ZaptecEntry())

    result = asyncio.run(
        executor._start_charging(
            "price_level_recovery",
            "cheap price",
            vehicle_vin="zaptec_standalone",
        )
    )

    assert result is False
    client.resume_charging.assert_not_awaited()
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["zaptec_standalone"]
    assert last_command["command"] == "start_price_level_recovery"
    assert last_command["success"] is False
    assert "manual already owns" in last_command["reason"]


def test_price_level_disabled_does_not_stop_unowned_charging(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {}

    async def disabled_decision(self, vehicle_vin, current_price_cents):
        return False, "Price-level charging is disabled", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("disabled price-level charging must be passive")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        disabled_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(enabled=False), _FakeConfigEntry()
    )
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "disabled"
    assert state.last_decision_reason == "Price-level charging is disabled"


def test_enabled_price_level_still_stops_external_high_price_charging(
    monkeypatch, fake_actions
):
    fake_actions._dynamic_ev_state = {}

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def is_charging(*args, **kwargs):
        return True

    stop_charging = AsyncMock(return_value=True)

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", is_charging)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        stop_charging,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    stop_charging.assert_awaited_once_with("Price above threshold", vehicle_vin=VIN)


def test_unknown_soc_uses_recovery_price_fallback(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def unknown_soc(self, vehicle_vin=None):
        return None

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        unknown_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(executor.get_charging_decision(25))

    assert should_charge is True
    assert mode == "price_level_recovery"
    assert "EV SOC unknown" in reason
    assert executor._state.last_decision == "wants_charge"


def test_unknown_vehicle_soc_uses_recovery_price_fallback(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def unknown_soc(self, vehicle_vin=None):
        return None

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        unknown_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(
        executor.get_charging_decision_for_vehicle(VIN, 25)
    )

    state = executor._get_or_create_vehicle_state(VIN)
    assert should_charge is True
    assert mode == "price_level_recovery"
    assert "EV SOC unknown" in reason
    assert state.last_decision == "wants_charge"
