"""A proposed explicit policy exception must never manufacture terminal proof."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from conftest import load_power_sync_submodule

m = load_power_sync_submodule("powerwall_local.settings_recovery", "settings_recovery_test")


def fixture():
    identity = {"entry_id": "entry", "site_id": "site", "gateway": "gateway", "pairing": "key"}
    original = {"id": "incident", "site_id": "site", "expires_at": 100,
                "updates": {"site_info.disallow_charge_from_grid_with_solar_installed": True},
                "resume_cloud": True, "restore_basis": {"force_mode_state": {"mode": "discharge"}}}
    state = {"pending": deepcopy(original), "history": []}
    config = {"site_info": {"max_site_meter_power_ac": 13.8, "min_site_meter_power_ac": -5},
              "unrelated": [None, True, 1.0, {"retained": "value"}]}
    flags = {"now": 1000, "allowed": True, "after_read": None, "reads": 0, "fail_save": False}
    saves = []
    lock = asyncio.Lock()

    async def load():
        return deepcopy(state)

    async def read():
        flags["reads"] += 1
        result = {"config": deepcopy(config), "hash": hashlib.sha256(json.dumps(config).encode()).hexdigest()}
        if flags["after_read"]:
            flags["after_read"]()
        return result

    def guard(incident, *, owner=None):
        return flags["allowed"] and state["pending"] == incident

    @asynccontextmanager
    async def gate():
        async with lock:
            yield

    async def save(expected, history, check):
        check()
        if flags["fail_save"]:
            raise OSError("storage failed")
        assert state == expected
        saves.append(deepcopy(history))
        state.update(pending=None, history=deepcopy(history))

    control = m.LocalSettingsRecovery(identity=lambda: identity, load=load, save=save, read=read,
        guard=guard, gate=gate, clock=lambda: flags["now"])
    return control, state, original, identity, config, flags, saves


async def proposal(control):
    return await control.execute({"operation": "inspect"}, actor="admin")


def accept(prepared):
    return {"operation": "resume", **{key: prepared[key] for key in ("entry_id", "site_id", "incident_id")},
            "expected_recovery_revision": prepared["recovery_revision"], "acknowledge": True,
            "accept_unknown_full_configuration_write": True}


@pytest.mark.asyncio
async def test_inspection_is_read_only_and_retains_unknown_incident():
    control, state, original, _, _, flags, saves = fixture()
    observed = await proposal(control)
    assert observed["old_outcome"] == "unknown"
    assert observed["old_write_may_apply_later"] is True
    assert observed["hardware_command_sent"] is False
    assert observed["local_settings_resumed"] is False
    assert observed["limits"]["import_limit_w"]["value_w"] == 13800
    assert flags["reads"] == 2 and not saves
    assert state == {"pending": original, "history": []}


@pytest.mark.asyncio
async def test_explicit_exception_keeps_original_unknown_and_current_full_baseline():
    control, state, original, identity, config, _, saves = fixture()
    prepared = await proposal(control)
    result = await control.execute(accept(prepared), actor="admin")
    assert result["local_settings_resumed"] is True
    assert result["old_outcome"] == "unknown"
    assert result["old_write_may_apply_later"] is True
    assert result["hardware_command_sent"] is False
    assert state["pending"] is None and len(saves) == 1
    retained = state["history"][0]
    assert {k: v for k, v in retained.items() if k != "supervised_local_recovery"} == original
    evidence = retained["supervised_local_recovery"]
    assert evidence["identity"] == identity
    assert evidence["baseline"]["config"] == config
    assert evidence["old_outcome"] == "unknown" and evidence["confirmed_by"] == "admin"
    assert evidence["accepted_unknown_full_configuration_write"] is True
    assert not retained.get("terminal")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("entry_id", "other"), ("site_id", "other"), ("incident_id", "other"),
    ("expected_recovery_revision", "stale"), ("acknowledge", False),
    ("accept_unknown_full_configuration_write", False),
])
async def test_exact_binding_and_both_acknowledgements_are_required(field, value):
    control, state, original, _, _, _, saves = fixture()
    request = accept(await proposal(control))
    request[field] = value
    with pytest.raises(ValueError):
        await control.execute(request, actor="admin")
    assert state["pending"] == original and not saves


@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["identity", "config", "incident", "history", "ownership"])
async def test_changed_proposal_cannot_authorize_a_later_state(what):
    control, state, _, identity, config, flags, saves = fixture()
    request = accept(await proposal(control))
    if what == "identity":
        identity["pairing"] = "changed"
    elif what == "config":
        config["unrelated"].append("new intent")
    elif what == "incident":
        state["pending"]["id"] = "new incident"
    elif what == "history":
        state["history"].append({"older": True})
    else:
        flags["allowed"] = False
    with pytest.raises(ValueError):
        await control.execute(request, actor="admin")
    assert state["pending"] is not None and not saves


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "config", "incident", "ownership"])
async def test_rechecks_authority_and_baseline_across_native_reads(change):
    control, state, _, identity, config, flags, saves = fixture()
    def mutate():
        if change == "identity":
            identity["gateway"] = "replacement"
        elif change == "config":
            config["site_info"]["max_site_meter_power_ac"] += 1
        elif change == "incident":
            state["pending"]["id"] = "replacement"
        else:
            flags["allowed"] = False
    flags["after_read"] = mutate
    with pytest.raises(ValueError):
        await proposal(control)
    assert state["pending"] is not None and not saves


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [None, "100", float("inf"), 1000, 940])
async def test_expiry_is_a_quiescence_check_never_implicit_authority(expiry):
    control, state, _, _, _, _, saves = fixture()
    state["pending"]["expires_at"] = expiry
    with pytest.raises(ValueError):
        await proposal(control)
    assert state["pending"] is not None and not saves


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native_site_limits", "other"])
async def test_cannot_discard_a_different_journals_pending_transaction(kind):
    control, state, _, _, _, _, saves = fixture()
    state["pending"]["kind"] = kind
    with pytest.raises(ValueError):
        await proposal(control)
    assert state["pending"] is not None and not saves


@pytest.mark.asyncio
async def test_save_failure_retains_block_and_does_not_report_resumption():
    control, state, original, _, _, flags, saves = fixture()
    request = accept(await proposal(control))
    flags["fail_save"] = True
    with pytest.raises(OSError):
        await control.execute(request, actor="admin")
    assert state["pending"] == original and not saves


@pytest.mark.asyncio
async def test_no_administrator_no_proposal_or_transition():
    control, state, _, _, _, flags, saves = fixture()
    with pytest.raises(ValueError):
        await control.execute({}, actor=None)
    assert not flags["reads"] and not saves and state["pending"]


@pytest.mark.asyncio
async def test_full_history_is_never_silently_pruned_to_unlock():
    control, state, _, _, _, _, saves = fixture()
    state["history"] = [{"retained": index} for index in range(m.MAX_HISTORY)]
    request = accept(await proposal(control))
    with pytest.raises(ValueError, match="history full"):
        await control.execute(request, actor="admin")
    assert state["pending"] is not None and not saves


@pytest.mark.asyncio
async def test_serialized_duplicate_cannot_make_a_second_transition():
    control, state, _, _, _, _, saves = fixture()
    request = accept(await proposal(control))
    results = await asyncio.gather(*(control.execute(request, actor="admin") for _ in range(2)),
                                   return_exceptions=True)
    assert all(value["success"] for value in results)
    assert sum(value.get("already_resumed", False) for value in results) == 1
    assert len(saves) == 1 and state["pending"] is None


def integrated_fixture():
    from test_tesla_local_settings import recovery_context, load_function, ROOT
    from test_powerwall_control_coordinator import m as site_module
    original = {"id": "incident", "site_id": "site", "expires_at": 1,
                "resume_cloud": True, "updates": {"site_info.flag": True},
                "restore_basis": {"force_mode_state": {"saved_operation_mode": "backup"}}}
    ns, runtime, saved = recovery_context({"tesla_settings_pending": original, "unrelated": {"keep": True}})
    entry = ns["entry"]
    entry.data["powerwall_local_public_key_der"] = "synthetic key"
    site = site_module.PowerwallSiteControl("site")
    config = {"site_info": {"max_site_meter_power_ac": 13.8, "min_site_meter_power_ac": -5}}
    async def read():
        return {"config": deepcopy(config), "hash": "native-hash"}
    ns.update(_local_powerwall_runtime=runtime, _site_control=site,
              _control_site_id="site", asynccontextmanager=asynccontextmanager,
              settings_recovery_store_state=m.store_state, settings_recovery_identity=m.identity_for_entry,
              SettingsRecoveryError=m.SettingsRecoveryError,
              local_battery_dispatch_configured=lambda _: True,
              resolve_connection_profile=lambda *_: NS(monitoring_only=False),
              foreign_site_owner=lambda *_: None, force_charge_state={}, force_discharge_state={},
              hold_soc_state={}, self_consumption_state={}, _read_native_limits_document=read,
              canonical_site_id=site_module.canonical_site_id)
    ns["hass"].auth = NS(async_get_user=AsyncMock(return_value=NS(id="admin", is_admin=True)))
    for name in ("_native_limits_block_reason", "_native_limits_allowed", "_local_powerwall_settings_write_allowed",
                 "_settings_recovery_current", "_settings_recovery_guard", "_settings_recovery_gate",
                 "_load_settings_recovery", "_save_settings_recovery", "handle_recover_powerwall_local_settings"):
        load_function(ROOT / "__init__.py", name, ns, strip_imports=True)
    control = m.LocalSettingsRecovery(identity=lambda: m.identity_for_entry(entry),
        load=ns["_load_settings_recovery"], save=ns["_save_settings_recovery"], read=read,
        guard=ns["_settings_recovery_guard"], gate=ns["_settings_recovery_gate"])
    ns["_settings_recovery_control"] = control
    async def call(data):
        return await ns["handle_recover_powerwall_local_settings"](NS(data=data, context=NS(user_id="admin")))
    return ns, runtime, saved, site, control, call


@pytest.mark.asyncio
async def test_real_handler_atomic_store_and_both_admission_surfaces_across_restart():
    from test_tesla_local_settings import load_settings_startup
    ns, runtime, saved, site, _, call = integrated_fixture()
    assert not ns["_local_powerwall_settings_write_allowed"]()
    assert not ns["_native_limits_allowed"](False)
    prepared = await call({"operation": "inspect", "entry_id": "entry"})
    assert runtime["powerwall_settings_pending"]
    result = await call(accept(prepared))
    assert result["local_settings_resumed"] and not result["hardware_command_sent"]
    assert ns["_local_powerwall_settings_write_allowed"]()
    assert ns["_native_limits_allowed"](False)
    assert saved["unrelated"] == {"keep": True}
    assert len(saved["tesla_settings_history"]) == 1
    assert not saved["tesla_settings_history"][0].get("terminal")
    assert not site._effects  # Metadata recovery is not a hardware command receipt.
    ns.update(STORAGE_VERSION=1, STORAGE_KEY="settings")
    await load_settings_startup(ns)()
    restored = ns["hass"].data["power_sync"]["entry"]
    assert restored["powerwall_settings_pending"] is None
    assert restored["powerwall_settings_history"] == saved["tesla_settings_history"]
    # A later pairing cannot inherit the exact earlier exception.
    ns["entry"].data["powerwall_local_public_key_der"] = "replacement key"
    await load_settings_startup(ns)()
    assert ns["hass"].data["power_sync"]["entry"]["powerwall_settings_pending"]["recovery_binding_changed"]


@pytest.mark.asyncio
async def test_real_late_receipt_updates_history_without_clearing_new_uncertainty():
    ns, runtime, saved, _, _, call = integrated_fixture()
    old = deepcopy(runtime["powerwall_settings_pending"])
    await call(accept(await call({"operation": "inspect"})))
    newer = {"id": "new-incident", "site_id": "site", "expires_at": 1, "updates": {"flag": False}}
    await ns["_record_tesla_settings_submission"](newer)
    runtime["powerwall_settings_pending"] = newer
    await ns["_record_tesla_settings_submission"](None, previous=old)
    assert saved["tesla_settings_pending"] == newer
    assert runtime["powerwall_settings_pending"] == newer
    assert saved["tesla_settings_history"][0]["terminal"] is True
    assert runtime["powerwall_settings_history"][0]["terminal"] is True
    ns["_sync_powerwall_settings_issue"].assert_called_with(ns["hass"], "entry", newer)
    assert not ns["_local_powerwall_settings_write_allowed"]()
    assert not ns["_native_limits_allowed"](False)


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [False, True])
async def test_real_unverified_save_never_opens_runtime_admission(written):
    ns, runtime, saved, site, _, call = integrated_fixture()
    request = accept(await call({"operation": "inspect"}))
    original_save = ns["store"].async_save
    async def write(value):
        if written:
            await original_save(value)
    ns["store"].async_save = AsyncMock(side_effect=write)
    ns["Store"] = lambda *args, **kwargs: NS(async_load=AsyncMock(return_value={"wrong": "readback"}))
    with pytest.raises(ValueError, match="persistence unconfirmed"):
        await call(request)
    assert runtime["powerwall_settings_pending"]
    assert not ns["_local_powerwall_settings_write_allowed"]()
    assert not ns["_native_limits_allowed"](False)
    assert "powerwall_settings_recovering" not in runtime and not site._effects
    if written:
        assert saved["tesla_settings_pending"] is None and saved["tesla_settings_history"]
        with pytest.raises(ValueError, match="reload required"):
            await call({"operation": "inspect"})
    else:
        assert saved["tesla_settings_pending"] and not saved.get("tesla_settings_history")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["submission", "control", "restore", "foreign", "non_admin"])
async def test_real_recovery_preserves_other_owners_and_administrator_requirement(kind):
    ns, runtime, saved, _, _, call = integrated_fixture()
    pending_task = None
    if kind in ("submission", "control"):
        pending_task = asyncio.create_task(asyncio.Event().wait())
        runtime["powerwall_settings_submissions" if kind == "submission" else "powerwall_settings_control_tasks"] = {pending_task}
    elif kind == "restore":
        runtime["pending_tesla_restore"] = {"owned": True}
    elif kind == "foreign":
        ns["foreign_site_owner"] = lambda *_: "different entry"
    else:
        ns["hass"].auth.async_get_user.return_value.is_admin = False
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await call({"operation": "inspect"})
        assert runtime["powerwall_settings_pending"] and saved["tesla_settings_pending"]
        assert not saved.get("tesla_settings_history")
    finally:
        if pending_task:
            pending_task.cancel()
            await asyncio.gather(pending_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_cancellation_during_durable_transition_leaves_runtime_closed():
    ns, runtime, saved, _, _, call = integrated_fixture()
    request = accept(await call({"operation": "inspect"}))
    entered, release = asyncio.Event(), asyncio.Event()
    original = ns["store"].async_save
    async def save(value):
        entered.set()
        await release.wait()
        await original(value)
    ns["store"].async_save = AsyncMock(side_effect=save)
    owner = asyncio.create_task(call(request))
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert runtime["powerwall_settings_pending"] and not ns["_native_limits_allowed"](False)
    release.set()
    outcomes = await asyncio.gather(*tuple(vars(ns["store"])["_power_sync_update_tasks"]), return_exceptions=True)
    assert any(isinstance(value, m.SettingsRecoveryError) for value in outcomes)
    assert saved["tesla_settings_pending"] is None
    assert saved["tesla_settings_history"][0]["supervised_local_recovery"]
    assert runtime["powerwall_settings_pending"]  # Restart reconstructs the complete persisted decision.


@pytest.mark.asyncio
async def test_shared_gate_serializes_real_mutations_and_records_no_effect():
    from test_powerwall_control_coordinator import m as site_module
    site = site_module.PowerwallSiteControl("site")
    entered, release, mutated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def recover():
        async with site.recovery_readback(entry_id="entry", is_current=lambda: True):
            entered.set()
            await release.wait()
    async def mutate():
        mutated.set()
    recovery = asyncio.create_task(recover())
    await entered.wait()
    assert not site._effects
    write = asyncio.create_task(site.run_write("new write", mutate, entry_id="entry"))
    await asyncio.sleep(0)
    assert not mutated.is_set()
    release.set()
    await asyncio.gather(recovery, write)
    assert mutated.is_set() and len(site._effects) == 1
