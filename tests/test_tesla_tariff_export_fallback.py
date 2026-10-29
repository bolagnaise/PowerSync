"""Tariff export acceptance and recovery without an export-rule restore dependency."""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_tesla_local_settings import (
    ROOT, export_confirmation_context, load_function, restore_context, settings_context,
)


def preparation(states, *, local_config=None, export_confirmed=True, grid_confirmed=True):
    transport = SimpleNamespace(read_config=AsyncMock(return_value=local_config))
    ns, runtime = settings_context(transport)
    state = {"saved_operation_mode": "self_consumption", "saved_export_rule": states["site"].get("saved_export_rule"),
             "saved_states": copy.deepcopy(states)}
    persisted, writes = [], []

    async def persist():
        persisted.append(copy.deepcopy(state))
        return True

    async def export(*args, **kwargs):
        assert persisted, "restore targets must be durable before enabling export"
        assert kwargs["require_readback"] is False
        writes.append(("export", args[1]))
        return export_confirmed

    async def grid(*args, **kwargs):
        writes.append(("grid", args[1]))
        return {"confirmed_sites": list(states) if grid_confirmed else []}

    async def mode(*args, **kwargs):
        writes.append(("mode", args[1]))
        return {"confirmed_sites": list(states)}

    ns.update(force_discharge_state=state, _command_generation=[1], _restore_gen=1,
              _tesla_charge_kick_generation=[1], _local_powerwall_runtime=runtime,
              was_already_force_discharging=False, _monitoring_mode_should_block_control=lambda call: False,
              network_guard=None, call=SimpleNamespace(data={}), source="user", duration=5,
              site_configs=[(site, "token", "fleet") for site in states],
              persist_force_mode_state=persist, _tesla_force_apply_export_rule=export,
              _tesla_force_apply_grid_charging=grid, _tesla_force_apply_operation_mode=mode,
              _tesla_force_result_all_confirmed=lambda result, sites: len(result["confirmed_sites"]) == len(sites),
              update_cached_export_rule=AsyncMock(), dt_util=SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)))
    permissions = {}
    exec((ROOT / "tesla_export_permissions.py").read_text(), permissions)
    ns["read_export_rule"] = permissions["read_export_rule"]
    load_function(ROOT / "__init__.py", "_capture_tesla_setting_baselines", ns, strip_imports=True)
    handler = next(n for n in ast.walk(ast.parse((ROOT / "__init__.py").read_text()))
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_force_discharge")
    body = next(n.body for n in handler.body if isinstance(n, ast.Try)
                and any(isinstance(c, ast.FunctionDef) and c.name == "_discharge_request_current" for c in n.body))
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef) and n.name == "_discharge_request_current")
    end = next(i for i, n in enumerate(body[start:], start) if isinstance(n, ast.If) and ast.unparse(n.test) == "not mode_success")
    fn = ast.AsyncFunctionDef(name="prepare", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=body[start:end], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "tariff preparation", "exec"), ns)
    return ns, state, persisted, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [{"response": {"result": True}}, {"response": {"code": 204, "message": "Updated"}}])
async def test_tariff_accepts_export_receipt_without_claiming_readback(monkeypatch, ack):
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, ack, {})
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation", require_readback=False)
    assert session.post.call_count == 1
    assert session.get.call_count == 0
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [{"response": {"result": False}}, {"response": {"code": 500, "message": "Error"}}, {}])
async def test_tariff_rejected_or_unknown_receipt_still_fails(monkeypatch, ack):
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, ack, {})
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation", require_readback=False)
    assert not session.get.called


@pytest.mark.parametrize("original", [None, "never", "pv_only", "battery_ok", "invalid"])
def test_tariff_preparation_marks_export_untracked_and_keeps_grid_confirmation(original):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": original}})
    asyncio.run(ns["prepare"]())
    saved = persisted[0]["saved_states"]["site"]
    assert saved["export_permission_untracked"] is True
    assert "saved_export_rule" not in saved
    assert state["saved_export_rule"] is None
    assert writes == [("export", "battery_ok"), ("grid", False), ("mode", "autonomous")]
    ns["update_cached_export_rule"].assert_not_awaited()


@pytest.mark.parametrize("export_confirmed,grid_confirmed,expected", [
    (False, True, [("export", "battery_ok")]),
    (True, False, [("export", "battery_ok"), ("grid", False)]),
])
def test_tariff_acceptance_does_not_bypass_rejection_or_grid_guard(export_confirmed, grid_confirmed, expected):
    ns, state, persisted, writes = preparation({"site": {}}, export_confirmed=export_confirmed, grid_confirmed=grid_confirmed)
    with pytest.raises(RuntimeError):
        asyncio.run(ns["prepare"]())
    assert writes == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provenance", ["new", "fallback_missing"])
async def test_retained_tariff_export_dependency_clears_after_other_restore_succeeds(provenance):
    ns, runtime, saved, call = restore_context(direction="discharge")
    basis = {"saved_export_rule": "battery_ok", "saved_tariff": {"name": "original"}}
    basis.update({"export_permission_untracked": True} if provenance == "new" else {"saved_export_rule_source": "fallback_missing"})
    state = ns["force_discharge_state"]
    state.update(command_status="restore_pending", saved_export_rule="battery_ok", saved_states={"site": basis})
    ns["_tesla_force_apply_export_rule"].return_value = False
    runtime["pending_tesla_restore"] = {"reason": "old export confirmation failed"}
    assert (await ns["handle_restore_normal"](call))["success"] is True
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()
    assert saved["force_mode_state"] is None and saved["pending_tesla_restore"] is None
    assert runtime["pending_tesla_restore"] is None and not state["active"]


@pytest.mark.asyncio
async def test_retained_untracked_export_still_preserves_failed_mode_restore():
    ns, runtime, saved, call = restore_context(direction="discharge")
    ns["force_discharge_state"].update(saved_states={"site": {"export_permission_untracked": True}})
    ns["_tesla_force_apply_operation_mode"].return_value = {"confirmed_sites": [], "failed_sites": ["site"]}
    assert (await ns["handle_restore_normal"](call))["success"] is False
    assert saved["force_mode_state"] and runtime["pending_tesla_restore"]
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_known_export_preference_keeps_strict_restoration():
    ns, runtime, saved, call = restore_context(direction="discharge")
    ns["force_discharge_state"].update(saved_states={"site": {"saved_export_rule": "pv_only", "saved_export_rule_source": "cloud"}})
    ns["_tesla_force_apply_export_rule"].return_value = False
    assert (await ns["handle_restore_normal"](call))["success"] is False
    assert ns["_tesla_force_apply_export_rule"].await_args.args[1] == "pv_only"
    assert runtime["pending_tesla_restore"]


@pytest.mark.asyncio
async def test_untracked_export_policy_survives_journal_and_startup_replay():
    from test_tesla_local_settings import hydrate_payload
    ns, runtime, saved, call = restore_context(direction="discharge")
    state = ns["force_discharge_state"]
    state.update(command_status="awaiting_grid_confirmation", saved_states={"site": {"export_permission_untracked": True}})
    assert await ns["persist_force_mode_state"]()
    import json
    payload = json.loads(json.dumps(saved["force_mode_state"]))
    await hydrate_payload(ns, payload)
    assert ns["force_discharge_state"]["saved_states"]["site"]["export_permission_untracked"] is True


@pytest.mark.asyncio
async def test_tariff_acceptance_still_requires_every_gateway(monkeypatch):
    from test_tesla_local_settings import HttpReply
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {}, {})
    session.post.side_effect = [HttpReply({"response": {"code": 204, "message": "Updated"}}),
                                HttpReply({"response": {"result": False}})]
    assert not await fn([("site", "token", "fleet"), ("second", "token", "fleet")],
                        "battery_ok", reason="force discharge export preparation", require_readback=False)
    assert session.post.call_count == 2 and session.get.call_count == 0


@pytest.mark.asyncio
async def test_obsolete_export_target_cannot_clear_an_uncertain_signed_submission():
    ns, runtime, saved, call = restore_context(direction="discharge")
    ns["force_discharge_state"].update(saved_states={"site": {"export_permission_untracked": True}})
    runtime["powerwall_settings_pending"] = {"id": "uncertain", "site_id": "site"}
    assert (await ns["handle_restore_normal"](call))["success"] is False
    assert saved["force_mode_state"] and runtime["pending_tesla_restore"]
    assert runtime["powerwall_settings_pending"]["id"] == "uncertain"
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 202, 204])
@pytest.mark.parametrize("strict", [False, True])
async def test_successful_http_status_with_explicit_export_receipt_is_accepted(monkeypatch, status, strict):
    fn, ns, runtime, session = export_confirmation_context(
        monkeypatch, {"response": {"code": 201, "message": "Updated"}},
        {"customer_preferred_export_rule": "battery_ok"}, status=status)
    result = {}
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                    require_readback=strict, submission_status=result)
    assert result["submitted"] is True and result["http_status"] == status
    assert session.get.call_count == int(strict)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_failed_export_keeps_http_status_and_reason_without_accepting_write(monkeypatch, status):
    fn, ns, runtime, session = export_confirmation_context(
        monkeypatch, {"response": {"result": False, "reason": "export permission denied"}}, {}, status=status)
    result = {}
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                        require_readback=False, submission_status=result)
    assert result["submitted"] is True and result["http_status"] == status
    assert result["reason"] == "export permission denied"
    assert session.get.call_count == 0


def test_tariff_service_error_preserves_the_export_failure_detail():
    ns, state, persisted, writes = preparation({"site": {}})

    async def rejected(*args, **kwargs):
        kwargs["submission_status"].update(submitted=True, http_status=403, reason="export permission denied")
        return False

    ns["_tesla_force_apply_export_rule"] = rejected
    with pytest.raises(RuntimeError, match="HTTP 403.*export permission denied"):
        asyncio.run(ns["prepare"]())


@pytest.mark.asyncio
async def test_tariff_fresh_matching_cloud_permission_needs_no_export_write(monkeypatch):
    fn, ns, runtime, session = export_confirmation_context(
        monkeypatch, {"response": {"result": False}}, {"customer_preferred_export_rule": "battery_ok"})
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                    require_readback=False, skip_matching=True)
    session.post.assert_not_called()
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_tariff_fresh_matching_local_permission_needs_no_export_write(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    transport = SimpleNamespace(read_config=AsyncMock(return_value={"site_info": {"customer_preferred_export_rule": "battery_ok"}}),
                                write_config=AsyncMock(return_value=True))
    ns, runtime = settings_context(transport)
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {}, {}, context=(ns, runtime))
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                    require_readback=False, skip_matching=True)
    transport.write_config.assert_not_awaited()
    session.post.assert_not_called()
    session.get.assert_not_called()
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "local"


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [{}, {"customer_preferred_export_rule": "pv_only"}])
async def test_tariff_missing_or_different_live_permission_still_requests_export(monkeypatch, readback):
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {"response": {"result": True}}, readback)
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                    require_readback=False, skip_matching=True)
    assert session.get.call_count == 1 and session.post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["local", "cloud"])
@pytest.mark.parametrize("change", ["generation", "policy"])
async def test_matching_permission_cannot_bypass_supersession_or_policy(monkeypatch, source, change):
    ns, runtime = settings_context()
    manager = SimpleNamespace(snapshot=SimpleNamespace(mode="off"))
    runtime["network_envelope_manager"] = manager
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {}, {}, context=(ns, runtime))

    async def changed_read(*args, **kwargs):
        if change == "generation":
            ns["_command_generation"][0] += 1
        else:
            manager.snapshot.mode = "active"
        return {"site_info": {"customer_preferred_export_rule": "battery_ok"}} if source == "local" else {"response": {"customer_preferred_export_rule": "battery_ok"}}

    if source == "local":
        transport = SimpleNamespace(read_config=AsyncMock(side_effect=changed_read), write_config=AsyncMock())
        ns["get_local_transport"] = lambda *args: transport
    else:
        session.get.return_value.json = AsyncMock(side_effect=changed_read)
    call = fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
              require_readback=False, skip_matching=True)
    if change == "generation":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert not await call
    session.post.assert_not_called()
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "unknown"


@pytest.mark.asyncio
async def test_unreadable_preflight_does_not_block_the_requested_export_write(monkeypatch):
    import aiohttp
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {"response": {"result": True}}, {})
    session.get.side_effect = aiohttp.ClientError("read unavailable")
    assert await fn([("site", "token", "fleet")], "battery_ok", reason="force discharge export preparation",
                    require_readback=False, skip_matching=True)
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_one_matching_gateway_does_not_skip_another_gateways_export_write(monkeypatch):
    from test_tesla_local_settings import HttpReply
    fn, ns, runtime, session = export_confirmation_context(monkeypatch, {"response": {"result": False}}, {})
    session.get.side_effect = [HttpReply({"response": {"customer_preferred_export_rule": "battery_ok"}}),
                               HttpReply({"response": {"customer_preferred_export_rule": "pv_only"}})]
    assert not await fn([("site", "token", "fleet"), ("second", "token", "fleet")], "battery_ok",
                        reason="force discharge export preparation", require_readback=False, skip_matching=True)
    assert session.post.call_count == 1
    assert "/second/" in session.post.call_args.args[0]
