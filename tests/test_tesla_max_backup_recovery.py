"""Supervised cleanup binds exact legacy evidence, never replays forcing."""
from __future__ import annotations

import ast
import asyncio
import importlib
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import load_power_sync_function
from test_tesla_max_backup_overlap import ROOT, overlap, elapse
from test_native_rate_admission import admission
from test_powerwall_battery_dispatch import call


@pytest.fixture
def recovery(overlap):
    p = overlap
    p.durable.value = {"end_ts": 100.0, "saved_reserve": 20.0, "local_event": False}
    p.runtime["store"] = NS(async_load=AsyncMock(side_effect=lambda: {"max_backup_schedule": deepcopy(p.durable.value)}))
    state = NS(fail=False, before=None)
    writes = []
    async def update(store, changes, *, expected, is_current, verify):
        assert store is p.runtime["store"] and verify is True
        if state.before is not None:
            await state.before()
        if not is_current() or p.durable.value != expected["max_backup_schedule"] or state.fail:
            return False
        p.durable.value = deepcopy(changes["max_backup_schedule"])
        writes.append(deepcopy(p.durable.value))
        return True
    p.ns.update(_update_powerwall_store=AsyncMock(side_effect=update),
        max_backup_recovery_status=p.core.max_backup_recovery_status,
        foreign_site_owner=p.core.foreign_site_owner,
        persistent_notification=NS(async_create=Mock(), async_dismiss=Mock()))
    tree = ast.parse((ROOT / "__init__.py").read_text())
    nodes = [deepcopy(next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.name == name)) for name in ("_notify_max_backup_recovery", "handle_recover_max_backup")]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)
    exec(compile(ast.fix_missing_locations(ast.Module([StripImports().visit(node) for node in nodes], type_ignores=[])),
                 "actual-max-backup-recovery", "exec"), p.ns)
    p.state, p.writes = state, writes
    return p


def request(p, **updates):
    data = {"entry_id": "entry", "site_id": p.site.site_id, "acknowledge": True,
            "attest_original_site": True,
            "record_id": p.core.max_backup_recovery_status("entry", p.durable.value, p.site.site_id)["record_id"]}
    data.update(updates)
    return call(**data)


@pytest.mark.asyncio
async def test_valid_legacy_float_reserve_binds_immutable_basis_before_cleanup(recovery):
    p = recovery
    original = deepcopy(p.durable.value)
    receipt = request(p).data["record_id"]
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert result == {"success": True, "status": "cleanup_completed", "site_id": "site", "physical_effect_confirmed": False}
    assert p.writes[0]["recovery"] == {"original_record": original, "original_digest": receipt,
        "attested_original_site": True, "bound_site_id": "site"}
    assert p.writes[0]["end_ts"] == 100.0 and p.writes[0]["apply_pending"] is True
    assert [r.data["percent"] for r in p.reserves] == [20]
    assert p.durable.value is None
    p.client.schedule_max_backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,error", [
    ({"acknowledge": False}, "acknowledge"),
    ({"acknowledge": 1}, "acknowledge"),
    ({"attest_original_site": False}, "attestation"),
    ({"attest_original_site": 1}, "attestation"),
    ({"record_id": "stale"}, "record changed"),
    ({"entry_id": "other"}, "Select"),
    ({"site_id": "other"}, "site changed"),
])
async def test_recovery_refuses_incomplete_or_stale_authority_before_write(recovery, changes, error):
    p = recovery
    before = deepcopy(p.durable.value)
    with pytest.raises(RuntimeError, match=error):
        await p.ns["handle_recover_max_backup"](request(p, **changes))
    assert p.durable.value == before and not p.reserves and not p.writes


@pytest.mark.asyncio
async def test_explicit_saved_site_cannot_be_rebound_even_with_attestation(recovery):
    p = recovery
    p.durable.value["site_id"] = "original-A"
    with pytest.raises(RuntimeError, match="another site"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert p.durable.value["site_id"] == "original-A" and not p.reserves and not p.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("end_ts", float("nan")), ("saved_reserve", None),
    ("saved_reserve", 20.5), ("saved_reserve", True), ("local_event", 1)])
async def test_malformed_basis_is_retained_without_guessing(recovery, field, value):
    p = recovery
    p.durable.value[field] = value
    receipt = request(p)
    with pytest.raises(RuntimeError, match="malformed"):
        await p.ns["handle_recover_max_backup"](receipt)
    assert p.durable.value[field] is value or p.durable.value[field] == value
    assert not p.reserves and not p.writes


@pytest.mark.asyncio
async def test_binding_storage_failure_preserves_legacy_record_and_sends_nothing(recovery):
    p = recovery
    p.state.fail = True
    original = deepcopy(p.durable.value)
    with pytest.raises(RuntimeError, match="persisted"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert p.durable.value == original and not p.reserves


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["record", "generation", "runtime"])
async def test_authority_revoked_while_binding_awaits_never_actuates(recovery, changed):
    p = recovery
    async def revoke():
        if changed == "record":
            p.durable.value["saved_reserve"] = 7
        elif changed == "generation":
            p.runtime["max_backup_generation"] = 9
        else:
            p.ns["hass"].data["power_sync"]["entry"] = dict(p.runtime)
    p.state.before = revoke
    with pytest.raises(RuntimeError, match="persisted"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert not p.reserves and not p.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["force", "native", "settings", "event"])
async def test_recovery_does_not_override_new_battery_or_settings_owner(recovery, owner):
    p = recovery
    if owner == "force":
        p.runtime["force_charge_state"]["active"] = True
    elif owner == "native":
        p.runtime["powerwall_native_max_charge"] = NS(pending=True)
    elif owner == "settings":
        p.runtime["powerwall_settings_pending"] = {"id": "unknown"}
    else:
        p.runtime["aemo_spike_manager"] = NS(_in_spike_mode=True)
    with pytest.raises(RuntimeError, match="other battery"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert not p.reserves and not p.writes


@pytest.mark.asyncio
async def test_native_unknown_cancel_keeps_attested_basis_across_restart_and_retry(recovery, monkeypatch):
    p = recovery
    p.durable.value["local_event"] = True
    original = deepcopy(p.durable.value)
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert result["status"] == "cleanup_pending" and not result["success"]
    p.client.cancel_max_backup.assert_not_awaited()
    p.client.cancel_max_backup.return_value = False
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value["recovery"]["original_record"] == original
    saved = deepcopy(p.durable.value)
    await p.ns["_resume_max_backup_if_persisted"]()
    assert p.durable.value == saved
    assert p.durable.value["recovery"]["original_record"] == original
    p.client.cancel_max_backup.return_value = True
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None
    p.client.schedule_max_backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_after_binding_keeps_durable_basis_for_retry(recovery):
    p = recovery
    async def cancelled(_request):
        raise asyncio.CancelledError()
    p.ns["handle_set_backup_reserve"] = cancelled
    with pytest.raises(asyncio.CancelledError):
        await p.ns["handle_recover_max_backup"](request(p))
    assert p.durable.value["site_id"] == "site"
    assert p.durable.value["recovery"]["original_record"]["saved_reserve"] == 20.0
    assert p.durable.value["apply_pending"] is True


def test_recovery_receipt_is_read_only_and_entry_specific(recovery):
    p = recovery
    p.runtime["max_backup_schedule"] = deepcopy(p.durable.value)
    before = deepcopy(p.durable.value)
    status = p.core.diagnostic_snapshot(p.ns["hass"], p.site)["obligations"]["entry"]["max_backup_recovery"]
    assert status["record_id"] == request(p).data["record_id"]
    assert status["attest_original_site_required"] and status["recoverable"]
    assert p.core.max_backup_recovery_status("other", before, "site")["record_id"] != status["record_id"]
    assert p.durable.value == before


@pytest.mark.asyncio
async def test_actual_registered_service_routes_to_selected_entry(recovery):
    p = recovery
    routing = ROOT / "licensing" / "entry_routing.py"
    register = load_power_sync_function("register_handlers", {}, path=routing)
    routed = load_power_sync_function("routed_handler", {}, path=routing)
    tree = ast.parse((ROOT / "__init__.py").read_text())
    registration = next(node for node in ast.walk(tree) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "async_register" and len(node.value.args) > 1
        and isinstance(node.value.args[1], ast.Constant) and node.value.args[1].value == "recover_max_backup")
    mapping = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "register_handlers")
    assert any(isinstance(key, ast.Constant) and key.value == "recover_max_backup" for key in mapping.args[2].keys)
    registered = {}
    p.ns["hass"].services = NS(async_register=lambda _domain, name, handler, **_kw: registered.update({name: handler}))
    other = AsyncMock()
    for owner, handler in (("entry", p.ns["handle_recover_max_backup"]), ("other", other)):
        register(p.ns["hass"], owner, {"recover_max_backup": handler})
        ns = {**p.ns, "entry": NS(entry_id=owner), "routed_handler": routed,
              "handle_recover_max_backup": handler, "SupportsResponse": NS(OPTIONAL=object())}
        exec(compile(ast.fix_missing_locations(ast.Module([deepcopy(registration)], type_ignores=[])), "actual-recovery-registration", "exec"), ns)
    result = await registered["recover_max_backup"](request(p))
    assert result["success"] and len(p.reserves) == 1
    other.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", ["00123"], indirect=True)
async def test_known_site_canonical_identity_needs_no_legacy_attestation(recovery):
    p = recovery
    p.durable.value["site_id"] = 123
    result = await p.ns["handle_recover_max_backup"](request(p, site_id="00123", attest_original_site=False))
    assert result["success"] and result["site_id"] == "123"


@pytest.mark.asyncio
async def test_stale_receipt_after_route_wait_sends_no_cleanup(recovery):
    p = recovery
    stale = request(p)
    async with p.site.route_scope("entry"):
        pending = asyncio.create_task(p.ns["handle_recover_max_backup"](stale))
        await asyncio.sleep(0)
        p.durable.value["saved_reserve"] = 7
    with pytest.raises(RuntimeError, match="record changed"):
        await pending
    assert not p.reserves and not p.writes


@pytest.mark.asyncio
async def test_retry_keeps_original_basis_and_never_applies_reserve_100(recovery):
    p = recovery
    original = deepcopy(p.durable.value)
    p.reserve_result.value = {"success": False}
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert not result["success"] and p.durable.value["recovery"]["original_record"] == original
    original_digest = p.durable.value["recovery"]["original_digest"]
    p.reserve_result.value = {"success": True}
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert result["success"] and p.durable.value is None
    assert all(value["recovery"]["original_digest"] == original_digest for value in p.writes)
    assert [r.data["percent"] for r in p.reserves] == [20, 20]


def bind_absence_helper(p):
    module = importlib.import_module(f"{p.core.__package__}.powerwall_local.transport")
    p.ns["manual_backup_is_absent"] = module.manual_backup_is_absent


@pytest.mark.asyncio
async def test_native_cleanup_uses_fresh_absence_only_after_signed_acceptance_drain(recovery, monkeypatch):
    p = recovery
    bind_absence_helper(p)
    p.durable.value["local_event"] = True
    p.client.get_backup_events = AsyncMock(return_value={"manual_backup": None, "backup_events": []})
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert result["status"] == "cleanup_pending"
    p.client.get_backup_events.assert_not_awaited()
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None
    p.client.cancel_max_backup.assert_not_awaited()
    p.client.schedule_max_backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [None, {}, {"manual_backup": None},
    {"manual_backup": {"active": False, "start_time": 9999999999}, "backup_events": []}])
async def test_unknown_or_present_native_read_cannot_retire_without_cancel_ack(recovery, monkeypatch, observed):
    p = recovery
    bind_absence_helper(p)
    p.durable.value["local_event"] = True
    p.client.get_backup_events = AsyncMock(return_value=observed)
    p.client.cancel_max_backup.return_value = False
    await p.ns["handle_recover_max_backup"](request(p))
    original = deepcopy(p.durable.value)
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value == original
    p.client.cancel_max_backup.assert_awaited_once()
    # A later absence cannot settle that lost cancel while its signature is valid.
    p.client.get_backup_events.return_value = {"manual_backup": None, "backup_events": []}
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.client.get_backup_events.await_count == 1 and p.durable.value == original
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None and p.client.cancel_max_backup.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "persistence"])
async def test_absence_cleanup_failure_keeps_original_basis_until_retry(recovery, monkeypatch, failure):
    p = recovery
    bind_absence_helper(p)
    p.durable.value["local_event"] = True
    p.client.get_backup_events = AsyncMock(return_value={"manual_backup": None, "backup_events": []})
    await p.ns["handle_recover_max_backup"](request(p))
    original = deepcopy(p.durable.value)
    if failure == "read":
        p.client.get_backup_events.side_effect = RuntimeError("read unavailable")
    else:
        p.durable.fail_clear = True
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value == original
    p.client.cancel_max_backup.assert_not_awaited()
    p.client.get_backup_events.side_effect = None
    p.durable.fail_clear = False
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["pairing", "client", "runtime", "owner"])
async def test_absence_from_old_identity_cannot_clear_saved_owner(recovery, monkeypatch, changed):
    p = recovery
    bind_absence_helper(p)
    p.durable.value["local_event"] = True
    async def read():
        if changed == "pairing":
            p.ns["entry"].options["powerwall_local_din"] = "replacement-DIN"
        elif changed == "client":
            p.runtime["powerwall_local"]["client"] = NS()
        elif changed == "runtime":
            p.ns["hass"].data["power_sync"]["entry"] = dict(p.runtime)
        else:
            p.ns["hass"].data["power_sync"]["foreign"] = {
                "entry": NS(data={"tesla_energy_site_id": "site"}, options={}),
                "force_charge_state": {"active": True}}
        return {"manual_backup": None, "backup_events": []}
    p.client.get_backup_events = AsyncMock(side_effect=read)
    await p.ns["handle_recover_max_backup"](request(p))
    original = deepcopy(p.durable.value)
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value == original
    p.client.cancel_max_backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["site", "attestation", "end", "reserve", "route", "recursive", "digest"])
async def test_recovery_metadata_cannot_change_original_cleanup_basis(recovery, tamper):
    p = recovery
    original = deepcopy(p.durable.value)
    digest = request(p).data["record_id"]
    p.durable.value.update(site_id="site", apply_pending=True, recovery={
        "original_record": original, "original_digest": digest,
        "bound_site_id": "site", "attested_original_site": True})
    if tamper == "site":
        p.durable.value["recovery"]["bound_site_id"] = "other"
    elif tamper == "attestation":
        p.durable.value["recovery"]["attested_original_site"] = False
    elif tamper == "end":
        p.durable.value["end_ts"] += 60
    elif tamper == "reserve":
        p.durable.value["saved_reserve"] = 7
    elif tamper == "route":
        p.durable.value["local_event"] = True
    elif tamper == "recursive":
        p.durable.value["recovery"]["original_record"]["recovery"] = {}
    else:
        p.durable.value["recovery"]["original_digest"] = "stale"
    before = deepcopy(p.durable.value)
    with pytest.raises(RuntimeError, match="malformed"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert p.durable.value == before and not p.writes and not p.reserves
    await p.ns["_resume_max_backup_if_persisted"]()
    assert p.runtime["max_backup_recovery_unavailable"]
    assert p.durable.value == before and not p.reserves
    p.client.cancel_max_backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["cancel", "absence"])
async def test_native_legacy_null_reserve_uses_no_reserve_actuator(recovery, monkeypatch, proof):
    p = recovery
    bind_absence_helper(p)
    p.durable.value.update(saved_reserve=None, local_event=True)
    if proof == "absence":
        p.client.get_backup_events = AsyncMock(return_value={"manual_backup": None, "backup_events": []})
    result = await p.ns["handle_recover_max_backup"](request(p))
    assert result["status"] == "cleanup_pending"
    assert p.writes[0]["recovery"]["original_record"]["saved_reserve"] is None
    elapse(monkeypatch)
    await p.ns["async_call_later"].call_args.args[2](None)
    assert p.durable.value is None and not p.reserves
    assert p.client.cancel_max_backup.await_count == (1 if proof == "cancel" else 0)
    p.client.schedule_max_backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_missing_reserve_key_is_malformed_before_binding(recovery):
    p = recovery
    p.durable.value["local_event"] = True
    p.durable.value.pop("saved_reserve")
    p.runtime["max_backup_schedule"] = deepcopy(p.durable.value)
    status = p.core.diagnostic_snapshot(p.ns["hass"], p.site)["obligations"]["entry"]["max_backup_recovery"]
    assert not status["recoverable"] and status["reason"] == "malformed_record"
    with pytest.raises(RuntimeError, match="malformed"):
        await p.ns["handle_recover_max_backup"](request(p))
    assert "saved_reserve" not in p.durable.value and not p.writes and not p.reserves
