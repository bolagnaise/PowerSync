"""Tesla event writes retain current authority through the shared site queue."""
from __future__ import annotations

import asyncio
import ast
import copy
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest


def event(kind, *, mode="self_consumption", restoring=False, send_result=True):
    harness = importlib.import_module("test_tesla_spike_session_tariff_baseline")
    raw = harness._FakeSession(
        tariff_rate_response=harness._FakeResponse(200, {"response": {"tariff_content_v2": harness.GENUINE_TARIFF}}),
        site_info_response=harness._FakeResponse(200, {"response": {"default_real_mode": mode}}))
    uploads = []
    namespace = harness._build_namespace(raw, uploads, send_result=send_result)
    if kind == "spike":
        manager = harness._make_aemo_manager(namespace, dict(harness.GENUINE_TARIFF))
    else:
        manager = harness._make_session_manager(namespace, dict(harness.GENUINE_TARIFF))
    manager._saved_operation_mode = mode
    manager._store = harness._FakeStore()
    session = NS(code="session-one", joined=True, session_type="saving", is_active=lambda: True,
                 start="start", end="end", octopoints_per_kwh=10)
    if kind == "session":
        manager._session_coordinator = NS(data={"sessions": [session]})
    if restoring:
        setattr(manager, "_in_spike_mode" if kind == "spike" else "_in_session_mode", True)
        manager._restore_pending = True
        if kind == "session":
            manager._active_session_code = session.code
    http = importlib.import_module(namespace["__package__"] + ".powerwall_control_http")
    control = importlib.import_module(namespace["__package__"] + ".powerwall_control")
    wrapped = http.controlled_session(manager.hass, raw)
    namespace["async_get_clientsession"] = lambda owner: wrapped
    return NS(manager=manager, runtime=manager.hass.data["power_sync"]["entry-1"],
              namespace=namespace, raw=raw, uploads=uploads, session=session,
              wrapped=wrapped, site=control.coordinator_for(manager.hass, site_id=manager.site_id))


async def enter(probe, kind):
    if kind == "spike":
        await probe.manager._enter_spike_mode(5000)
    else:
        await probe.manager._enter_session_mode(probe.session)


async def restore(probe, kind):
    if kind == "spike":
        await probe.manager._exit_spike_mode(0)
    else:
        await probe.manager._exit_session_mode()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
@pytest.mark.parametrize("change", ["monitoring", "runtime", "site", "force", "native", "quarantine", "baseline", "network"])
async def test_final_event_post_rechecks_authority_after_the_site_queue(kind, change):
    p = event(kind)
    queued = asyncio.Event()
    original = p.wrapped.post
    def post(*args, **kwargs):
        queued.set()
        return original(*args, **kwargs)
    p.wrapped.post = post
    async with p.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(enter(p, kind))
        await asyncio.wait_for(queued.wait(), timeout=2)
        assert p.raw.post_calls == []
        if change == "monitoring":
            p.manager.entry.options["monitoring_mode"] = True
        elif change == "runtime":
            p.manager.hass.data["power_sync"]["entry-1"] = {}
        elif change == "site":
            p.manager.entry.data["tesla_energy_site_id"] = "new-site"
        elif change == "force":
            p.runtime["force_charge_state"] = {"active": True}
        elif change == "native":
            p.runtime["powerwall_native_max_charge"] = NS(pending=True)
        elif change == "quarantine":
            p.runtime["powerwall_settings_pending"] = {"id": "unknown", "site_id": "site-1"}
        elif change == "baseline":
            p.manager._saved_operation_mode = "backup"
        else:
            p.runtime["network_export_guard"] = NS(manager=NS(snapshot=NS(mode="active")))
    await asyncio.wait_for(pending, timeout=2)
    assert all(payload["default_real_mode"] != "autonomous" for _url, payload in p.raw.post_calls)
    assert all(tariff["code"] == "AGL-TOU-1" for tariff in p.uploads)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
@pytest.mark.parametrize("owner", ["force", "local", "native", "restore", "startup", "native_recovery", "backup", "quarantine", "optimizer"])
async def test_background_event_never_overwrites_existing_control_owners(kind, owner):
    p = event(kind)
    key, value = {
        "force": ("force_discharge_state", {"active": True}),
        "local": ("powerwall_battery_dispatch", NS(pending=True)),
        "native": ("powerwall_native_max_charge", NS(pending=True)),
        "restore": ("pending_tesla_restore", {"pending": True}),
        "startup": ("native_max_startup_pending", True),
        "native_recovery": ("native_max_recovery_unavailable", True),
        "backup": ("max_backup_end_ts", 100),
        "quarantine": ("powerwall_settings_pending", {"id": "pending"}),
        "optimizer": ("optimization_coordinator", NS(_enabled=True)),
    }[owner]
    p.runtime[key] = value
    await enter(p, kind)
    assert p.raw.post_calls == [] and p.uploads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_stopped_or_disabled_event_still_restores_its_own_restrictive_baseline(kind):
    p = event(kind, mode="backup", restoring=True)
    p.manager._stopped = True
    p.manager.entry.options["aemo_spike_enabled"] = False
    p.runtime["powerwall_local_dispatch_stopping"] = True
    p.runtime["network_export_guard"] = NS(manager=NS(snapshot=NS(mode="active")))
    await restore(p, kind)
    assert [payload["default_real_mode"] for _url, payload in p.raw.post_calls] == ["self_consumption", "backup"]
    assert len(p.uploads) == 1 and p.uploads[0]["code"] == "AGL-TOU-1"
    assert p.manager._restore_pending is False
    assert p.manager._saved_tariff is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_failed_tariff_restore_cannot_reenable_autonomous_export(kind):
    p = event(kind, mode="autonomous", restoring=True, send_result=False)
    await restore(p, kind)
    assert [payload["default_real_mode"] for _url, payload in p.raw.post_calls] == ["self_consumption"]
    assert p.manager._restore_pending is True
    assert p.manager._store.save_calls[-1]["in_event"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_network_guard_permits_tariff_cleanup_but_not_autonomous_reentry(kind):
    p = event(kind, mode="autonomous", restoring=True)
    p.runtime["network_export_guard"] = NS(manager=NS(snapshot=NS(mode="active")))
    await restore(p, kind)
    assert [payload["default_real_mode"] for _url, payload in p.raw.post_calls] == ["self_consumption"]
    assert p.uploads[0]["code"] == "AGL-TOU-1"
    assert p.manager._restore_pending is True and p.manager._saved_tariff is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_tariff_callback_rechecks_event_authority_after_wait(kind):
    p = event(kind, mode="autonomous")
    entered, release = asyncio.Event(), asyncio.Event()
    checks = []
    async def upload(*args, is_current, **kwargs):
        entered.set()
        await release.wait()
        checks.append(is_current())
        return checks[-1]
    p.namespace["send_tariff_to_tesla"] = upload
    pending = asyncio.create_task(enter(p, kind))
    await asyncio.wait_for(entered.wait(), timeout=2)
    p.manager.entry.options["monitoring_mode"] = True
    release.set()
    await asyncio.wait_for(pending, timeout=2)
    assert checks == [False]
    assert p.manager._restore_pending is True


@pytest.mark.asyncio
async def test_saving_session_replacement_while_queued_cannot_start_old_session():
    p = event("session")
    guard = p.manager._event_guard(exporting=True)
    assert guard()
    # The full production entry's current closure additionally binds the live
    # session code; test it after the site queue has delayed submission.
    queued = asyncio.Event()
    original = p.wrapped.post
    def post(*args, **kwargs):
        queued.set()
        return original(*args, **kwargs)
    p.wrapped.post = post
    async with p.site.write_context("preceding mutation"):
        pending = asyncio.create_task(enter(p, "session"))
        await asyncio.wait_for(queued.wait(), timeout=2)
        p.manager._session_coordinator.data["sessions"] = []
    await asyncio.wait_for(pending, timeout=2)
    assert p.raw.post_calls == [] and p.uploads == []
    assert p.manager._restore_pending is True


@pytest.mark.asyncio
async def test_startup_pending_event_defers_until_runtime_and_quarantine_are_loaded():
    p = event("session", restoring=True)
    p.manager.hass.data["power_sync"].pop("entry-1")
    await restore(p, "session")
    assert p.raw.post_calls == [] and p.manager._restore_pending
    p.manager.hass.data["power_sync"]["entry-1"] = p.runtime
    await p.manager.check_and_handle_sessions()
    assert p.manager._restore_pending is False
    assert p.manager._saved_tariff is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_event_route_wait_cannot_overtake_a_new_force_owner(kind):
    p = event(kind)
    async with p.site.route_lock:
        pending = asyncio.create_task(enter(p, kind))
        await asyncio.sleep(0)
        assert not pending.done() and p.raw.get_calls == []
        p.runtime["force_discharge_state"] = {"active": True}
    await asyncio.wait_for(pending, timeout=2)
    assert p.raw.post_calls == [] and p.uploads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
@pytest.mark.parametrize("owner", ["force", "native", "restore", "quarantine"])
async def test_foreign_site_owner_blocks_event_before_baseline_capture(kind, owner):
    p = event(kind)
    p.manager._saved_tariff = None
    p.manager._saved_operation_mode = None
    other = {"entry": NS(data={"tesla_energy_site_id": p.manager.site_id}, options={})}
    key, value = {
        "force": ("force_discharge_state", {"active": True}),
        "native": ("powerwall_native_max_charge", NS(pending=True)),
        "restore": ("pending_tesla_restore", {"site_id": p.manager.site_id}),
        "quarantine": ("powerwall_settings_pending", {"site_id": p.manager.site_id}),
    }[owner]
    other[key] = value
    p.manager.hass.data["power_sync"]["entry-2"] = other
    await enter(p, kind)
    assert p.raw.get_calls == [] and p.raw.post_calls == [] and p.uploads == []
    assert p.manager._saved_tariff is None and p.manager._saved_operation_mode is None
    assert p.manager._store.save_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_foreign_owner_acquired_after_queue_blocks_final_event_write(kind):
    p = event(kind)
    queued = asyncio.Event()
    original = p.wrapped.post
    def post(*args, **kwargs):
        queued.set()
        return original(*args, **kwargs)
    p.wrapped.post = post
    async with p.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(enter(p, kind))
        await asyncio.wait_for(queued.wait(), timeout=2)
        p.manager.hass.data["power_sync"]["entry-2"] = {
            "entry": NS(data={"tesla_energy_site_id": p.manager.site_id}, options={}),
            "force_discharge_state": {"active": True},
        }
    await asyncio.wait_for(pending, timeout=2)
    assert p.raw.post_calls == [] and p.uploads == []
    assert p.manager._saved_tariff is not None and p.manager._restore_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_queued_foreign_write_cannot_enter_during_event_baseline_capture(kind):
    p = event(kind)
    harness = importlib.import_module("test_tesla_spike_session_tariff_baseline")
    http = importlib.import_module(p.namespace["__package__"] + ".powerwall_control_http")
    control = importlib.import_module(p.namespace["__package__"] + ".powerwall_control")
    reading, release_read, foreign_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    p.manager._saved_tariff = p.manager._saved_operation_mode = None
    p.raw._tariff_rate_response = harness._BlockingJsonResponse(
        200, {"response": {"tariff_content_v2": harness.GENUINE_TARIFF}}, reading, release_read)
    foreign_session = http.controlled_session(p.manager.hass, p.raw, entry_id="entry-2")

    async def foreign_write():
        foreign_started.set()
        async with foreign_session.post("https://fleet/api/1/energy_sites/site-1/grid_import_export",
                json={"customer_preferred_export_rule": "battery_ok"}):
            pass

    pending_event = None
    try:
        async with p.site.write_context("preceding physical mutation"):
            pending_foreign = asyncio.create_task(foreign_write())
            await asyncio.wait_for(foreign_started.wait(), timeout=2)
            await asyncio.sleep(0)
            assert not pending_foreign.done() and p.raw.post_calls == []
            pending_event = asyncio.create_task(enter(p, kind))
            await asyncio.wait_for(reading.wait(), timeout=2)
            assert not getattr(p.manager, "_in_spike_mode" if kind == "spike" else "_in_session_mode")
            assert p.manager._saved_tariff is None
        with pytest.raises(control.ControlWriteRejected):
            await asyncio.wait_for(pending_foreign, timeout=2)
        assert p.raw.post_calls == []
    finally:
        release_read.set()
        if pending_event is not None:
            await asyncio.wait_for(pending_event, timeout=2)
    assert getattr(p.manager, "_in_spike_mode" if kind == "spike" else "_in_session_mode")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["spike", "session"])
async def test_unrelated_site_owner_does_not_block_same_entry_event_cleanup(kind):
    p = event(kind, mode="backup", restoring=True)
    p.manager.hass.data["power_sync"]["entry-2"] = {
        "entry": NS(data={"tesla_energy_site_id": "different-site"}, options={}),
        "force_discharge_state": {"active": True},
    }
    await restore(p, kind)
    assert [payload["default_real_mode"] for _url, payload in p.raw.post_calls] == ["self_consumption", "backup"]
    assert p.manager._restore_pending is False


@pytest.mark.asyncio
async def test_saving_session_persists_original_baseline_before_first_mutation():
    p = event("session")
    events = []
    p.raw.events = events
    p.manager._store.events = events
    await enter(p, "session")
    first_write = next(index for index, value in enumerate(events) if value[0] == "post")
    first_save = next(index for index, value in enumerate(events) if value[0] == "save")
    assert first_save < first_write
    assert events[first_save][1]["saved_operation_mode"] == "self_consumption"
    assert events[first_save][1]["saved_tariff"]["code"] == "AGL-TOU-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_failed", [False, True])
async def test_actual_startup_retry_runs_after_native_fence_and_preserves_failed_recovery(native_failed):
    p = event("session", restoring=True)
    p.runtime["native_max_startup_pending"] = True
    p.manager._store._data = {"in_event": True, "saved_tariff": dict(p.manager._saved_tariff),
        "saved_operation_mode": p.manager._saved_operation_mode,
        "active_session_code": p.manager._active_session_code}
    path = Path(__file__).parents[1] / "custom_components/power_sync/__init__.py"
    setup = next(node for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
    index = next(index for index, node in enumerate(setup.body) if isinstance(node, ast.For)
                 and isinstance(node.target, ast.Name) and node.target.id == "event_manager")
    gate, retry = setup.body[index-1:index+1]
    assert isinstance(gate, ast.AsyncWith)
    run = ast.parse("async def run(): pass").body[0]
    run.body = copy.deepcopy([gate, retry])
    namespace = dict(_local_powerwall_route_lock=p.site.route_lock,
        _local_powerwall_runtime=p.runtime, _native_max_store=NS(async_load=AsyncMock(
            return_value=None, side_effect=OSError("store unavailable") if native_failed else None)),
        aemo_spike_manager=None, saving_session_tariff_manager=p.manager,
        _LOGGER=logging.getLogger(__name__))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[run], type_ignores=[])),
                 str(path), "exec"), namespace)
    await namespace["run"]()
    assert "native_max_startup_pending" not in p.runtime
    if native_failed:
        assert p.runtime["native_max_recovery_unavailable"] is True
        assert p.raw.post_calls == [] and p.manager._restore_pending
    else:
        assert p.manager._restore_pending is False and p.manager._saved_tariff is None
