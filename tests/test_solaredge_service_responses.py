"""Execute SolarEdge service branches to verify their response contract."""

from __future__ import annotations

import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

INIT_PATH = Path(__file__).resolve().parents[1] / "custom_components/power_sync/__init__.py"


@lru_cache(maxsize=1)
def _setup_node():
    return next(
        node for node in ast.parse(INIT_PATH.read_text()).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )


def _load_manual_branch(service, namespace):
    handler = next(node for node in _setup_node().body if getattr(node, "name", None) == service)
    branch = [
        node for node in handler.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_solaredge_local"
    ][-1]
    # Keep the entire backend branch, including failure handling and returns.
    wrapper = ast.parse("async def invoke(call): pass").body[0]
    wrapper.body = branch.body
    exec(  # noqa: S102 - Execute repository code without importing Home Assistant.
        compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(INIT_PATH), "exec"),
        namespace,
    )
    return namespace["invoke"]


def _load_setup_helper(name, namespace):
    node = next(
        node for node in _setup_node().body if getattr(node, "name", None) == name
    )
    exec(  # noqa: S102 - Execute repository code without importing Home Assistant.
        compile(
            ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            str(INIT_PATH),
            "exec",
        ),
        namespace,
    )
    return namespace[name]


def _load_solaredge_release_preflight(service, namespace):
    """Run the actual pre-arm SolarEdge gate without importing Home Assistant."""
    handler = next(node for node in _setup_node().body if getattr(node, "name", None) == service)
    cancel_index = next(
        index
        for index, node in enumerate(handler.body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_cancel_all_force_timers"
    )
    preflight_index = next(
        index
        for index, node in enumerate(handler.body[:cancel_index])
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "is_solaredge_local"
            for target in node.targets
        )
    )
    wrapper = ast.parse("async def invoke(): pass").body[0]
    wrapper.body = copy.deepcopy(handler.body[preflight_index:cancel_index])
    exec(  # noqa: S102 - Execute repository code without importing Home Assistant.
        compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(INIT_PATH), "exec"),
        namespace,
    )
    return namespace["invoke"], handler, preflight_index, cancel_index


def _context(method, outcome):
    events = []

    async def write(*args, **kwargs):
        events.append("write")
        return outcome

    async def persist():
        events.append("persist")

    async def guarded(callback):
        return await callback(2000)

    coordinator = SimpleNamespace(
        **{method: AsyncMock(side_effect=write)}, generation=7, intent_generation=7
    )
    data = {"solaredge_coordinator": coordinator}
    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": data}},
            async_create_task=lambda coroutine: coroutine.close(),
        ),
        "entry": SimpleNamespace(entry_id="entry"),
        "DOMAIN": "power_sync",
        "source": "user",
        "duration": 15,
        "command_power_w": 2000,
        "force_charge_state": {"active": False},
        "force_discharge_state": {"active": False},
        "self_consumption_state": {"active": False},
        "hold_soc_state": {"active": False},
        "_clear_self_consumption_state": Mock(),
        "_clear_hold_soc_state": Mock(),
        "_restore_solaredge_curtailment_for_dispatch": AsyncMock(return_value=True),
        "_guarded_force_discharge_write": guarded,
        "_LOGGER": Mock(),
        "HomeAssistantError": RuntimeError,
        "_notify_api_error": AsyncMock(),
        "async_track_point_in_utc_time": Mock(return_value=Mock()),
        "async_dispatcher_send": Mock(),
        "persist_force_mode_state": AsyncMock(side_effect=persist),
        "dt_util": SimpleNamespace(utcnow=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc)),
        "timedelta": timedelta,
        "_restore_superseded": Mock(return_value=False),
        "_cancel_all_force_timers": Mock(),
        "_command_generation": [0],
        "suppress_notification": True,
    }
    return namespace, coordinator, events


@pytest.mark.parametrize("direction", ["charge", "discharge"])
def test_solaredge_failed_transition_preserves_previous_force_lifecycle(direction):
    """A rejected replacement command cannot remove the active cleanup timer."""
    prior_timer = Mock()
    previous_state = {
        "active": True,
        "expires_at": "existing-expiry",
        "cancel_expiry_timer": prior_timer,
        "duration": 30,
    }
    primary_state = {"active": False}
    cancel_timers = Mock()
    persist = AsyncMock()
    namespace = {
        "hass": SimpleNamespace(),
        "DOMAIN": "power_sync",
        "SERVICE_RESTORE_NORMAL": "restore_normal",
        "force_charge_state": primary_state if direction == "charge" else previous_state,
        "force_discharge_state": primary_state if direction == "discharge" else previous_state,
        "self_consumption_state": {"active": False},
        "_clear_self_consumption_state": Mock(),
        "_cancel_all_force_timers": cancel_timers,
        "_command_generation": [7],
        "dt_util": SimpleNamespace(
            utcnow=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc)
        ),
        "timedelta": timedelta,
        "_LOGGER": Mock(),
        "HomeAssistantError": RuntimeError,
        "async_dispatcher_send": Mock(),
        "async_track_point_in_utc_time": Mock(return_value=Mock()),
        "persist_force_mode_state": persist,
    }
    helper = _load_setup_helper("_commit_solaredge_force_transition", namespace)
    writer = AsyncMock(return_value=False)
    coordinator = SimpleNamespace(intent_generation=11)

    with pytest.raises(RuntimeError, match=f"force {direction} was not confirmed"):
        asyncio.run(helper(direction, 15, 2000, "user", coordinator, writer))

    assert previous_state == {
        "active": True,
        "expires_at": "existing-expiry",
        "cancel_expiry_timer": prior_timer,
        "duration": 30,
    }
    assert primary_state == {"active": False}
    cancel_timers.assert_not_called()
    prior_timer.assert_not_called()
    persist.assert_not_awaited()
    namespace["async_dispatcher_send"].assert_not_called()
    writer.assert_awaited_once()


@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("outcome", [True, False])
def test_manual_force_response_requires_confirmed_write(direction, outcome):
    method = f"force_{direction}"
    namespace, coordinator, events = _context(method, outcome)
    _load_setup_helper("_commit_solaredge_force_transition", namespace)
    invoke = _load_manual_branch(f"handle_{method}", namespace)
    try:
        result = asyncio.run(invoke(SimpleNamespace(data={})))
    except RuntimeError as error:
        # Later guarded control changes may propagate rejected writes as errors.
        assert not outcome
        assert "SolarEdge" in str(error)
        result = None
    getattr(coordinator, method).assert_awaited_once()
    assert getattr(coordinator, method).call_args.args == (15,)
    assert getattr(coordinator, method).call_args.kwargs["power_w"] == 2000
    state = namespace[f"{method}_state"]
    if outcome:
        assert result == {"success": True}
        assert state["active"] is True
        assert state["duration"] == 15
        namespace["async_track_point_in_utc_time"].assert_called_once()
        namespace["persist_force_mode_state"].assert_awaited_once()
        assert events == ["write", "persist"]
    else:
        assert result != {"success": True}
        assert state["active"] is False
        namespace["async_track_point_in_utc_time"].assert_not_called()
        namespace["persist_force_mode_state"].assert_not_awaited()


def test_solaredge_force_discharge_persistence_failure_keeps_confirmed_state_active():
    namespace, coordinator, _events = _context("force_discharge", True)
    _load_setup_helper("_commit_solaredge_force_transition", namespace)
    namespace["persist_force_mode_state"] = AsyncMock(
        side_effect=OSError("storage unavailable")
    )
    invoke = _load_manual_branch("handle_force_discharge", namespace)

    result = asyncio.run(invoke(SimpleNamespace(data={})))

    assert result["success"] is True
    assert "restart state" in result["warning"]
    assert namespace["force_discharge_state"]["active"] is True
    namespace["async_track_point_in_utc_time"].assert_called_once()
    coordinator.force_discharge.assert_awaited_once()


def test_solaredge_force_charge_persistence_failure_keeps_confirmed_state_active():
    namespace, coordinator, _events = _context("force_charge", True)
    _load_setup_helper("_commit_solaredge_force_transition", namespace)
    namespace["persist_force_mode_state"] = AsyncMock(
        side_effect=OSError("storage unavailable")
    )
    invoke = _load_manual_branch("handle_force_charge", namespace)

    result = asyncio.run(invoke(SimpleNamespace(data={})))

    assert result["success"] is True
    assert "restart state" in result["warning"]
    assert namespace["force_charge_state"]["active"] is True
    namespace["async_track_point_in_utc_time"].assert_called_once()
    namespace["async_dispatcher_send"].assert_called_once()
    coordinator.force_charge.assert_awaited_once()


@pytest.mark.parametrize("direction", ["charge", "discharge"])
def test_rejected_solaredge_release_preserves_existing_force_lifecycle(direction):
    """A pre-hardware release rejection must not cancel or replace a force timer."""
    service = f"handle_force_{direction}"
    state_key = f"force_{direction}_state"
    prior_timer = Mock()
    state = {
        "active": True,
        "expires_at": "existing-expiry",
        "cancel_expiry_timer": prior_timer,
        "duration": 30,
    }
    release = AsyncMock(return_value=False)
    coordinator = SimpleNamespace(
        force_charge=AsyncMock(), force_discharge=AsyncMock()
    )

    async def guarded(callback):
        return await callback(2000)

    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": {"solaredge_coordinator": coordinator}}}
        ),
        "entry": SimpleNamespace(
            entry_id="entry", data={"battery_system": "solaredge"}
        ),
        "DOMAIN": "power_sync",
        "CONF_BATTERY_SYSTEM": "battery_system",
        "CONF_SOLAREDGE_HOST": "solaredge_host",
        "CONF_SOLAREDGE_ENTITY_PREFIX": "solaredge_entity_prefix",
        "BATTERY_SYSTEM_SOLAREDGE": "solaredge",
        "force_charge_state": state if direction == "charge" else {"active": False},
        "force_discharge_state": state if direction == "discharge" else {"active": False},
        "_restore_solaredge_curtailment_for_dispatch": release,
        "_guarded_force_discharge_write": guarded,
        "_LOGGER": Mock(),
        "HomeAssistantError": RuntimeError,
    }
    invoke, handler, preflight_index, cancel_index = _load_solaredge_release_preflight(
        service, namespace
    )

    with pytest.raises(RuntimeError, match="curtailment release"):
        asyncio.run(invoke())

    # The extracted gate is from the real handler and precedes the destructive
    # lifecycle work in that handler, rather than testing a look-alike helper.
    assert preflight_index < cancel_index
    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == state_key
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "active"
            for target in node.targets
        )
        for node in handler.body[cancel_index + 1 :]
    )
    assert state == {
        "active": True,
        "expires_at": "existing-expiry",
        "cancel_expiry_timer": prior_timer,
        "duration": 30,
    }
    prior_timer.assert_not_called()
    release.assert_awaited_once()
    coordinator.force_charge.assert_not_awaited()
    coordinator.force_discharge.assert_not_awaited()


@pytest.mark.parametrize("outcome", [True, False])
def test_manual_restore_response_requires_confirmed_write(outcome):
    namespace, coordinator, events = _context("restore_normal", outcome)
    namespace["force_charge_state"]["active"] = True
    namespace["force_discharge_state"]["active"] = True
    invoke = _load_manual_branch("handle_restore_normal", namespace)
    if outcome:
        assert asyncio.run(invoke(SimpleNamespace(data={}))) == {"success": True}
        assert namespace["force_charge_state"]["active"] is False
        assert namespace["force_discharge_state"]["active"] is False
        namespace["persist_force_mode_state"].assert_awaited_once()
        assert events == ["write", "persist"]
    else:
        with pytest.raises(RuntimeError, match="SolarEdge"):
            asyncio.run(invoke(SimpleNamespace(data={})))
        assert namespace["force_charge_state"]["active"] is True
        assert namespace["force_discharge_state"]["active"] is True
        namespace["persist_force_mode_state"].assert_not_awaited()
        namespace["async_dispatcher_send"].assert_not_called()
    if outcome:
        namespace["_cancel_all_force_timers"].assert_called_once_with(
            "confirmed SolarEdge restore_normal"
        )
    else:
        namespace["_cancel_all_force_timers"].assert_not_called()
    coordinator.restore_normal.assert_awaited_once()


def _reconcile_service(result):
    coordinator = SimpleNamespace(reconcile_result=AsyncMock(return_value=result))
    namespace = {
        "ServiceCall": SimpleNamespace,
        "HomeAssistantError": RuntimeError,
        "DOMAIN": "power_sync",
        "hass": SimpleNamespace(data={"power_sync": {"entry": {"solaredge_coordinator": coordinator}}}),
    }
    return _load_setup_helper("handle_reconcile_solaredge_control", namespace), coordinator


@pytest.mark.parametrize("reason", [
    "readback_unavailable", "readback_not_fresh", "identity_mismatch",
    "controller_write_busy", "upstream_write_busy", "malformed_snapshot",
    "active_command", "unsupported_storage_mode", "baseline_mismatch", "persistence_failed",
])
def test_reconcile_service_reports_safe_rejection(reason):
    result = {"success": False, "control_health": "reconciliation_required", "reason": reason}
    if reason == "baseline_mismatch":
        result["fields"] = ["backup_reserve"]
    invoke, coordinator = _reconcile_service(result)
    call = SimpleNamespace(data={"entry_id": "entry", "acknowledge": True})
    with pytest.raises(RuntimeError, match=reason) as failure:
        asyncio.run(invoke(call))
    if reason == "baseline_mismatch":
        assert "backup_reserve" in str(failure.value)
    coordinator.reconcile_result.assert_awaited_once_with()


def test_reconcile_service_returns_confirmation_source():
    result = {"success": True, "control_health": "ready", "reason": "native_self_consumption", "confirmation_source": "fresh_upstream_storage_poll"}
    invoke, coordinator = _reconcile_service(result)
    assert asyncio.run(invoke(SimpleNamespace(data={"entry_id": "entry", "acknowledge": True}))) == result
    coordinator.reconcile_result.assert_awaited_once_with()


@pytest.mark.parametrize("acknowledge", [None, False, "true", 1])
def test_reconcile_service_requires_explicit_acknowledgement(acknowledge):
    invoke, coordinator = _reconcile_service({})
    with pytest.raises(RuntimeError, match="acknowledge: true"):
        asyncio.run(invoke(SimpleNamespace(data={"entry_id": "entry", "acknowledge": acknowledge})))
    coordinator.reconcile_result.assert_not_awaited()


def test_reconcile_service_rejects_missing_entry():
    invoke, coordinator = _reconcile_service({})
    with pytest.raises(RuntimeError, match="Select a PowerSync entry"):
        asyncio.run(invoke(SimpleNamespace(data={"entry_id": "missing", "acknowledge": True})))
    coordinator.reconcile_result.assert_not_awaited()


@pytest.mark.parametrize("reason", [
    "readback_unavailable", "readback_not_fresh", "identity_mismatch",
    "controller_write_busy", "upstream_write_busy", "malformed_snapshot",
    "active_command", "unsupported_storage_mode", "baseline_mismatch", "persistence_failed",
])
def test_reconcile_service_returns_failure_to_response_clients(reason):
    result = {"success": False, "control_health": "reconciliation_required", "reason": reason}
    if reason == "baseline_mismatch":
        result["fields"] = ["backup_reserve"]
    invoke, coordinator = _reconcile_service(result)
    call = SimpleNamespace(data={"entry_id": "entry", "acknowledge": True}, return_response=True)
    assert asyncio.run(invoke(call)) == result
    coordinator.reconcile_result.assert_awaited_once_with()
