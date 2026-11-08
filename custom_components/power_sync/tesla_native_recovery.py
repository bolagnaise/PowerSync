"""Bounded native charging-state reconciliation, preserving unknown outcomes.

This is operational recovery under the provider's stateful-command contract.
It is deliberately not a terminal/no-late-effect proof for an old command.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import re
import time
from typing import Mapping
from uuid import uuid4

ACTIVE_KEY = "_tesla_native_reconciliation"
BACKOFF_KEY = "_tesla_native_reconciliation_retry_after"
RESULT_KEY = "native_reconciliation"
SETTLE_SECONDS = 60.0
SAMPLE_SECONDS = 3.0
METER_SAMPLE_SECONDS = 5.0
TIMEOUT_SECONDS = 25.0


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _candidates(manager, hass, entry, runtime, vin, *, check_settling=True):
    ledger = manager._ledger(runtime, vin)
    if not isinstance(ledger, Mapping) or manager._journal_malformed(runtime, vin):
        return None
    if any(not h.get("recovery_retired") for h in manager._hazards(ledger)):
        return None
    rows = {aid: row for aid, row in ledger["attempts"].items()
            if not row.get("recovery_retired") and row.get("outcome", "pending") in {"pending", "sent_unknown"}}
    if not rows:
        return None
    producer = None
    for row in rows.values():
        route = deepcopy(row.get("route", {}))
        route.pop("session_scope", None)
        if (route.get("kind") != "ha" or row.get("operation") not in {"start", "stop", "rate", "limit"}
                or not manager._compatible(hass, entry, runtime, vin, route)):
            return None
        identity = route["entity_identity"]
        key = tuple(identity.get(field) for field in ("platform", "config_entry_id", "device_id"))
        if producer is not None and key != producer:
            return None
        producer = key
        stamp = row.get("submitted_epoch")
        # Teslemetry documents command retries lasting up to 90 seconds.
        settle = 120.0 if identity.get("platform") == "teslemetry" else SETTLE_SECONDS
        if check_settling:
            if stamp is None:
                baseline = runtime[manager._SIDECAR_STATE_KEY].get("native_reconciliation_settling", {}).get(vin, {})
                if (baseline.get("record_digests", {}).get(row["attempt_id"]) != manager._attempt_record_digest(row)
                        or not _number(baseline.get("settle_until"), 0, time.time())):
                    return None
            elif not _number(stamp, 0, time.time() - settle):
                return None
    selected_route = ledger.get("route")
    if selected_route is not None:
        selected_route = {k: v for k, v in selected_route.items() if k != "session_scope"}
        identity = selected_route.get("entity_identity", {})
        if (selected_route.get("kind") != "ha"
                or not manager._compatible(hass, entry, runtime, vin, selected_route)
                or tuple(identity.get(field) for field in ("platform", "config_entry_id", "device_id")) != producer):
            return None
    return ledger, rows, producer


def _no_writers(manager, hass, vin):
    # Do not cancel or forget shielded provider tasks. A predispatch pending
    # ticket is caught by the exact ledger digest and final submission fence.
    for runtime in hass.data.get("power_sync", {}).values():
        if not isinstance(runtime, dict):
            continue
        tracked = runtime.get(manager._SUBMISSIONS_KEY, {})
        records = runtime.get(manager._SIDECAR_STATE_KEY, {}).get("submissions", {})
        for sid, value in tracked.items():
            record = records.get(sid, value if isinstance(value, Mapping) else {})
            if isinstance(record, Mapping) and record.get("vehicle_id") not in (None, vin):
                continue
            task = manager._tracked_submission_task(value)
            if task is not None and not task.done():
                return False
    return True


def _sample(observation, vin, producer, after):
    if observation is None:
        return None
    if (observation.vin != vin or (observation.domain, observation.config_entry_id, observation.device_id) != producer
            or not _number(observation.source_charge_timestamp, after, time.time())
            or observation.source_charge_timestamp <= after
            or time.time() - observation.source_charge_timestamp > 90
            or str(observation.charge_state).lower() not in {"stopped", "complete", "disconnected", "nopower"}
            or not _number(observation.charger_actual_current, 0, 0)
            or not _number(observation.charger_power, 0, 0)
            or not _number(observation.requested_current, 0, 1000)
            or not _number(observation.charge_limit_soc, 0, 100)):
        return None
    return {"timestamp": observation.source_charge_timestamp,
            "producer_identity": observation.producer_identity,
            "requested_current": observation.requested_current,
            "charge_limit_soc": observation.charge_limit_soc,
            "charge_state": observation.charge_state, "actual_current": 0, "power": 0,
            "origin": getattr(observation, "origin", "native_coordinator"),
            "field_provenance": deepcopy(dict(getattr(observation, "field_provenance", {})))}


async def async_reconcile_native_history(hass, entry, vin, *, authorize):
    """Try once, read only, then durably release this exact operational hold.

    No control, wake, retry loop, historical value reconstruction or outcome
    rewrite occurs here. The caller must re-enter normal command admission.
    """
    from . import tesla_ev_recovery as manager
    from .tesla_ev_observation import (
        async_read_tesla_vehicle, async_read_independent_tesla_vehicle,
        independent_tesla_source_binding,
    )

    runtime = manager._runtime(hass, entry)
    if (not isinstance(vin, str) or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin)
            or runtime is None or not manager._sidecar_ready(runtime)
            or runtime.get(manager._SIDECAR_HOLD_KEY) is not None
            or runtime.get(ACTIVE_KEY) is not None):
        return False
    previous_result = runtime[manager._SIDECAR_STATE_KEY].get(RESULT_KEY, {}).get(vin, {})
    retry_after = previous_result.get("retry_after", 0)
    if (_number(retry_after, 0, float("inf")) and time.time() < retry_after
            or time.time() < runtime.get(BACKOFF_KEY, {}).get(vin, 0)):
        return False
    candidates = _candidates(manager, hass, entry, runtime, vin, check_settling=False)
    if candidates is None or not _no_writers(manager, hass, vin) or authorize() is not True:
        return False
    ledger, rows, producer = candidates
    untimed = {aid: manager._attempt_record_digest(row) for aid, row in rows.items()
               if row.get("submitted_epoch") is None}
    if untimed:
        state = runtime[manager._SIDECAR_STATE_KEY]
        baseline = state.get("native_reconciliation_settling", {}).get(vin, {})
        if baseline.get("record_digests") != untimed:
            # Missing historical timing needs one finite, durable interval
            # after local quiescence, not an impossible historical timestamp.
            async with manager._journal_lock(runtime):
                if not _no_writers(manager, hass, vin) or authorize() is not True:
                    return False
                before = deepcopy(state)
                state.setdefault("native_reconciliation_settling", {})[vin] = {
                    "record_digests": untimed, "settle_until": time.time() +
                    (120.0 if producer[0] == "teslemetry" else SETTLE_SECONDS)}
                manager._next_revision(state)
                if not await manager._save_sidecar(hass, entry, runtime, state):
                    manager._restore_sidecar_state(state, before)
            return False
    if _candidates(manager, hass, entry, runtime, vin) is None:
        return False
    # Other entries must not carry another unresolved writer for this VIN.
    for other in hass.data.get("power_sync", {}).values():
        if isinstance(other, dict) and other is not runtime:
            other_ledger = manager._ledger(other, vin)
            if other_ledger and manager._unresolved_routes(other_ledger):
                return False
    config = deepcopy({**entry.data, **entry.options})
    owner = deepcopy(manager._owner_snapshot(hass, entry, vin))
    generation = manager.current_vehicle_generation(hass, entry, vin)
    digest = manager._digest(ledger)
    route = deepcopy(next(iter(rows.values()))["route"])
    preferred = route["entity_id"]
    source = independent_tesla_source_binding(hass, vin, preferred_entity_id=preferred)
    budget = hass.data.get("power_sync_native_rate_budget")
    managed = getattr(budget, "managed", {}).get(vin)
    local = (runtime.get("powerwall_local") or {}).get("coordinator")
    refresh_meter = getattr(local, "async_request_refresh", None)
    # Only this exact existing reservation may use the new observations.
    managed_scope = (isinstance(managed, Mapping) and managed.get("entry_id") == entry.entry_id
        and str(managed.get("identity", "")).rsplit(":", 1)[0] == ":".join(producer)
        and callable(refresh_meter))
    receipts = []
    token = {"token": uuid4().hex, "vehicle_id": vin, "interrupted": False,
             "interrupt_event": asyncio.Event()}
    runtime[ACTIVE_KEY] = token

    def current():
        return (manager._runtime(hass, entry) is runtime and manager._sidecar_ready(runtime)
                and runtime.get(manager._SIDECAR_HOLD_KEY) is None
                and not runtime.get(manager._RECOVERY_STOPPING_KEY)
                and runtime.get(ACTIVE_KEY) is token and not token["interrupted"]
                and {**entry.data, **entry.options} == config
                and manager._owner_snapshot(hass, entry, vin) == owner
                and manager.current_vehicle_generation(hass, entry, vin) == generation
                and manager._digest(manager._ledger(runtime, vin)) == digest
                and _candidates(manager, hass, entry, runtime, vin) is not None
                and _no_writers(manager, hass, vin) and authorize() is True
                and independent_tesla_source_binding(hass, vin, preferred_entity_id=preferred) == source)

    async def read(after):
        if not current():
            return None
        observation = await async_read_tesla_vehicle(hass, vin, preferred_entity_id=preferred, timeout=5)
        sample = _sample(observation, vin, producer, after)
        if sample is None and current() and source is not None and producer[0] in {"tesla_fleet", "teslemetry", "tessie"}:
            observation = await async_read_independent_tesla_vehicle(
                hass, vin, preferred_entity_id=preferred, timeout=5, updated_after=after,
                allow_paid_refresh=producer[0] == "teslemetry", authorize=current)
            sample = _sample(observation, vin, producer, after)
        meter_at = None
        if sample is not None and current() and managed_scope:
            try:
                await asyncio.wait_for(refresh_meter(), timeout=5)
                if getattr(local, "last_update_success", False):
                    meter_at = getattr(local, "last_success_ts", None)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if sample is not None and current():
            receipts.append((deepcopy(sample), meter_at))
            return sample
        return None

    success = False
    result = {"success": False, "error": "fresh_stopped_vehicle_reports_unavailable"}
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            started = time.time()
            # Providers may timestamp charge documents in whole seconds.
            # The reader itself verifies a new acquisition and source advance.
            first = await read(math.floor(started) - 0.001)
            if first is None:
                return False
            separation = max(SAMPLE_SECONDS, METER_SAMPLE_SECONDS) if managed_scope else SAMPLE_SECONDS
            try:
                await asyncio.wait_for(token["interrupt_event"].wait(), timeout=separation)
                result["error"] = "recovery_preempted_by_stop"
                return False
            except TimeoutError:
                pass
            second = await read(first["timestamp"] + separation - 0.01)
            if (second is None or any(first[key] != second[key] for key in (
                    "producer_identity", "requested_current", "charge_limit_soc"))):
                return False
            async with manager._journal_lock(runtime):
                if not current():
                    result["error"] = "recovery_scope_changed"
                    return False
                state = runtime[manager._SIDECAR_STATE_KEY]
                before_state = deepcopy(state)
                before_root = manager._snapshot_recovery(runtime)
                marker = {"state": "native_state_reconciled", "historical_outcome": "unknown",
                          "recovery_id": token["token"], "schema_version": 1, "phase": "installed",
                          "vehicle_id": vin, "producer": list(producer),
                          "owner": deepcopy(owner), "config_digest": manager._digest(config),
                          "previous_generation": generation, "installed_generation": generation + 1,
                          "reconciled_at": time.time(), "attempt_ids": list(rows),
                          "record_digests": {aid: manager._attempt_record_digest(row) for aid, row in rows.items()},
                          "observations": [first, second], "policy": "stateful_command_reconciliation_v1"}
                for aid in rows:
                    ledger["attempts"][aid]["recovery_retired"] = deepcopy(marker)
                rebound = deepcopy(ledger.get("route") or route)
                rebound.pop("session_scope", None)
                rebound.pop("cleanup_only", None)
                ledger["route"] = rebound
                state["generations"][vin] = generation + 1
                try:
                    if not manager._sync_sidecar_projection(runtime, state):
                        raise ValueError("reconciliation_projection_failed")
                    manager._next_revision(state)
                    if not await manager._save_sidecar(hass, entry, runtime, state):
                        raise ValueError("reconciliation_save_failed")
                    # Revalidate durability while the submission fence remains active.
                    if (manager._runtime(hass, entry) is not runtime or token["interrupted"]
                            or {**entry.data, **entry.options} != config
                            or manager._owner_snapshot(hass, entry, vin) != owner
                            or independent_tesla_source_binding(hass, vin, preferred_entity_id=preferred) != source
                            or not _no_writers(manager, hass, vin)
                            or not all(manager._compatible(hass, entry, runtime, vin,
                                {k: v for k, v in row["route"].items() if k != "session_scope"})
                                for row in rows.values())):
                        raise ValueError("reconciliation_scope_changed")
                except BaseException:
                    manager._restore_sidecar_state(state, before_state)
                    manager._restore_recovery(runtime, before_root)
                    # A cancelled Store save may already have committed. Drain
                    # a compensating conservative save before releasing the gate.
                    await asyncio.shield(manager._save_sidecar(hass, entry, runtime, state))
                    raise
                success = True
                result = {"success": True, "recovery_id": token["token"], "phase": "installed",
                          "covered_attempt_ids": list(rows), "historical_outcome": "unknown"}
            # The command journal is durable. Settle only the captured VIN's
            # reservation through the allocator's existing two-sample proof,
            # under its site lock and outside the journal lock. This avoids
            # replacing one recovered hold with an obsolete 80 A reservation.
            if managed_scope:
                def installed_current():
                    return (manager._runtime(hass, entry) is runtime
                        and not token["interrupted"] and not runtime.get(manager._RECOVERY_STOPPING_KEY)
                        and {**entry.data, **entry.options} == config
                        and manager._owner_snapshot(hass, entry, vin) == owner
                        and manager.current_vehicle_generation(hass, entry, vin) == generation + 1
                        and budget.managed.get(vin) is managed
                        and _no_writers(manager, hass, vin)
                        and independent_tesla_source_binding(hass, vin, preferred_entity_id=preferred) == source
                        and all(manager._compatible(hass, entry, runtime, vin,
                            {k: v for k, v in row["route"].items() if k != "session_scope"}) for row in rows.values()))
                async with budget.admission_lock(managed["site"]):
                    for sample, meter_at in receipts:
                        if not installed_current():
                            break
                        await budget.reconcile_managed(vin, {
                            "identity": managed["identity"], "amps": sample["requested_current"],
                            "setting_at": sample["timestamp"], "vehicle_at": sample["timestamp"],
                            "meter_at": meter_at, "actual_w": 0, "charging": False,
                            "owner_active": bool(owner.get("owner")),
                            "source_identity": sample["producer_identity"],
                        }, is_current=installed_current)
                result["headroom_reconciled"] = vin not in budget.managed or not budget.managed[vin].get("uncertain")
            return True
    except asyncio.CancelledError:
        result["error"] = "recovery_cancelled"
        raise
    except Exception as error:
        if success:
            result["headroom_reconciled"] = False
            return True
        result["error"] = str(error) if isinstance(error, ValueError) else "recovery_failed"
        return False
    finally:
        if runtime.get(ACTIVE_KEY) is token:
            runtime.pop(ACTIVE_KEY, None)
        if not success:
            if token["interrupted"]:
                result["error"] = "recovery_preempted_by_stop"
            result["retry_after"] = time.time() + (0 if token["interrupted"] else SETTLE_SECONDS)
            runtime.setdefault(BACKOFF_KEY, {})[vin] = result["retry_after"]
        # Persist retry/status metadata independently of the immutable command
        # rows. Restarting must not reset the provider-read backoff.
        if manager._runtime(hass, entry) is runtime and manager._sidecar_ready(runtime):
            async with manager._journal_lock(runtime):
                state = runtime[manager._SIDECAR_STATE_KEY]
                previous = deepcopy(state.get(RESULT_KEY, {}))
                state.setdefault(RESULT_KEY, {})[vin] = deepcopy(result)
                manager._next_revision(state)
                if not await manager._save_sidecar(hass, entry, runtime, state):
                    state[RESULT_KEY] = previous
