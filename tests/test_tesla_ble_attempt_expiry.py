"""An unconfirmed BLE command must not block a vehicle forever.

Live: one lost readback (a 22:14 Stop, a 09:11 1 A rate) blocked every later
command to that car indefinitely. Live 2026-11-27: a 6 A rate the car already
had could never be read back and blocked a Smart Schedule start.

A lost rate cannot start or stop charging, so it is retired at once without a
cloud read. A Start or Stop is retired by the first fresh independent cloud
report after a short settle window, whatever charging state it shows.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib
import sys
import time
import types

import pytest

from test_tesla_ev_stock_recovery import (  # noqa: F401  (fixture import)
    VIN_A,
    VIN_B,
    _bridge,
    _observation,
    harness,
    recovery,
)

SETTLE = recovery.BLE_ATTEMPT_SETTLE_SECONDS


def _ownership(owner=None):
    module = types.ModuleType("power_sync.automations.ev_ownership")
    module.get_ev_ownership = lambda *_args: (VIN_A, owner) if owner else (None, None)

    async def persist(*_args, **_kwargs):
        return True

    module.persist_ev_runtime_state = persist
    return module


@pytest.fixture(autouse=True)
def fake_ownership(monkeypatch):
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_ownership", _ownership())


def _seed(hass, entry, *, operation="stop", age=SETTLE + 60, bound=None, incident=None):
    bridge = _bridge(hass, entry, VIN_A)
    runtime = hass.data["power_sync"][entry.entry_id]
    sidecar = runtime["_tesla_ev_recovery_sidecar"]
    route = {"kind": "ble", "prefix": bridge.prefix,
             "entity_identity": bridge.identities["charger"]}
    if bound is not None:
        route["effect_bound"] = bound
    submitted = time.time() - age
    sidecar["recovery"]["vehicles"] = {VIN_A: {"attempts": {"attempt-1": {
        "attempt_id": "attempt-1", "operation": operation, "outcome": "sent_unknown",
        "submitted_epoch": submitted, "route": route,
    }}, "hazards": {}, "route": {"kind": "ha", "vehicle_id": VIN_A}}}
    if incident is not None:
        sidecar["incidents"]["incident-1"] = incident
        sidecar["obligations"]["incident-1"] = {"phase": incident["phase"]}
        sidecar["liabilities"]["incident-1"] = {"state": "unknown"}
    runtime[recovery.RECOVERY_KEY] = deepcopy(sidecar["recovery"])
    return runtime, submitted


def _stopped(stamp):
    return replace(_observation(VIN_A, stamp=stamp), charge_state="Stopped",
                   charger_actual_current=0, charger_power=0)


def _held_incident(**changes):
    return {"incident_id": "incident-1", "phase": "held", "scope_closed": True,
            "current_candidate_vehicles": [VIN_A],
            "liability_refs": [{"vehicle_id": VIN_A, "attempt_id": "attempt-1"}],
            "last_error": "invalid_reconciliation", **changes}


def _attempt(runtime):
    return runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"]["attempt-1"]


@pytest.mark.asyncio
async def test_settled_stop_is_retired_and_the_vehicle_is_released(harness):
    hass, entry = harness
    runtime, _ = _seed(hass, entry)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["reason"] == "ble_recovery"

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert retired, reason
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None
    saved = runtime["_tesla_ev_recovery_sidecar_store"].value
    record = saved["recovery"]["vehicles"][VIN_A]["attempts"]["attempt-1"]
    # History is kept; only the liability is retired.
    assert record["outcome"] == "sent_unknown" and record["recovery_retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("observed,expected", [
    (None, "no fresh independent vehicle reading"),
    ("old", "predates the settle window"),
])
async def test_unproven_stop_stays_held(harness, observed, expected):
    hass, entry = harness
    runtime, submitted = _seed(hass, entry)
    before = deepcopy(runtime[recovery.RECOVERY_KEY])
    sample = {
        None: None,
        "old": _stopped(submitted + 60),
    }[observed]

    retired, reason = await recovery.async_expire_settled_ble_attempts(hass, entry, VIN_A, sample)

    assert not retired and expected in reason
    assert runtime[recovery.RECOVERY_KEY] == before
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["reason"] == "ble_recovery"


@pytest.mark.asyncio
@pytest.mark.parametrize("weakened,expected", [
    ({"origin": "native_coordinator"}, "not an independent native cloud report"),
    ({"field_provenance": {}}, "lacks field provenance"),
    ({"field_provenance": {"charge_state": "charge_state"}}, "lacks field provenance"),
])
async def test_cached_or_unattributed_reports_cannot_retire_a_stop(harness, weakened, expected):
    """Only an independent cloud document, field by field, proves the effect."""
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident())
    sidecar_before = deepcopy(runtime["_tesla_ev_recovery_sidecar"])

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, replace(_stopped(time.time()), **weakened))

    assert not retired and expected in reason
    assert runtime["_tesla_ev_recovery_sidecar"] == sidecar_before
    assert not _attempt(runtime).get("recovery_retired")


@pytest.mark.asyncio
async def test_attempt_inside_the_settle_window_stays_held(harness):
    hass, entry = harness
    _seed(hass, entry, age=60)

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert not retired and "settling" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop"])
async def test_settled_start_or_stop_is_retired_by_any_fresh_report(harness, operation):
    """The settled report is the car's real state; control resumes from it."""
    hass, entry = harness
    runtime, _ = _seed(hass, entry, operation=operation)

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _observation(VIN_A, stamp=time.time()))

    assert retired, reason
    assert _attempt(runtime)["recovery_retired"]
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_start_inside_the_settle_window_stays_held(harness):
    hass, entry = harness
    _seed(hass, entry, operation="start", age=60)

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _observation(VIN_A, stamp=time.time()))

    assert not retired and "settling" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 60])
async def test_lost_rate_is_retired_at_once_without_a_report(harness, age):
    """Live 2026-11-27: a 6 A write to a car already at 6 A was never read back.

    Old or new amps, a rate write cannot start or stop charging, so it must
    not hold the car until a cloud report arrives.
    """
    hass, entry = harness
    runtime, _ = _seed(hass, entry, operation="rate", age=age,
                       bound={"kind": "rate", "min_amps": 6, "max_amps": 6})
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["reason"] == "ble_recovery"
    assert recovery.ble_attempt_expiry_needs_report(hass, entry, VIN_A) is False
    assert recovery.ble_attempt_expiry_due(hass, entry, VIN_A)[0] <= time.time()

    retired, reason = await recovery.async_expire_settled_ble_attempts(hass, entry, VIN_A, None)

    assert retired, reason
    assert _attempt(runtime)["recovery_retired"]
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_unknown_operations_are_never_expired_automatically(harness):
    hass, entry = harness
    _seed(hass, entry, operation="wake")

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert not retired and "not expired automatically" in reason


@pytest.mark.asyncio
async def test_single_vehicle_held_incident_is_released_with_its_attempt(harness):
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident())

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert retired and "released 1 recovery incident" in reason
    sidecar = runtime["_tesla_ev_recovery_sidecar"]
    assert sidecar["incidents"]["incident-1"]["phase"] == "released"
    assert sidecar["obligations"]["incident-1"]["phase"] == "released"
    assert sidecar["liabilities"]["incident-1"]["state"] == "retained_reconciled"
    saved = runtime["_tesla_ev_recovery_sidecar_store"].value
    assert saved["incidents"]["incident-1"]["phase"] == "released"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"current_candidate_vehicles": [VIN_A, VIN_B]},
    {"phase": "reconciling"},
    {"scope_closed": False},
    {"liability_refs": [{"vehicle_id": VIN_A, "attempt_id": "attempt-1"},
                        {"vehicle_id": VIN_A, "hazard_id": "ble:bridge_a"}]},
])
async def test_incident_beyond_this_attempt_keeps_everything_held(harness, changes):
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident(**changes))
    before = deepcopy(runtime["_tesla_ev_recovery_sidecar"])

    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert not retired and "still needs the bridge recovery" in reason
    assert runtime["_tesla_ev_recovery_sidecar"] == before
    assert not _attempt(runtime).get("recovery_retired")


@pytest.mark.asyncio
async def test_save_failure_rolls_back_attempt_and_incident(harness, monkeypatch):
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident())
    sidecar_before = deepcopy(runtime["_tesla_ev_recovery_sidecar"])
    journal_before = deepcopy(runtime[recovery.RECOVERY_KEY])

    async def refuse(*_args, **_kwargs):
        return False

    monkeypatch.setattr(recovery, "_save_sidecar", refuse)
    retired, reason = await recovery.async_expire_settled_ble_attempts(
        hass, entry, VIN_A, _stopped(time.time()))

    assert not retired and "could not be saved" in reason
    assert runtime["_tesla_ev_recovery_sidecar"] == sidecar_before
    assert runtime[recovery.RECOVERY_KEY] == journal_before


def _worker(hass, entry, runtime, monkeypatch, reads, *, owned=False):
    observation = importlib.import_module("power_sync.tesla_ev_observation")

    async def independent(_hass, vin, **kwargs):
        reads.append(kwargs)
        assert vin == VIN_A and kwargs["allow_paid_refresh"] is False
        return _stopped(time.time())

    def forbidden(*_args, **_kwargs):
        pytest.fail("Expiry must not use the cached coordinator observation")

    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", independent)
    monkeypatch.setattr(observation, "get_tesla_vehicle_observation", forbidden)
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_ownership",
                        _ownership({"owner": "powersync"} if owned else None))
    automatic = importlib.import_module("power_sync.tesla_ev_auto_recovery")
    restarts = []

    async def stock_recovery(self, vin):
        restarts.append(vin)

    monkeypatch.setattr(automatic.NativeRecoveryWorker, "_recover_stock", stock_recovery)
    worker = automatic.NativeRecoveryWorker(hass, entry, runtime)
    runtime[automatic.WORKER_KEY] = worker
    return worker, restarts


@pytest.mark.asyncio
async def test_worker_retires_a_settled_stop_from_one_independent_read(harness, monkeypatch):
    hass, entry = harness
    runtime, submitted = _seed(hass, entry, incident=_held_incident())
    reads = []
    worker, restarts = _worker(hass, entry, runtime, monkeypatch, reads)

    await worker.async_tick()

    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None
    assert restarts == []
    assert len(reads) == 1
    # The report must be newer than the settle window, not just recent.
    assert reads[0]["updated_after"] == submitted + SETTLE
    assert runtime["_tesla_ev_recovery_sidecar"]["incidents"]["incident-1"]["phase"] == "released"


@pytest.mark.asyncio
async def test_worker_retires_a_settled_stop_while_a_blocked_session_holds_the_lease(
        harness, monkeypatch):
    """Live 2026-11-26: a solar-surplus Stop lost its BLE readback at 14:00.

    The session kept its lease while its commands were blocked, and the
    worker refused to read while any lease existed, so the hold never ended
    even though the cloud showed the car stopped.
    """
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident())
    reads = []
    worker, restarts = _worker(hass, entry, runtime, monkeypatch, reads, owned=True)

    await worker.async_tick()

    assert len(reads) == 1
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None
    assert restarts == []
    assert runtime["_tesla_ev_recovery_sidecar"]["incidents"]["incident-1"]["phase"] == "released"


@pytest.mark.asyncio
async def test_worker_keeps_hold_without_reading_while_writing_or_settling(harness, monkeypatch):
    hass, entry = harness
    runtime, _ = _seed(hass, entry, incident=_held_incident())
    reads = []
    worker, restarts = _worker(hass, entry, runtime, monkeypatch, reads)
    in_flight = asyncio.get_running_loop().create_future()
    runtime[recovery._SUBMISSIONS_KEY] = {"submission-1": {"task": in_flight}}

    await worker.async_tick()

    assert reads == []
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["reason"] == "ble_recovery"
    assert restarts == [VIN_A]
    assert worker.held_logged[VIN_A][0] == "a charging command is in flight"
    in_flight.cancel()

    runtime, _ = _seed(hass, entry, age=60)
    worker, restarts = _worker(hass, entry, runtime, monkeypatch, reads)
    await worker.async_tick()
    assert reads == [] and "to settle" in worker.held_logged[VIN_A][0]


@pytest.mark.asyncio
async def test_worker_reads_at_most_once_per_interval(harness, monkeypatch):
    hass, entry = harness
    runtime, _ = _seed(hass, entry)
    reads = []
    worker, _ = _worker(hass, entry, runtime, monkeypatch, reads)
    observation = importlib.import_module("power_sync.tesla_ev_observation")

    async def stale(_hass, vin, **kwargs):
        reads.append(kwargs)
        return _stopped(time.time() - SETTLE - 300)

    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", stale)
    await worker.async_tick()
    await worker.async_tick()

    assert len(reads) == 1
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["reason"] == "ble_recovery"
    assert worker.held_logged[VIN_A][0] == "waiting before the next independent vehicle read"


@pytest.mark.asyncio
async def test_worker_retires_a_lost_rate_without_reading(harness, monkeypatch):
    hass, entry = harness
    runtime, _ = _seed(hass, entry, operation="rate", age=5,
                       bound={"kind": "rate", "min_amps": 6, "max_amps": 6})
    reads = []
    worker, restarts = _worker(hass, entry, runtime, monkeypatch, reads)

    await worker.async_tick()

    assert reads == []
    assert restarts == []
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None
