"""Execute SolarEdge service branches to verify their response contract."""

from __future__ import annotations

import ast
import asyncio
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
    branch = next(
        node for node in handler.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_solaredge_local"
    )
    # Keep the entire backend branch, including failure handling and returns.
    wrapper = ast.parse("async def invoke(call): pass").body[0]
    wrapper.body = branch.body
    exec(  # noqa: S102 - Execute repository code without importing Home Assistant.
        compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(INIT_PATH), "exec"),
        namespace,
    )
    return namespace["invoke"]


def _context(method, outcome):
    events = []

    async def write(*args, **kwargs):
        events.append("write")
        return outcome

    async def persist():
        events.append("persist")

    async def guarded(callback):
        return await callback(2000)

    coordinator = SimpleNamespace(**{method: AsyncMock(side_effect=write)}, generation=7)
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
        "suppress_notification": True,
    }
    return namespace, coordinator, events


@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("outcome", [True, False])
def test_manual_force_response_requires_confirmed_write(direction, outcome):
    method = f"force_{direction}"
    namespace, coordinator, events = _context(method, outcome)
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
    coordinator.restore_normal.assert_awaited_once()
