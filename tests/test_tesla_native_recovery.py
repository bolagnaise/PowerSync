"""Operational recovery of legacy native commands without rewriting history."""
import asyncio
from copy import deepcopy
import importlib
from types import SimpleNamespace
import time

import pytest
import pytest_asyncio

from test_tesla_ev_recovery import harness, _sidecar_harness, _Entity, VIN_A, VIN_B, recovery
from power_sync import tesla_native_recovery as native


@pytest_asyncio.fixture
async def case(harness, monkeypatch, request):
    hass, entry, store, registry, owners = _sidecar_harness(harness)
    # Match the real installed Fleet inventory: one Rate with no saved value,
    # five Stops, no terminal proof for any of the six.
    import sys
    monkeypatch.setitem(sys.modules, "power_sync.tesla_ev_recovery", recovery)
    monkeypatch.setattr(sys.modules["power_sync"], "tesla_ev_recovery", recovery, raising=False)
    entry.data.update(ev_provider="fleet_api")
    parameter = getattr(request, "param", "rate")
    platform = parameter.get("platform", "teslemetry") if isinstance(parameter, dict) else "teslemetry"
    operation = parameter.get("operation", "rate") if isinstance(parameter, dict) else parameter
    for registration in registry.values():
        if registration.platform == "teslemetry":
            registration.platform = platform
    registry["number.fallback_current"] = _Entity("number.fallback_current", device="native-device",
                                                 entry="native-entry", platform=platform)
    registry["number.fallback_limit"] = _Entity("number.fallback_limit", device="native-device",
                                               entry="native-entry", platform=platform)
    assert await recovery.async_load_recovery_state(hass, entry)
    monkeypatch.setattr(recovery, "time", SimpleNamespace(time=lambda: time.time()-180, monotonic=time.monotonic))
    for operation in [operation, *(["stop"] * 5)]:
        entity = {"rate": "number.fallback_current", "limit": "number.fallback_limit"}.get(operation, "switch.fallback")
        ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, {"kind": "ha"}, operation, entity)
        assert ticket
        assert await recovery.async_finish_attempt(ticket, "sent_unknown")
    monkeypatch.setattr(recovery, "time", time)
    runtime = hass.data["power_sync"][entry.entry_id]
    # Keep the actual source acquisition API; fake its I/O endpoints only.
    observation = importlib.import_module("power_sync.tesla_ev_observation")
    calls, hook = [], {"read": None}
    binding = dict(vin=VIN_A, domain=platform, config_entry_id="native-entry",
                   device_id="native-device", producer_identity="source-1")
    monkeypatch.setattr(observation, "independent_tesla_source_binding", lambda *a, **k: dict(binding))

    async def cached(*a, **kw):
        calls.append(("cache", kw))
        return None # normal HA refresh keeps its stale charge document

    async def independent(*a, **kw):
        calls.append(("independent", kw))
        assert kw["authorize"]()
        sample = SimpleNamespace(**binding, source_charge_timestamp=time.time(), charge_state="Stopped",
                                 charger_actual_current=0, charger_power=0, requested_current=6, charge_limit_soc=80)
        if hook["read"]:
            await hook["read"](sample, len([x for x in calls if x[0] == "independent"]))
        return sample

    monkeypatch.setattr(observation, "async_read_tesla_vehicle", cached)
    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", independent)
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 0.001)
    return SimpleNamespace(hass=hass, entry=entry, store=store, registry=registry, owners=owners,
                           runtime=runtime, calls=calls, hook=hook, binding=binding)


async def reconcile(c, authorize=lambda: True):
    return await native.async_reconcile_native_history(c.hass, c.entry, VIN_A, authorize=authorize)


@pytest.mark.asyncio
async def test_legacy_six_unknowns_recover_and_start_rate_stop_work_after_reload(case):
    c = case
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"
    assert await reconcile(c)
    assert [x[0] for x in c.calls] == ["cache", "independent", "cache", "independent"]
    assert all(kw["allow_paid_refresh"] for key, kw in c.calls if key == "independent")
    assert recovery.current_vehicle_generation(c.hass, c.entry, VIN_A) == 1
    for aid, row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].items():
        old = before["vehicles"][VIN_A]["attempts"][aid]
        assert row["outcome"] == "sent_unknown"
        assert {k: v for k, v in row.items() if k != "recovery_retired"} == old
        assert row["recovery_retired"]["historical_outcome"] == "unknown"
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "ha"
    for op in ("start", "rate", "limit", "stop"):
        ticket = await recovery.async_begin_attempt(c.hass, c.entry, VIN_A, {"kind": "ha"}, op, "switch.fallback")
        assert ticket
        assert await recovery.async_finish_attempt(ticket, "accepted_unconfirmed")
    assert not c.hass.services.calls # recovery itself never commands


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["stale", "wrong_vin", "wrong_producer", "charging", "power", "current",
                                     "missing_setting", "settings_changed", "owner", "registry", "new_stop", "cancel", "save"])
async def test_recovery_failure_preserves_original_hold_and_retries_are_bounded(case, variant):
    c = case
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])

    async def change(sample, index):
        if variant == "stale": sample.source_charge_timestamp = time.time()-90
        elif variant == "wrong_vin": sample.vin = VIN_B
        elif variant == "wrong_producer": sample.device_id = "other"
        elif variant == "charging": sample.charge_state = "Charging"
        elif variant == "power": sample.charger_power = 1
        elif variant == "current": sample.charger_actual_current = 1
        elif variant == "missing_setting": sample.requested_current = None
        elif variant == "settings_changed" and index == 2: sample.requested_current = 32
        elif variant == "owner": c.owners[VIN_A]["session_id"] = "changed"
        elif variant == "registry": c.registry["switch.fallback"].unique_id = "changed"
        elif variant == "new_stop":
            assert recovery.recovery_control_allowed(c.hass, c.entry, VIN_A, 0, "stop")
        elif variant == "cancel": raise asyncio.CancelledError
        elif variant == "save": c.store.fail = True
    c.hook["read"] = change
    if variant == "cancel":
        with pytest.raises(asyncio.CancelledError): await reconcile(c)
    else:
        assert not await reconcile(c)
    assert c.runtime[recovery.RECOVERY_KEY] == before
    assert native.ACTIVE_KEY not in c.runtime
    count = len(c.calls)
    if variant == "new_stop":
        # An intentional Stop pre-emption must not impose a read-failure
        # cooldown on the user's next fresh Start request.
        c.hook["read"] = None
        assert await reconcile(c)
        assert len(c.calls) == count + 4
    else:
        assert not await reconcile(c)
        assert len(c.calls) == count
    assert not c.hass.services.calls


@pytest.mark.asyncio
async def test_active_submission_and_positive_callbacks_cannot_cross_reconciliation(case):
    c = case
    child = asyncio.create_task(asyncio.Event().wait())
    c.runtime[recovery._SUBMISSIONS_KEY] = {"live": child}
    try:
        assert not await reconcile(c)
        assert not c.calls and not child.cancelled()
    finally:
        child.cancel()
        with pytest.raises(asyncio.CancelledError): await child
        c.runtime.pop(recovery._SUBMISSIONS_KEY)

    async def callback(sample, index):
        assert not recovery.recovery_control_allowed(c.hass, c.entry, VIN_A, 0, "start")
        assert await recovery.async_begin_attempt(c.hass, c.entry, VIN_A, {"kind": "ha"}, "start", "switch.fallback") is None
    c.hook["read"] = callback
    assert await reconcile(c)
    assert not recovery.recovery_control_allowed(c.hass, c.entry, VIN_A, 0, "start")
    assert recovery.recovery_control_allowed(c.hass, c.entry, VIN_A, 1, "start")


@pytest.mark.asyncio
async def test_new_unknown_after_recovery_requires_new_samples_and_settling(case):
    c = case
    assert await reconcile(c)
    ticket = await recovery.async_begin_attempt(c.hass, c.entry, VIN_A, {"kind": "ha"}, "rate", "number.fallback_current")
    assert ticket and await recovery.async_finish_attempt(ticket, "sent_unknown")
    count = len(c.calls)
    assert not await reconcile(c)
    assert len(c.calls) == count
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"
    refs = recovery._unresolved_liabilities(c.runtime)
    assert [ref["attempt_id"] for ref in refs] == [ticket.attempt_id]


@pytest.mark.asyncio
async def test_obsolete_cleanup_scope_is_reconciled_without_adopting_an_old_lease(case):
    c = case
    ledger = c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    ledger["route"]["cleanup_only"] = True
    ledger["route"]["session_scope"] = {"session_id": "expired-owner"}
    assert await recovery._persist(c.hass, c.entry, c.runtime)
    assert await reconcile(c)
    route = recovery.get_vehicle_route(c.hass, c.entry, VIN_A)
    assert route["kind"] == "ha" and "cleanup_only" not in route and "session_scope" not in route


@pytest.mark.asyncio
async def test_other_vehicle_is_preserved_and_other_entry_writer_refuses(case):
    c = case
    other = {"route": None, "attempts": {}, "hazards": {}}
    c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_B] = deepcopy(other)
    c.hass.data["power_sync"]["other-entry"] = {
        recovery.RECOVERY_KEY: deepcopy(c.runtime[recovery.RECOVERY_KEY])}
    assert not await reconcile(c) and not c.calls
    c.hass.data["power_sync"].pop("other-entry")
    assert await reconcile(c)
    assert c.runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_B] == other


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_change", [None, "vin", "deadline"])
async def test_finite_native_discovery_recovers_before_it_captures_a_missing_binding(case, monkeypatch, scope_change):
    # Execute the production resolver and its recovery wrapper, including the
    # real recovery manager/Store. Only source I/O and the identity resolver
    # are faked; returning an entity is the precondition finite Start lacked.
    import ast
    from pathlib import Path
    from typing import Mapping
    from conftest import load_source_node
    commands = importlib.import_module("power_sync.teslemetry_bluetooth_commands")
    monkeypatch.setattr(commands, "native_command_entity", lambda h, entity, op: "switch.fallback")
    c = case
    path = Path(__file__).resolve().parents[1] / "custom_components/power_sync/automations/actions.py"
    nodes = [load_source_node(path, name) for name in ("_tesla_native_deadline_token", "_tesla_reconcile_native_history", "_tesla_ha_command_entity")]
    scope = {"__name__": "power_sync.automations.actions", "__package__": "power_sync.automations",
             "Mapping": Mapping, "Any": __import__("typing").Any, "datetime": __import__("datetime").datetime,
             "math": __import__("math"), "_tesla_transport_authority": lambda *a, **k: lambda: True}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "native-selection", "exec"), scope)
    params = {"vehicle_vin": VIN_A, "_boost_start_deadline": time.time()+20}
    if scope_change:
        async def change(sample, index):
            if scope_change == "vin": params["vehicle_vin"] = VIN_B
            else: params["_boost_start_deadline"] += 100
        c.hook["read"] = change
        with pytest.raises(recovery.RecoverySubmissionDenied, match="request changed"):
            await scope["_tesla_ha_command_entity"](c.hass, c.entry, params, "start")
        assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"
    else:
        assert await scope["_tesla_ha_command_entity"](c.hass, c.entry, params, "start") == "switch.fallback"
        assert len(c.calls) == 4
        assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "ha"


@pytest.mark.asyncio
async def test_authority_loss_and_host_reload_never_release(case):
    c = case
    allowed = [False]
    assert not await reconcile(c, lambda: allowed[0]) and not c.calls
    allowed[0] = True
    replacement = {}
    async def reload(sample, index):
        c.hass.data["power_sync"][c.entry.entry_id] = replacement
    c.hook["read"] = reload
    assert not await reconcile(c, lambda: allowed[0])
    assert c.hass.data["power_sync"][c.entry.entry_id] is replacement
    assert all(not row.get("recovery_retired") for row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].values())


@pytest.mark.asyncio
async def test_whole_second_vehicle_timestamps_do_not_create_an_impossible_freshness_gate(case, monkeypatch):
    c = case
    clock = [int(time.time()) + 0.5]
    monkeypatch.setattr(native, "time", SimpleNamespace(time=lambda: clock[0]))
    async def seconds(sample, index):
        if index == 2:
            clock[0] += 4
        sample.source_charge_timestamp = int(clock[0])
    c.hook["read"] = seconds
    assert await reconcile(c)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["start", "limit", "stop"], indirect=True)
async def test_native_all_operation_types_share_operational_recovery(case):
    c = case
    assert await reconcile(c)
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["kind"] == "ha"
    assert all(row["outcome"] == "sent_unknown" and row["recovery_retired"]["phase"] == "installed"
               for row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].values())


@pytest.mark.asyncio
async def test_status_and_read_backoff_survive_reload_without_reblocking_completed_history(case):
    c = case
    async def stale(sample, index): sample.source_charge_timestamp = time.time()-90
    c.hook["read"] = stale
    assert not await reconcile(c)
    status = recovery.ev_recovery_status(c.hass, c.entry, VIN_A)
    assert len(status["historical_unknown_attempts"]) == 6
    assert status["native_last_reconciliation"]["retry_after"] > time.time()
    count = len(c.calls)
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    c.runtime.pop(native.BACKOFF_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    assert not await reconcile(c) and len(c.calls) == count
    # Advance only the saved test cooldown, not a command deadline/attempt.
    c.runtime[recovery._SIDECAR_STATE_KEY][native.RESULT_KEY][VIN_A]["retry_after"] = 0
    c.hook["read"] = None
    assert await reconcile(c)
    status = recovery.ev_recovery_status(c.hass, c.entry, VIN_A)
    assert status["recovery_required"] is False
    assert not status["retained_attempts"]
    assert len(status["historical_unknown_attempts"]) == 6
    assert all(row["operationally_reconciled"] for row in status["historical_unknown_attempts"])
    assert status["native_last_reconciliation"]["phase"] == "installed"


@pytest.mark.asyncio
async def test_recovery_settles_only_exact_car_reservation_through_shared_allocator(case, monkeypatch):
    c = case
    from power_sync.native_rate_budget import SharedRateBudget
    clock = [time.time()+1]
    monkeypatch.setattr(native, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(native, "METER_SAMPLE_SECONDS", 0.001)
    c.owners[VIN_A] = {} # the previous session has been torn down, as in manual Start
    class Store:
        def __init__(self): self.value = {}
        async def async_save(self, value): self.value = deepcopy(value)
    store = Store()
    budget = SharedRateBudget(store, clock=lambda: clock[0])
    budget.loaded = True
    identity = ":".join(c.registry["switch.fallback"].__dict__[k]
                        for k in ("platform", "config_entry_id", "device_id", "unique_id"))
    record = {"entry_id": c.entry.entry_id, "site": "site", "identity": identity,
              "entity_id": "switch.fallback", "reserved_w": 18400, "target_w": 18400,
              "target_amps": 80, "submitted_at": clock[0]-180, "uncertain": True}
    other = dict(record, entry_id="other-entry", identity="teslemetry:other:other:other")
    budget.managed = {VIN_A: record, VIN_B: deepcopy(other)}
    c.hass.data["power_sync_native_rate_budget"] = budget
    local = SimpleNamespace(last_update_success=True, last_success_ts=clock[0])
    async def meter(): local.last_success_ts = clock[0]
    local.async_request_refresh = meter
    c.runtime["powerwall_local"] = {"coordinator": local}
    async def read(sample, index):
        if index == 2: clock[0] += 5
        sample.source_charge_timestamp = clock[0]
    c.hook["read"] = read
    assert await reconcile(c)
    assert VIN_A not in budget.managed
    assert budget.managed[VIN_B] == other
    assert store.value["managed"] == {VIN_B: other}
    assert c.runtime[recovery._SIDECAR_STATE_KEY][native.RESULT_KEY][VIN_A]["headroom_reconciled"]
    # The saved allocation remains a normal budget projection with the other
    # car's full demand; no recovery-specific exemption is installed.
    assert store.value["managed"][VIN_B]["reserved_w"] == 18400


@pytest.mark.asyncio
async def test_cancel_during_committing_store_drains_and_restores_a_conservative_journal(case):
    c = case
    original_save = c.store.async_save
    entered, finish = asyncio.Event(), asyncio.Event()
    async def save(value):
        rows = value["recovery"]["vehicles"][VIN_A]["attempts"].values()
        if any(row.get("recovery_retired") for row in rows):
            entered.set()
            await finish.wait()
        return await original_save(value)
    c.store.async_save = save
    task = asyncio.create_task(reconcile(c))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert native.ACTIVE_KEY in c.runtime
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert all(not row.get("recovery_retired") for row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].values())
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{"platform": "tesla_fleet"}, {"platform": "tessie"}], indirect=True)
async def test_native_recovery_uses_the_same_exact_source_for_each_supported_provider(case):
    c = case
    assert await reconcile(c)
    assert [kind for kind, _ in c.calls] == ["cache", "independent", "cache", "independent"]
    assert all(kwargs["allow_paid_refresh"] is False for kind, kwargs in c.calls if kind == "independent")
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["entity_identity"]["platform"] == c.binding["domain"]


@pytest.mark.asyncio
async def test_real_control_guard_allows_stop_to_preempt_recovery_before_waiting_for_owner_lock(case, monkeypatch):
    import ast
    from contextlib import nullcontext
    from contextvars import ContextVar
    from functools import wraps
    from inspect import signature
    from pathlib import Path
    from conftest import load_source_node
    c = case
    gate = SimpleNamespace(lock=asyncio.Lock(), owner=None, cleanup_active={}, blocked=lambda _: False)
    async def load(): pass
    async def ownership(*_): return gate
    gate.load = load
    path = Path(__file__).resolve().parents[1] / "custom_components/power_sync/teslemetry_solar.py"
    node = load_source_node(path, "guard_ev_control")
    scope = {"signature": signature, "wraps": wraps, "asyncio": asyncio, "ownership": ownership,
             "_EV_CLEANUP_CONTEXT": ContextVar("cleanup", default=False),
             "_ev_submission_scope": lambda *_: nullcontext()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "guard", "exec"), scope)
    observed = asyncio.Event()
    async def read(sample, index): observed.set()
    c.hook["read"] = read
    monkeypatch.setattr(native, "SAMPLE_SECONDS", 20)
    async def _action_start_ev_charging(hass, config_entry, params): return await reconcile(c)
    calls = []
    async def _action_stop_ev_charging(hass, config_entry, params): calls.append("stop"); return True
    start = scope["guard_ev_control"](_action_start_ev_charging)
    stop = scope["guard_ev_control"](_action_stop_ev_charging)
    positive = asyncio.create_task(start(c.hass, c.entry, {"vehicle_vin": VIN_A}))
    await observed.wait()
    assert await asyncio.wait_for(stop(c.hass, c.entry, {"vehicle_vin": VIN_A}), timeout=1)
    assert await positive is False and calls == ["stop"]
    assert recovery.get_vehicle_route(c.hass, c.entry, VIN_A)["reason"] == "native_recovery"


@pytest.mark.asyncio
async def test_missing_legacy_submission_time_gets_one_durable_settling_interval(case, monkeypatch):
    c = case
    for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
        for row in root["vehicles"][VIN_A]["attempts"].values(): row["submitted_epoch"] = None
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    assert not await reconcile(c) and not c.calls
    baseline = deepcopy(c.store.value["native_reconciliation_settling"][VIN_A])
    assert baseline["settle_until"] > time.time()
    assert not await reconcile(c)
    assert c.store.value["native_reconciliation_settling"][VIN_A] == baseline
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    assert not await reconcile(c) and not c.calls
    assert c.runtime[recovery.RECOVERY_KEY] == before
    clock = [baseline["settle_until"]+1]
    monkeypatch.setattr(native, "time", SimpleNamespace(time=lambda: clock[0]))
    async def advancing(sample, index):
        if index == 2: clock[0] += 4
        sample.source_charge_timestamp = clock[0]
    c.hook["read"] = advancing
    assert await reconcile(c)
    assert all(row["submitted_epoch"] is None for row in c.store.value["recovery"]["vehicles"][VIN_A]["attempts"].values())
