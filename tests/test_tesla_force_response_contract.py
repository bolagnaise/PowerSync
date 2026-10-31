"""Actual force-handler refusals satisfy HA's requested-response contract."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import load_power_sync_submodule


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


@pytest.fixture(scope="module")
def source_tree():
    return ast.parse((COMPONENT / "__init__.py").read_text())


@pytest.fixture
def force_handler(monkeypatch, source_tree):
    tree = source_tree
    package = ModuleType("tesla_force_response_probe")
    package.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, package.__name__, package)

    def load(direction, *, monitoring=True, monitoring_only=False):
        def find(name):
            return deepcopy(next(node for node in ast.walk(tree)
                                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                 and node.name == name))

        handler = find(f"handle_force_{direction}")
        guard_index = next(index for index, node in enumerate(handler.body)
                           if isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                           and isinstance(node.test.func, ast.Name)
                           and node.test.func.id == "_monitoring_mode_should_block_control")
        # Preserve the complete production prefix and monitoring branch. The
        # first downstream actuator boundary records whether routing continued.
        handler.body = handler.body[:guard_index + 1] + ast.parse(
            "return await downstream(call)").body
        downstream = AsyncMock(return_value={"success": True, "downstream": "unchanged"})
        runtime = {"force_charge_state": {"active": False}, "force_discharge_state": {"active": False},
                   "powerwall_settings_control_tasks": set()}
        namespace = {
            "__package__": package.__name__,
            "dt_util": SimpleNamespace(utcnow=lambda: datetime(2026, 10, 31, tzinfo=timezone.utc)),
            "hass": SimpleNamespace(data={"power_sync": {"entry": runtime}}),
            "entry": SimpleNamespace(entry_id="entry", data={"battery_system": "tesla"}, options={}),
            "DOMAIN": "power_sync", "_LOGGER": logging.getLogger(__name__),
            "DEFAULT_DISCHARGE_DURATION": 30, "DISCHARGE_DURATIONS": [5, 15, 30, 60],
            "HomeAssistantError": RuntimeError,
            "asyncio": asyncio, "active_battery_system": "tesla", "BATTERY_SYSTEM_TESLA": "tesla",
            "_local_powerwall_runtime": runtime, "time": SimpleNamespace(time=lambda: 100),
            "_resolve_force_command_power_w": lambda direction, watts: int(watts),
            "_coerce_force_power_w": int,
            "battery_connection_profile": SimpleNamespace(monitoring_only=monitoring_only),
            "_is_monitoring_mode": lambda: monitoring, "downstream": downstream,
        }
        module = ast.Module(body=[find("_control_call_source"),
                                 find("_monitoring_mode_should_block_control"),
                                 find("_track_tesla_settings_control"), handler], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<actual force monitoring branch>", "exec",
                     flags=__import__("__future__").annotations.compiler_flag), namespace)
        return namespace[handler.name], downstream, runtime

    return load


def service_call(source):
    return SimpleNamespace(data={"source": source, "duration": 5, "power_w": 1000},
                           context=SimpleNamespace(user_id=None, parent_id=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("source", ["validation", "automation", "unknown"])
async def test_monitoring_refusal_returns_failure_response_without_actuator_routing(force_handler, direction, source):
    handler, downstream, runtime = force_handler(direction)
    before = deepcopy(runtime)
    response = await handler(service_call(source))
    assert isinstance(response, dict), "HA return_response=True requires a dictionary"
    assert response == {"success": False, "error": "blocked by monitoring mode"}
    downstream.assert_not_awaited()
    assert runtime == before


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_monitoring_only_connection_refuses_manual_call_with_failure_response(force_handler, direction):
    handler, downstream, _ = force_handler(direction, monitoring_only=True)
    assert await handler(service_call("manual")) == {"success": False, "error": "blocked by monitoring mode"}
    downstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("monitoring,source", [(False, "validation"), (True, "manual"), (True, "user")])
async def test_allowed_control_continues_to_original_downstream_response(force_handler, direction, monitoring, source):
    handler, downstream, _ = force_handler(direction, monitoring=monitoring)
    call = service_call(source)
    assert await handler(call) == {"success": True, "downstream": "unchanged"}
    downstream.assert_awaited_once_with(call)


@pytest.mark.asyncio
async def test_optimizer_discharge_keeps_existing_monitoring_exception(force_handler):
    handler, downstream, _ = force_handler("discharge")
    with pytest.raises(RuntimeError, match="Optimizer force discharge blocked by monitoring mode"):
        await handler(service_call("optimizer"))
    downstream.assert_not_awaited()


@pytest.fixture
def restore_prefix(source_tree):
    pending_module = load_power_sync_submodule("powerwall_local.native_max_charge", "tesla_restore_response_probe")

    def load(*, stop=True, superseded=None):
        def find(name):
            return deepcopy(next(node for node in ast.walk(source_tree)
                                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name))
        handler = find("handle_restore_normal")
        boundary = next(index for index, node in enumerate(handler.body)
                        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                        and target.id == "solaredge_restore_coordinator" for target in node.targets))
        handler.body = handler.body[:boundary] + ast.parse("return await downstream(call)").body
        runtime = {"powerwall_settings_control_tasks": set(), "pending_tesla_restore": {"id": "owned"}}
        stop_call, downstream = AsyncMock(return_value=stop), AsyncMock(return_value={"success": "downstream"})
        namespace = {
            "__package__": "tesla_restore_response_probe", "asyncio": asyncio,
            "_LOGGER": logging.getLogger(__name__), "_command_generation": [1],
            "_tesla_charge_kick_generation": [1], "_tesla_operation_generation": [1],
            "hass": SimpleNamespace(data={"power_sync": {"entry": runtime}}),
            "entry": SimpleNamespace(entry_id="entry"), "DOMAIN": "power_sync",
            "active_battery_system": "tesla", "BATTERY_SYSTEM_TESLA": "tesla",
            "HomeAssistantError": RuntimeError, "_stop_local_powerwall_dispatch": stop_call,
            "downstream": downstream,
        }
        if superseded == "after_stop":
            async def stopped():
                namespace["_command_generation"][0] += 1
                return stop
            stop_call.side_effect = stopped
        elif superseded == "before_stop":
            namespace["_command_generation"][0] += 1
        module = ast.Module(body=[find("_control_call_source"), find("_track_tesla_settings_control"), handler], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<actual restore prefix>", "exec",
                     flags=__import__("__future__").annotations.compiler_flag), namespace)
        return namespace[handler.name], stop_call, downstream, runtime, pending_module.NativeMaxRestorePending

    return load


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["local", "native"])
async def test_completed_local_or_native_stop_returns_confirmed_workflow_response(restore_prefix, owner):
    handler, stop, downstream, runtime, _ = restore_prefix()
    runtime["powerwall_battery_dispatch" if owner == "local" else "powerwall_native_max_charge"] = SimpleNamespace(pending=True)
    call = service_call("validation")
    call.data["_confirm_restore"] = True
    response = await handler(call)
    assert response == {"success": True, "error": None}
    assert response.get("physical_effect_confirmed") is not True
    stop.assert_awaited_once()
    downstream.assert_not_awaited()
    assert runtime["pending_tesla_restore"] == {"id": "owned"}


@pytest.mark.asyncio
@pytest.mark.parametrize("moment", ["before_stop", "after_stop"])
async def test_superseded_confirmed_restore_returns_failure_without_further_routing(restore_prefix, moment):
    handler, stop, downstream, runtime, _ = restore_prefix(stop=False, superseded=moment)
    call = service_call("validation")
    call.data.update(_confirm_restore=True, _tesla_restore_generation=1)
    assert await handler(call) == {"success": False, "error": "restoration superseded"}
    assert stop.await_count == int(moment == "after_stop")
    downstream.assert_not_awaited()
    assert runtime["pending_tesla_restore"] == {"id": "owned"}


@pytest.mark.asyncio
@pytest.mark.parametrize("source,confirm,raises", [("manual", True, True), ("validation", False, True),
                                                   ("user", False, False)])
async def test_pending_native_restore_preserves_existing_exception_and_background_rules(restore_prefix, source, confirm, raises):
    handler, stop, downstream, runtime, pending_error = restore_prefix()
    stop.side_effect = pending_error("physical settling remains pending")
    call = service_call(source)
    call.data["_confirm_restore"] = confirm
    if raises:
        with pytest.raises(pending_error, match="physical settling"):
            await handler(call)
    else:
        assert await handler(call) is None
    downstream.assert_not_awaited()
    assert runtime["pending_tesla_restore"] == {"id": "owned"}


@pytest.mark.asyncio
async def test_completed_stop_does_not_report_success_for_superseded_restore(restore_prefix):
    handler, _, downstream, _, _ = restore_prefix(stop=True, superseded="after_stop")
    call = service_call("validation")
    call.data.update(_confirm_restore=True, _tesla_restore_generation=1)
    assert await handler(call) == {"success": False, "error": "restoration superseded"}
    downstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,error", [
    ("completed", None), ("write_error", "restore driver failed"),
    ("persist_initial", "restore state could not be persisted"),
    ("persist_completion", "restore completion could not be persisted"),
    ("superseded", "restoration superseded"), ("monitoring", "blocked by monitoring mode"),
])
async def test_full_tesla_restore_returns_completed_or_failed_confirmed_workflow(outcome, error):
    from test_tesla_local_settings import restore_context
    namespace, runtime, _, call = restore_context()
    original_active = namespace["force_discharge_state"]["active"]
    if outcome == "write_error":
        namespace["_tesla_force_apply_operation_mode"].side_effect = RuntimeError(error)
    elif outcome == "persist_initial":
        namespace["persist_force_mode_state"] = AsyncMock(return_value=False)
    elif outcome == "persist_completion":
        update_store = namespace["_update_powerwall_store"]
        async def fail_completion(store, updates, **kwargs):
            if updates.get("force_mode_state", "absent") is None:
                return False
            return await update_store(store, updates, **kwargs)
        namespace["_update_powerwall_store"] = fail_completion
    elif outcome == "superseded":
        async def superseded(*args, **kwargs):
            namespace["_command_generation"][0] += 1
            return {"confirmed_sites": ["site"], "failed_sites": []}
        namespace["_tesla_force_apply_operation_mode"].side_effect = superseded
    elif outcome == "monitoring":
        namespace["_monitoring_mode_should_block_control"] = lambda call: True
    response = await namespace["handle_restore_normal"](call)
    assert response == {"success": error is None, "error": error}
    assert response.get("physical_effect_confirmed") is not True
    if error is not None:
        assert namespace["force_discharge_state"]["active"] is original_active
    if outcome in ("persist_initial", "monitoring"):
        namespace["_tesla_force_apply_operation_mode"].assert_not_awaited()
    if outcome == "completed":
        assert runtime["pending_tesla_restore"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,error", [
    ("completed", None), ("missing_reserve", "saved backup reserve unavailable"),
    ("missing_site", "Tesla site configuration unavailable"),
    ("unverified", "saved reserve did not verify"), ("superseded", "restoration superseded"),
    ("persist_completion", "restore state could not be persisted"),
])
async def test_tesla_hold_cleanup_confirmed_response_does_not_invent_completion(outcome, error):
    from test_tesla_local_settings import restore_context
    namespace, _, _, call = restore_context()
    for key in ("force_charge_state", "force_discharge_state"):
        namespace[key].update(active=False, saved_tariff=None, saved_operation_mode=None,
                              saved_grid_charging_enabled=None, saved_export_rule=None, saved_states={})
    hold = namespace["hold_soc_state"]
    hold.update(active=True, brand="tesla", saved_backup_reserve=None if outcome == "missing_reserve" else 30)
    namespace.update(
        _disabled_optimizer_backup_reserve_target=lambda entry: (None, "unavailable"),
        _clear_hold_soc_state=lambda: hold.update(active=False),
        _normalize_tesla_backup_reserve_percent=int,
        _tesla_force_pulse_backup_reserve=AsyncMock(return_value={"confirmed_sites": ["site"], "failed_sites": []}),
        _tesla_force_confirm_backup_reserve=AsyncMock(return_value=True),
    )
    if outcome == "missing_site":
        namespace["_get_tesla_site_configs"] = lambda *args: []
    elif outcome == "unverified":
        namespace["_tesla_force_pulse_backup_reserve"].return_value = {"confirmed_sites": [], "failed_sites": ["site"]}
    elif outcome == "superseded":
        async def superseded(*args, **kwargs):
            namespace["_tesla_reserve_generation"][0] += 1
            return {"confirmed_sites": ["site"], "failed_sites": []}
        namespace["_tesla_force_pulse_backup_reserve"].side_effect = superseded
    elif outcome == "persist_completion":
        persist = namespace["persist_force_mode_state"]
        async def fail_completion():
            return await persist() if hold["active"] else False
        namespace["persist_force_mode_state"] = fail_completion
    assert await namespace["handle_restore_normal"](call) == {"success": error is None, "error": error}
    if outcome in ("missing_reserve", "missing_site", "unverified"):
        assert hold["active"] is True
        namespace["async_track_point_in_utc_time"].assert_called_once()
