"""Deleting an EV profile must retire its separately persisted schedule."""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def load_node(path, name, namespace):
    node = next(n for n in ast.parse(path.read_text()).body if getattr(n, "name", None) == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)


class SettingsStore:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.fail = False

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("settings write failed")
        self.data = copy.deepcopy(data)


class ProfileStore:
    def __init__(self, data):
        self._data = copy.deepcopy(data)
        self.saved = copy.deepcopy(data)
        self.fail = False

    async def async_save(self):
        if self.fail:
            raise OSError("profile write failed")
        self.saved = copy.deepcopy(self._data)


@pytest.fixture
def deletion(monkeypatch):
    for name, path in (("power_sync", ROOT), ("power_sync.automations", ROOT / "automations")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    actions = types.ModuleType("power_sync.automations.actions")
    actions._dynamic_ev_state = {}
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    sessions = types.ModuleType("power_sync.automations.ev_charging_session")
    sessions.get_session_manager = lambda: None
    monkeypatch.setitem(sys.modules, sessions.__name__, sessions)
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    planner.get_price_level_executor = lambda: None
    monkeypatch.setitem(sys.modules, planner.__name__, planner)
    namespace = {
        "__package__": "power_sync", "asyncio": asyncio,
        "HomeAssistantView": object, "DOMAIN": "power_sync",
        "_LOGGER": logging.getLogger(__name__),
        "web": SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(body=body, status=status)),
    }
    load_node(ROOT / "__init__.py", "_update_powerwall_store", namespace)
    load_node(ROOT / "__init__.py", "VehicleChargingConfigView", namespace)

    def build(target="generic_ev", *, profile=True):
        schedule = {"enabled": True, "departure_times": {"0": "07:00"}}
        durable = SettingsStore({
            "auto_schedule_settings": {target: schedule, "other": {"enabled": True}},
            "cached_vehicle_soc": {target: {"soc": 74}, "other": {"soc": 50}},
            "force_mode_state": {"preserve": True},
        })
        profiles = ProfileStore({"vehicle_charging_configs": [
            *([{"vehicle_id": target, "charger_type": "generic" if target == "generic_ev" else "byd"}] if profile else []),
            {"vehicle_id": "other", "charger_type": "generic"},
        ]})
        executor = SimpleNamespace(
            _settings=copy.deepcopy(durable.data["auto_schedule_settings"]),
            _state={target: object(), "other": object()},
            _cached_soc=copy.deepcopy(durable.data["cached_vehicle_soc"]),
            _current_charge_amps={target: 16}, _store=durable,
            _clear_active_charging_preserve_intent=lambda *args: None,
        )
        planner.get_auto_schedule_executor = lambda: executor
        hass = SimpleNamespace(data={"power_sync": {"entry": {
            "automation_store": profiles, "store": durable,
            "auto_schedule_executor": executor,
        }}})
        return namespace["VehicleChargingConfigView"](hass), profiles, durable, executor

    return build


@pytest.mark.parametrize("target", ["generic_ev", "byd_device"])
def test_delete_retires_enabled_schedule_and_cached_soc_across_restart(deletion, target):
    view, profiles, durable, executor = deletion(target)
    response = asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": target})))
    assert response.status == 200, response.body
    assert target not in executor._settings
    # These are the exact maps load_settings reads on the next HA restart.
    restored = asyncio.run(durable.async_load())
    assert target not in restored["auto_schedule_settings"]
    assert target not in restored["cached_vehicle_soc"]
    assert restored["auto_schedule_settings"]["other"] == {"enabled": True}
    assert restored["cached_vehicle_soc"]["other"] == {"soc": 50}
    assert restored["force_mode_state"] == {"preserve": True}
    assert [c["vehicle_id"] for c in profiles.saved["vehicle_charging_configs"]] == ["other"]


def test_previously_deleted_profile_can_retire_orphan_schedule(deletion):
    view, _, durable, _ = deletion(profile=False)
    response = asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"})))
    assert response.status == 200, response.body
    assert "generic_ev" not in durable.data["auto_schedule_settings"]


def test_durable_write_failure_does_not_report_deleted_or_remove_profile(deletion):
    view, profiles, durable, _ = deletion()
    durable.fail = True
    response = asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"})))
    assert response.status == 500
    assert any(c["vehicle_id"] == "generic_ev" for c in profiles.saved["vehicle_charging_configs"])
    assert "generic_ev" in durable.data["auto_schedule_settings"]
    durable.fail = False
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"}))).status == 200


def test_profile_write_failure_can_retry_after_schedule_was_retired(deletion):
    view, profiles, durable, _ = deletion()
    profiles.fail = True
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"}))).status == 500
    assert "generic_ev" not in durable.data["auto_schedule_settings"]
    profiles._data = copy.deepcopy(profiles.saved)  # Reload after an interrupted request.
    profiles.fail = False
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"}))).status == 200


def test_unknown_vehicle_refuses_without_changing_other_schedules(deletion):
    view, profiles, durable, _ = deletion()
    before = copy.deepcopy(durable.data)
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "unknown"}))).status == 404
    assert durable.data == before
    assert profiles._data == profiles.saved


def test_delete_does_not_touch_another_entry_store(deletion):
    view, _, _, _ = deletion()
    other = SettingsStore({"auto_schedule_settings": {"generic_ev": {"enabled": True}}})
    view._hass.data["power_sync"]["unrelated"] = {
        "automation_store": object(), "store": other,
    }
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": "generic_ev"}))).status == 200
    assert other.data["auto_schedule_settings"]["generic_ev"]["enabled"] is True


@pytest.mark.parametrize("target", ["generic_ev", "byd_device"])
def test_restart_load_does_not_restore_deleted_enabled_schedule(deletion, target):
    view, _, durable, _ = deletion(target)
    assert asyncio.run(view.delete(SimpleNamespace(query={"vehicle_id": target}))).status == 200

    async def discover(*args):
        return []

    class Settings(SimpleNamespace):
        @classmethod
        def from_dict(cls, data):
            return cls(**data)

    namespace = {
        "_LOGGER": logging.getLogger(__name__),
        "discover_all_tesla_vehicles": discover,
        "_configured_ble_prefixes": lambda *args, **kwargs: [],
        "_normalize_stored_auto_schedule_ids": lambda *args, **kwargs: False,
        "_configured_charger_type": lambda opts: "generic",
        "AutoScheduleSettings": Settings, "AutoScheduleState": SimpleNamespace,
    }
    path = ROOT / "automations/ev_charging_planner.py"
    cls = next(n for n in ast.parse(path.read_text()).body if getattr(n, "name", None) == "AutoScheduleExecutor")
    method = next(n for n in cls.body if getattr(n, "name", None) == "load_settings")
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    restarted = SimpleNamespace(
        hass=view._hass, config_entry=SimpleNamespace(data={}, options={}),
        _settings={}, _state={},
        _sync_charger_params_from_vehicle_configs=lambda *args: None,
        resolve_vehicle_capacity=lambda *args: None,
        _load_curtailment_recovery=lambda: None,
    )
    asyncio.run(namespace["load_settings"](restarted, durable))
    assert restarted._settings["other"].enabled is True
    if target == "generic_ev":
        # The configured loadpoint remains available, with a fresh DISABLED schedule.
        assert restarted._settings[target].enabled is False
    else:
        assert target not in restarted._settings
    assert target not in restarted._cached_soc
