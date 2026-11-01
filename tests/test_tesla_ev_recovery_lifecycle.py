"""Exercise authoritative EV lifecycle hydration and exact recovery routing."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import logging
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_power_sync_submodule


PACKAGE = "_power_sync_ev_recovery_lifecycle"
ownership = load_power_sync_submodule("automations.ev_ownership", PACKAGE)
recovery = load_power_sync_submodule("tesla_ev_recovery", PACKAGE)
INIT = Path(__file__).resolve().parents[1] / "custom_components/power_sync/__init__.py"
INIT_TREE = ast.parse(INIT.read_text())
VIN = "5YJ3E1EA7NF0000A1"


def _function(name, *, hass=None, entry=None):
    node = next(node for node in ast.walk(INIT_TREE) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    namespace = {
        "__name__": f"{PACKAGE}._lifecycle_probe",
        "__package__": PACKAGE,
        "DOMAIN": "power_sync", "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
        "copy": __import__("copy"), "asyncio": asyncio,
        "Any": Any, "ServiceCall": object,
        "_LOGGER": logging.getLogger(__name__), "hass": hass, "entry": entry,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(INIT), "exec"), namespace)
    return namespace[name]


class _Sidecar:
    def __init__(self, events, value=None):
        self.events, self.value = events, deepcopy(value)
        self.fail_load = self.fail_save = False
        self.cancel_load = False

    async def async_load(self):
        self.events.append("sidecar_load")
        if self.cancel_load:
            raise asyncio.CancelledError
        if self.fail_load:
            raise OSError("load unavailable")
        return deepcopy(self.value)

    async def async_save(self, value):
        self.events.append("sidecar_save")
        if self.fail_save:
            return False
        self.value = deepcopy(value)


class _Legacy:
    def __init__(self, events, data=None):
        self.events, self._data = events, deepcopy(data or {})
        self.fail_save = False

    async def async_save(self):
        self.events.append("legacy_save")
        return not self.fail_save


def _context(*, enrolled=False, sidecar_value=None, legacy=None):
    events = []
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    if enrolled:
        entry.data["_tesla_ev_recovery_enrolled"] = True
    sidecar = _Sidecar(events, sidecar_value)
    store = _Legacy(events, legacy)
    runtime = {"automation_store": store, "_tesla_ev_recovery_sidecar_store": sidecar}
    tasks = []

    def update(target, *, data):
        assert sidecar.value is not None, "enrollment cannot precede durable sidecar creation"
        events.append("enrollment_marker")
        target.data = data
        return True

    hass = SimpleNamespace(
        data={"power_sync": {entry.entry_id: runtime}},
        config_entries=SimpleNamespace(async_update_entry=update),
        async_create_task=tasks.append,
    )
    return hass, entry, runtime, store, sidecar, events, tasks


def _close(tasks):
    for task in tasks:
        task.close()


@pytest.mark.asyncio
async def test_initial_hydration_imports_actual_finite_facts_before_enrollment():
    legacy = {"ev_runtime_state": {
        "transport_recovery": {"vehicles": {}},
        "boost_sessions": {VIN: {"expires_at": "original", "status": "pending"}},
        "ev_quick_control_obligations": {VIN: {"obligation_id": "original", "expires_at": "original"}},
    }}
    hass, entry, runtime, store, sidecar, events, tasks = _context(legacy=legacy)
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert entry.data["_tesla_ev_recovery_enrolled"] is True
    assert events.index("sidecar_save") < events.index("enrollment_marker")
    assert runtime["_tesla_ev_recovery_sidecar_loaded"] is True
    assert sidecar.value["obligations"]["ev_boost_sessions"] == legacy["ev_runtime_state"]["boost_sessions"]
    assert sidecar.value["obligations"]["ev_quick_control_obligations"] == legacy["ev_runtime_state"]["ev_quick_control_obligations"]
    assert tasks == []


@pytest.mark.asyncio
async def test_bootstrap_keeps_positive_control_held_while_finite_import_awaits():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    original_save = sidecar.async_save
    checked = []

    async def check_import(value):
        if runtime.get("_tesla_ev_recovery_sidecar_loaded") is True:
            checked.append(True)
            assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
            assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
        await original_save(value)

    sidecar.async_save = check_import
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert checked
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")


@pytest.mark.asyncio
async def test_hydration_cannot_enroll_replacement_runtime_after_store_await():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    replacement = {"automation_store": object()}
    original_load = sidecar.async_load

    async def replace_runtime():
        value = await original_load()
        hass.data["power_sync"][entry.entry_id] = replacement
        return value

    sidecar.async_load = replace_runtime
    assert not await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert entry.data.get("_tesla_ev_recovery_enrolled") is not True
    assert hass.data["power_sync"][entry.entry_id] is replacement
    assert replacement == {"automation_store": replacement["automation_store"]}
    assert tasks == []


@pytest.mark.asyncio
async def test_established_missing_sidecar_holds_positive_control_without_recreating():
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True)
    assert not await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert sidecar.value is None
    assert events == ["sidecar_load"]
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == "sidecar_missing"
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    assert tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load", "save", "enrollment"])
async def test_hydration_failure_never_grants_positive_authority(failure):
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    if failure == "load":
        sidecar.fail_load = True
    elif failure == "save":
        sidecar.fail_save = True
    else:
        hass.config_entries.async_update_entry = lambda *_args, **_kwargs: False
    assert not await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert entry.data.get("_tesla_ev_recovery_enrolled") is not True
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    assert tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [["retained old footprint"], {"old": "missing schema"}])
async def test_malformed_legacy_transport_is_not_imported_as_empty_history(malformed):
    hass, entry, runtime, store, sidecar, events, tasks = _context(legacy={
        "ev_runtime_state": {"transport_recovery": malformed},
    })
    assert not await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert runtime["ev_transport_recovery"] == malformed
    assert sidecar.value is None
    assert entry.data.get("_tesla_ev_recovery_enrolled") is not True
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")


@pytest.mark.asyncio
async def test_hydration_cancellation_propagates_and_leaves_control_held():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    sidecar.cancel_load = True
    with pytest.raises(asyncio.CancelledError):
        await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == "sidecar_hydration_cancelled"
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")


@pytest.mark.asyncio
async def test_existing_empty_obligations_and_late_serializer_cannot_resurrect_old_records():
    state = recovery._new_sidecar()
    state["obligations"] = {"ev_boost_sessions": {}, "ev_quick_control_obligations": {}}
    old = {"ev_runtime_state": {
        "boost_sessions": {VIN: {"status": "old"}},
        "ev_quick_control_obligations": {VIN: {"obligation_id": "old"}},
    }}
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True, sidecar_value=state, legacy=old)
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert runtime["ev_boost_sessions"] == {}
    assert runtime["ev_quick_control_obligations"] == {}
    ownership.restore_ev_runtime_state(hass, entry, store)
    assert runtime["ev_boost_sessions"] == {}
    assert runtime["ev_quick_control_obligations"] == {}
    assert "enrollment_marker" not in events
    _close(tasks)


@pytest.mark.asyncio
async def test_authoritative_empty_families_do_not_remigrate_old_active_leases():
    state = recovery._new_sidecar()
    state["obligations"] = {"ev_boost_sessions": {}, "ev_quick_control_obligations": {}}
    old = {"ev_runtime_state": {"active_ownership": {
        VIN: {"owner_mode": "boost", "expires_at": "2099-01-01T00:00:00+00:00"},
        "GENERIC": {"owner_mode": "manual", "quick_control": True, "expires_at": "2099-01-01T00:00:00+00:00"},
    }}}
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True, sidecar_value=state, legacy=old)
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    result = ownership.restore_ev_runtime_state(hass, entry, store)
    assert result["boost_sessions"] == {}
    assert result["resumable_manual_sessions"] == {}
    assert runtime["ev_boost_sessions"] == {}
    _close(tasks)


@pytest.mark.asyncio
async def test_external_runtime_save_persists_sidecar_before_legacy_lock():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    runtime["ev_quick_control_obligations"] = {VIN: {"obligation_id": "finite", "expires_at": "original"}}
    events.clear()
    assert await ownership.persist_ev_runtime_state(hass, entry)
    assert events == ["sidecar_save", "sidecar_save", "legacy_save"]
    assert sidecar.value["obligations"]["ev_quick_control_obligations"] == runtime["ev_quick_control_obligations"]
    _close(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_finite_start_admission_requires_sidecar_before_timer_arm(monkeypatch, save_fails):
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    sidecar.fail_save = save_fails
    event_module = types.ModuleType("homeassistant.helpers.event")
    armed = []

    def arm(_hass, _callback, _when):
        record = sidecar.value["obligations"]["ev_quick_control_obligations"][VIN]
        armed.append(deepcopy(record))
        assert record["expires_at"] == "2099-01-01T00:00:00+00:00"
        return lambda: None

    event_module.async_track_point_in_utc_time = arm
    monkeypatch.setitem(sys.modules, event_module.__name__, event_module)
    result = await ownership.async_begin_ev_quick_control_obligation(hass, entry, VIN, {
        "vehicle_vin": VIN, "charger_type": "tesla", "quick_control": True,
        "expires_at": "2099-01-01T00:00:00+00:00", "_boost_start_deadline": 4_070_908_800.0,
    })
    if save_fails:
        assert result is None
        assert armed == []
        assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    else:
        assert result is not None
        assert armed[0]["obligation_id"] == result["obligation_id"]
    _close(tasks)


@pytest.mark.asyncio
async def test_manager_projection_save_does_not_reenter_owned_journal_lock():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)

    async def manager_save():
        async with recovery._journal_lock(runtime):
            return await recovery._persist(hass, entry, runtime)

    assert await asyncio.wait_for(manager_save(), 1)
    _close(tasks)


@pytest.mark.asyncio
async def test_failed_lifecycle_save_blocks_new_rates_but_keeps_stop_available():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    original_deadline = "2099-01-01T00:00:00+00:00"
    runtime["ev_quick_control_obligations"] = {VIN: {"obligation_id": "finite", "expires_at": original_deadline}}
    sidecar.fail_save = True
    events.clear()
    assert not await ownership.persist_ev_runtime_state(hass, entry)
    assert "legacy_save" not in events
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    sidecar.fail_save = False
    assert await ownership.persist_ev_runtime_state(hass, entry)
    assert runtime.get("_tesla_ev_recovery_sidecar_hold") is None
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert sidecar.value["obligations"]["ev_quick_control_obligations"][VIN]["expires_at"] == original_deadline
    _close(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_hold", ["sidecar_missing", "sidecar_malformed", "finite_obligations_malformed", "enrollment_marker_save_failed"])
async def test_retry_never_relabels_or_clears_recovery_owned_holds(foreign_hold):
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    runtime["_tesla_ev_recovery_sidecar_hold"] = foreign_hold
    sidecar.fail_save = True
    assert not await ownership.persist_ev_runtime_state(hass, entry)
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == foreign_hold
    sidecar.fail_save = False
    assert await ownership.persist_ev_runtime_state(hass, entry)
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == foreign_hold
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    _close(tasks)


@pytest.mark.asyncio
async def test_transient_retry_keeps_old_scoped_incident_and_hazard_held():
    state = recovery._new_sidecar()
    state["incidents"]["original"] = {"phase": "held", "current_candidate_vehicles": [VIN]}
    state["recovery"]["vehicles"][VIN] = {"route": None, "attempts": {}, "hazards": {
        "old": {"kind": "ble_quarantine", "state": "pending", "prefix": "original"},
    }}
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True, sidecar_value=state)
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    original_incidents = deepcopy(sidecar.value["incidents"])
    sidecar.fail_save = True
    assert not await ownership.persist_ev_runtime_state(hass, entry)
    sidecar.fail_save = False
    assert await ownership.persist_ev_runtime_state(hass, entry)
    assert runtime.get("_tesla_ev_recovery_sidecar_hold") is None
    assert sidecar.value["incidents"] == original_incidents
    assert recovery.get_vehicle_route(hass, entry, VIN)["kind"] == "blocked"
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert recovery.recovery_control_allowed(hass, entry, "5YJ3E1EA7NF0000B2", 0, "start")
    _close(tasks)


@pytest.mark.asyncio
async def test_internal_projection_failure_retains_sidecar_and_blocks_positive_admission():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    store.fail_save = True
    async with recovery._journal_lock(runtime):
        assert not await recovery._persist(hass, entry, runtime)
    assert sidecar.value is not None
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == "lifecycle_projection_save_failed"
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    store.fail_save = False
    async with recovery._journal_lock(runtime):
        assert await recovery._persist(hass, entry, runtime)
    assert runtime.get("_tesla_ev_recovery_sidecar_hold") is None
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    _close(tasks)


@pytest.mark.asyncio
async def test_sidecar_reconciles_failed_boost_takeover_before_snapshotting_deadline():
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    runtime["ev_boost_sessions"] = {VIN: {
        "superseded": True, "status": "superseded", "status_before_supersession": "active",
        "ownership_generation": "original-owner", "expires_at": "2099-01-01T00:00:00+00:00",
    }}
    runtime["ev_ownership"] = {VIN: {"owner": "powersync", "owner_mode": "boost", "ownership_generation": "original-owner"}}
    assert await ownership.persist_ev_runtime_state(hass, entry)
    record = sidecar.value["obligations"]["ev_boost_sessions"][VIN]
    assert record["status"] == "active"
    assert "superseded" not in record
    assert record["expires_at"] == "2099-01-01T00:00:00+00:00"
    _close(tasks)


@pytest.mark.asyncio
async def test_late_restore_retains_new_authoritative_transport_and_finite_maps():
    state = recovery._new_sidecar()
    state["recovery"] = {"vehicles": {VIN: {"route": None, "attempts": {}, "hazards": {
        "pending": {"kind": "ble_quarantine", "state": "pending", "prefix": "original"},
    }}}}
    state["obligations"] = {
        "ev_boost_sessions": {VIN: {"status": "newer", "expires_at": "original"}},
        "ev_quick_control_obligations": {VIN: {"obligation_id": "newer", "expires_at": "original"}},
    }
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True, sidecar_value=state)
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    store._data["ev_runtime_state"] = {
        "transport_recovery": {"vehicles": {}}, "boost_sessions": {},
        "ev_quick_control_obligations": {},
    }
    restored = ownership.restore_ev_runtime_state(hass, entry, store)
    assert runtime["ev_transport_recovery"] == state["recovery"]
    assert restored["boost_sessions"] == state["obligations"]["ev_boost_sessions"]
    assert restored["ev_quick_control_obligations"] == state["obligations"]["ev_quick_control_obligations"]
    _close(tasks)


@pytest.mark.asyncio
async def test_malformed_finite_sidecar_family_is_retained_and_blocks_admission():
    state = recovery._new_sidecar()
    state["obligations"] = {"ev_boost_sessions": {}, "ev_quick_control_obligations": "retained malformed evidence"}
    hass, entry, runtime, store, sidecar, events, tasks = _context(enrolled=True, sidecar_value=state)
    assert not await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert runtime["ev_quick_control_obligations"] == "retained malformed evidence"
    assert ownership._runtime_snapshot(hass, entry)["ev_quick_control_obligations"] == "retained malformed evidence"
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    _close(tasks)


@pytest.mark.asyncio
async def test_recover_service_routes_exact_entry_and_reports_boolean_failure(monkeypatch):
    stock = load_power_sync_submodule("tesla_ev_stock_recovery", PACKAGE)
    routing = load_power_sync_submodule("licensing.entry_routing", PACKAGE)
    hass, first, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, first, store)
    second = SimpleNamespace(entry_id="entry-2", data={}, options={})
    hass.data["power_sync"][second.entry_id] = {"_tesla_ev_recovery_sidecar": recovery._new_sidecar()}
    calls = []

    async def recover(_hass, selected, vin, *, incident_id=None):
        calls.append((selected.entry_id, vin, incident_id))
        return selected is first

    monkeypatch.setattr(stock, "async_recover_vehicle_transport", recover)
    handlers = {}
    for selected in (first, second):
        handlers[selected.entry_id] = _function("handle_recover_ev_transport", hass=hass, entry=selected)
        routing.register_handlers(hass, selected.entry_id, {"recover_ev_transport": handlers[selected.entry_id]})
    service = routing.routed_handler(hass, second.entry_id, "recover_ev_transport", handlers[second.entry_id])
    result = await service(SimpleNamespace(data={"entry_id": first.entry_id, "vin": VIN.lower(), "incident_id": "incident-a"}))
    assert result["success"] is True
    assert result["vin"] == VIN
    assert calls == [(first.entry_id, VIN, "incident-a")]
    result = await service(SimpleNamespace(data={"entry_id": second.entry_id, "vin": VIN}))
    assert result["success"] is False
    assert calls[-1] == (second.entry_id, VIN, None)
    _close(tasks)


@pytest.mark.asyncio
async def test_recover_service_rejects_invalid_vin_without_calling_recovery(monkeypatch):
    stock = load_power_sync_submodule("tesla_ev_stock_recovery", PACKAGE)
    calls = []

    async def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("invalid scope reached recovery")

    monkeypatch.setattr(stock, "async_recover_vehicle_transport", forbidden)
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    result = await _function("handle_recover_ev_transport", hass=hass, entry=entry)(SimpleNamespace(data={"vin": "ble_alias"}))
    assert result == {"success": False, "error": "invalid_vin", "recovery": None}
    assert calls == []


def test_setup_hydrates_before_native_rate_and_arms_quick_before_boost_await():
    setup = next(node for node in INIT_TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
    calls = [(node.lineno, ast.unparse(node.func)) for node in ast.walk(setup) if isinstance(node, ast.Call)]
    def line(name):
        return next(line for line, target in calls if target == name)
    assert line("_async_prepare_ev_recovery_lifecycle") < line("_prepare_tesla_native_rate")
    assert line("ev_command_view.restore_ev_quick_control_obligations") < line("ChargingBoostView(hass, entry).restore_boost_sessions")


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_owned_unload_barrier_fences_before_runtime_discard(save_fails):
    hass, entry, runtime, store, sidecar, events, tasks = _context()
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    sidecar.fail_save = save_fails
    entered, release = asyncio.Event(), asyncio.Event()

    async def prior_store_task():
        entered.set()
        await release.wait()
        events.append("old_store_task_drained")

    old_store_task = asyncio.create_task(prior_store_task())
    recovery._storage_tasks(runtime).add(old_store_task)
    old_store_task.add_done_callback(recovery._storage_tasks(runtime).discard)
    unload = next(node for node in INIT_TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_unload_entry")
    barrier = next(node for node in unload.body if isinstance(node, ast.If) and "_ev_recovery_lifecycle_prepared" in ast.unparse(node.test))
    function = ast.AsyncFunctionDef(
        name="unload_barrier", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[deepcopy(barrier), ast.Return(ast.Constant(True))], decorator_list=[],
    )
    namespace = {"__name__": f"{PACKAGE}._unload_probe", "__package__": PACKAGE,
        "hass": hass, "entry": entry, "entry_data": runtime, "asyncio": asyncio,
        "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(INIT), "exec"), namespace)
    shutdown = asyncio.create_task(namespace["unload_barrier"]())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    assert await asyncio.wait_for(shutdown, 1) is True
    assert "old_store_task_drained" in events
    assert hass.data["power_sync"][entry.entry_id] is runtime
    assert not recovery.recovery_control_allowed(hass, entry, VIN, 0, "start")
    # Retired positive authority is fenced; physical cleanup remains available
    # even if the last shutdown save failed.
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
    _close(tasks)


def test_unload_saves_obligations_and_shuts_recovery_before_discarding_runtime():
    unload = next(node for node in INIT_TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_unload_entry")
    calls = [(node.lineno, ast.unparse(node.func)) for node in ast.walk(unload) if isinstance(node, ast.Call)]
    def line(name):
        return next(line for line, target in calls if target == name)
    assert line("persist_ev_runtime_state") < line("async_shutdown_recovery_state")
    assert line("async_shutdown_recovery_state") < line("hass.data[DOMAIN].pop")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_bootstrap", [False, True])
async def test_first_setup_await_holds_actual_legacy_bt_number_write(monkeypatch, existing_bootstrap):
    """A retained HTTP callback cannot exploit the missing-number-row path."""
    from collections.abc import Mapping
    from contextvars import ContextVar
    import inspect
    import math
    from typing import Callable, Optional

    constants = load_power_sync_submodule("const", PACKAGE)
    solar = load_power_sync_submodule("teslemetry_solar", PACKAGE)
    admission = load_power_sync_submodule("native_rate_admission", PACKAGE)
    hass, entry, _runtime, store, sidecar, events, tasks = _context()
    entry.options = {"ev_provider": constants.EV_PROVIDER_TESLEMETRY_BT}
    bootstrap = {"tariff_schedule": {"original": True}}
    if existing_bootstrap:
        hass.data["power_sync"][entry.entry_id] = bootstrap
    else:
        hass.data["power_sync"].pop(entry.entry_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_setup_await(_hass):
        entered.set()
        await release.wait()

    manager = types.ModuleType(f"{PACKAGE}.licensing.manager")
    manager.async_get_manager = first_setup_await
    monkeypatch.setitem(sys.modules, manager.__name__, manager)
    setup = deepcopy(next(node for node in INIT_TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"))
    first_await = next(index for index, statement in enumerate(setup.body) if any(isinstance(node, ast.Await) for node in ast.walk(statement)))
    setup.body = setup.body[:first_await + 1]
    namespace = {"__name__": f"{PACKAGE}._setup_prelude", "__package__": PACKAGE,
                 "DOMAIN": "power_sync", "HomeAssistant": object, "ConfigEntry": object}
    exec(compile(ast.Module(body=[setup], type_ignores=[]), str(INIT), "exec"), namespace)
    setup_task = asyncio.create_task(namespace["async_setup_entry"](hass, entry))
    await asyncio.wait_for(entered.wait(), 1)
    # Ordinary read-only ownership/status lookups may create this bootstrap
    # dictionary too; they must preserve the setup hold already installed.
    assert ownership.get_ev_ownerships(hass, entry) == {}

    prefix = VIN.lower()
    number = constants.TESLEMETRY_BT_NUMBER_CHARGE_AMPS.format(prefix=prefix)
    switch = constants.TESLEMETRY_BT_SWITCH_CHARGE.format(prefix=prefix)
    sensor = constants.TESLEMETRY_BT_SENSOR_CHARGING_STATE.format(prefix=prefix)
    states = {number: SimpleNamespace(state="10", attributes={"min": 1, "max": 32}),
              switch: SimpleNamespace(state="off"), sensor: SimpleNamespace(state="Stopped")}
    registrations = {entity_id: SimpleNamespace(entity_id=entity_id, platform="tesla_bluetooth",
        config_entry_id="legacy-bt", device_id="legacy-device", unique_id=entity_id,
        disabled_by=None) for entity_id in (switch, sensor)}
    ha = sys.modules.get("homeassistant") or types.ModuleType("homeassistant")
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    registry = types.ModuleType("homeassistant.helpers.entity_registry")
    registry.async_get = lambda _hass: SimpleNamespace(async_get=registrations.get)
    monkeypatch.setitem(sys.modules, "homeassistant", ha)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, registry.__name__, registry)
    monkeypatch.setattr(ha, "helpers", helpers, raising=False)
    monkeypatch.setattr(helpers, "entity_registry", registry, raising=False)
    hass.states = SimpleNamespace(get=states.get)
    writes = []

    async def write(domain, service, data, **_kwargs):
        writes.append((domain, service, data))

    hass.services = SimpleNamespace(async_call=write)
    source = INIT.parent / "automations/actions.py"
    selected = {"_run_teslemetry_bt_transport_command", "_tesla_transport_authority",
        "_tesla_legacy_entity_identity", "_tesla_legacy_transport_authorized",
        "_run_tesla_legacy_with_authority", "_set_ev_charging_amps_teslemetry_bt",
        "_tesla_call_service", "_tesla_charge_current_positive_floor",
        "_generic_charger_entity_bounds", "_coerce_positive_int", "_is_number_range_error",
        "_legacy_teslemetry_bt_identity", "_is_teslemetry_bt_available"}
    nodes = [node for node in ast.parse(source.read_text()).body if getattr(node, "name", None) in selected]
    action_ns = {**vars(constants), "__name__": f"{PACKAGE}.automations._setup_probe", "__spec__": None,
        "__package__": f"{PACKAGE}.automations", "asyncio": asyncio, "inspect": inspect,
        "math": math, "Mapping": Mapping, "Any": Any, "Optional": Optional, "Callable": Callable,
        "_LOGGER": logging.getLogger(__name__), "DEFAULT_VEHICLE_ID": "_default",
        "er": registry,
        "prepare_ev_submission": admission.prepare_ev_submission,
        "_tesla_submission_guard": ContextVar("setup_guard", default=None),
        "_tesla_submission_handoff": ContextVar("setup_handoff", default=None),
        "_tesla_submission_tracker": ContextVar("setup_tracker", default=None),
        "_tesla_legacy_authority": ContextVar("setup_authority", default=None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])), str(source), "exec"), action_ns)
    assert action_ns["_legacy_teslemetry_bt_identity"](hass, prefix)
    assert action_ns["_is_teslemetry_bt_available"](hass, prefix)
    assert number not in registrations and hass.states.get(number) is not None

    class GateStore:
        async def async_load(self):
            return {}

    gate = solar.SolarOwnership(GateStore())

    async def get_gate(_hass, _entry_id):
        return gate

    monkeypatch.setattr(solar, "ownership", get_gate)

    @solar.guard_ev_control
    async def legacy_rate(hass, config_entry, params):
        return (await action_ns["_run_teslemetry_bt_transport_command"](
            hass, config_entry, params, prefix, "rate",
            lambda: action_ns["_set_ev_charging_amps_teslemetry_bt"](hass, prefix, 6),
        ))[0]

    try:
        result = await legacy_rate(hass, entry, {"vehicle_vin": VIN})
        assert writes == []
        assert result is False
        runtime = hass.data["power_sync"][entry.entry_id]
        assert runtime["_tesla_ev_recovery_sidecar_hold"] == "sidecar_setup_pending"
        if existing_bootstrap:
            assert runtime is bootstrap and runtime["tariff_schedule"] == {"original": True}
        assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "stop")
        assert recovery.get_vehicle_route(hass, entry, VIN) is None
        assert hass.states.get(sensor).state == "Stopped"
        assert entry.data.get("_tesla_ev_recovery_enrolled") is not True
    finally:
        release.set()
        await setup_task
    runtime["automation_store"] = store
    runtime["_tesla_ev_recovery_sidecar_store"] = sidecar
    assert await _function("_async_prepare_ev_recovery_lifecycle")(hass, entry, store)
    assert runtime.get("_tesla_ev_recovery_sidecar_hold") is None
    assert runtime["_tesla_ev_recovery_sidecar_loaded"] is True
    assert recovery.recovery_control_allowed(hass, entry, VIN, 0, "rate")
    _close(tasks)
