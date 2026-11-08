"""One physical generic charger with isolated vehicle preferences and SOC."""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib
import logging
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Optional, List, Dict, Any

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


@pytest.fixture
def env(monkeypatch):
    for name, path in (("power_sync", ROOT), ("power_sync.automations", ROOT / "automations")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    for leaf in ("shared_generic_charger", "generic_charger_soc"):
        monkeypatch.delitem(sys.modules, f"power_sync.automations.{leaf}", raising=False)
    const = types.ModuleType("power_sync.const")
    const.CONF_GENERIC_CHARGER_SOC_ENTITY = "generic_charger_soc_entity"
    const.CONF_GENERIC_CHARGER_SOC_ENTITY_2 = "generic_charger_soc_entity_2"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    shared = importlib.import_module("power_sync.automations.shared_generic_charger")
    tree = ast.parse((ROOT / "automations/ev_charging_planner.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ("ChargingPriority", "AutoScheduleSettings")]
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    planner.__dict__.update(dataclass=dataclass, field=field, Enum=Enum,
                            Optional=Optional, List=List, Dict=Dict, Any=Any,
                            CAPACITY_SOURCE_DEFAULT_ESTIMATE="default_estimate")
    monkeypatch.setitem(sys.modules, planner.__name__, planner)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])), "real_settings", "exec"), vars(planner))
    planner.AutoScheduleState = lambda vehicle_id: NS(vehicle_id=vehicle_id, is_charging=False, curtailment_override_active=False)
    actions = types.ModuleType("power_sync.automations.actions")
    actions._start_dynamic_lock = asyncio.Lock()
    actions._dynamic_ev_state = {}
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    lease = {}
    ownership = types.ModuleType("power_sync.automations.ev_ownership")
    ownership.get_ev_ownership = lambda *_: ("generic_ev", lease or None)
    ownership.is_solar_surplus_owner_mode = lambda mode: mode == "solar_surplus"
    monkeypatch.setitem(sys.modules, ownership.__name__, ownership)

    class Store:
        def __init__(self):
            self._data = {"unrelated": {"keep": True}}
            self.disk = copy.deepcopy(self._data)
            self.fail = False
        async def async_save(self):
            if self.fail:
                raise OSError("disk full")
            self.disk = copy.deepcopy(self._data)
    store = Store()
    now = datetime.now(timezone.utc)
    states = {
        "sensor.connector": NS(state="disconnected", last_reported=now),
        "sensor.meter": NS(state="0", attributes={"unit_of_measurement": "kW"}),
        "sensor.byd_soc": NS(state="30"),
        "sensor.zeekr_soc": NS(state="80"),
    }
    entry = NS(entry_id="entry", data={}, options={
        "generic_charger_enabled": True,
        "generic_charger_switch_entity": "switch.charger",
        "generic_charger_amps_entity": "number.charger_amps",
        "generic_charger_status_entity": "sensor.connector",
        "generic_charger_power_entity": "sensor.meter",
        "generic_charger_soc_entity": "sensor.zeekr_soc",
    })
    runtime = {"automation_store": store}
    hass = NS(data={"power_sync": {"entry": runtime}}, states=NS(get=states.get))
    initial = planner.AutoScheduleSettings(vehicle_id="generic_ev", charger_type="generic", target_soc=90, departure_times={0: "07:30"}, max_charge_amps=16, voltage=230, phases=3)
    executor = NS(hass=hass, config_entry=entry, _settings={"generic_ev": initial}, _state={}, _cached_soc={}, _current_charge_amps={})
    executor.get_settings = lambda _id: executor._settings[_id]
    return NS(shared=shared, planner=planner, actions=actions, hass=hass, entry=entry, runtime=runtime, store=store, states=states, executor=executor, lease=lease)


async def add(e, name="BYD", sensor="sensor.byd_soc", capacity=60):
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "save", "name": name, "soc_entity": sensor, "battery_capacity_kwh": capacity})
    return e.store._data[e.shared.KEY]["profiles"][-1]["id"]


@pytest.mark.asyncio
async def test_two_cars_restore_distinct_targets_departures_soc_and_capacity_on_restart(env):
    e = env
    byd = await add(e)
    assert e.executor._settings["generic_ev"].target_soc == 90
    e.executor._settings["generic_ev"].target_soc = 95
    e.executor._settings["generic_ev"].departure_times = {1: "08:15"}
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": zeekr})
    settings = e.executor._settings["generic_ev"]
    assert settings.enabled is False
    assert settings.max_charge_amps == 16 and settings.phases == 3
    assert settings.battery_capacity_kwh == 86
    settings.target_soc = 75
    settings.departure_times = {4: "06:30"}
    assert e.shared.profile_soc(e.hass, e.shared.shared_data(e.hass, e.entry)) == 80
    await e.shared.save_selected_schedule(e.executor)
    e.store._data = copy.deepcopy(e.store.disk)
    e.executor._cached_soc["generic_ev"] = {"soc": 80}
    e.shared.restore_schedule(e.executor)
    assert e.executor._settings["generic_ev"].target_soc == 75
    assert e.executor._settings["generic_ev"].departure_times == {4: "06:30"}
    assert "generic_ev" not in e.executor._cached_soc
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": byd})
    settings = e.executor._settings["generic_ev"]
    assert settings.target_soc == 95 and settings.departure_times == {1: "08:15"}
    assert settings.display_name == "BYD" and settings.battery_capacity_kwh == 60
    assert e.shared.profile_soc(e.hass, e.shared.shared_data(e.hass, e.entry)) == 30
    assert list(e.executor._settings) == ["generic_ev"]
    assert e.store._data["unrelated"] == {"keep": True}


@pytest.mark.asyncio
async def test_selected_soc_never_borrows_another_car_or_legacy_fallback(env):
    e = env
    await add(e)
    soc = importlib.import_module("power_sync.automations.generic_charger_soc")
    assert soc.resolve_generic_charger_soc(e.hass, e.entry.options) == 30
    e.states["sensor.byd_soc"].state = "unavailable"
    assert soc.resolve_generic_charger_soc(e.hass, e.entry.options) is None
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": None})
    assert soc.resolve_generic_charger_soc(e.hass, e.entry.options) is None
    assert not e.executor._settings["generic_ev"].enabled
    assert not e.shared.start_allowed(e.hass, e.entry, {"charger_type": "generic", "_user_initiated": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("hazard", ["owned", "pending", "dynamic", "boost", "inflight", "connected", "stale", "future", "power", "command", "retained_dynamic"])
async def test_selection_refuses_control_liabilities_and_unproven_disconnect(env, hazard):
    e = env
    chosen = await add(e)
    before = copy.deepcopy(e.store.disk)
    if hazard == "owned": e.lease.update(owner="powersync")
    if hazard == "pending": e.runtime["_pending_generic_ev_starts"] = {"generic_ev": {"pending": True}}
    if hazard == "dynamic": e.runtime["dynamic_ev_state"] = {"generic_ev": {"active": False}}
    if hazard == "boost": e.runtime["ev_boost_sessions"] = {"generic_ev": {"stop_required": True}}
    if hazard == "inflight": e.executor._start_in_flight = {"generic_ev"}
    if hazard == "connected": e.states["sensor.connector"].state = "charging"
    if hazard == "stale": e.states["sensor.connector"].last_reported -= timedelta(minutes=5)
    if hazard == "future": e.states["sensor.connector"].last_reported += timedelta(minutes=5)
    if hazard == "power": e.states["sensor.meter"].state = "7"
    if hazard == "command": e.runtime["_shared_generic_commands"] = 1
    if hazard == "retained_dynamic": e.actions._dynamic_ev_state["entry"] = {"generic_ev": {"cleanup": True}}
    with pytest.raises(ValueError):
        await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": chosen})
    assert e.store.disk == before


@pytest.mark.asyncio
async def test_failed_selection_persistence_retains_previous_car_and_runtime(env):
    e = env
    byd = await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    old = e.executor._settings["generic_ev"]
    e.store.fail = True
    with pytest.raises(OSError):
        await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": zeekr})
    assert e.shared.selected_profile(e.shared.shared_data(e.hass, e.entry))["id"] == byd
    assert e.executor._settings["generic_ev"] is old
    assert not e.runtime.get("_shared_generic_switching")


@pytest.mark.asyncio
async def test_deleting_inactive_car_keeps_selected_schedule_and_deleting_active_disables_it(env):
    e = env
    byd = await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    e.executor._settings["generic_ev"].enabled = True
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "delete", "vehicle_id": zeekr})
    assert e.executor._settings["generic_ev"].enabled
    assert e.shared.selected_profile(e.shared.shared_data(e.hass, e.entry))["id"] == byd
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "delete", "vehicle_id": byd})
    assert not e.executor._settings["generic_ev"].enabled
    assert not e.shared.start_allowed(e.hass, e.entry, {"charger_type": "generic"})


@pytest.mark.asyncio
async def test_entity_command_keeps_selection_fixed_across_await(env):
    e = env
    await add(e)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    @e.shared.generic_entity_command
    async def start(hass, entity):
        calls.append(entity)
        entered.set()
        await release.wait()
        return True
    task = asyncio.create_task(start(e.hass, "switch.charger"))
    await entered.wait()
    assert e.shared.selection_block_reason(e.hass, e.entry, e.executor)
    release.set()
    assert await task
    assert calls == ["switch.charger"] and e.runtime["_shared_generic_commands"] == 0


@pytest.mark.asyncio
async def test_selection_rechecks_ownership_after_saving_previous_schedule(env):
    e = env
    byd = await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    entered, release = asyncio.Event(), asyncio.Event()
    save = e.store.async_save

    async def paused_save():
        entered.set()
        await release.wait()
        await save()

    e.store.async_save = paused_save
    task = asyncio.create_task(e.shared.update(
        e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": zeekr},
    ))
    await asyncio.wait_for(entered.wait(), 1)
    # A controller may acquire ownership during the awaited storage operation.
    e.lease.update(owner="powersync")
    release.set()
    with pytest.raises(ValueError, match="release the current charging session"):
        await asyncio.wait_for(task, 1)
    assert e.shared.selected_profile(e.shared.shared_data(e.hass, e.entry))["id"] == byd
    assert e.executor._settings["generic_ev"].display_name == "BYD"


@pytest.mark.asyncio
async def test_stale_schedule_start_token_cannot_control_newly_selected_car(env):
    e = env
    byd = await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    old_params = {"charger_type": "generic", "_shared_generic_profile_id": byd}
    assert e.shared.start_allowed(e.hass, e.entry, old_params)
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": zeekr})
    assert not e.shared.start_allowed(e.hass, e.entry, old_params)
    assert e.shared.start_allowed(e.hass, e.entry, {"charger_type": "generic", "_shared_generic_profile_id": zeekr})
    e.entry.options["generic_charger_switch_entity"] = "switch.different_charger"
    assert not e.shared.start_allowed(e.hass, e.entry, {"charger_type": "generic"})


def test_legacy_single_car_is_unchanged_without_profiles(env):
    e = env
    old = e.executor._settings["generic_ev"]
    e.shared.restore_schedule(e.executor)
    assert e.executor._settings["generic_ev"] is old
    soc = importlib.import_module("power_sync.automations.generic_charger_soc")
    assert soc.resolve_generic_charger_soc(e.hass, e.entry.options) == 80
    assert e.shared.start_allowed(e.hass, e.entry, {"charger_type": "generic"})


@pytest.mark.asyncio
async def test_queued_old_start_and_old_stop_are_rejected_at_command_boundary(env):
    e = env
    byd = await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    tree = ast.parse((ROOT / "automations/actions.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_action_start_ev_charging_dynamic")
    node.decorator_list = []
    calls = []
    async def dispatch(*args):
        calls.append(args)
        return True
    namespace = {"__package__": "power_sync.automations", "_start_dynamic_lock": e.actions._start_dynamic_lock,
                 "_action_start_ev_charging_dynamic_locked": dispatch}
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    # This wrapper's local DOMAIN import is part of the real production path.
    sys.modules["power_sync.const"].DOMAIN = "power_sync"
    exec(compile(ast.fix_missing_locations(unit), "real_start_boundary", "exec"), namespace)
    async with e.actions._start_dynamic_lock:
        task = asyncio.create_task(namespace[node.name](e.hass, e.entry, {"charger_type": "generic"}))
        await asyncio.sleep(0)
        # A previously queued command must retain its old profile identity.
        e.store._data[e.shared.KEY]["selected_vehicle_id"] = zeekr
    assert await task is False
    assert calls == []
    assert not e.shared.stop_allowed(e.hass, e.entry, {"charger_type": "generic", "_shared_generic_profile_id": byd})
    assert e.shared.stop_allowed(e.hass, e.entry, {"charger_type": "generic"})


@pytest.mark.asyncio
async def test_unavailable_selected_soc_clears_real_schedule_before_any_start(env):
    e = env
    await add(e)
    e.states["sensor.byd_soc"].state = "unavailable"
    state = e.executor._state["generic_ev"]
    state.current_plan = object()
    state.current_window = object()
    e.executor.get_state = lambda _: state
    tree = ast.parse((ROOT / "automations/ev_charging_planner.py").read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_vehicle")
    namespace = {"__package__": "power_sync.automations"}
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), "real_missing_soc_evaluation", "exec"), namespace)
    await namespace[node.name](e.executor, "generic_ev", e.executor._settings["generic_ev"], {}, 20)
    assert state.current_plan is None and state.current_window is None
    assert state.last_decision == "soc_unavailable"


@pytest.mark.asyncio
async def test_profiles_are_available_before_late_automation_engine_registration(env):
    e = env
    await add(e)
    zeekr = await add(e, "Zeekr", "sensor.zeekr_soc", 86)
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": zeekr})
    e.executor._settings["generic_ev"].target_soc = 72
    await e.shared.save_selected_schedule(e.executor)
    e.store._data = copy.deepcopy(e.store.disk)
    e.runtime.pop("automation_store")
    # Real setup loads AutomationStore long before it registers the engine.
    e.shared.bind_profile_store(e.hass, e.entry, e.store)
    e.executor._settings["generic_ev"] = e.planner.AutoScheduleSettings(target_soc=99)
    e.shared.restore_schedule(e.executor)
    assert e.executor._settings["generic_ev"].target_soc == 72
    assert e.executor._settings["generic_ev"].display_name == "Zeekr"
    soc = importlib.import_module("power_sync.automations.generic_charger_soc")
    assert soc.resolve_generic_charger_soc(e.hass, e.entry.options) == 80


@pytest.mark.asyncio
async def test_physical_charger_remains_visible_and_metered_without_selected_soc(env, monkeypatch):
    e = env
    await add(e)
    await e.shared.update(e.hass, e.entry, e.executor, {"operation": "select", "vehicle_id": None})
    const = sys.modules["power_sync.const"]
    for name in ("AMPS", "ENABLED", "POWER", "STATUS", "SWITCH"):
        setattr(const, f"CONF_GENERIC_CHARGER_{name}_ENTITY" if name != "ENABLED" else "CONF_GENERIC_CHARGER_ENABLED",
                f"generic_charger_{name.lower()}_entity" if name != "ENABLED" else "generic_charger_enabled")
    # Import the real status producer in an isolated module slot: its ownership
    # dependency is intentionally faked by this fixture and must not leak into
    # whichever file a full-suite worker executes next.
    status = types.ModuleType("power_sync.automations.loadpoint_status")
    status.__package__ = "power_sync.automations"
    monkeypatch.setitem(sys.modules, status.__name__, status)
    exec(compile((ROOT / "automations/loadpoint_status.py").read_text(), "real_loadpoint_status", "exec"), vars(status))
    tree = ast.parse((ROOT / "__init__.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_generic_charger_observation_from_config")
    namespace = {"__package__": "power_sync", "_kw_from_power_state": lambda state: float(state.state)}
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), "real_generic_observation", "exec"), namespace)
    observation = namespace[node.name](e.hass, e.entry.options)
    assert observation["include_idle"] is True
    assert observation["ev_soc"] is None
    assert observation["ev_power_kw"] == 0
    e.states["sensor.meter"].state = "7"
    observation = namespace[node.name](e.hass, e.entry.options)
    assert observation["ev_power_kw"] == 7
    assert observation["ev_soc"] is None
