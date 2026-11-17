"""Unattended reconciliation of retained native EV commands after restart."""
import asyncio
from copy import deepcopy
import importlib
import time
from types import SimpleNamespace

import pytest

from test_tesla_native_recovery import case, harness, recovery, VIN_A, VIN_B


@pytest.fixture(autouse=True)
def existing_telemetry(case, monkeypatch):
    from power_sync import tesla_native_recovery as native
    from power_sync import tesla_ev_observation as observation
    c = case
    c.live_read = observation.async_read_tesla_vehicle
    c.live_independent = observation.async_read_independent_tesla_vehicle

    async def cached(_hass, _vin, _preferred):
        c.calls.append(("existing", {}))
        sample = SimpleNamespace(**c.binding, source_charge_timestamp=time.time(),
            charge_state="Stopped", charger_actual_current=0, charger_power=0,
            requested_current=6, charge_limit_soc=80)
        if c.hook["read"]:
            await c.hook["read"](sample, len(c.calls))
        return sample

    async def forbidden(*args, **kwargs):
        raise AssertionError("Automatic recovery must not call a live provider reader")

    monkeypatch.setattr(native, "_cached_vehicle_report", cached)
    monkeypatch.setattr(native, "CACHED_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(observation, "async_read_tesla_vehicle", forbidden)
    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", forbidden)


def worker(c):
    module = importlib.import_module("power_sync.tesla_ev_auto_recovery")
    instance = module.NativeRecoveryWorker(c.hass, c.entry, c.runtime)
    c.runtime[module.WORKER_KEY] = instance
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    {"platform": platform, "operation": "start", "only_start": True}
    for platform in ("tesla_fleet", "teslemetry", "tessie")
], indirect=True)
async def test_worker_recovers_first_native_start_without_dynamic_session(case):
    c = case
    instance = worker(c)
    assert "dynamic_ev_state" not in c.runtime
    await instance.async_tick()
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "ha"
    assert [name for name, _ in c.calls] == ["existing", "existing"]
    assert not c.hass.services.calls
    assert c.runtime[recovery._SIDECAR_STATE_KEY]["automatic_native_recovery"][VIN_A]["state"] == "reconciled"
    count = len(c.calls)
    await instance.async_tick()
    assert len(c.calls) == count
    assert all(row["outcome"] == "sent_unknown" and row["recovery_retired"]
        for row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].values())


@pytest.mark.asyncio
async def test_worker_backoff_is_finite_and_survives_restart(case, monkeypatch):
    c = case
    instance = worker(c)
    module = importlib.import_module("power_sync.tesla_ev_auto_recovery")
    now = module.time.time()
    budget = {"attempts": 20, "last_attempt_at": now,
        "next_attempt_at": now + 3600, "state": "waiting_for_vehicle_data"}
    c.runtime[recovery._SIDECAR_STATE_KEY]["automatic_native_recovery"] = {VIN_A: budget}
    assert await recovery.async_persist_recovery_state(c.hass, c.entry)
    await instance.async_tick()
    assert c.calls == []
    # A restart must not shorten the backoff.
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    await worker(c).async_tick()
    assert c.calls == []
    # After the finite delay another cache-only attempt is permitted.
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: now + 3601))
    await worker(c).async_tick()
    assert c.calls
    saved = c.store.value["automatic_native_recovery"][VIN_A]
    assert saved["attempts"] == 21
    assert saved["next_attempt_at"] - saved["last_attempt_at"] == module.MAX_RETRY_SECONDS


@pytest.mark.asyncio
async def test_worker_refuses_changed_runtime_before_any_provider_read(case):
    c = case
    instance = worker(c)
    replacement = {}
    c.hass.data["power_sync"][c.entry.entry_id] = replacement
    await instance.async_tick()
    assert not c.calls and replacement == {}


@pytest.mark.asyncio
async def test_worker_does_not_refresh_when_producer_is_rebound(case):
    c = case
    instance = worker(c)
    c.registry["number.fallback_current"].unique_id = "replacement-producer"
    await instance.async_tick()
    assert not c.calls
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_worker_retains_malformed_budget_without_provider_io(case):
    c = case
    instance = worker(c)
    c.runtime[recovery._SIDECAR_STATE_KEY]["automatic_native_recovery"] = {VIN_A: "invalid-history"}
    await instance.async_tick()
    assert not c.calls
    assert c.runtime[recovery._SIDECAR_STATE_KEY]["automatic_native_recovery"][VIN_A] == "invalid-history"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["unchanged", "charging", "wrong_identity", "save_failed"])
async def test_worker_failure_keeps_hold_and_persists_read_budget(case, variant, monkeypatch):
    c = case
    instance = worker(c)
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    stamp = None
    if variant == "unchanged":
        from power_sync import tesla_native_recovery
        monkeypatch.setattr(tesla_native_recovery, "SAMPLE_SECONDS", 0.03)

    async def alter(sample, index):
        nonlocal stamp
        if variant == "unchanged":
            stamp = stamp or sample.source_charge_timestamp
            sample.source_charge_timestamp = stamp
        elif variant == "charging":
            sample.charge_state = "Charging"
            sample.charger_actual_current = 6
            sample.charger_power = 1
        elif variant == "wrong_identity":
            sample.device_id = "replacement-device"

    c.hook["read"] = alter
    if variant == "save_failed":
        c.store.fail = True
    await instance.async_tick()
    assert c.runtime[recovery.RECOVERY_KEY] == before
    assert not c.hass.services.calls
    count = len(c.calls)
    if variant == "save_failed":
        assert count == 0  # No provider I/O until the budget is durable.
        return
    state = c.store.value["automatic_native_recovery"][VIN_A]
    assert state["attempts"] == 1
    assert state["next_attempt_at"] > state["last_attempt_at"]
    await instance.async_tick()
    assert len(c.calls) == count
    # Reload cannot reset automatic retries or the provider-call budget.
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    await worker(c).async_tick()
    assert len(c.calls) == count


@pytest.mark.asyncio
async def test_worker_cancellation_retains_history_and_stops_before_replacement(case):
    c = case
    instance = worker(c)
    started = asyncio.Event()

    async def hang(sample, index):
        started.set()
        await asyncio.Event().wait()

    c.hook["read"] = hang
    task = asyncio.create_task(instance.async_tick())
    await started.wait()
    await instance.async_stop()
    assert task.done()
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"
    assert c.store.value["automatic_native_recovery"][VIN_A]["attempts"] == 1
    assert not c.hass.services.calls


@pytest.mark.asyncio
async def test_worker_does_not_overlap_or_consume_another_vins_history(case):
    c = case
    instance = worker(c)
    started, release = asyncio.Event(), asyncio.Event()

    async def pause(sample, index):
        if index == 1:
            started.set()
            await release.wait()

    c.hook["read"] = pause
    c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_B] = {"route": None, "attempts": {}, "hazards": {}}
    other = deepcopy(c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_B])
    task = asyncio.create_task(instance.async_tick())
    await started.wait()
    await instance.async_tick()
    assert len(c.calls) == 1
    release.set()
    await task
    assert c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_B] == other
    assert not c.hass.services.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_phase", [0, 7, 17, 29])
async def test_cache_recovery_waits_for_real_provider_poll_phases(case, monkeypatch, poll_phase):
    """Ordinary 30s polling, not manufactured timestamps per cache read, supplies proof."""
    from power_sync import tesla_native_recovery as native
    c = case
    clock = {"now": time.time()}
    base = clock["now"] - poll_phase

    async def cached(*_args):
        acquired = base + int((clock["now"] - base) // 30) * 30
        c.calls.append(("existing", {"timestamp": acquired}))
        return SimpleNamespace(**c.binding, source_charge_timestamp=acquired,
            charge_state="Stopped", charger_actual_current=0, charger_power=0,
            requested_current=6, charge_limit_soc=80)

    async def wait(awaitable, timeout):
        awaitable.close()
        clock["now"] += timeout
        raise TimeoutError

    monkeypatch.setattr(native, "time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(native, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": wait}))
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 3)
    monkeypatch.setattr(native, "CACHED_TIMEOUT_SECONDS", 75)
    monkeypatch.setattr(native, "_cached_vehicle_report", cached)
    await worker(c).async_tick()
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "ha"
    stamps = [kw["timestamp"] for _, kw in c.calls]
    assert stamps[-1] - stamps[0] == 30
    assert len(stamps) >= 2 and not c.hass.services.calls


@pytest.mark.asyncio
async def test_lifecycle_timer_is_unique_and_stopped_before_late_callback(case, monkeypatch):
    import sys
    import types
    module = importlib.import_module("power_sync.tesla_ev_auto_recovery")
    timers, stopped = [], []
    event = types.ModuleType("homeassistant.helpers.event")

    def track(_hass, callback, interval):
        timers.append((callback, interval.total_seconds()))
        return lambda: stopped.append(True)

    event.async_track_time_interval = track
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", event)
    module.start(case.hass, case.entry)
    module.start(case.hass, case.entry)
    assert len(timers) == 1 and timers[0][1] == 60
    assert not case.calls
    await timers[0][0]()
    count = len(case.calls)
    await module.async_stop(case.hass, case.entry)
    assert stopped == [True]
    await timers[0][0]()
    assert len(case.calls) == count and not case.hass.services.calls


@pytest.mark.asyncio
async def test_contradictory_passive_sample_discards_the_pair(case, monkeypatch):
    from power_sync import tesla_native_recovery as native
    c = case
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 0.03)

    async def contradict(sample, index):
        if index == 2:
            sample.charge_state = "Charging"
            sample.charger_actual_current = 6
            sample.charger_power = 1

    c.hook["read"] = contradict
    await worker(c).async_tick()
    assert len(c.calls) == 2
    assert c.runtime[recovery.RECOVERY_KEY] == before


@pytest.mark.asyncio
async def test_foreground_live_recovery_preempts_passive_wait_without_transferring_permission(case, monkeypatch):
    from power_sync import tesla_native_recovery as native
    from power_sync import tesla_ev_observation as observation
    c = case
    first = asyncio.Event()
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 10)
    monkeypatch.setattr(native, "CACHED_TIMEOUT_SECONDS", 30)

    async def accepted(sample, index):
        first.set()

    c.hook["read"] = accepted
    task = asyncio.create_task(worker(c).async_tick())
    await first.wait()
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 0.001)
    monkeypatch.setattr(observation, "async_read_tesla_vehicle", c.live_read)
    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", c.live_independent)
    assert await native.async_reconcile_native_history(c.hass, c.entry, VIN_A, authorize=lambda: True)
    assert task.done()
    assert [name for name, _ in c.calls] == ["existing", "cache", "independent", "cache", "independent"]
    assert c.runtime[recovery._SIDECAR_STATE_KEY]["automatic_native_recovery"][VIN_A]["allow_paid_refresh"] is False
    assert not c.hass.services.calls


@pytest.mark.asyncio
async def test_advancing_data_can_shorten_backoff_only_after_core_minimum(case, monkeypatch):
    c = case
    instance = worker(c)
    module = importlib.import_module("power_sync.tesla_ev_auto_recovery")
    now = time.time()
    state = c.runtime[recovery._SIDECAR_STATE_KEY]
    state["automatic_native_recovery"] = {VIN_A: {"attempts": 4,
        "last_attempt_at": now - 10, "last_source_timestamp": now - 20,
        "next_attempt_at": now + 3600, "state": "waiting_for_vehicle_data"}}
    state["native_reconciliation"] = {VIN_A: {"success": False, "retry_after": now + 60}}
    assert await instance._reserve(VIN_A, now) is None
    assert state["automatic_native_recovery"][VIN_A]["attempts"] == 4
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: now + 65))
    record = await instance._reserve(VIN_A, now)
    assert record["attempts"] == 5 and not c.calls


@pytest.mark.asyncio
async def test_retired_runtime_cannot_save_late_passive_evidence(case, monkeypatch):
    from power_sync import tesla_native_recovery as native
    c = case
    instance = worker(c)
    first = asyncio.Event()
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 10)
    monkeypatch.setattr(native, "CACHED_TIMEOUT_SECONDS", 30)

    async def sampled(sample, index):
        first.set()

    c.hook["read"] = sampled
    task = asyncio.create_task(instance.async_tick())
    await first.wait()
    before = deepcopy(c.store.value)
    replacement = {}
    c.hass.data["power_sync"][c.entry.entry_id] = replacement
    await instance.async_stop()
    assert task.done()
    assert c.store.value == before and replacement == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["new_session", "producer_changed", "no_owner", "foreign_owner", "cleanup_only", "revoked", "save_failed", "finite_cleanup", "owner_changed_during_save", "producer_changed_during_save"])
async def test_new_plan_reclaims_only_completed_same_producer_session_route(case, monkeypatch, variant):
    """Completed history must not strand a fresh owned plan on its old lease."""
    import ast
    from collections.abc import Mapping
    from pathlib import Path
    from conftest import load_source_node
    c = case
    await worker(c).async_tick()
    c.owners[VIN_A] = {"owner": "powersync", "owner_mode": "solar_surplus", "session_id": "old-session"}
    ticket = await recovery.async_begin_attempt(c.hass, c.entry, VIN_A,
        {"kind": "ha"}, "start", "switch.fallback")
    assert ticket and await recovery.async_finish_attempt(ticket, "accepted_unconfirmed")
    history = deepcopy(c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"])
    c.owners[VIN_A] = {"owner": "powersync", "owner_mode": "solar_surplus", "session_id": "new-session"}
    if variant == "producer_changed":
        c.registry["switch.fallback"].unique_id = "replacement"
    elif variant == "no_owner":
        c.owners.pop(VIN_A)
    elif variant == "foreign_owner":
        c.owners[VIN_A]["owner"] = "external"
    elif variant == "cleanup_only":
        c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"]["cleanup_only"] = True
    elif variant == "save_failed":
        c.store.fail = True
    elif variant == "finite_cleanup":
        c.runtime[recovery._SIDECAR_STATE_KEY]["obligations"]["ev_quick_control_obligations"] = {VIN_A: {"status": "pending_cleanup"}}
    elif variant in {"owner_changed_during_save", "producer_changed_during_save"}:
        save = c.store.async_save

        async def changed(value):
            if variant == "owner_changed_during_save":
                c.owners[VIN_A]["session_id"] = "intervening-session"
            else:
                c.registry["switch.fallback"].unique_id = "intervening-producer"
            return await save(value)

        monkeypatch.setattr(c.store, "async_save", changed)
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "route_identity_changed"
    diagnostics = recovery.ev_recovery_status(c.hass, c.entry, VIN_A)["native_route_diagnostics"]
    assert diagnostics["producer_binding_matches"] is (variant != "producer_changed")
    assert diagnostics["session_scope_matches"] is False
    source = Path(__file__).parents[1] / "custom_components/power_sync/automations/actions.py"
    node = load_source_node(source, "_tesla_transport_route")
    scope = {"__name__": "power_sync.automations._route_probe", "__package__": "power_sync.automations",
        "DOMAIN": "power_sync", "Mapping": Mapping,
        "_tesla_transport_authority": lambda *a, **k: lambda: variant != "revoked"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(source), "exec"), scope)
    route = await scope["_tesla_transport_route"](c.hass, c.entry, {"vehicle_vin": VIN_A}, "start")
    assert route["kind"] == ("ha" if variant == "new_session" else "blocked")
    if variant == "new_session":
        assert route["session_scope"]["session_id"] == "new-session"
    assert c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"] == history
    assert not c.hass.services.calls
