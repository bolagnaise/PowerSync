"""Tariff discharge explicitly enables battery export when Tesla omits its rule."""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_tesla_local_settings import ROOT, load_function, restore_context, settings_context


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


@pytest.mark.parametrize("status", ["absent", "null", "unavailable"])
def test_tariff_missing_original_rule_uses_explicit_durable_battery_export_fallback(status):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": None, "saved_export_rule_status": status}})
    asyncio.run(ns["prepare"]())
    saved = persisted[0]["saved_states"]["site"]
    assert saved["saved_export_rule"] == "battery_ok"
    assert saved["saved_export_rule_source"] == "fallback_missing"
    assert state["saved_export_rule"] == "battery_ok"
    assert writes == [("export", "battery_ok"), ("grid", False), ("mode", "autonomous")]


@pytest.mark.parametrize("rule", ["never", "pv_only", "battery_ok"])
def test_tariff_preserves_observed_original_rule(rule):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": rule, "saved_export_rule_source": "cloud"}})
    asyncio.run(ns["prepare"]())
    assert persisted[0]["saved_states"]["site"]["saved_export_rule"] == rule
    assert persisted[0]["saved_states"]["site"]["saved_export_rule_source"] == "cloud"
    assert writes[0] == ("export", "battery_ok")


@pytest.mark.parametrize("status", ["invalid", "conflict"])
def test_tariff_rejects_malformed_or_conflicting_original_without_writes(status):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": None, "saved_export_rule_status": status}})
    with pytest.raises(RuntimeError, match="original Tesla export permission"):
        asyncio.run(ns["prepare"]())
    assert not persisted and not writes


def test_tariff_missing_secondary_rule_does_not_inherit_primary_permission():
    ns, state, persisted, writes = preparation({
        "site": {"saved_export_rule": "never", "saved_export_rule_source": "cloud"},
        "second": {"saved_export_rule": None, "saved_export_rule_status": "absent"}})
    asyncio.run(ns["prepare"]())
    saved = persisted[0]["saved_states"]
    assert saved["site"]["saved_export_rule"] == "never"
    assert saved["second"]["saved_export_rule"] == "battery_ok"
    assert saved["second"]["saved_export_rule_source"] == "fallback_missing"


@pytest.mark.parametrize("local_rule", ["never", "pv_only", "battery_ok"])
def test_tariff_prefers_real_local_rule_over_missing_cloud(local_rule):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": None, "saved_export_rule_status": "absent"}},
                                               local_config={"site_info": {"customer_preferred_export_rule": local_rule}})
    asyncio.run(ns["prepare"]())
    assert persisted[0]["saved_states"]["site"]["saved_export_rule"] == local_rule
    assert persisted[0]["saved_states"]["site"]["saved_export_rule_source"] == "local"


@pytest.mark.parametrize("export_confirmed,grid_confirmed,expected", [
    (False, True, [("export", "battery_ok")]),
    (True, False, [("export", "battery_ok"), ("grid", False)]),
])
def test_tariff_fallback_keeps_post_write_confirmation_gates(export_confirmed, grid_confirmed, expected):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": None, "saved_export_rule_status": "absent"}},
                                               export_confirmed=export_confirmed, grid_confirmed=grid_confirmed)
    with pytest.raises(RuntimeError):
        asyncio.run(ns["prepare"]())
    assert writes == expected
    assert persisted[0]["saved_states"]["site"]["saved_export_rule_source"] == "fallback_missing"


@pytest.mark.parametrize("local_rule", ["invalid", True, {}])
def test_tariff_malformed_local_rule_is_not_treated_as_missing(local_rule):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": None, "saved_export_rule_status": "absent"}},
                                               local_config={"site_info": {"customer_preferred_export_rule": local_rule}})
    with pytest.raises(RuntimeError, match="original Tesla export permission"):
        asyncio.run(ns["prepare"]())
    assert not persisted and not writes


@pytest.mark.parametrize("rule", [None, "pv_only"])
def test_tariff_legacy_active_session_without_per_site_state_can_be_extended(rule):
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": rule}})
    ns["was_already_force_discharging"] = True
    state["saved_states"] = {}
    asyncio.run(ns["prepare"]())
    assert persisted[0]["saved_states"]["site"]["saved_export_rule"] == (rule or "battery_ok")
    assert writes[0] == ("export", "battery_ok")


def test_tariff_legacy_multi_site_primary_original_remains_exact():
    ns, state, persisted, writes = preparation({"site": {"saved_export_rule": "never"}, "second": {}})
    ns["was_already_force_discharging"] = True
    state["saved_states"] = {}
    asyncio.run(ns["prepare"]())
    states = persisted[0]["saved_states"]
    assert states["site"]["saved_export_rule"] == "never"
    assert states["second"]["saved_export_rule"] == "battery_ok"
    assert states["second"]["saved_export_rule_source"] == "fallback_missing"


@pytest.mark.asyncio
async def test_tariff_fallback_provenance_survives_real_journal_save():
    ns, runtime, saved, call = restore_context(direction="discharge")
    state = ns["force_discharge_state"]
    state.update(command_status="awaiting_grid_confirmation", saved_export_rule="battery_ok",
                 saved_states={"site": {"saved_export_rule": "battery_ok", "saved_export_rule_source": "fallback_missing"}})
    assert await ns["persist_force_mode_state"]()
    journal = saved["force_mode_state"]
    assert journal["saved_states"]["site"]["saved_export_rule_source"] == "fallback_missing"
    # The durable JSON is the only state a restarted process can use.
    import json
    replay = json.loads(json.dumps(journal))
    assert replay["saved_states"]["site"]["saved_export_rule"] == "battery_ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_tariff_restore_uses_persisted_fallback_and_retains_failed_restore(confirmed):
    ns, runtime, saved, call = restore_context(direction="discharge")
    state = ns["force_discharge_state"]
    state.update(saved_export_rule="battery_ok", saved_states={"site": {
        "saved_export_rule": "battery_ok", "saved_export_rule_source": "fallback_missing"}})
    ns["_tesla_force_apply_export_rule"].return_value = confirmed
    result = await ns["handle_restore_normal"](call)
    export = ns["_tesla_force_apply_export_rule"]
    assert export.await_args.args[1] == "battery_ok"
    assert result["success"] is confirmed
    if confirmed:
        assert saved["force_mode_state"] is None
    else:
        assert saved["force_mode_state"]["saved_states"]["site"]["saved_export_rule_source"] == "fallback_missing"
        assert runtime["pending_tesla_restore"]
