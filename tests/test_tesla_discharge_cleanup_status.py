"""Ticket #439: failed discharge cleanup must not claim confirmed battery flow."""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def function(path, name):
    tree = ast.parse((ROOT / path).read_text())
    return copy.deepcopy(next(n for n in ast.walk(tree)
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and n.name == name))


def compile_nodes(nodes, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__", level=0,
                            names=[ast.alias(name="annotations")]), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<ticket-439>", "exec"), namespace)


def sensor_attributes(state, monkeypatch):
    node = next(
        copy.deepcopy(n) for c in ast.walk(ast.parse((ROOT / "sensor.py").read_text()))
        if isinstance(c, ast.ClassDef) and c.name == "BatteryModeSensor"
        for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "extra_state_attributes"
    )
    node.decorator_list = []
    monkeypatch.setitem(sys.modules, "homeassistant.util", NS(dt=NS(
        utcnow=lambda: datetime(2026, 10, 20, 13, 40, tzinfo=timezone.utc))))
    namespace = {"DOMAIN": "power_sync", **{
        "BATTERY_MODE_STATE_" + mode.upper(): mode for mode in
        ("force_charge", "force_discharge", "hold_soc", "self_consumption")}}
    compile_nodes([node], namespace)
    subject = NS(hass=NS(data={"power_sync": {"entry": {"force_discharge_state": state}}}),
                 _entry=NS(entry_id="entry"), _get_current_mode=lambda: "force_discharge")
    return namespace["extra_state_attributes"](subject)


@pytest.mark.parametrize("restore_raises", [False, True])
def test_failed_tariff_cleanup_reports_pending_restore(restore_raises, monkeypatch):
    state, snapshots, calls, events = {}, [], [], []
    now = datetime(2026, 10, 20, 13, 40, tzinfo=timezone.utc)

    async def persist():
        snapshots.append(dict(state))

    async def restore(*args, **kwargs):
        calls.append((args, kwargs))
        if restore_raises:
            raise RuntimeError("provider unavailable")
        # restore_normal can return without raising while scheduling its retry.

    class ServiceCall:
        def __init__(self, _domain, _service, data, context=None):
            self.data = data
            self.context = context

    async def handle_restore_normal(call):
        calls.append((("power_sync", "restore_normal", call.data), {}))
        if restore_raises:
            raise RuntimeError("provider unavailable")

    namespace = dict(force_discharge_state=state, source="optimizer", call=NS(context=None),
                     hass=NS(data={}, services=NS(async_call=restore)),
                     entry=NS(entry_id="entry"), DOMAIN="power_sync",
                     SERVICE_RESTORE_NORMAL="restore_normal", dt_util=NS(utcnow=lambda: now),
                     timedelta=timedelta, persist_force_mode_state=persist,
                     ServiceCall=ServiceCall, handle_restore_normal=handle_restore_normal,
                     _tesla_force_retry_expiry=lambda _state, expiry, **_kw: expiry,
                     async_dispatcher_send=lambda *args: events.append(args),
                     _LOGGER=logging.getLogger(__name__))
    compile_nodes([function("__init__.py", "_cleanup_failed_tesla_force_discharge")], namespace)
    asyncio.run(namespace["_cleanup_failed_tesla_force_discharge"]("tariff upload failed"))
    assert state["active"] is True  # Required ownership for safe restore retries.
    assert state["commitment_eligible"] is False
    assert calls[0][0][:2] == ("power_sync", "restore_normal")
    assert calls[0][0][2]["_allow_monitoring_restore"] is True
    attributes = sensor_attributes(state, monkeypatch)
    assert attributes["command_status"] == "restore_pending"
    assert attributes["physical_effect"] == "unverified"
    assert "restor" in attributes["description"].lower()
    assert "being force discharged" not in attributes["description"].lower()
    assert snapshots[0]["command_status"] == "restore_pending"
    assert events[0][2]["command_status"] == "restore_pending"


def test_reported_projected_soc_blocks_export():
    node = function("optimization/coordinator.py", "_force_discharge_reaches_reserve")
    namespace = {}
    compile_nodes([node], namespace)
    subject = NS(_reserve_ratio=lambda value: value, _supports_target_export_power=lambda: True)
    assert namespace[node.name](subject, NS(soc=0.348), 0.365, 0.36) == (True, 0.348)
    assert namespace[node.name](subject, NS(soc=0.36), 0.365, 0.36) == (False, 0.36)


def test_cleanup_status_survives_storage_and_reload():
    state = dict(active=True, expires_at=None, saved_tariff=None,
                 saved_operation_mode="self_consumption", saved_backup_reserve=20,
                 saved_export_rule="battery_ok", command_status="restore_pending")
    stored = {}

    async def load():
        return {}

    async def save(value):
        stored.update(value)

    namespace = dict(store=NS(async_load=load, async_save=save),
                     force_charge_state={"active": False}, force_discharge_state=state,
                     hold_soc_state={"active": False}, self_consumption_state={"active": False},
                     _coerce_force_power_w=lambda value: value,
                     _select_restorable_tesla_tariff=lambda value: value,
                     datetime=datetime, hass=NS(data={}), DOMAIN="power_sync",
                     entry=NS(entry_id="entry"), tesla_grid_charging_preferences={},
                     _LOGGER=logging.getLogger(__name__))
    node = function("__init__.py", "persist_force_mode_state")
    compile_nodes([node], namespace)
    asyncio.run(namespace[node.name]())
    assert stored["force_mode_state"]["command_status"] == "restore_pending"
    setup = function("__init__.py", "async_setup_entry")
    seed = next(n for n in ast.walk(setup) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "persisted_force_state.get('mode') == 'charge'")
    reloaded = {}
    compile_nodes([seed], dict(persisted_force_state=stored["force_mode_state"],
                              force_discharge_state=reloaded, force_charge_state={}))
    assert reloaded["command_status"] == "restore_pending"


@pytest.mark.parametrize("restore_failed", [False, True])
def test_cleanup_status_clears_only_after_verified_restore(restore_failed):
    handler = function("__init__.py", "handle_restore_normal")
    body = next(n.body for n in ast.walk(handler) if isinstance(n, ast.Try)
                and any(isinstance(child, ast.If)
                        and ast.unparse(child.test) == "tesla_restore_failed"
                        for child in n.body))
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "tesla_restore_failed")
    end = next(i for i in range(start + 1, len(body)) if isinstance(body[i], ast.Assign)
               and ast.unparse(body[i].targets[0]) == "force_charge_state['active']")
    node = ast.AsyncFunctionDef(name="finish", decorator_list=[],
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=body[start:end])
    state = {"active": True, "command_status": "restore_pending"}

    async def persist(*_args, **_kwargs):
        pass

    namespace = dict(force_discharge_state=state, tesla_restore_failed=restore_failed,
                     confirm_restore=True, skip_backup_reserve_restore=False,
                     persist_pending_tesla_restore=persist, persist_force_mode_state=persist,
                     _schedule_tesla_restore_retry=lambda _reason: True)
    compile_nodes([node], namespace)
    asyncio.run(namespace["finish"]())
    assert state["active"] is restore_failed
    assert state.get("command_status") == ("restore_pending" if restore_failed else None)


def test_failed_discharge_service_result_reaches_optimizer():
    node = function("optimization/battery_controller.py", "force_discharge")
    node.decorator_list = []
    calls = []

    async def service(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": False, "error": "tariff upload did not verify"}

    namespace = dict(_LOGGER=logging.getLogger(__name__),
                     optimizer_service_data=lambda _self, data: data)
    compile_nodes([node], namespace)
    controller = NS(battery_system="tesla", hass=NS(services=NS(async_call=service)))
    assert asyncio.run(namespace[node.name](controller, duration_minutes=5)) is False
    assert calls[0][1]["return_response"] is True
