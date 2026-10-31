"""Max Backup and force dispatch keep mutually exclusive site ownership."""
from __future__ import annotations

import ast
from copy import deepcopy
import asyncio
import importlib
from inspect import signature
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import load_power_sync_function
from test_native_rate_admission import admission
from test_powerwall_battery_dispatch import call, load_routing
from test_service_calls import CurrentServiceCall


ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def elapse(monkeypatch, seconds=60):
    import time
    advanced = time.monotonic() + seconds
    monkeypatch.setattr(time, "monotonic", lambda: advanced)


@pytest.fixture
def overlap(admission, request):
    ns, controller, client, saved = load_routing(tesla_control_method="local")
    entry, hass, runtime = ns["entry"], ns["hass"], ns["_local_powerwall_runtime"]
    entry.data["tesla_energy_site_id"] = getattr(request, "param", "site")
    core = importlib.import_module(f"{admission.m.__package__}.powerwall_control")
    site = core.coordinator_for(hass, entry)
    ns["_local_powerwall_route_lock"] = site.route_lock
    runtime.update(powerwall_site_control=site, force_charge_state=ns["force_charge_state"],
                   force_discharge_state=ns["force_discharge_state"], hold_soc_state=ns["hold_soc_state"],
                   self_consumption_state=ns["self_consumption_state"])
    ns["make_service_call"] = load_power_sync_function("make_service_call",
        {"signature": signature, "ServiceCall": CurrentServiceCall}, path=ROOT / "service_calls.py")
    tree = ast.parse((ROOT / "__init__.py").read_text())
    functions = [deepcopy(next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == name)) for name in
                 ("_max_backup_site_current", "_max_backup_restore", "_restore_max_backup_window", "handle_schedule_max_backup",
                  "_resume_max_backup_if_persisted")]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)
    functions = [StripImports().visit(node) for node in functions]
    mutations, reserves, queued = [], [], asyncio.Event()
    durable = NS(value=None, fail_clear=False)
    reserve_result = NS(value={"success": True, "error": None}, error=None)
    async def native_event(seconds, *, end_ts=None):
        async def submit():
            mutations.append(("backup", seconds))
            return True
        queued.set()
        return await site.run_write("test native backup boundary", submit, entry_id=entry.entry_id)
    client.schedule_max_backup = AsyncMock(side_effect=native_event)
    client.cancel_max_backup = AsyncMock(return_value=True)
    async def local_power(watts, duration):
        async def submit():
            mutations.append(("power", watts))
            return True
        queued.set()
        return await site.run_write("test local power boundary", submit, entry_id=entry.entry_id)
    client.dispatch_battery_power = AsyncMock(side_effect=local_power)
    async def reserve_handler(request):
        async with site.write_context("test reserve boundary", entry_id=entry.entry_id):
            reserves.append(request)
            if reserve_result.error is not None:
                raise reserve_result.error
            return reserve_result.value
    reserve_handler.__name__ = "handle_set_backup_reserve"
    async def persist_schedule(payload, *, is_current=None):
        if (is_current is not None and not is_current()) or (payload is None and durable.fail_clear):
            return False
        durable.value = deepcopy(payload)
        return True
    ns.update(
        async_call_later=Mock(side_effect=lambda *_args: Mock()),
        _persist_max_backup_schedule=AsyncMock(side_effect=persist_schedule),
        _get_tesla_coordinator_for_service=lambda _name: NS(_site_info_cache={"backup_reserve_percent": 2}),
        canonical_site_id=core.canonical_site_id,
        CONF_TESLA_ENERGY_SITE_ID="tesla_energy_site_id",
        SERVICE_SET_BACKUP_RESERVE="set_backup_reserve",
        handle_set_backup_reserve=admission.m.guard_battery_transition(hass, entry, reserve_handler),
    )
    hass.services = NS(async_call=AsyncMock(side_effect=AssertionError("Reserve fallback must remain in the route-owning task")))
    exec(compile(ast.fix_missing_locations(ast.Module(functions, type_ignores=[])), "<actual backup handler>", "exec",
                 flags=__import__("__future__").annotations.compiler_flag), ns)
    for name in ("handle_force_charge", "handle_force_discharge", "handle_schedule_max_backup"):
        ns[name] = admission.m.guard_battery_transition(hass, entry, ns[name])
    return NS(ns=ns, runtime=runtime, site=site, core=core, controller=controller, client=client,
              saved=saved, mutations=mutations, reserves=reserves, queued=queued, durable=durable,
              reserve_result=reserve_result, guard=admission.m.guard_battery_transition)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "optimizer"])
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_actual_force_service_refuses_backup_owner_before_mutating_state(overlap, source, direction):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    journal = deepcopy(p.ns["_persist_max_backup_schedule"].await_args.args[0])
    state = deepcopy(p.ns[f"force_{direction}_state"])
    cancel = p.runtime["max_backup_cancel"]
    with pytest.raises(RuntimeError, match="Max Backup window or cleanup"):
        await p.ns[f"handle_force_{direction}"](call(source=source, duration=15, power_w=1000))
    assert p.ns[f"force_{direction}_state"] == state
    assert p.mutations == [("backup", 60)]
    assert p.ns["_persist_max_backup_schedule"].await_count == 2
    assert p.ns["_persist_max_backup_schedule"].await_args.args[0] == journal
    assert p.runtime["max_backup_end_ts"] == journal["end_ts"]
    cancel.assert_not_called()
    p.client.cancel_max_backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "optimizer"])
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_actual_backup_refuses_existing_numeric_force_without_staging_a_schedule(overlap, source, direction):
    p = overlap
    try:
        await p.ns[f"handle_force_{direction}"](call(source=source, duration=15, power_w=1000))
        original = deepcopy(p.saved)
        with pytest.raises(RuntimeError, match="Stop or restore"):
            await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
        assert p.saved == original and p.controller.pending
        assert "max_backup_end_ts" not in p.runtime
        p.client.schedule_max_backup.assert_not_awaited()
        p.ns["_persist_max_backup_schedule"].assert_not_awaited()
    finally:
        await p.controller.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["native", "legacy", "restore", "settings"])
async def test_backup_refuses_other_authoritative_control_and_recovery(overlap, owner):
    p = overlap
    if owner == "native":
        p.runtime["powerwall_native_max_charge"] = NS(pending=True)
    elif owner == "legacy":
        p.ns["force_discharge_state"].update(active=True, saved_tariff={"name": "baseline"})
    elif owner == "restore":
        p.runtime["pending_tesla_restore"] = {"reason": "cleanup unconfirmed"}
    else:
        p.runtime["powerwall_settings_pending"] = {"id": "unresolved signed write"}
    with pytest.raises(RuntimeError, match="Stop or restore"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    p.client.schedule_max_backup.assert_not_awaited()
    p.ns["_persist_max_backup_schedule"].assert_not_awaited()


@pytest.mark.asyncio
async def test_replacing_existing_backup_preserves_original_baseline_and_cancels_only_prior_timer(overlap):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    baseline, cancel = p.runtime["max_backup_saved_reserve"], p.runtime["max_backup_cancel"]
    p.runtime["powerwall_local"]["coordinator"].data.backup_reserve_percent = 99
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2))
    assert p.runtime["max_backup_saved_reserve"] == baseline
    cancel.assert_called_once()
    assert p.mutations == [("backup", 60), ("backup", 120)]


@pytest.mark.asyncio
async def test_reserve_fallback_uses_same_task_and_current_ha_service_payload(overlap):
    p = overlap
    p.runtime["powerwall_local"].pop("client")
    request = call(source="user", duration_minutes=1)
    await asyncio.wait_for(p.ns["handle_schedule_max_backup"](request), timeout=2)
    assert len(p.reserves) == 1
    assert p.reserves[0].hass is p.ns["hass"]
    assert p.reserves[0].data == {"percent": 100, "source": "user"}
    assert p.reserves[0].context is request.context
    p.ns["hass"].services.async_call.assert_not_awaited()
    assert p.runtime["max_backup_local_event"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_backup_ownership_appearing_while_force_awaits_final_gate_revokes_submission(overlap, direction):
    p = overlap
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        async with p.site.write_context("earlier driver"):
            entered.set()
            await release.wait()
    holder = asyncio.create_task(hold())
    await entered.wait()
    force = asyncio.create_task(p.ns[f"handle_force_{direction}"](call(source="user", duration=15, power_w=1000)))
    await asyncio.wait_for(p.queued.wait(), timeout=2)
    p.runtime["max_backup_end_ts"] = 1  # Expiry alone cannot release retained ownership.
    release.set()
    await holder
    try:
        with pytest.raises(RuntimeError):
            await force
        assert not p.mutations and p.runtime["max_backup_end_ts"] == 1
    finally:
        await p.controller.stop()


@pytest.mark.asyncio
async def test_queued_backup_rechecks_force_owner_before_schedule_staging(overlap):
    p = overlap
    async with p.site.route_scope("entry"):
        scheduled = asyncio.create_task(p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1)))
        await asyncio.sleep(0)
        p.ns["force_discharge_state"]["active"] = True
    with pytest.raises(RuntimeError, match="Stop or restore"):
        await scheduled
    assert "max_backup_end_ts" not in p.runtime and not p.mutations


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_local_renewal_stops_for_new_backup_but_cleanup_and_settings_remain_allowed(overlap, direction):
    p = overlap
    try:
        await p.ns[f"handle_force_{direction}"](call(source="user", duration=15, power_w=1000))
        sent = list(p.mutations)
        p.runtime["max_backup_end_ts"] = 1
        with pytest.raises(ValueError, match="ownership or safety"):
            await p.controller._write()
        assert p.mutations == sent
        await p.controller.stop()
        await p.ns["handle_set_backup_reserve"](call(source="user", percent=20))
        async def restore(request):
            async with p.site.write_context("cleanup", entry_id="entry"):
                return True
        restore.__name__ = "handle_restore_normal"
        assert await p.guard(p.ns["hass"], p.ns["entry"], restore)(call(source="user")) is True
        assert p.runtime["max_backup_end_ts"] == 1
    finally:
        await p.controller.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_expiry_retains_backup_owner_until_cleanup_retry_is_confirmed(overlap, local, failure, monkeypatch):
    p = overlap
    if not local:
        p.runtime["powerwall_local"].pop("client")
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    baseline = deepcopy(p.durable.value)
    callback = p.ns["async_call_later"].call_args.args[2]
    if local:
        p.client.cancel_max_backup.return_value = False
        if failure == "exception":
            p.client.cancel_max_backup.side_effect = RuntimeError("cancel outcome unknown")
    else:
        p.reserve_result.value = {"success": False, "error": "restore unconfirmed"}
        if failure == "exception":
            p.reserve_result.error = RuntimeError("restore outcome unknown")
    await callback(None)
    if local:
        baseline["apply_pending"] = True
    assert p.durable.value == baseline
    assert p.runtime["max_backup_end_ts"] == baseline["end_ts"]
    assert p.runtime["max_backup_saved_reserve"] == baseline["saved_reserve"]
    assert p.runtime["max_backup_restore_attempts"] == 1
    with pytest.raises(RuntimeError, match="Max Backup window or cleanup"):
        await p.ns["handle_force_discharge"](call(source="user", power_w=1000, duration=15))
    p.client.cancel_max_backup.side_effect = None
    p.client.cancel_max_backup.return_value = True
    p.reserve_result.error = None
    p.reserve_result.value = {"success": True, "error": None}
    retry = p.ns["async_call_later"].call_args.args[2]
    assert p.ns["async_call_later"].call_args.args[1] == 60
    elapse(monkeypatch)
    await retry(None)
    assert p.durable.value is None
    assert "max_backup_end_ts" not in p.runtime
    assert p.client.schedule_max_backup.await_count == int(local)
    p.client.dispatch_battery_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_retries_are_bounded_without_releasing_original_window(overlap, monkeypatch):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    baseline = deepcopy(p.durable.value)
    p.client.cancel_max_backup.return_value = False
    for _ in range(3):
        elapse(monkeypatch)
        await p.ns["async_call_later"].call_args.args[2](None)
    assert p.runtime["max_backup_restore_attempts"] == 3
    assert p.ns["async_call_later"].call_count == 3  # Original timer plus two retries.
    baseline["apply_pending"] = True
    assert p.durable.value == baseline and p.runtime["max_backup_end_ts"] == baseline["end_ts"]
    assert p.client.schedule_max_backup.await_count == 1


@pytest.mark.asyncio
async def test_old_retry_cannot_touch_replacement_window_or_its_original_baseline(overlap, monkeypatch):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    original = p.runtime["max_backup_saved_reserve"]
    p.client.cancel_max_backup.return_value = False
    await p.ns["async_call_later"].call_args.args[2](None)
    old_retry = p.ns["async_call_later"].call_args.args[2]
    p.client.cancel_max_backup.return_value = True
    elapse(monkeypatch)
    await old_retry(None)
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2))
    replacement = deepcopy(p.durable.value)
    calls = p.client.cancel_max_backup.await_count
    await old_retry(None)
    assert p.client.cancel_max_backup.await_count == calls
    assert p.durable.value == replacement
    assert replacement["saved_reserve"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False])
async def test_failed_cleanup_refuses_replacement_without_new_actuation_or_baseline_loss(overlap, local):
    p = overlap
    if not local:
        p.runtime["powerwall_local"].pop("client")
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    baseline = deepcopy(p.durable.value)
    p.client.cancel_max_backup.return_value = False
    p.reserve_result.value = {"success": False}
    await p.ns["async_call_later"].call_args.args[2](None)
    retry_cancel = p.runtime["max_backup_cancel"]
    mutations, reserves = list(p.mutations), list(p.reserves)
    with pytest.raises(RuntimeError, match="Existing Max Backup cleanup must resolve"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2))
    baseline["apply_pending"] = local
    assert p.durable.value == baseline and p.runtime["max_backup_end_ts"] == baseline["end_ts"]
    assert p.mutations == mutations and p.reserves == reserves
    assert p.client.schedule_max_backup.await_count == int(local)
    retry_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_replacement_during_cancel_cannot_clear_replacement_journal(overlap):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    entered, release = asyncio.Event(), asyncio.Event()
    async def cancel():
        entered.set()
        await release.wait()
        return True
    p.client.cancel_max_backup.side_effect = cancel
    expired = asyncio.create_task(p.ns["async_call_later"].call_args.args[2](None))
    await asyncio.wait_for(entered.wait(), timeout=2)
    replacement = {"end_ts": p.runtime["max_backup_end_ts"] + 60, "saved_reserve": 7, "local_event": True}
    p.ns["hass"].data["power_sync"]["entry"] = {"max_backup_end_ts": replacement["end_ts"],
        "max_backup_generation": p.runtime["max_backup_generation"] + 1, "max_backup_saved_reserve": 7}
    p.durable.value = deepcopy(replacement)
    release.set()
    await expired
    assert p.durable.value == replacement
    assert p.ns["_persist_max_backup_schedule"].await_count == 3


@pytest.mark.asyncio
async def test_old_timer_cannot_adopt_reloaded_runtime_with_same_generation(overlap):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    old_timer = p.ns["async_call_later"].call_args.args[2]
    replacement = dict(p.runtime)
    replacement["max_backup_end_ts"] += 60
    p.ns["hass"].data["power_sync"]["entry"] = replacement
    p.durable.value["end_ts"] = replacement["max_backup_end_ts"]
    preserved = deepcopy(p.durable.value)
    await old_timer(None)
    p.client.cancel_max_backup.assert_not_awaited()
    assert p.durable.value == preserved


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False])
async def test_restart_of_expired_window_retries_only_cleanup_without_replaying_backup(overlap, local, monkeypatch):
    p = overlap
    p.durable.value = {"end_ts": 1, "saved_reserve": 2, "local_event": local, "site_id": "site", "apply_pending": False}
    p.client.cancel_max_backup.return_value = False
    p.reserve_result.value = {"success": False}
    await p.ns["_restore_max_backup_window"](1, 2, local_event=local, site_id="site")
    assert p.durable.value == {"end_ts": 1, "saved_reserve": 2, "local_event": local, "site_id": "site", "apply_pending": local}
    assert p.runtime["max_backup_end_ts"] == 1
    p.client.schedule_max_backup.assert_not_awaited()
    p.client.dispatch_battery_power.assert_not_awaited()
    p.client.cancel_max_backup.return_value = True
    p.reserve_result.value = {"success": True}
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None and "max_backup_end_ts" not in p.runtime


@pytest.mark.asyncio
async def test_successful_native_ack_does_not_release_owner_before_durable_clear(overlap, monkeypatch):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    original = deepcopy(p.durable.value)
    p.durable.fail_clear = True
    await p.ns["async_call_later"].call_args.args[2](None)
    original["apply_pending"] = True
    assert p.durable.value == original and p.runtime["max_backup_end_ts"] == original["end_ts"]
    p.durable.fail_clear = False
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None


@pytest.mark.asyncio
async def test_backup_durable_native_intent_exists_before_actuator(overlap):
    p = overlap
    observed = []
    async def inspect_intent(*args, **kwargs):
        observed.append(deepcopy(p.durable.value))
        return True
    p.client.schedule_max_backup.side_effect = inspect_intent
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    assert observed and observed[0] is not None
    assert observed[0]["local_event"] is True
    assert observed[0]["saved_reserve"] == p.runtime["max_backup_saved_reserve"]


@pytest.mark.asyncio
async def test_backup_persistence_failure_refuses_actuation(overlap):
    p = overlap
    p.ns["_persist_max_backup_schedule"].side_effect = None
    p.ns["_persist_max_backup_schedule"].return_value = False
    with pytest.raises(RuntimeError, match="persist"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    assert not p.mutations and not p.reserves


@pytest.mark.asyncio
async def test_baseline_read_cannot_renew_original_backup_deadline(overlap, monkeypatch):
    import time
    p = overlap
    now = [10000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    async def late_baseline():
        now[0] += 61
        return 2
    p.runtime["optimization_coordinator"] = NS(resolve_restore_target=late_baseline)
    with pytest.raises(RuntimeError, match="expired|deadline"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    assert not p.mutations and not p.reserves


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["false", "exception", "cancel"])
async def test_unacknowledged_native_start_retains_durable_cleanup_without_reserve_fallback(overlap, outcome, monkeypatch):
    p = overlap
    if outcome == "false":
        p.client.schedule_max_backup.side_effect = None
        p.client.schedule_max_backup.return_value = False
    else:
        p.client.schedule_max_backup.side_effect = (asyncio.CancelledError() if outcome == "cancel"
                                                   else RuntimeError("reply lost"))
    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    assert p.durable.value["local_event"] is True
    assert p.durable.value["saved_reserve"] == p.runtime["max_backup_saved_reserve"]
    assert p.runtime["max_backup_restore_attempts"] == 1 and not p.reserves
    # A restart of staged/unknown intent only cleans up; it never replays a start.
    p.client.schedule_max_backup.reset_mock()
    await p.ns["_restore_max_backup_window"](p.durable.value["end_ts"], p.durable.value["saved_reserve"],
        local_event=True, apply_pending=True, site_id="site")
    p.client.schedule_max_backup.assert_not_awaited()
    p.client.cancel_max_backup.assert_not_awaited()
    assert p.ns["async_call_later"].call_args.args[1] == pytest.approx(60, abs=1)
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    p.client.cancel_max_backup.assert_awaited_once()
    assert p.durable.value is None


@pytest.mark.asyncio
async def test_failed_replacement_save_keeps_prior_timer_route_and_baseline(overlap):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    original = deepcopy(p.durable.value)
    cancel = p.runtime["max_backup_cancel"]
    generation = p.runtime["max_backup_generation"]
    p.ns["_persist_max_backup_schedule"].side_effect = None
    p.ns["_persist_max_backup_schedule"].return_value = False
    with pytest.raises(RuntimeError, match="persist"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2))
    assert p.durable.value == original
    assert p.runtime["max_backup_generation"] == generation
    assert p.runtime["max_backup_end_ts"] == original["end_ts"]
    assert p.runtime["max_backup_saved_reserve"] == original["saved_reserve"]
    assert p.runtime["max_backup_cancel"] is cancel
    cancel.assert_not_called()
    assert p.client.schedule_max_backup.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("native_first", [True, False])
async def test_cross_route_replacement_requires_original_cleanup(overlap, native_first):
    p = overlap
    p.ns["entry"].options["tesla_control_method"] = "local" if native_first else "tariff"
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    original, cancel = deepcopy(p.durable.value), p.runtime["max_backup_cancel"]
    p.ns["entry"].options["tesla_control_method"] = "tariff" if native_first else "local"
    with pytest.raises(RuntimeError, match="control route"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2))
    assert p.durable.value == original
    assert p.runtime["max_backup_local_event"] is native_first
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_tariff_backup_does_not_attempt_unreachable_paired_lan(overlap):
    p = overlap
    p.ns["entry"].options["tesla_control_method"] = "tariff"
    p.client.schedule_max_backup.side_effect = RuntimeError("LAN unreachable")
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    p.client.schedule_max_backup.assert_not_awaited()
    assert p.reserves[0].data["percent"] == 100
    assert p.durable.value["local_event"] is False


@pytest.mark.asyncio
async def test_queued_backup_final_deadline_refuses_native_actuator(overlap, monkeypatch):
    import time
    p = overlap
    now = [10000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        async with p.site.write_context("earlier driver"):
            entered.set()
            await release.wait()
    holder = asyncio.create_task(hold())
    await entered.wait()
    schedule = asyncio.create_task(p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1)))
    await asyncio.wait_for(p.queued.wait(), timeout=2)
    now[0] += 61
    # The immutable original wall deadline is also a final apply predicate.
    release.set()
    await holder
    with pytest.raises(RuntimeError, match="expired|deadline|current"):
        await schedule
    assert not p.mutations and p.durable.value["end_ts"] == 10060


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_ack", [False, None])
async def test_transport_never_starts_backup_after_unacknowledged_cancel(overlap, cancel_ack):
    module = importlib.import_module(f"{overlap.core.__package__}.powerwall_local.transport")
    transport = object.__new__(module.TEDAPIv1rTransport)
    transport.cancel_manual_backup = AsyncMock(return_value=cancel_ack)
    transport.post_v1r = AsyncMock()
    assert await transport.schedule_manual_backup("DIN", 60) is False
    transport.post_v1r.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_native_packet_end_does_not_extend_after_cancel_wait(overlap, monkeypatch):
    module = importlib.import_module(f"{overlap.core.__package__}.powerwall_local.transport")
    transport = object.__new__(module.TEDAPIv1rTransport)
    now = [10000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    async def cancel(_din):
        now[0] += 20
        return True
    transport.cancel_manual_backup = cancel
    reply = module.transport_pb2.MessageEnvelope()
    reply.teg.schedule_manual_backup_event_response.SetInParent()
    transport.post_v1r = AsyncMock(return_value=module.TEDAPIResponse(True, reply.SerializeToString()))
    assert await transport.schedule_manual_backup("DIN", 60, end_ts=10060) is True
    packet = module.transport_pb2.MessageEnvelope()
    packet.ParseFromString(transport.post_v1r.await_args.args[0])
    info = packet.teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == 10020
    assert info.duration_seconds == 40
    now[0] = 10061
    with pytest.raises(module.PowerwallLocalError, match="expired"):
        transport.post_v1r.await_args.kwargs["before_send"]()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_cancel", "after_cancel"])
async def test_transport_expired_backup_creates_no_start_packet(overlap, monkeypatch, phase):
    module = importlib.import_module(f"{overlap.core.__package__}.powerwall_local.transport")
    transport = object.__new__(module.TEDAPIv1rTransport)
    now = [10061.0 if phase == "before_cancel" else 10000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    async def cancel(_din):
        now[0] = 10061
        return True
    transport.cancel_manual_backup = AsyncMock(side_effect=cancel)
    transport.post_v1r = AsyncMock()
    with pytest.raises(module.PowerwallLocalError, match="expired"):
        await transport.schedule_manual_backup("DIN", 60, end_ts=10060)
    transport.post_v1r.assert_not_awaited()
    assert transport.cancel_manual_backup.await_count == (0 if phase == "before_cancel" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "optimizer"])
@pytest.mark.parametrize("service", ["handle_hold_battery_soc", "handle_set_self_consumption"])
async def test_backup_owner_refuses_competing_timed_episode_before_handler(overlap, source, service):
    p = overlap
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    handler = AsyncMock()
    handler.__name__ = service
    with pytest.raises(RuntimeError, match="Max Backup window or cleanup"):
        await p.guard(p.ns["hass"], p.ns["entry"], handler)(call(source=source, duration=15))
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "cancel"])
async def test_prior_expiry_waits_for_replacement_staging_rollback(overlap, failure):
    p = overlap
    p.ns["entry"].options["tesla_control_method"] = "tariff"
    await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    original = deepcopy(p.durable.value)
    old_timer = p.ns["async_call_later"].call_args.args[2]
    entered, release = asyncio.Event(), asyncio.Event()
    persist = p.ns["_persist_max_backup_schedule"].side_effect
    async def blocked_save(payload, *, is_current=None):
        if payload is not None and payload["end_ts"] > original["end_ts"]:
            entered.set()
            await release.wait()
            if failure == "cancel":
                raise asyncio.CancelledError()
            return False
        return await persist(payload, is_current=is_current)
    p.ns["_persist_max_backup_schedule"].side_effect = blocked_save
    replacing = asyncio.create_task(p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=2)))
    await asyncio.wait_for(entered.wait(), timeout=2)
    expiring = asyncio.create_task(old_timer(None))
    await asyncio.sleep(0)
    assert not expiring.done()
    release.set()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await replacing
    await expiring
    assert [request.data["percent"] for request in p.reserves] == [100, original["saved_reserve"]]
    assert p.durable.value is None and "max_backup_end_ts" not in p.runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_site", [None, "different-site"])
async def test_resume_missing_or_mismatched_site_retains_record_without_recovery_io(overlap, recorded_site):
    p = overlap
    record = {"end_ts": 1, "saved_reserve": 2, "local_event": True}
    if recorded_site is not None:
        record["site_id"] = recorded_site
    p.durable.value = deepcopy(record)
    p.runtime["store"] = NS(async_load=AsyncMock(return_value={"max_backup_schedule": record}))
    await p.ns["_resume_max_backup_if_persisted"]()
    assert p.durable.value == record and p.runtime["max_backup_recovery_unavailable"]
    assert p.runtime["max_backup_saved_reserve"] == 2
    p.client.cancel_max_backup.assert_not_awaited()
    p.client.schedule_max_backup.assert_not_awaited()
    assert not p.reserves
    with pytest.raises(RuntimeError, match="Max Backup"):
        await p.ns["handle_force_charge"](call(source="user", duration=15, power_w=1000))


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", ["00123"], indirect=True)
async def test_resume_canonical_site_id_recovers_same_physical_site(overlap):
    p = overlap
    p.ns["entry"].data["tesla_energy_site_id"] = "00123"
    record = {"end_ts": 1, "saved_reserve": 2, "local_event": False, "site_id": 123}
    p.durable.value = deepcopy(record)
    p.runtime["store"] = NS(async_load=AsyncMock(return_value={"max_backup_schedule": record}))
    await p.ns["_resume_max_backup_if_persisted"]()
    assert p.reserves[0].data["percent"] == 2 and p.durable.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["runtime", "window"])
async def test_delayed_startup_read_cannot_adopt_new_runtime_or_window(overlap, replacement):
    p = overlap
    entered, release = asyncio.Event(), asyncio.Event()
    async def load():
        entered.set()
        await release.wait()
        return {"max_backup_schedule": {"end_ts": 1, "saved_reserve": 2, "local_event": True, "site_id": "site"}}
    p.runtime["store"] = NS(async_load=load)
    resume = asyncio.create_task(p.ns["_resume_max_backup_if_persisted"]())
    await asyncio.wait_for(entered.wait(), timeout=2)
    newer = p.runtime if replacement == "window" else dict(p.runtime)
    newer.update(max_backup_generation=9, max_backup_end_ts=9999999999, max_backup_saved_reserve=7)
    p.ns["hass"].data["power_sync"]["entry"] = newer
    release.set()
    await resume
    assert newer["max_backup_generation"] == 9 and newer["max_backup_saved_reserve"] == 7
    p.client.cancel_max_backup.assert_not_awaited()
    assert not p.reserves


def test_rebound_entry_backup_ownership_stays_with_recorded_site(overlap):
    p = overlap
    p.ns["entry"].data["tesla_energy_site_id"] = "B"
    p.runtime.update(max_backup_site_id="A", max_backup_end_ts=1, max_backup_saved_reserve=2,
                     max_backup_recovery_unavailable=True)
    assert p.core.foreign_site_owner(p.ns["hass"], "other", "A") == "entry"
    assert p.core.foreign_site_owner(p.ns["hass"], "other", "B") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("record", ["invalid record", {"end_ts": "invalid", "saved_reserve": 2},
                                    {"end_ts": float("inf"), "saved_reserve": 2}])
async def test_malformed_saved_backup_is_quarantined_without_clear_or_actuation(overlap, record):
    p = overlap
    p.durable.value = deepcopy(record)
    p.runtime["store"] = NS(async_load=AsyncMock(return_value={"max_backup_schedule": record}))
    await p.ns["_resume_max_backup_if_persisted"]()
    assert p.durable.value == record and p.runtime["max_backup_recovery_unavailable"]
    p.ns["_persist_max_backup_schedule"].assert_not_awaited()
    p.client.cancel_max_backup.assert_not_awaited()
    assert not p.reserves


@pytest.mark.asyncio
async def test_unknown_late_native_apply_cannot_release_owner_before_acceptance_drain(overlap, monkeypatch):
    p = overlap
    active = [False]
    p.client.schedule_max_backup.side_effect = RuntimeError("submitted reply lost")
    with pytest.raises(RuntimeError, match="reply lost"):
        await p.ns["handle_schedule_max_backup"](call(source="user", duration_minutes=1))
    record = deepcopy(p.durable.value)
    async def cancel():
        active[0] = False
        return True
    p.client.cancel_max_backup.side_effect = cancel
    # The submitted signed event arrives after the caller has lost its reply.
    active[0] = True
    await p.ns["async_call_later"].call_args.args[2](None)
    assert active[0] and p.durable.value == record
    p.client.cancel_max_backup.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Max Backup window or cleanup"):
        await p.ns["handle_force_discharge"](call(source="user", duration=15, power_w=1000))
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert not active[0] and p.durable.value is None
