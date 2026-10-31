"""An incomplete restore retries its original policy, not a new safe handoff."""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def retry_probe(monkeypatch, *, source="user", force_restore=False, owner="user",
                mode="autonomous", direction="discharge", retry_count=0):
    """Execute the real timer callback and downstream saved-mode selection."""
    tree = ast.parse((ROOT / "__init__.py").read_text())
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_restore_normal")
    schedule = next(node for node in ast.walk(handler)
                    if isinstance(node, ast.FunctionDef) and node.name == "_schedule_tesla_restore_retry")
    source_helper = next(node for node in ast.walk(tree)
                         if isinstance(node, ast.FunctionDef) and node.name == "_control_call_source")

    def assignment(name):
        return next(node for node in ast.walk(handler) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))

    policy = [assignment(name) for name in ("source", "force_restore", "optimizer_owned_restore")]
    mode_nodes = [assignment(name) for name in ("discharge_saved", "charge_saved", "restore_mode")]
    mode_guard = next(node for node in ast.walk(handler) if isinstance(node, ast.If)
                      and isinstance(node.test, ast.BoolOp)
                      and isinstance(node.test.values[0], ast.Name)
                      and node.test.values[0].id == "force_restore")
    select_mode = ast.FunctionDef(name="select_mode", args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg="call")], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=copy.deepcopy(policy + mode_nodes + [mode_guard]) + [ast.Return(ast.Name("restore_mode", ast.Load()))],
        decorator_list=[])
    package_name = "tesla_restore_retry_semantics_probe"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    routing = importlib.import_module(package_name + ".licensing.entry_routing")
    assert routing.entry_service_data("entry", {"source": "user"})["entry_id"] == "entry"

    state = {"active": True, "source": owner, "saved_operation_mode": "backup",
             "saved_states": {"site": {"saved_operation_mode": mode}}}
    charge = state if direction == "charge" else {"active": False}
    discharge = state if direction == "discharge" else {"active": False}
    command = [41]
    callbacks, dispatches, observed_modes = [], [], []
    original_data = {"source": source, "_force_restore": force_restore, "_restore_retry": retry_count,
        "_licensing_generation": 5, "_licensing_entry_generation": 8}
    call = NS(data=original_data, context=NS(user_id="original-user"))
    namespace = {"__package__": package_name, "ServiceCall": object, "_LOGGER": logging.getLogger(__name__),
        "call": call, "force_charge_state": charge, "force_discharge_state": discharge,
        "restore_retry_count": retry_count, "restore_was_force_charging": direction == "charge",
        "_restore_generation": command[0], "_command_generation": command,
        "skip_backup_reserve_restore": True, "site_configs": [("site", "test-token", "test-provider")],
        "site_id": "site", "DOMAIN": "power_sync", "SERVICE_RESTORE_NORMAL": "restore_normal",
        "entry": NS(entry_id="entry", data={}), "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "timedelta": timedelta, "dt_util": NS(utcnow=lambda: datetime(2026, 10, 31, tzinfo=timezone.utc)),
        "async_track_point_in_utc_time": lambda _hass, callback, when: callbacks.append((callback, when)) or (lambda: None)}

    async def service(domain, name, payload, **kwargs):
        dispatches.append((domain, name, payload, kwargs))
        # HA's delayed service invocation has a new context. The retry payload
        # must carry policy identity rather than relying on the old UI context.
        observed_modes.append(namespace["select_mode"](NS(data=payload, context=NS(user_id=None))))

    namespace["hass"] = NS(data={"power_sync_license_manager": object(),
        "power_sync": {"entry": {"pending_tesla_restore": {"reason": "grid readback pending"}}}},
        services=NS(async_call=service))
    module = ast.Module(body=[copy.deepcopy(source_helper), *copy.deepcopy(policy),
                             copy.deepcopy(schedule), select_mode], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<actual Tesla retry policy>", "exec"), namespace)
    return NS(namespace=namespace, callbacks=callbacks, dispatches=dispatches,
              modes=observed_modes, command=command, state=state, call=call)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("source,force_restore,owner,mode,expected", [
    pytest.param("user", False, "user", "autonomous", "autonomous", id="manual-savings"),
    pytest.param("manual", False, "user", "backup", "backup", id="manual-backup"),
    pytest.param("force_timer", False, "user", "autonomous", "autonomous", id="manual-expiry"),
    pytest.param("optimizer", False, "user", "autonomous", "self_consumption", id="optimizer-source"),
    pytest.param("user", False, "optimizer", "autonomous", "self_consumption", id="optimizer-owner"),
    pytest.param("startup_pending_restore", True, "user", "autonomous", "self_consumption", id="startup-forced"),
    pytest.param("user", True, "user", "autonomous", "self_consumption", id="explicit-forced"),
])
async def test_actual_restore_retry_preserves_original_source_and_mode_policy(
        monkeypatch, direction, source, force_restore, owner, mode, expected):
    p = retry_probe(monkeypatch, source=source, force_restore=force_restore,
                    owner=owner, mode=mode, direction=direction)
    assert p.namespace["_schedule_tesla_restore_retry"]("grid readback pending") is True
    callback, when = p.callbacks[0]
    assert when == datetime(2026, 10, 31, tzinfo=timezone.utc) + timedelta(seconds=60)
    await callback(when)
    assert p.modes == [expected]
    domain, service, payload, kwargs = p.dispatches[0]
    assert (domain, service, kwargs) == ("power_sync", "restore_normal", {"blocking": True})
    assert payload["source"] == source and payload["_force_restore"] is force_restore
    assert payload["entry_id"] == "entry"
    assert payload["_restore_retry"] == 1 and payload["_tesla_restore_generation"] == 41
    assert payload["_allow_monitoring_restore"] is True
    assert payload["_skip_backup_reserve_restore"] is True
    if source == "optimizer":
        assert payload["_licensing_generation"] == 5 and payload["_licensing_entry_generation"] == 8
    assert p.state["active"] is True


@pytest.mark.asyncio
async def test_second_manual_retry_does_not_upgrade_to_forced_handoff(monkeypatch):
    first = retry_probe(monkeypatch)
    first.namespace["_schedule_tesla_restore_retry"]("first delayed readback")
    await first.callbacks[0][0](first.callbacks[0][1])
    payload = first.dispatches[0][2]
    second = retry_probe(monkeypatch, source=payload["source"],
                         force_restore=payload["_force_restore"], retry_count=payload["_restore_retry"])
    second.namespace["_schedule_tesla_restore_retry"]("second delayed readback")
    await second.callbacks[0][0](second.callbacks[0][1])
    assert first.modes == second.modes == ["autonomous"]
    assert second.dispatches[0][2]["_restore_retry"] == 2


@pytest.mark.asyncio
async def test_retry_policy_fix_keeps_generation_rejection_and_pending_owner(monkeypatch):
    p = retry_probe(monkeypatch)
    p.namespace["_schedule_tesla_restore_retry"]("pending restore")
    p.command[0] += 1
    await p.callbacks[0][0](p.callbacks[0][1])
    assert p.dispatches == [] and p.modes == [] and p.state["active"] is True


def test_retry_policy_fix_keeps_existing_retry_budget(monkeypatch):
    p = retry_probe(monkeypatch, retry_count=3)
    assert p.namespace["_schedule_tesla_restore_retry"]("still pending") is False
    assert p.callbacks == [] and p.state["active"] is True
