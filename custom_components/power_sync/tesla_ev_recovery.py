"""Durable Tesla EV transport hazards and bounded BLE isolation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import inspect
import json
import math
import re
import time
from typing import Any, Mapping
from uuid import uuid4

DOMAIN = "power_sync"
RUNTIME_RECOVERY_KEY = "ev_transport_recovery"
RECOVERY_KEY = RUNTIME_RECOVERY_KEY
LEGACY_RECOVERY_KEY = "transport_recovery"
ROUTE_KINDS = frozenset({"ble", "teslemetry_bt", "teslemetry_bluetooth", "ha"})
OUTCOMES = frozenset({
    "not_sent", "sent_unknown", "accepted_unconfirmed", "confirmed", "rejected"
})
_TERMINAL_ATTEMPT_OUTCOMES = frozenset({
    "not_sent", "accepted_unconfirmed", "confirmed", "rejected",
})
_TERMINAL_REMOVALS_KEY = "terminal_removals"
BLE_ISOLATION_TIMEOUT = 10.0
RECOVERY_DRAIN_TIMEOUT = 30.0
_MISSING = object()
_JOURNAL_LOCK_KEY = "_ev_transport_recovery_lock"
_SIDECAR_STORE_KEY = "_tesla_ev_recovery_sidecar_store"
_SIDECAR_STATE_KEY = "_tesla_ev_recovery_sidecar"
_SIDECAR_LOADED_KEY = "_tesla_ev_recovery_sidecar_loaded"
_SIDECAR_HOLD_KEY = "_tesla_ev_recovery_sidecar_hold"
_SUBMISSIONS_KEY = "_tesla_ev_recovery_submissions"
_STORAGE_TASKS_KEY = "_tesla_ev_recovery_storage_tasks"
_RECOVERY_EPOCH_KEY = "_tesla_ev_recovery_epoch"
_RECOVERY_STOPPING_KEY = "_tesla_ev_recovery_stopping"
_BEST_EFFORT_CLEANUP_KEY = "_tesla_ev_recovery_best_effort_cleanup"
_TERMINAL_SIDECAR_BEFORE_KEY = "_tesla_ev_recovery_terminal_sidecar_before"
SIDECAR_SCHEMA_VERSION = 1
RECOVERY_PHASES = frozenset({"held", "quiescing", "reset_pending", "reconciling", "released"})
POSITIVE_OPERATIONS = frozenset({"start", "rate", "limit", "set_current", "set_charge_limit"})
_INTERRUPTED_PHASES = frozenset({"quiescing", "reset_pending", "reconciling"})


class RecoverySubmissionDenied(RuntimeError):
    """A final recovery fence refused a provider handover before mutation."""

    ev_recovery_not_submitted = True


class RecoveryPersistenceRefused(RecoverySubmissionDenied):
    """A narrowly scoped cleanup Stop may proceed without a new journal row.

    The exception is intentionally distinct from an ordinary admission
    refusal.  Callers may dispatch one in-flight compensating Stop when
    the durable cleanup scope was validated before the failed save and the
    current binding still matches after it.  No positive operation may use
    this result.
    """

    ev_recovery_persistence_refused = True
    ev_recovery_best_effort_cleanup = True

    def __init__(self, message: str, *, vehicle_id: str, route: Mapping[str, Any]):
        super().__init__(message)
        self.vehicle_id = vehicle_id
        self.route = deepcopy(dict(route))
        self.cleanup_scope_key = self.route.get("_cleanup_scope_key")
        self.cleanup_claim_id = self.route.get("_cleanup_claim_id")


@dataclass(frozen=True, slots=True)
class RecoveryTicket:
    hass: Any
    entry: Any
    vehicle_id: str
    attempt_id: str
    route: Mapping[str, Any]
    # The live runtime object is an in-flight generation fence.  A ticket
    # reconstructed from durable evidence gets the new runtime object, while
    # a ticket retained across a reload cannot mutate the replacement state.
    runtime_ref: Any = None


def _runtime(hass: Any, entry: Any) -> dict[str, Any] | None:
    data = getattr(hass, "data", {}).get(DOMAIN, {})
    runtime = data.get(getattr(entry, "entry_id", None)) if isinstance(data, Mapping) else None
    return runtime if isinstance(runtime, dict) else None


def _journal_lock(runtime: dict[str, Any]) -> asyncio.Lock:
    lock = runtime.get(_JOURNAL_LOCK_KEY)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        runtime[_JOURNAL_LOCK_KEY] = lock
    return lock


def _recovery(runtime: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    """Return the durable root without replacing malformed evidence."""
    if RECOVERY_KEY in runtime:
        recovery = runtime.get(RECOVERY_KEY)
        if not isinstance(recovery, dict):
            return None
    elif LEGACY_RECOVERY_KEY in runtime:
        recovery = runtime.get(LEGACY_RECOVERY_KEY)
        if not isinstance(recovery, dict):
            return None
    elif create:
        recovery = runtime[RECOVERY_KEY] = {"vehicles": {}}
    else:
        return None

    vehicles = recovery.get("vehicles", _MISSING)
    if vehicles is _MISSING:
        # An existing root without its schema marker is malformed evidence;
        # only a newly-created root may start with an empty vehicle map.
        return None
    if not isinstance(vehicles, dict):
        return None
    return recovery


def _ledger(runtime: dict[str, Any], vid: str, *, create=False) -> dict[str, Any] | None:
    recovery = _recovery(runtime, create=create)
    if recovery is None:
        return None
    vehicles = recovery["vehicles"]
    ledger = vehicles.get(vid)
    new_ledger = False
    if not isinstance(ledger, dict):
        if not create:
            return None
        if vid in vehicles:
            return None
        ledger = vehicles[vid] = {"route": None, "attempts": {}, "hazards": {}}
        new_ledger = True

    if "route" in ledger and ledger["route"] is not None and not isinstance(ledger["route"], Mapping):
        return None
    for key in ("attempts", "hazards"):
        value = ledger.get(key, _MISSING)
        if value is _MISSING:
            if not create or not new_ledger:
                return None
            ledger[key] = {}
        elif not isinstance(value, dict):
            return None
    for attempt in ledger["attempts"].values():
        if (not isinstance(attempt, dict)
                or not isinstance(attempt.get("route"), Mapping)):
            return None
    for hazard in ledger["hazards"].values():
        if not isinstance(hazard, dict):
            return None
    if RECOVERY_KEY not in runtime and LEGACY_RECOVERY_KEY in runtime:
        # Migrate only after the complete selected ledger has passed schema
        # validation; malformed legacy evidence remains byte-for-byte intact.
        # Use a copy so a failed mutation can remove the migrated root without
        # having changed the legacy evidence underneath it.
        runtime[RECOVERY_KEY] = deepcopy(recovery)
        recovery = runtime[RECOVERY_KEY]
        ledger = recovery["vehicles"][vid]
    return ledger


def _journal_malformed(runtime: dict[str, Any], vid: str | None = None) -> bool:
    """Whether an existing journal cannot be safely interpreted."""
    if RECOVERY_KEY in runtime:
        recovery = runtime.get(RECOVERY_KEY)
    elif LEGACY_RECOVERY_KEY in runtime:
        recovery = runtime.get(LEGACY_RECOVERY_KEY)
    else:
        return False
    if not isinstance(recovery, dict):
        return True
    vehicles = recovery.get("vehicles", _MISSING)
    if vehicles is _MISSING:
        return True
    if not isinstance(vehicles, dict):
        return True
    selected = vehicles.get(vid) if vid is not None else None
    ledgers = [selected] if vid is not None and vid in vehicles else vehicles.values()
    for ledger in ledgers:
        if not isinstance(ledger, dict):
            return True
        if ledger.get("route") is not None and not isinstance(ledger.get("route"), Mapping):
            return True
        for key in ("attempts", "hazards"):
            value = ledger.get(key, _MISSING)
            if value is _MISSING:
                return True
            if not isinstance(value, dict):
                return True
            if isinstance(value, dict):
                for item in value.values():
                    if not isinstance(item, dict):
                        return True
                    if key == "attempts" and not isinstance(item.get("route"), Mapping):
                        return True
    return False


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _entry_config_value(entry: Any, key: str) -> Any:
    """Read one pairing input without retaining credentials or other options."""
    options = getattr(entry, "options", {})
    data = getattr(entry, "data", {})
    if isinstance(options, Mapping) and key in options:
        return options.get(key)
    return data.get(key) if isinstance(data, Mapping) else None


def _producer_identity(route: Mapping[str, Any] | None,
                       identity: Mapping[str, Any] | None = None) -> str | None:
    if isinstance(route, Mapping):
        for key in ("producer_identity", "command_producer_identity"):
            value = route.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        target = route.get("target")
        if isinstance(target, Mapping):
            value = target.get("producer_identity")
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(identity, Mapping):
        fields = ("platform", "config_entry_id", "device_id", "unique_id")
        values = [identity.get(field) for field in fields]
        if all(isinstance(value, str) and value.strip() for value in values):
            return ":".join(str(value).strip() for value in values)
    return None


def _pairing_digest(
    entry: Any,
    route: Mapping[str, Any] | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
) -> str:
    """Digest only the EV pairing inputs that can change command ownership.

    Access tokens, monitoring settings, and unrelated integration options are
    intentionally excluded.  A producer identity is included when a route is
    available so a VIN cannot silently move to another command source.
    """
    kind = str(route.get("kind") or "") if isinstance(route, Mapping) else ""
    prefix = route.get("prefix") if kind == "ble" else None
    mapping = None
    if isinstance(route, Mapping):
        mapping = route.get("vehicle_mapping", route.get("ble_mapping"))
    return _digest({
        "entry_id": getattr(entry, "entry_id", None),
        "ev_provider": _entry_config_value(entry, "ev_provider"),
        "ble_prefix": _entry_config_value(entry, "tesla_ble_entity_prefix"),
        "ble_mapping": _entry_config_value(entry, "tesla_ble_vehicle_mapping"),
        "route_prefix": prefix,
        "route_mapping": mapping,
        "producer_identity": _producer_identity(route, identity),
    })


def _runtime_digest(runtime: dict[str, Any], entry: Any) -> str:
    return _digest({"entry_id": getattr(entry, "entry_id", None),
                    "setup_generation": runtime.get("setup_generation"),
                    "runtime_generation": runtime.get("runtime_generation")})


def _setup_digest(runtime: dict[str, Any], entry: Any) -> str:
    # Keep the legacy diagnostic field, but do not hash credentials or
    # unrelated monitoring/options into persisted recovery evidence.
    return _digest({
        "entry_id": getattr(entry, "entry_id", None),
        "setup_generation": runtime.get("setup_generation"),
        "ev_provider": _entry_config_value(entry, "ev_provider"),
        "ble_prefix": _entry_config_value(entry, "tesla_ble_entity_prefix"),
        "ble_mapping": _entry_config_value(entry, "tesla_ble_vehicle_mapping"),
    })


def _owner_snapshot(hass: Any, entry: Any, vid: str) -> dict[str, Any]:
    try:
        from .automations.ev_ownership import get_ev_ownership

        lease_id, lease = get_ev_ownership(hass, entry, vid)
        lease = lease if isinstance(lease, Mapping) else {}
        return {key: lease.get(key) for key in (
            "owner", "owner_mode", "session_id", "ownership_generation"
        )} | {"lease_id": lease_id}
    except Exception:
        return {}


def _session_scope(snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a lease identity that can keep a native route session-scoped.

    A native command issued without an active ownership lease must remain
    ephemeral.  An owner, lease key, or generation is sufficient evidence that
    the caller has a live session to carry the exact producer into subsequent
    rate/limit commands; the full snapshot is retained so a reload or lease
    replacement cannot silently adopt that producer.
    """
    if not isinstance(snapshot, Mapping):
        return None
    if not any(snapshot.get(key) is not None for key in (
        "owner", "lease_id", "session_id", "ownership_generation",
    )):
        return None
    return {
        key: snapshot.get(key)
        for key in ("owner", "owner_mode", "session_id", "ownership_generation", "lease_id")
    }


def _registry(hass: Any) -> Any | None:
    try:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(hass)
    except Exception:
        return getattr(hass, "entity_registry", None)


def _identity(hass: Any, entity_id: str) -> dict[str, Any] | None:
    getter = getattr(_registry(hass), "async_get", None)
    try:
        entity = getter(entity_id) if callable(getter) else None
    except Exception:
        entity = None
    if entity is None:
        return None
    values = {key: getattr(entity, key, None) for key in (
        "platform", "config_entry_id", "device_id", "unique_id"
    )}
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        return None
    if getattr(entity, "disabled_by", None) is not None:
        return None
    return values | {"disabled_by": None, "entity_id": entity_id}


def _pin(hass: Any, entry: Any, runtime: dict[str, Any], vid: str,
         route: Mapping[str, Any], entity_id: str) -> dict[str, Any] | None:
    kind = str(route.get("kind") or "")
    if kind not in ROUTE_KINDS or not entity_id:
        return None
    if kind == "ble" and not str(route.get("prefix") or ""):
        return None
    identity = _identity(hass, entity_id)
    if identity is None:
        return None
    pinned = dict(route)
    pinned.update({
        "kind": kind, "entity_id": entity_id, "entity_identity": identity,
        "vehicle_id": vid,
        "runtime_digest": _runtime_digest(runtime, entry),
        "setup_digest": _setup_digest(runtime, entry),
        "pairing_digest": _pairing_digest(entry, route, identity=identity),
        "owner_snapshot": _owner_snapshot(hass, entry, vid),
    })
    producer_identity = _producer_identity(route, identity)
    if producer_identity is None:
        return None
    pinned["producer_identity"] = producer_identity
    if pinned.get("prefix") is not None:
        pinned["prefix"] = str(pinned["prefix"])
    if kind == "ble" and route.get("fallback") is not None:
        fallback = route["fallback"]
        if not isinstance(fallback, Mapping) or fallback.get("kind") != "ha":
            return None
        pinned_fallback = _pin(hass, entry, runtime, vid, fallback,
                               str(fallback.get("entity_id") or ""))
        if pinned_fallback is None:
            return None
        # Capture the alternate producer before the first local dispatch.
        # A later entity replacement cannot silently become the handoff.
        pinned["fallback"] = pinned_fallback
    return pinned


def _cleanup_route_matches(
    durable: Mapping[str, Any] | None,
    requested: Mapping[str, Any],
    vehicle_id: str,
) -> bool:
    """Match a cleanup route against its durable producer identity."""
    if not isinstance(durable, Mapping) or durable.get("vehicle_id") not in (None, vehicle_id):
        return False
    if durable.get("kind") != requested.get("kind"):
        return False
    durable_identity = durable.get("entity_identity")
    requested_identity = requested.get("entity_identity")
    if not isinstance(durable_identity, Mapping) or not isinstance(requested_identity, Mapping):
        return False
    same_entity = str(durable.get("entity_id") or "") == str(requested.get("entity_id") or "")
    if not same_entity:
        # Native HA operations use different entities for Start/Stop versus
        # Rate/Limit.  A cross-entity cleanup match is safe only through the
        # durable session anchor captured with the original native session;
        # same-VIN/same-device discovery without that anchor is rejected.
        if durable.get("kind") != "ha" or requested.get("kind") != "ha":
            return False
        anchor = durable.get("session_anchor")
        anchor_identity = anchor.get("entity_identity") if isinstance(anchor, Mapping) else None
        if not isinstance(anchor_identity, Mapping):
            return False
        if any(anchor_identity.get(field) != requested_identity.get(field)
               for field in ("entity_id", "platform", "config_entry_id", "device_id", "unique_id", "disabled_by")):
            return False
        if any(durable_identity.get(field) != requested_identity.get(field)
               for field in ("platform", "config_entry_id", "device_id")):
            return False
        return bool(durable.get("producer_identity") and requested.get("producer_identity"))
    for field in ("prefix", "producer_identity", "pairing_digest"):
        if durable.get(field) != requested.get(field):
            return False
    return all(
        durable_identity.get(field) == requested_identity.get(field)
        for field in ("entity_id", "platform", "config_entry_id", "device_id", "unique_id", "disabled_by")
    )


def _cleanup_scope_matches(
    runtime: dict[str, Any],
    vehicle_id: str,
    requested: Mapping[str, Any],
    cleanup_obligation_token: str | None,
) -> bool:
    """Return true only for pre-existing durable Stop authority.

    This intentionally reads the hydrated sidecar, never the mutable legacy
    projection that the failed attempt is about to change.  A matching
    unresolved route/hazard is sufficient to cover the compensating Stop; a
    finite cleanup obligation additionally requires its exact persisted token.
    """
    state = runtime.get(_SIDECAR_STATE_KEY)
    if not _sidecar_valid(state):
        return False
    recovery = _sidecar_recovery(runtime, state)
    if not isinstance(recovery, Mapping):
        return False
    vehicles = recovery.get("vehicles")
    ledger = vehicles.get(vehicle_id) if isinstance(vehicles, Mapping) else None
    if not isinstance(ledger, Mapping):
        ledger = None

    covered = False
    if ledger is not None:
        attempts = ledger.get("attempts", {})
        for record in attempts.values() if isinstance(attempts, Mapping) else ():
            if not isinstance(record, Mapping) or record.get("recovery_retired"):
                continue
            if record.get("outcome", "pending") not in {"pending", "sent_unknown"}:
                continue
            durable_route = record.get("route")
            if _cleanup_route_matches(durable_route, requested, vehicle_id):
                covered = True
                break
            # A BLE attempt may have durably captured the exact native
            # fallback producer.  It is still covered only when that pinned
            # fallback identity is the requested Stop target.
            if isinstance(durable_route, Mapping) and _cleanup_route_matches(
                durable_route.get("fallback"), requested, vehicle_id,
            ):
                covered = True
                break
        if not covered:
            for hazard in _hazards(ledger):
                if not isinstance(hazard, Mapping) or hazard.get("recovery_retired"):
                    continue
                if hazard.get("state") not in {"pending", "isolated"}:
                    continue
                if requested.get("kind") != "ble" or requested.get("prefix") != hazard.get("prefix"):
                    continue
                binding = hazard.get("binding")
                entities = binding.get("entities") if isinstance(binding, Mapping) else None
                charger_identity = entities.get("charger") if isinstance(entities, Mapping) else None
                requested_identity = requested.get("entity_identity")
                if (isinstance(charger_identity, Mapping)
                        and isinstance(requested_identity, Mapping)
                        and all(charger_identity.get(field) == requested_identity.get(field)
                                for field in ("entity_id", "platform", "config_entry_id", "device_id", "unique_id", "disabled_by"))):
                    covered = True
                    break

    if cleanup_obligation_token is None:
        return covered
    if not isinstance(cleanup_obligation_token, str) or not cleanup_obligation_token.strip():
        return covered
    obligations = state.get("obligations")
    families = obligations.get("ev_quick_control_obligations") if isinstance(obligations, Mapping) else None
    record = families.get(vehicle_id) if isinstance(families, Mapping) else None
    if not isinstance(record, Mapping) or record.get("obligation_id") != cleanup_obligation_token:
        return covered
    if str(record.get("vehicle_id") or vehicle_id) != vehicle_id:
        return covered
    params = record.get("params")
    if not isinstance(params, Mapping):
        return covered
    # A durable route is the strongest binding.  If it is absent, native
    # cleanup obligations must carry the exact mutation entity and registry
    # identity captured before the original positive dispatch.
    if ledger is not None and _cleanup_route_matches(ledger.get("route"), requested, vehicle_id):
        return True
    expected_entity = params.get("_tesla_command_entity_id")
    expected_identity = params.get("_tesla_native_command_identity")
    return (
        requested.get("kind") == "ha"
        and isinstance(expected_entity, str)
        and expected_entity == requested.get("entity_id")
        and isinstance(expected_identity, Mapping)
        and _cleanup_route_matches(
            {"kind": "ha", "entity_id": expected_entity,
             "entity_identity": expected_identity,
             "vehicle_id": vehicle_id,
             "producer_identity": requested.get("producer_identity"),
             "pairing_digest": requested.get("pairing_digest")},
            requested, vehicle_id,
        )
    )


def _cleanup_binding_current(
    hass: Any, entry: Any, runtime: dict[str, Any], vehicle_id: str,
    requested: Mapping[str, Any],
) -> bool:
    """Recheck current registry/config/runtime binding after a failed save."""
    if requested.get("vehicle_id") != vehicle_id:
        return False
    current_identity = _identity(hass, str(requested.get("entity_id") or ""))
    if not isinstance(current_identity, Mapping):
        return False
    expected_identity = requested.get("entity_identity")
    if not isinstance(expected_identity, Mapping):
        return False
    if any(current_identity.get(field) != expected_identity.get(field)
           for field in ("entity_id", "platform", "config_entry_id", "device_id", "unique_id", "disabled_by")):
        return False
    if requested.get("pairing_digest") != _pairing_digest(
        entry, requested, identity=current_identity,
    ):
        return False
    return (
        requested.get("runtime_digest") == _runtime_digest(runtime, entry)
        and requested.get("setup_digest") == _setup_digest(runtime, entry)
    )


def _claim_best_effort_cleanup(
    runtime: dict[str, Any], vehicle_id: str, requested: Mapping[str, Any],
    cleanup_obligation_token: str | None,
) -> tuple[tuple[str, str], str] | None:
    """Claim one cleanup scope until its provider child has finished.

    The claim is deliberately in-memory.  The durable liability that makes a
    best-effort Stop admissible remains in the sidecar; this marker only
    prevents two callers from handing the same persisted cleanup scope to the
    provider concurrently.  It is released by ``async_track_cleanup_submission``
    when the child completes or when a final pre-handover fence rejects it.
    """
    scope_key = cleanup_obligation_token or _digest({
        "vehicle_id": vehicle_id,
        "kind": requested.get("kind"),
        "entity_id": requested.get("entity_id"),
        "entity_identity": requested.get("entity_identity"),
        "prefix": requested.get("prefix"),
    })
    claimed = runtime.get(_BEST_EFFORT_CLEANUP_KEY)
    if isinstance(claimed, set):
        # Preserve a claim created by an older in-process module revision
        # rather than silently allowing a duplicate handover.
        claimed = {
            key: {"claim_id": "legacy-inflight", "status": "claimed"}
            for key in claimed
        }
        runtime[_BEST_EFFORT_CLEANUP_KEY] = claimed
    elif not isinstance(claimed, dict):
        claimed = {}
        runtime[_BEST_EFFORT_CLEANUP_KEY] = claimed
    key = (vehicle_id, str(scope_key))
    if key in claimed:
        return None
    claim_id = uuid4().hex
    claimed[key] = {"claim_id": claim_id, "status": "claimed"}
    return key, claim_id


def _release_best_effort_cleanup(
    runtime: dict[str, Any], scope_key: Any, claim_id: Any,
) -> None:
    """Release only the matching in-flight cleanup claim."""
    claimed = runtime.get(_BEST_EFFORT_CLEANUP_KEY)
    if not isinstance(claimed, dict):
        return
    current = claimed.get(scope_key)
    if isinstance(current, Mapping) and current.get("claim_id") == claim_id:
        claimed.pop(scope_key, None)


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _target(target: Any) -> dict[str, Any] | None:
    if target is None:
        return None
    fields = ("vin", "operation", "entity_id", "entity_domain", "platform", "unique_id",
              "config_entry_id", "device_id", "native_config_entry_ids")
    return {name: getattr(target, name) for name in fields if hasattr(target, name)}


def _sidecar_key(entry: Any) -> str:
    return f"{DOMAIN}.ev_transport_recovery.{getattr(entry, 'entry_id', '')}"


def _new_sidecar() -> dict[str, Any]:
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "revision": 0,
        "incidents": {},
        "generations": {},
        "liabilities": {},
        "obligations": {},
        "submissions": {},
        "recovery": {"vehicles": {}},
    }


def _recovery_projection_valid(value: Any) -> bool:
    """Validate the legacy journal projection before importing it."""
    if not isinstance(value, Mapping) or not isinstance(value.get("vehicles"), Mapping):
        return False
    for ledger in value["vehicles"].values():
        if not isinstance(ledger, Mapping):
            return False
        if ledger.get("route") is not None and not isinstance(ledger.get("route"), Mapping):
            return False
        for key in ("attempts", "hazards"):
            records = ledger.get(key)
            if not isinstance(records, Mapping):
                return False
            for record in records.values():
                if not isinstance(record, Mapping):
                    return False
                if key == "attempts" and not isinstance(record.get("route"), Mapping):
                    return False
    return True


def _attempt_record_digest(record: Mapping[str, Any]) -> str:
    """Digest the immutable portion of one attempt for terminal removal proof."""
    return _digest({
        key: record.get(key)
        for key in (
            "attempt_id", "vehicle_id", "operation", "entity_id", "target",
            "route", "submitted_epoch", "original_deadline", "owner_snapshot",
            "runtime_digest", "pairing_digest", "setup_digest",
        )
    })


def _terminal_removals_valid(value: Any) -> bool:
    """Validate sidecar proofs before they can remove a stale projection row."""
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    for vehicle_id, records in value.items():
        if not isinstance(vehicle_id, str) or not isinstance(records, Mapping):
            return False
        for attempt_id, proof in records.items():
            if (not isinstance(attempt_id, str)
                    or not isinstance(proof, Mapping)
                    or proof.get("attempt_id") != attempt_id
                    or proof.get("vehicle_id") != vehicle_id
                    or proof.get("outcome") not in _TERMINAL_ATTEMPT_OUTCOMES
                    or not isinstance(proof.get("operation"), str)
                    or not proof.get("operation")
                    or not isinstance(proof.get("entity_id"), str)
                    or not proof.get("entity_id")
                    or not isinstance(proof.get("route"), Mapping)
                    or not isinstance(proof.get("record_digest"), str)
                    or not proof.get("record_digest")
                    or any(
                        not isinstance(proof.get(key), str) or not proof.get(key)
                        for key in ("runtime_digest", "pairing_digest", "setup_digest")
                    )):
                return False
    return True


def _terminal_removal_matches(
    record: Mapping[str, Any] | None,
    proof: Mapping[str, Any],
) -> bool:
    """Match a removal proof without ever deleting a durable unknown."""
    if not isinstance(record, Mapping):
        return False
    if (record.get("attempt_id") != proof.get("attempt_id")
            or record.get("vehicle_id") != proof.get("vehicle_id")
            or record.get("outcome") == "sent_unknown"
            or proof.get("outcome") not in _TERMINAL_ATTEMPT_OUTCOMES):
        return False
    existing_outcome = str(record.get("outcome") or "pending")
    proof_outcome = str(proof.get("outcome") or "")
    if existing_outcome not in {"pending", proof_outcome, "accepted_unconfirmed"}:
        return False
    if (existing_outcome == "accepted_unconfirmed"
            and proof_outcome not in {"accepted_unconfirmed", "confirmed"}):
        return False
    if (proof.get("operation") != record.get("operation")
            or proof.get("entity_id") != record.get("entity_id")
            or proof.get("route") != record.get("route")):
        return False
    return _attempt_record_digest(record) == proof.get("record_digest")


def _cleanup_route_has_liabilities(ledger: Any) -> bool:
    if not isinstance(ledger, Mapping):
        return True
    for key, field, unresolved in (
        ("attempts", "outcome", ("pending", "sent_unknown")),
        ("hazards", "state", ("pending", "isolated")),
    ):
        records = ledger.get(key, {})
        if not isinstance(records, Mapping) or any(
            not isinstance(record, Mapping)
            or (not record.get("recovery_retired") and record.get(field, "pending") in unresolved)
            for record in records.values()
        ):
            return True
    return False


def _terminal_cleanup_proof_matches_route(
    recovery: Mapping[str, Any],
    vehicle_id: Any,
    route: Any,
) -> bool:
    """Whether a durable Stop proof authorizes clearing one stale route.

    This is used only while copying an already-authoritative sidecar into the
    legacy live projection.  The route must match the complete route captured
    by the proof, so a different producer, pairing, or lease can never be
    cleared merely because an older route was cleanup-only.
    """
    if not isinstance(route, Mapping) or route.get("cleanup_only") is not True:
        return False
    vehicles = recovery.get("vehicles", {})
    if (not isinstance(vehicles, Mapping)
            or _cleanup_route_has_liabilities(vehicles.get(str(vehicle_id)))):
        return False
    removals = recovery.get(_TERMINAL_REMOVALS_KEY)
    if not isinstance(removals, Mapping):
        return False
    if not _terminal_removals_valid(removals):
        return False
    for proofs in removals.values():
        if not isinstance(proofs, Mapping):
            continue
        for proof in proofs.values():
            proof_route = proof.get("route") if isinstance(proof, Mapping) else None
            if (
                isinstance(proof, Mapping)
                and proof.get("vehicle_id") == str(vehicle_id)
                and proof.get("operation") == "stop"
                and proof.get("outcome") in _TERMINAL_ATTEMPT_OUTCOMES
                and isinstance(proof_route, Mapping)
                and proof_route.get("cleanup_only") is True
                and dict(proof_route) == dict(route)
            ):
                return True
    return False


def _sidecar_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        return False
    if type(value.get("revision")) is not int or value["revision"] < 0:
        return False
    for key in ("incidents", "generations", "liabilities", "obligations", "submissions"):
        if not isinstance(value.get(key), dict):
            return False
    for incident in value["incidents"].values():
        if not isinstance(incident, Mapping) or incident.get("phase") not in RECOVERY_PHASES:
            return False
    for generation in value["generations"].values():
        if type(generation) is not int or generation < 0:
            return False
    recovery = value.get("recovery")
    return (_recovery_projection_valid(recovery)
            and _terminal_removals_valid(
                recovery.get(_TERMINAL_REMOVALS_KEY)
                if isinstance(recovery, Mapping) else None
            ))


def _sidecar_store(hass: Any, entry: Any, runtime: dict[str, Any]) -> Any | None:
    store = runtime.get(_SIDECAR_STORE_KEY)
    if store is not None:
        return store
    try:
        from homeassistant.helpers.storage import Store

        store = Store(hass, 1, _sidecar_key(entry), atomic_writes=True)
    except Exception:
        return None
    runtime[_SIDECAR_STORE_KEY] = store
    return store


def _storage_tasks(runtime: dict[str, Any]) -> set[asyncio.Task[Any]]:
    tasks = runtime.get(_STORAGE_TASKS_KEY)
    if not isinstance(tasks, set):
        tasks = set()
        runtime[_STORAGE_TASKS_KEY] = tasks
    return tasks


async def _drain_storage_tasks(runtime: dict[str, Any]) -> None:
    """Wait for Store writes from this runtime before its replacement."""
    while True:
        pending = [task for task in _storage_tasks(runtime) if not task.done()]
        if not pending:
            return
        for task in pending:
            try:
                await asyncio.shield(task)
            except BaseException:
                # A failed old write cannot be allowed to block unload.  The
                # conservative sidecar/hold remains authoritative on reload.
                pass


def _reopen_interrupted_recovery(state: dict[str, Any]) -> bool:
    """Turn an abandoned recovery workflow back into a retryable hold.

    A host restart can leave a durable workflow in reset_pending or
    reconciling after the task that owned its token disappeared.  Retaining
    the incident and all historical liabilities while rotating its token is
    the only safe startup behavior; a fresh lifecycle may then explicitly
    resume the held scope after its own enrollment and observations.
    """
    changed = False
    incidents = state.get("incidents", {})
    obligations = state.get("obligations", {})
    if not isinstance(incidents, dict):
        incidents = {}
    if not isinstance(obligations, dict):
        obligations = {}
    for incident_id, incident in incidents.items():
        if (not isinstance(incident, dict)
                or "phase" not in incident
                or incident.get("phase") == "released"):
            continue
        if incident.get("phase") in _INTERRUPTED_PHASES:
            token = uuid4().hex
            incident["phase"] = "held"
            incident["retryable"] = True
            incident["last_error"] = "recovery_workflow_interrupted"
            if incident.get("phase_token") != token:
                incident["phase_token"] = token
                changed = True
        else:
            token = incident.get("phase_token")
        obligation = obligations.get(incident_id)
        if (isinstance(obligation, dict)
                and obligation.get("phase") not in (None, "released")):
            if obligation.get("phase") in _INTERRUPTED_PHASES:
                if not isinstance(token, str) or not token:
                    token = uuid4().hex
                obligation["phase"] = "held"
                obligation["retryable"] = True
                obligation["last_error"] = "recovery_workflow_interrupted"
            if isinstance(token, str) and obligation.get("phase_token") != token:
                obligation["phase_token"] = token
                changed = True
    # Lifecycle obligations can exist without an incident record (for
    # example, a finite cleanup timer).  They still need their old token
    # revoked after a crash, while preserving their deadline and scope.
    for obligation_id, obligation in obligations.items():
        if (not isinstance(obligation, dict)
                or "phase" not in obligation
                or obligation.get("phase") == "released"):
            continue
        if obligation_id in incidents:
            continue
        if obligation.get("phase") in _INTERRUPTED_PHASES:
            token = uuid4().hex
            obligation["phase"] = "held"
            obligation["retryable"] = True
            obligation["last_error"] = "recovery_workflow_interrupted"
            if obligation.get("phase_token") != token:
                obligation["phase_token"] = token
                changed = True
    return changed


def _merge_recovery_roots(
    primary: Mapping[str, Any], legacy: Mapping[str, Any], *,
    prefer_legacy_mutable: bool = False,
) -> dict[str, Any]:
    """Merge records while preserving immutable evidence and current outcomes."""

    def hazard_identity_matches(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
        """Return whether two stable-key hazards describe the same incident.

        ``ble:<prefix>`` predates per-attempt hazard identity, so a later
        projection can reuse that key for a new quarantine.  Mutable state
        (especially ``recovery_retired``) must never cross that boundary.
        A missing attempt or producer binding cannot certify that a retirement
        belongs to the same quarantine. Legacy records are therefore kept as
        history rather than allowed to transfer mutable state.
        """
        for field in ("attempt_id", "prefix"):
            left = existing.get(field)
            right = incoming.get(field)
            if (left is None or right is None
                    or str(left) != str(right)):
                return False
        left_producer = existing.get("producer_identity")
        right_producer = incoming.get("producer_identity")
        if ((left_producer is None) != (right_producer is None)
                or (left_producer is not None
                    and str(left_producer) != str(right_producer))):
            return False
        left_binding = existing.get("binding")
        right_binding = incoming.get("binding")
        if not isinstance(left_binding, Mapping) or not isinstance(right_binding, Mapping):
            return False
        left_entities = left_binding.get("entities")
        right_entities = right_binding.get("entities")
        left_charger = left_entities.get("charger") if isinstance(left_entities, Mapping) else None
        right_charger = right_entities.get("charger") if isinstance(right_entities, Mapping) else None
        if not isinstance(left_charger, Mapping) or not isinstance(right_charger, Mapping):
            return False
        for field in ("platform", "config_entry_id", "device_id", "unique_id"):
            left_value = left_charger.get(field)
            right_value = right_charger.get(field)
            if (left_value is None or right_value is None
                    or str(left_value) != str(right_value)):
                return False
        left_entity_id = left_charger.get("entity_id")
        right_entity_id = right_charger.get("entity_id")
        if ((left_entity_id is None) != (right_entity_id is None)
                or (left_entity_id is not None
                    and str(left_entity_id) != str(right_entity_id))):
            return False
        return True

    def hazard_is_unresolved(item: Mapping[str, Any]) -> bool:
        return (item.get("state") in ("pending", "isolated")
                and not item.get("recovery_retired"))

    def hazard_history_key(target: Mapping[str, Any], item_id: str,
                           item: Mapping[str, Any]) -> str:
        identity = {
            field: deepcopy(item.get(field))
            for field in ("attempt_id", "prefix", "producer_identity", "binding")
        }
        attempt_id = str(item.get("attempt_id") or "")
        suffix = attempt_id or f"legacy-{_digest(identity)[:16]}"
        base = f"{item_id}@{suffix}"
        candidate = base
        suffix = 2
        while candidate in target:
            existing = target.get(candidate)
            existing_identity = {
                field: deepcopy(existing.get(field))
                for field in ("attempt_id", "prefix", "producer_identity", "binding")
            } if isinstance(existing, Mapping) else None
            if existing_identity is not None and _digest(existing_identity) == _digest(identity):
                return candidate
            candidate = f"{base}#{suffix}"
            suffix += 1
        return candidate

    def preserve_hazard_history(target: dict[str, Any], item_id: str,
                                item: Mapping[str, Any]) -> None:
        history_id = hazard_history_key(target, item_id, item)
        # A replayed projection is idempotent.  Never replace an existing
        # historical proof with a weaker pending copy from a stale source.
        if history_id not in target:
            target[history_id] = deepcopy(item)

    result = deepcopy(dict(primary))
    vehicles = result.setdefault("vehicles", {})
    old_vehicles = legacy.get("vehicles", {}) if isinstance(legacy, Mapping) else {}
    if isinstance(old_vehicles, Mapping):
        for vid, old_ledger in old_vehicles.items():
            if not isinstance(old_ledger, Mapping):
                continue
            had_primary_ledger = vid in vehicles
            current = vehicles.setdefault(vid, {"route": None, "attempts": {}, "hazards": {}})
            if not isinstance(current, dict):
                continue
            for key in ("attempts", "hazards"):
                source = old_ledger.get(key, {})
                if not isinstance(source, Mapping):
                    continue
                target = current.setdefault(key, {})
                if isinstance(target, dict):
                    for item_id, item in source.items():
                        existing = target.get(item_id)
                        if not isinstance(existing, Mapping):
                            target[item_id] = deepcopy(item)
                            continue
                        if (key == "hazards" and isinstance(item, Mapping)
                                and not hazard_identity_matches(existing, item)):
                            # A stable prefix key can legitimately represent a
                            # new quarantine after the previous one retired.
                            # Keep both immutable records.  The unresolved
                            # record owns the stable key so all existing guards
                            # continue to fail closed; the older record moves to
                            # a deterministic history key and retains its proof.
                            existing_unresolved = hazard_is_unresolved(existing)
                            incoming_unresolved = hazard_is_unresolved(item)
                            if existing_unresolved and not incoming_unresolved:
                                preserve_hazard_history(target, str(item_id), item)
                            else:
                                preserve_hazard_history(target, str(item_id), existing)
                                target[item_id] = deepcopy(item)
                            continue
                        if not prefer_legacy_mutable:
                            # The sidecar is authoritative during reload.  A
                            # legacy snapshot may only contribute records absent
                            # from it; it cannot roll back a known liability.
                            continue
                        merged = dict(existing)
                        if key == "attempts":
                            # Attempt identity, route, target, owner and deadline
                            # are immutable.  Runtime completion and retirement
                            # markers are the mutable projection fields.
                            incoming_outcome = item.get("outcome")
                            current_outcome = existing.get("outcome", "pending")
                            if incoming_outcome is not None:
                                # A live legacy projection can lag the durable
                                # sidecar.  Treat sent_unknown as a real
                                # unresolved state, so a stale pending projection
                                # cannot erase that evidence.  Terminal outcomes
                                # remain monotonic at the projection boundary.
                                terminal = _TERMINAL_ATTEMPT_OUTCOMES
                                outcome_rank = {
                                    "pending": 0,
                                    "sent_unknown": 1,
                                    "accepted_unconfirmed": 2,
                                    "not_sent": 3,
                                    "confirmed": 3,
                                    "rejected": 3,
                                }
                                current_rank = outcome_rank.get(str(current_outcome), 0)
                                incoming_rank = outcome_rank.get(str(incoming_outcome), 0)
                                if ((current_outcome in terminal
                                     and incoming_outcome == current_outcome)
                                        or (current_outcome not in terminal
                                            and incoming_rank >= current_rank)):
                                    merged["outcome"] = incoming_outcome
                            if "recovery_retired" in item:
                                merged["recovery_retired"] = deepcopy(item["recovery_retired"])
                        else:
                            current_state = str(existing.get("state") or "pending")
                            incoming_state = str(item.get("state") or "pending")
                            rank = {"pending": 0, "isolated": 1, "retired": 2}
                            if rank.get(incoming_state, 0) >= rank.get(current_state, 0):
                                for field in ("state", "binding", "control_off_at", "status_off_at", "fence_proof"):
                                    if field in item:
                                        merged[field] = deepcopy(item[field])
                            if "recovery_retired" in item:
                                merged["recovery_retired"] = deepcopy(item["recovery_retired"])
                        target[item_id] = merged
            if (
                prefer_legacy_mutable
                and "route" in old_ledger
                and not (
                    current.get("route") is None
                    and _terminal_cleanup_proof_matches_route(
                        result, vid, old_ledger.get("route")
                    )
                )
            ):
                # A live B completion clears a retired BLE route by writing None;
                # setdefault would resurrect that route from the sidecar.  The
                # exception above is a sidecar-authorized terminal Stop clear;
                # it must propagate into the legacy projection instead.
                current["route"] = deepcopy(old_ledger.get("route"))
            elif not had_primary_ledger and old_ledger.get("route") is not None:
                current["route"] = deepcopy(old_ledger.get("route"))

    # The live automation projection is intentionally allowed to omit a
    # terminal attempt, but omission alone is ambiguous after reload.  Apply
    # only finish-specific proofs that match the complete immutable record;
    # malformed proofs and unknown outcomes remain untouched.
    removals = result.get(_TERMINAL_REMOVALS_KEY, {})
    # A cleanup-only native route is a stop fence, not a permanent provider
    # preference.  The exact terminal-removal proof is the only authority
    # that may clear one during projection merge: an obligation disappearing
    # by itself is insufficient because the physical Stop could still be
    # unknown.  Track only routes whose matching Stop record was removed and
    # settle them after all proofs have been applied, so another unresolved
    # attempt/hazard keeps the vehicle fail-closed.
    terminal_cleanup_routes: set[str] = set()
    if isinstance(removals, Mapping):
        for vid, proofs in removals.items():
            if not isinstance(proofs, Mapping):
                continue
            ledger = vehicles.get(vid)
            if not isinstance(ledger, dict):
                continue
            attempts = ledger.get("attempts")
            if not isinstance(attempts, dict):
                continue
            for attempt_id, proof in proofs.items():
                existing = attempts.get(attempt_id)
                if _terminal_removal_matches(existing, proof):
                    ledger_route = ledger.get("route")
                    if (
                        isinstance(ledger_route, Mapping)
                        and ledger_route.get("cleanup_only") is True
                        and proof.get("operation") == "stop"
                        and dict(ledger_route) == dict(proof.get("route", {}))
                    ):
                        terminal_cleanup_routes.add(str(vid))
                    attempts.pop(attempt_id, None)
                elif (
                    # A previous merge may already have applied this exact
                    # terminal proof and removed the attempt while a stale
                    # legacy projection kept the cleanup-only route.  The
                    # proof itself is durable, schema-validated evidence;
                    # permit the idempotent route clear only when its complete
                    # route matches, never merely because the attempt is
                    # absent.
                    existing is None
                    and _terminal_removals_valid({
                        str(vid): {str(attempt_id): proof}
                    })
                    and isinstance(ledger.get("route"), Mapping)
                    and ledger["route"].get("cleanup_only") is True
                    and proof.get("operation") == "stop"
                    and dict(ledger["route"]) == dict(proof.get("route", {}))
                ):
                    terminal_cleanup_routes.add(str(vid))
    for vid in terminal_cleanup_routes:
        ledger = vehicles.get(vid)
        if not isinstance(ledger, dict):
            continue
        remaining_attempts = ledger.get("attempts", {})
        if any(
            isinstance(record, Mapping)
            and not record.get("recovery_retired")
            and record.get("outcome", "pending") in ("pending", "sent_unknown")
            for record in remaining_attempts.values()
        ) if isinstance(remaining_attempts, Mapping) else True:
            continue
        remaining_hazards = ledger.get("hazards", {})
        if any(
            isinstance(hazard, Mapping)
            and not hazard.get("recovery_retired")
            and hazard.get("state") in ("pending", "isolated")
            for hazard in remaining_hazards.values()
        ) if isinstance(remaining_hazards, Mapping) else True:
            continue
        if isinstance(ledger.get("route"), Mapping) and ledger["route"].get("cleanup_only") is True:
            ledger["route"] = None
    return result


def _sidecar_recovery(runtime: dict[str, Any], state: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    selected = state if state is not None else runtime.get(_SIDECAR_STATE_KEY)
    if not isinstance(selected, Mapping):
        return None
    recovery = selected.get("recovery")
    return recovery if isinstance(recovery, dict) else None


def _sync_sidecar_projection(runtime: dict[str, Any], state: dict[str, Any]) -> bool:
    recovery = _recovery(runtime, create=False)
    if recovery is None:
        return False
    state["recovery"] = _merge_recovery_roots(
        state.get("recovery", {"vehicles": {}}), recovery,
        prefer_legacy_mutable=True,
    )
    return True


async def _save_sidecar(
    hass: Any,
    entry: Any,
    runtime: dict[str, Any],
    state: Mapping[str, Any],
    *,
    allow_stopping: bool = False,
) -> bool:
    # The lifecycle can replace an entry's runtime while a Store write is
    # still shielded.  Never let an old callback create or commit a write
    # against the replacement sidecar.  The shutdown path is the sole caller
    # allowed to persist a final conservative snapshot while stopping.
    if _runtime(hass, entry) is not runtime:
        return False
    if runtime.get(_RECOVERY_STOPPING_KEY) is True and not allow_stopping:
        return False
    store = _sidecar_store(hass, entry, runtime)
    if store is None or not hasattr(store, "async_save"):
        return False
    payload = deepcopy(dict(state))
    task: asyncio.Task[Any] | None = None

    async def _write() -> Any:
        if _runtime(hass, entry) is not runtime:
            return False
        if runtime.get(_RECOVERY_STOPPING_KEY) is True and not allow_stopping:
            return False
        return await store.async_save(payload)

    try:
        task = asyncio.ensure_future(_write())
        _storage_tasks(runtime).add(task)
        result = await asyncio.shield(task)
        return result is not False
    except asyncio.CancelledError:
        if task is not None:
            try:
                await asyncio.shield(task)
            except BaseException:
                pass
        raise
    except Exception:
        return False
    finally:
        if task is not None:
            _storage_tasks(runtime).discard(task)


async def async_load_recovery_state(
    hass: Any, entry: Any, *, enrolled: bool | None = None,
) -> bool:
    """Hydrate the durable recovery sidecar before EV callbacks are enabled."""
    runtime = _runtime(hass, entry)
    if runtime is None:
        return False
    # A successful load starts a new runtime generation.  The old generation
    # is fenced by identity during replacement; this flag also protects the
    # short interval where a caller still holds the old runtime object.
    runtime[_RECOVERY_STOPPING_KEY] = False
    prior_epoch = runtime.get(_RECOVERY_EPOCH_KEY)
    runtime[_RECOVERY_EPOCH_KEY] = (
        prior_epoch + 1 if type(prior_epoch) is int and prior_epoch >= 0 else 1
    )
    if enrolled is True:
        runtime["_tesla_ev_recovery_enrolled"] = True
    store = _sidecar_store(hass, entry, runtime)
    if store is None or not hasattr(store, "async_load"):
        runtime[_SIDECAR_HOLD_KEY] = "sidecar_store_unavailable"
        return False
    try:
        loaded = await store.async_load()
    except asyncio.CancelledError:
        raise
    except Exception:
        runtime[_SIDECAR_HOLD_KEY] = "sidecar_load_failed"
        return False

    legacy_present = RECOVERY_KEY in runtime or LEGACY_RECOVERY_KEY in runtime
    legacy = _recovery(runtime, create=False)
    # `_recovery` intentionally returns None for a malformed existing root so
    # mutation callers cannot silently replace it.  The same rule applies at
    # startup: preserve the raw value and hold rather than importing an empty
    # journal over evidence that cannot be interpreted safely.
    if legacy_present and legacy is None:
        runtime[_SIDECAR_HOLD_KEY] = "legacy_journal_malformed"
        return False
    if loaded is None:
        # A first enrollment may import only records that genuinely survived in
        # the current runtime projection.  Once the sidecar has been marked
        # established, its disappearance is a hold rather than a clean slate.
        if (runtime.get("_tesla_ev_recovery_sidecar_established")
                or runtime.get("_tesla_ev_recovery_enrolled") is True
                or enrolled is True):
            runtime[_SIDECAR_HOLD_KEY] = "sidecar_missing"
            return False
        if legacy is not None and not _recovery_projection_valid(legacy):
            runtime[_SIDECAR_HOLD_KEY] = "legacy_journal_malformed"
            return False
        state = _new_sidecar()
        if legacy is not None:
            state["recovery"] = deepcopy(legacy)
        if not await _save_sidecar(hass, entry, runtime, state):
            runtime[_SIDECAR_HOLD_KEY] = "sidecar_initial_save_failed"
            return False
    elif not _sidecar_valid(loaded):
        runtime[_SIDECAR_HOLD_KEY] = "sidecar_malformed"
        return False
    else:
        state = deepcopy(loaded)
        if legacy is not None and not _recovery_projection_valid(legacy):
            runtime[_SIDECAR_HOLD_KEY] = "legacy_journal_malformed"
            return False
        if legacy is not None:
            merged = _merge_recovery_roots(state["recovery"], legacy)
            if merged != state["recovery"]:
                state["recovery"] = merged
                state["revision"] += 1
                if not await _save_sidecar(hass, entry, runtime, state):
                    runtime[_SIDECAR_HOLD_KEY] = "sidecar_merge_save_failed"
                    return False

    if _reopen_interrupted_recovery(state):
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            runtime[_SIDECAR_HOLD_KEY] = "recovery_interrupted_save_failed"
            return False

    runtime[_SIDECAR_STATE_KEY] = state
    runtime[_SIDECAR_LOADED_KEY] = True
    runtime["_tesla_ev_recovery_enrolled"] = True
    runtime["_tesla_ev_recovery_sidecar_established"] = True
    runtime.pop(_SIDECAR_HOLD_KEY, None)
    # The legacy journal remains a projection for old callers; all new
    # liability/incident records live in the sidecar above.
    runtime[RECOVERY_KEY] = deepcopy(state["recovery"])
    return True


def _sidecar_ready(runtime: dict[str, Any]) -> bool:
    return runtime.get(_SIDECAR_LOADED_KEY) is True and _sidecar_valid(runtime.get(_SIDECAR_STATE_KEY))


def _next_revision(state: dict[str, Any]) -> int:
    state["revision"] = int(state.get("revision", 0)) + 1
    return state["revision"]


def _restore_sidecar_state(state: dict[str, Any], before: Mapping[str, Any]) -> None:
    state.clear()
    state.update(deepcopy(dict(before)))


async def _persist_recovery_sidecar(
    hass: Any, entry: Any, runtime: dict[str, Any],
) -> bool:
    if not _sidecar_ready(runtime):
        return False
    state = runtime[_SIDECAR_STATE_KEY]
    before_override = runtime.pop(_TERMINAL_SIDECAR_BEFORE_KEY, _MISSING)
    before = (
        deepcopy(before_override)
        if isinstance(before_override, Mapping) else deepcopy(state)
    )
    if not _sync_sidecar_projection(runtime, state):
        return False
    state["revision"] = int(state.get("revision", 0)) + 1
    try:
        saved = await _save_sidecar(hass, entry, runtime, state)
    except asyncio.CancelledError:
        # _save_sidecar drains its shielded Store task before propagating.
        # Keep the in-memory mutation conservative because the durable write
        # may already have committed the new liability.
        raise
    if not saved:
        _restore_sidecar_state(state, before)
    elif (_runtime(hass, entry) is runtime
            and runtime.get(_SIDECAR_STATE_KEY) is state):
        # Publish only the exact terminal Stop route clear. Other live journal
        # objects retain their identity and every unresolved liability.
        live = _recovery(runtime, create=False)
        live_vehicles = live.get("vehicles", {}) if isinstance(live, Mapping) else {}
        saved_vehicles = state["recovery"]["vehicles"]
        for vid, ledger in live_vehicles.items():
            saved_ledger = saved_vehicles.get(vid)
            if (isinstance(ledger, dict) and isinstance(saved_ledger, Mapping)
                    and saved_ledger.get("route") is None
                    and not _cleanup_route_has_liabilities(ledger)
                    and _terminal_cleanup_proof_matches_route(
                        state["recovery"], vid, ledger.get("route"))):
                ledger["route"] = None
    return saved


async def async_persist_recovery_state(hass: Any, entry: Any) -> bool:
    """Persist the authoritative sidecar without invoking legacy projection hooks."""
    runtime = _runtime(hass, entry)
    if runtime is None:
        return False
    async with _journal_lock(runtime):
        return await _persist_recovery_sidecar(hass, entry, runtime)


async def async_shutdown_recovery_state(
    hass: Any, entry: Any, runtime: dict[str, Any] | None = None,
) -> bool:
    """Fence a runtime and drain its sidecar writes before lifecycle unload.

    Callers should invoke this while the entry still points at ``runtime``.
    The identity check in ``_save_sidecar`` remains authoritative if unload
    races a replacement.  An old runtime is never permitted to write after
    its stopping epoch has been revoked, even when the entry dictionary has
    not yet been replaced by the caller.
    """
    active = _runtime(hass, entry)
    selected = runtime if isinstance(runtime, dict) else active
    if selected is None:
        return False
    save_result = True
    if active is selected:
        async with _journal_lock(selected):
            selected[_RECOVERY_STOPPING_KEY] = True
            prior_epoch = selected.get(_RECOVERY_EPOCH_KEY)
            selected[_RECOVERY_EPOCH_KEY] = (
                prior_epoch + 1 if type(prior_epoch) is int and prior_epoch >= 0 else 1
            )
            state = selected.get(_SIDECAR_STATE_KEY)
            if isinstance(state, dict) and _sidecar_valid(state):
                before = deepcopy(state)
                if _reopen_interrupted_recovery(state):
                    _next_revision(state)
                # Even an already-held incident gets a new phase token above;
                # save it so callbacks from the unloading owner cannot resume
                # the old workflow after a reload.
                save_result = await _save_sidecar(
                    hass, entry, selected, state, allow_stopping=True,
                )
                if not save_result:
                    _restore_sidecar_state(state, before)
                    selected[_SIDECAR_HOLD_KEY] = "recovery_shutdown_save_failed"
    else:
        # A replacement already won the identity race.  Retain the old
        # runtime's uncertainty fence, but do not write through it.
        selected[_RECOVERY_STOPPING_KEY] = True
        prior_epoch = selected.get(_RECOVERY_EPOCH_KEY)
        selected[_RECOVERY_EPOCH_KEY] = (
            prior_epoch + 1 if type(prior_epoch) is int and prior_epoch >= 0 else 1
        )
    await _drain_storage_tasks(selected)
    return save_result


async def async_update_recovery_obligations(
    hass: Any, entry: Any, obligations: Mapping[str, Any], *,
    expected_revision: int | None = None,
) -> bool:
    """Persist lifecycle obligations owned by callers (Boost/quick-control)."""
    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime) or not isinstance(obligations, Mapping):
        return False
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        if (expected_revision is not None
                and int(state.get("revision", 0)) != expected_revision):
            return False
        before = deepcopy(state)
        target = state.setdefault("obligations", {})
        if not isinstance(target, dict):
            _restore_sidecar_state(state, before)
            return False
        for key, value in obligations.items():
            normalized_key = str(key)
            if normalized_key in state.get("incidents", {}):
                _restore_sidecar_state(state, before)
                return False
            target[normalized_key] = deepcopy(value)
        _next_revision(state)
        saved = await _save_sidecar(hass, entry, runtime, state)
        if not saved:
            _restore_sidecar_state(state, before)
        return saved


def recovery_sidecar_snapshot(hass: Any, entry: Any) -> dict[str, Any] | None:
    runtime = _runtime(hass, entry)
    state = runtime.get(_SIDECAR_STATE_KEY) if runtime else None
    return deepcopy(state) if isinstance(state, Mapping) else None


def restore_recovery_projection(hass: Any, entry: Any) -> bool:
    """Reapply the authoritative sidecar projection after legacy restore."""
    runtime = _runtime(hass, entry)
    state = runtime.get(_SIDECAR_STATE_KEY) if runtime else None
    if not isinstance(state, Mapping) or not _sidecar_valid(state):
        return False
    runtime[RECOVERY_KEY] = deepcopy(state["recovery"])
    return True


async def _persist(hass: Any, entry: Any, runtime: dict[str, Any]) -> bool:
    try:
        if _sidecar_ready(runtime):
            if not await _persist_recovery_sidecar(hass, entry, runtime):
                return False

        from .automations.ev_ownership import persist_ev_runtime_state

        persist_kwargs = {}
        try:
            parameters = inspect.signature(persist_ev_runtime_state).parameters.values()
            if any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters):
                persist_kwargs["_recovery_already_saved"] = True
            elif "_recovery_already_saved" in inspect.signature(
                    persist_ev_runtime_state
            ).parameters:
                persist_kwargs["_recovery_already_saved"] = True
        except (TypeError, ValueError):
            pass
        result = persist_ev_runtime_state(
            hass, entry, runtime.get("automation_store"), **persist_kwargs,
        )
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The persistence coroutine may already have entered its
                # save.  Drain it before propagating cancellation, otherwise
                # a late commit can resurrect a rolled-back journal.
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass
                raise
        if result is not True:
            return False
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _candidate_list(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes)) or value is None:
        return [] if value is None else [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [deepcopy(item) for item in value]


def _producer_scope_matches(producers: list[Any], identity: Mapping[str, Any]) -> bool:
    """Require one exact physical producer for one reset transaction."""
    if len(producers) != 1 or not isinstance(identity, Mapping):
        return False
    selected = producers[0]
    if isinstance(selected, Mapping):
        # A structured producer scope is an identity claim, not a display
        # label.  Requiring the complete binding prevents a caller from
        # resetting whichever producer happens to share one device or prefix.
        producer_fields = {
            "platform": selected.get("platform"),
            "config_entry_id": selected.get("config_entry_id", selected.get("entry_id")),
            "device_id": selected.get("device_id"),
            "unique_id": selected.get("unique_id", selected.get("charger_unique_id")),
        }
        if any(not isinstance(value, str) or not value.strip()
               for value in producer_fields.values()):
            return False
        if any(producer_fields[field] != identity.get(field)
               for field in producer_fields):
            return False
        if selected.get("vin") is not None and selected.get("vin") != identity.get("vin"):
            return False
        if selected.get("prefix") is not None and not str(selected["prefix"]).strip():
            return False
        producer_identity = selected.get("producer_identity")
        if producer_identity is not None:
            expected = ":".join(producer_fields[field].strip()
                                 for field in ("platform", "config_entry_id", "device_id", "unique_id"))
            if producer_identity != expected:
                return False
        return True
    if isinstance(selected, str) and selected.strip():
        return selected in {
            str(identity.get("device_id") or ""),
            str(identity.get("unique_id") or ""),
            str(identity.get("producer_identity") or ""),
        }
    return False


def _liability_matches_stock_scope(ref: Mapping[str, Any], producer: Any) -> bool:
    """A stock reset may retire only BLE liabilities on its exact producer."""
    route = ref.get("route")
    route_identity = None
    if isinstance(route, Mapping):
        if route.get("kind") != "ble":
            return False
        route_values = {
            str(route.get("prefix") or ""),
            str(route.get("producer_identity") or ""),
        }
        route_identity = route.get("entity_identity")
        if isinstance(route_identity, Mapping):
            route_values.update(
                str(route_identity.get(field) or "")
                for field in ("device_id", "unique_id", "config_entry_id")
            )
    elif ref.get("kind") == "ble_quarantine":
        route_values = {str(ref.get("prefix") or "")}
        binding = ref.get("binding")
        if isinstance(binding, Mapping):
            entities = binding.get("entities")
            charger = entities.get("charger") if isinstance(entities, Mapping) else None
            if isinstance(charger, Mapping):
                route_identity = charger
    else:
        return False
    if isinstance(producer, Mapping):
        producer_fields = {
            "platform": producer.get("platform"),
            "config_entry_id": producer.get("config_entry_id", producer.get("entry_id")),
            "device_id": producer.get("device_id"),
            "unique_id": producer.get("unique_id", producer.get("charger_unique_id")),
        }
        if any(not isinstance(value, str) or not value.strip()
               for value in producer_fields.values()):
            return False
        if not isinstance(route_identity, Mapping):
            return False
        # Rate/Limit entities have their own unique IDs on the same physical
        # node as the Charger switch. A node restart retires all of its RAM
        # queues; it does not require those different entity IDs to be equal.
        if (not isinstance(route_identity.get("unique_id"), str)
                or not route_identity["unique_id"].strip()
                or any(route_identity.get(field) != producer_fields[field]
                       for field in ("platform", "config_entry_id", "device_id"))):
            return False
        expected_identity = ":".join(producer_fields[field].strip()
                                      for field in ("platform", "config_entry_id", "device_id", "unique_id"))
        if (producer.get("producer_identity") is not None
                and producer.get("producer_identity") != expected_identity):
            return False
        producer_prefix = producer.get("prefix")
        if producer_prefix is not None:
            expected_prefix = ref.get("prefix")
            if expected_prefix is None and isinstance(route, Mapping):
                expected_prefix = route.get("prefix")
            if str(producer_prefix) != str(expected_prefix or ""):
                return False
        # The exact entity binding above is sufficient.  Prefix is an
        # additional guard when the adapter supplied one; it is not a
        # substitute for the complete identity.
        return True
    else:
        producer_values = {str(producer or "")}
    normalize = lambda value: value.strip().lower().replace("-", "_")
    return any(normalize(value) and normalize(value) in {
        normalize(candidate) for candidate in producer_values
    } for value in route_values)


def _incident_for(state: Mapping[str, Any], incident_id: str | None) -> tuple[str, dict[str, Any]] | None:
    incidents = state.get("incidents", {})
    if not isinstance(incidents, Mapping):
        return None
    if incident_id is not None:
        selected = incidents.get(incident_id)
        return (incident_id, selected) if isinstance(selected, dict) else None
    for key, incident in incidents.items():
        if isinstance(incident, dict) and incident.get("phase") != "released":
            return str(key), incident
    return None


def _unresolved_liabilities(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    recovery = _recovery(runtime, create=False)
    if not isinstance(recovery, Mapping):
        return []
    result: list[dict[str, Any]] = []
    vehicles = recovery.get("vehicles", {})
    if not isinstance(vehicles, Mapping):
        return result
    for vid, ledger in vehicles.items():
        if not isinstance(ledger, Mapping):
            continue
        attempts = ledger.get("attempts", {})
        if isinstance(attempts, Mapping):
            for attempt_id, record in attempts.items():
                if (isinstance(record, Mapping)
                        and record.get("outcome", "pending") in ("pending", "sent_unknown")):
                    result.append({
                        "vehicle_id": str(vid),
                        "attempt_id": str(attempt_id),
                        "outcome": str(record.get("outcome", "pending")),
                        "operation": record.get("operation"),
                        "route": deepcopy(record.get("route")),
                    })
        hazards = ledger.get("hazards", {})
        if isinstance(hazards, Mapping):
            for hazard_id, hazard in hazards.items():
                if isinstance(hazard, Mapping) and hazard.get("state") in ("pending", "isolated"):
                    result.append({
                        "vehicle_id": str(vid),
                        "hazard_id": str(hazard_id),
                        # Stable hazard IDs are retained for compatibility,
                        # while attempt_id binds retirement to this exact
                        # quarantine when a prefix is reused.
                        "attempt_id": hazard.get("attempt_id"),
                        "kind": hazard.get("kind"),
                        "state": hazard.get("state"),
                        "prefix": hazard.get("prefix"),
                        "binding": deepcopy(hazard.get("binding")),
                        "producer_identity": hazard.get("producer_identity"),
                    })
    return result


def _select_liability_refs(all_refs: list[dict[str, Any]], requested: Any) -> list[dict[str, Any]] | None:
    """Select only actual unresolved records; never synthesize a command ID."""
    if requested is None:
        return deepcopy(all_refs)
    selected: list[dict[str, Any]] = []
    for candidate in _candidate_list(requested):
        matches = []
        for ref in all_refs:
            if isinstance(candidate, Mapping):
                if (candidate.get("vehicle_id") is not None
                        and str(candidate.get("vehicle_id")) != str(ref.get("vehicle_id"))):
                    continue
                # Hazard attempt_id is an identity binding for a quarantine,
                # not an additional match for an attempt-only request.  In
                # normal operation the two records may intentionally carry
                # the same originating command ID.
                if (candidate.get("attempt_id") is not None
                        and candidate.get("hazard_id") is None
                        and ref.get("hazard_id") is not None):
                    continue
                if candidate.get("attempt_id") is not None and candidate.get("attempt_id") != ref.get("attempt_id"):
                    continue
                if candidate.get("hazard_id") is not None and candidate.get("hazard_id") != ref.get("hazard_id"):
                    continue
                if candidate.get("attempt_id") is None and candidate.get("hazard_id") is None:
                    continue
                matches.append(ref)
            elif str(candidate) in {str(ref.get("attempt_id")), str(ref.get("hazard_id"))}:
                matches.append(ref)
        if len(matches) != 1:
            return None
        selected.extend(matches)
    if len({(item.get("vehicle_id"), item.get("attempt_id"), item.get("hazard_id")) for item in selected}) != len(selected):
        return None
    return deepcopy(selected)


async def async_record_evidence_loss(
    hass: Any,
    entry: Any,
    *,
    surviving_facts: Any = None,
    provenance: Any = None,
    missing_fields: Any = None,
    candidate_producers: Any = None,
    candidate_vehicles: Any = None,
    scope_basis: Any = None,
    vehicle_id: Any = None,
    expected_revision: int | None = None,
    kind: str = "evidence_lost",
    covered_attempts: Any = None,
    stock_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record uncertainty when a historical journal was genuinely lost."""
    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime):
        return None
    incident_kind = str(kind or "").strip().lower()
    if incident_kind not in {"evidence_lost", "command_uncertainty"}:
        return None
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        before_state = deepcopy(state)
        revision = int(state.get("revision", 0))
        if expected_revision is not None and expected_revision != revision:
            return None
        vehicles = _candidate_list(candidate_vehicles)
        if vehicle_id is not None and str(vehicle_id) not in {str(item) for item in vehicles}:
            vehicles.append(str(vehicle_id))
        if (incident_kind == "command_uncertainty"
                and (not _candidate_list(candidate_producers) or not vehicles)):
            return None
        incident_id = uuid4().hex
        all_refs = _unresolved_liabilities(runtime)
        if stock_scope is not None:
            # The adapter's pre-lock snapshot is only an admission hint.  A
            # new BLE quarantine may be recorded between that snapshot and
            # this journal lock, so reselect every currently unresolved record
            # for the exact VIN and physical producer now.
            if incident_kind != "command_uncertainty" or not isinstance(stock_scope, Mapping):
                return None
            stock_vehicle = stock_scope.get("vehicle_id")
            stock_producer = stock_scope.get("producer")
            if (stock_vehicle is None or not isinstance(stock_producer, Mapping)
                    or not str(stock_vehicle).strip()
                    or str(stock_vehicle) not in {str(item) for item in vehicles}):
                return None
            if not any(
                _producer_scope_matches([candidate], stock_producer)
                for candidate in _candidate_list(candidate_producers)
            ):
                return None
            scoped_refs = [
                ref for ref in all_refs
                if str(ref.get("vehicle_id")) == str(stock_vehicle)
                and _liability_matches_stock_scope(ref, stock_producer)
            ]
            liability_refs = _select_liability_refs(scoped_refs, None)
        else:
            liability_refs = _select_liability_refs(all_refs, covered_attempts)
        if liability_refs is None or (incident_kind == "command_uncertainty" and not liability_refs):
            return None
        generations = state.setdefault("generations", {})
        for item in vehicles:
            vid = str(item)
            current = generations.get(vid, 0)
            generations[vid] = (current if type(current) is int and current >= 0 else 0) + 1
        incident = {
            "incident_id": incident_id,
            "kind": incident_kind,
            "phase": "held",
            "created_at": time.time(),
            "surviving_facts": deepcopy(surviving_facts) if surviving_facts is not None else [],
            "provenance": deepcopy(provenance) if provenance is not None else [],
            "missing_fields": _candidate_list(missing_fields),
            "current_candidate_producers": _candidate_list(candidate_producers),
            "current_candidate_vehicles": vehicles,
            "scope_basis": deepcopy(scope_basis),
            "scope_closed": False,
            "operator_scope_confirmation": None,
            "generation": {str(item): generations[str(item)] for item in vehicles},
            "liability_refs": liability_refs,
            "covered_attempts": deepcopy(liability_refs),
            "retirement_evidence": [],
            "reconciliation_evidence": {},
        }
        state.setdefault("incidents", {})[incident_id] = incident
        state.setdefault("liabilities", {})[incident_id] = {
            "state": "unknown",
            "kind": incident_kind,
            "scope": deepcopy(incident["current_candidate_producers"]),
            "vehicles": deepcopy(vehicles),
            "references": deepcopy(liability_refs),
        }
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            _restore_sidecar_state(state, before_state)
            return None
        return deepcopy(incident)


async def async_confirm_recovery_scope(
    hass: Any,
    entry: Any,
    incident_id: str,
    *,
    expected_revision: int,
    candidate_producers: Any = None,
    candidate_vehicles: Any = None,
    scope_basis: Any = None,
    operator_confirmation: Any = False,
    automatic: bool = False,
) -> bool:
    """Close a recovery scope from operator or complete journal evidence.

    Automatic closure is deliberately limited to a command-uncertainty
    incident whose every recorded liability is matched to one complete,
    exact producer binding.  Evidence-loss incidents still require an
    explicit operator confirmation because no historical command identity is
    available to validate.
    """
    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime):
        return False
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        before_state = deepcopy(state)
        if int(state.get("revision", 0)) != expected_revision:
            return False
        selected = _incident_for(state, incident_id)
        if selected is None:
            return False
        _, incident = selected
        producers = _candidate_list(candidate_producers)
        vehicles = _candidate_list(candidate_vehicles)
        # ``None`` means the caller intentionally wants to retain the
        # incident's already recorded candidate set.  An explicitly empty
        # list is an omission of required operator coverage and must stay
        # closed rather than silently inheriting stale scope.
        if candidate_producers is None:
            producers = _candidate_list(incident.get("current_candidate_producers"))
        if candidate_vehicles is None:
            vehicles = _candidate_list(incident.get("current_candidate_vehicles"))
        if not producers or not vehicles:
            return False
        # Confirmation may expand a candidate inventory. It cannot remove a
        # still plausible old command producer or vehicle to obtain release.
        if (not {_digest(item) for item in _candidate_list(incident.get("current_candidate_producers"))}
                <= {_digest(item) for item in producers}
                or not {str(item) for item in _candidate_list(incident.get("current_candidate_vehicles"))}
                <= {str(item) for item in vehicles}):
            return False
        automatic_scope = False
        if automatic:
            if incident.get("kind") != "command_uncertainty":
                return False
            refs = incident.get("liability_refs")
            if (not isinstance(refs, list) or not refs
                    or len(producers) != 1
                    or not isinstance(producers[0], Mapping)):
                return False
            producer = producers[0]
            # A structured producer must be complete before it can authorize
            # retirement of the exact journaled BLE producer.
            producer_identity = {
                "platform": producer.get("platform"),
                "config_entry_id": producer.get("config_entry_id", producer.get("entry_id")),
                "device_id": producer.get("device_id"),
                "unique_id": producer.get("unique_id", producer.get("charger_unique_id")),
                "vin": producer.get("vin"),
            }
            if (not _producer_scope_matches(producers, producer_identity)
                    or any(not isinstance(ref, Mapping)
                           or not (ref.get("attempt_id") is not None
                                   or ref.get("hazard_id") is not None)
                           or not _liability_matches_stock_scope(ref, producer)
                           for ref in refs)):
                return False
            covered_vehicles = {str(ref.get("vehicle_id")) for ref in refs}
            if covered_vehicles != {str(item) for item in vehicles}:
                return False
            automatic_scope = True
        elif operator_confirmation is not True:
            return False
        generations = state.setdefault("generations", {})
        for vehicle in vehicles:
            vid = str(vehicle)
            if vid not in incident.get("generation", {}):
                generations[vid] = current_vehicle_generation(hass, entry, vid) + 1
                incident.setdefault("generation", {})[vid] = generations[vid]
        incident["current_candidate_producers"] = producers
        incident["current_candidate_vehicles"] = vehicles
        liability = state.get("liabilities", {}).get(incident_id)
        if isinstance(liability, dict):
            liability["scope"] = deepcopy(producers)
            liability["vehicles"] = deepcopy(vehicles)
        incident["scope_basis"] = deepcopy(
            scope_basis if scope_basis is not None
            else ("known_journal_and_registry" if automatic_scope
                  else incident.get("scope_basis"))
        )
        incident["scope_closed"] = True
        incident["operator_scope_confirmation"] = {
            "confirmed": True,
            "confirmed_at": time.time(),
            "producers": deepcopy(producers),
            "vehicles": deepcopy(vehicles),
            "automatic": automatic_scope,
            "confirmed_by": "recorded_binding" if automatic_scope else "operator",
        }
        _next_revision(state)
        saved = await _save_sidecar(hass, entry, runtime, state)
        if not saved:
            _restore_sidecar_state(state, before_state)
            return None
        return deepcopy(incident)


def current_vehicle_generation(hass: Any, entry: Any, vehicle_id: Any) -> int:
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    if runtime is None or not vid:
        return 0
    state = runtime.get(_SIDECAR_STATE_KEY)
    generations = state.get("generations", {}) if isinstance(state, Mapping) else {}
    value = generations.get(vid) if isinstance(generations, Mapping) else None
    return value if type(value) is int and value >= 0 else 0


def _incident_covers_vehicle(incident: Mapping[str, Any], vehicle_id: str) -> bool:
    vehicles = incident.get("current_candidate_vehicles")
    if isinstance(vehicles, (list, tuple, set, frozenset)):
        # An explicitly empty candidate set does not prove that no vehicle is
        # affected; it is an incomplete inventory and must retain the site
        # wide positive-control hold until the scope is closed authoritatively.
        if not vehicles:
            return True
        if any(not isinstance(item, (str, int)) for item in vehicles):
            return True
        return vehicle_id in {str(item) for item in vehicles}
    generation = incident.get("generation")
    if isinstance(generation, Mapping):
        return vehicle_id in {str(item) for item in generation}
    liability = incident.get("liability_refs")
    if isinstance(liability, list):
        return any(isinstance(item, Mapping) and str(item.get("vehicle_id")) == vehicle_id
                   for item in liability)
    # A scope-less incident cannot be safely shown to cover only one car.
    return True


def recovery_control_allowed(
    hass: Any,
    entry: Any,
    vehicle_id: Any,
    generation: Any,
    operation: str,
    permission: Any = None,
) -> bool:
    """Final synchronous generation/phase fence for a provider mutation."""
    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime):
        # A runtime that explicitly enrolled for lifecycle recovery but could
        # not hydrate its durable sidecar is fail-closed.  Lightweight unit
        # fixtures which never enrolled retain the legacy behavior below.
        if (runtime is not None and runtime.get(_SIDECAR_HOLD_KEY) is not None
                and str(operation or "").strip().lower() not in {
                    "stop", "cleanup_stop", "force_stop",
                }):
            return False
        return True
    operation_name = str(operation or "").strip().lower()
    # A caller holding the old lifecycle generation must not mutate (or even
    # create a pending record for) the sidecar while unload is fencing it.
    # The replacement runtime will re-open only a durable held workflow.
    if operation_name in {"stop", "cleanup_stop", "force_stop"}:
        return True
    if runtime.get(_RECOVERY_STOPPING_KEY) is True:
        return False
    if operation_name not in POSITIVE_OPERATIONS:
        return False
    if runtime.get(_SIDECAR_HOLD_KEY) is not None:
        return False
    if type(generation) is not int or generation != current_vehicle_generation(hass, entry, vehicle_id):
        return False
    state = runtime[_SIDECAR_STATE_KEY]
    for incident in state.get("incidents", {}).values():
        if (not isinstance(incident, Mapping)
                or incident.get("phase") == "released"
                or not _incident_covers_vehicle(incident, str(vehicle_id))):
            continue
        return False
    if permission is None or permission is True:
        return True
    return (
        isinstance(permission, Mapping)
        and permission.get("phase") == "released"
        and permission.get("revision") == state.get("revision")
    )


async def async_advance_recovery_generation(
    hass: Any,
    entry: Any,
    vehicle_ids: Any,
    *,
    reason: str = "producer_recovery",
) -> dict[str, int] | None:
    """Fence old callbacks for the supplied VINs and persist the new values."""
    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime):
        return None
    ids = [str(item) for item in _candidate_list(vehicle_ids) if str(item)]
    if not ids:
        return None
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        before_state = deepcopy(state)
        generations = state.setdefault("generations", {})
        result: dict[str, int] = {}
        for vid in ids:
            old = generations.get(vid, 0)
            old = old if type(old) is int and old >= 0 else 0
            result[vid] = generations[vid] = old + 1
        state.setdefault("obligations", {})[f"generation:{uuid4().hex}"] = {
            "kind": "generation_fence", "vehicles": ids, "reason": reason,
            "generations": deepcopy(result), "created_at": time.time(),
        }
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            _restore_sidecar_state(state, before_state)
            return None
        return result


async def _finish_submission(hass: Any, entry: Any, runtime: dict[str, Any], submission_id: str,
                             *, status: str, error: str | None = None) -> None:
    async with _journal_lock(runtime):
        if _runtime(hass, entry) is not runtime or not _sidecar_ready(runtime):
            return
        state = runtime[_SIDECAR_STATE_KEY]
        record = state.get("submissions", {}).get(submission_id)
        if not isinstance(record, dict):
            return
        record["status"] = status
        record["completed_at"] = time.time()
        if error is not None:
            record["error"] = error
        _next_revision(state)
        try:
            await _save_sidecar(hass, entry, runtime, state)
        except BaseException:
            pass


async def async_track_submission(
    hass: Any,
    entry: Any,
    submit_callable: Any,
    generation: int,
    *,
    vehicle_id: Any = None,
    operation: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> Any:
    """Track a real provider task before awaiting it; cancellation never cancels the child."""
    runtime = _runtime(hass, entry)
    if runtime is None:
        raise RuntimeError("EV recovery runtime is unavailable")
    if not _sidecar_ready(runtime):
        if runtime.get(_SIDECAR_HOLD_KEY) is not None:
            raise RecoverySubmissionDenied("EV recovery sidecar is not ready")
        # Fixture runtimes which never enroll lifecycle recovery retain their
        # existing provider wrappers; production setup opts into this path by
        # hydrating the sidecar first.
        task = asyncio.ensure_future(submit_callable() if callable(submit_callable) else submit_callable)
        return await asyncio.shield(task)
    vid = str(vehicle_id or "")
    operation_name = str(operation or "").strip().lower()

    def _allowed() -> bool:
        return (
            type(generation) is int
            and recovery_control_allowed(hass, entry, vid, generation, operation_name)
        )

    if not _allowed():
        raise RecoverySubmissionDenied("EV recovery authority changed before submission")
    submission_id = uuid4().hex
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        state.setdefault("submissions", {})[submission_id] = {
            "submission_id": submission_id, "vehicle_id": vid,
            "operation": str(operation), "generation": generation,
            "status": "pending", "created_at": time.time(),
            "metadata": deepcopy(dict(metadata or {})),
        }
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            state["submissions"].pop(submission_id, None)
            raise RuntimeError("EV submission ticket was not persisted")
        if not _allowed():
            state["submissions"].pop(submission_id, None)
            _next_revision(state)
            await _save_sidecar(hass, entry, runtime, state)
            raise RecoverySubmissionDenied("EV recovery authority changed before handover")

        async def _run_submission() -> Any:
            # The durable ticket is already committed, but this final check is
            # immediately adjacent to the provider handover.  A generation or
            # incident change during ticket persistence cannot cross it.
            if not _allowed():
                await _finish_submission(
                    hass, entry, runtime, submission_id,
                    status="not_sent", error="recovery_authority_changed",
                )
                raise RecoverySubmissionDenied("EV recovery authority changed before provider call")
            if callable(submit_callable):
                result = submit_callable()
            else:
                result = submit_callable
                if inspect.iscoroutine(result):
                    result.close()
                    raise RecoverySubmissionDenied("EV recovery authority changed before provider call")
            if inspect.isawaitable(result):
                return await result
            return result

        task = asyncio.ensure_future(_run_submission())
        runtime.setdefault(_SUBMISSIONS_KEY, {})[submission_id] = task
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        async def _late_finalize() -> None:
            try:
                await asyncio.shield(task)
                await _finish_submission(hass, entry, runtime, submission_id, status="completed")
            except RecoverySubmissionDenied as exc:
                await _finish_submission(hass, entry, runtime, submission_id,
                                         status="not_sent", error=type(exc).__name__)
            except BaseException as exc:
                await _finish_submission(hass, entry, runtime, submission_id,
                                         status="error", error=type(exc).__name__)
            finally:
                runtime.get(_SUBMISSIONS_KEY, {}).pop(submission_id, None)
        asyncio.create_task(_late_finalize())
        raise
    except RecoverySubmissionDenied:
        raise
    except BaseException as exc:
        await _finish_submission(hass, entry, runtime, submission_id,
                                 status="error", error=type(exc).__name__)
        raise
    else:
        await _finish_submission(hass, entry, runtime, submission_id, status="completed")
        return result
    finally:
        if task.done():
            runtime.get(_SUBMISSIONS_KEY, {}).pop(submission_id, None)


def _tracked_submission_task(value: Any) -> Any:
    """Return a provider task from either the legacy or metadata form."""
    if isinstance(value, Mapping):
        return value.get("task")
    return value


async def async_track_cleanup_submission(
    hass: Any,
    entry: Any,
    refusal: Any,
    submit_callable: Any,
    *,
    vehicle_id: Any,
    generation: Any,
) -> Any:
    """Track a best-effort cleanup Stop without another Store prerequisite.

    ``async_begin_attempt`` has already restored the failed journal mutation
    and returned a typed ``RecoveryPersistenceRefused`` for a matching durable
    liability.  This function owns the short-lived claim and puts the actual
    provider child in the same submission registry used by ordinary journaled
    commands.  Outer cancellation shields that child; the claim is released
    only after it finishes or a final pre-handover fence proves that no write
    occurred, so a later fresh Stop can retry the same durable obligation.
    """
    route = getattr(refusal, "route", None)
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    initial_valid = not (
        not isinstance(refusal, RecoveryPersistenceRefused)
        or runtime is None
        or not _sidecar_ready(runtime)
        or runtime.get(_RECOVERY_STOPPING_KEY) is True
        or not isinstance(route, Mapping)
        or route.get("cleanup_scope_valid") is not True
        or route.get("vehicle_id") not in (None, vid)
        or getattr(refusal, "vehicle_id", vid) != vid
        or type(generation) is not int
        or generation != current_vehicle_generation(hass, entry, vid)
    )
    if not initial_valid:
        if isinstance(runtime, dict) and isinstance(route, Mapping):
            _release_best_effort_cleanup(
                runtime,
                route.get("_cleanup_scope_key"),
                route.get("_cleanup_claim_id"),
            )
        raise RecoverySubmissionDenied(
            "EV cleanup provider handover is no longer authorized"
        )

    cleanup_token = route.get("cleanup_obligation_id")
    scope_key = route.get("_cleanup_scope_key")
    claim_id = route.get("_cleanup_claim_id")
    claimed = runtime.get(_BEST_EFFORT_CLEANUP_KEY)

    def _release_claim() -> None:
        _release_best_effort_cleanup(runtime, scope_key, claim_id)

    def _valid_before_handover() -> bool:
        current_runtime = _runtime(hass, entry)
        if (
            current_runtime is not runtime
            or runtime.get(_RECOVERY_STOPPING_KEY) is True
            or not _sidecar_ready(runtime)
            or route.get("vehicle_id") not in (None, vid)
            or type(generation) is not int
            or generation != current_vehicle_generation(hass, entry, vid)
            or not _cleanup_binding_current(hass, entry, runtime, vid, route)
            or not _cleanup_scope_matches(runtime, vid, route, cleanup_token)
        ):
            return False
        current_claims = runtime.get(_BEST_EFFORT_CLEANUP_KEY)
        current_claim = (
            current_claims.get(scope_key)
            if isinstance(current_claims, Mapping) else None
        )
        return (
            isinstance(current_claim, Mapping)
            and current_claim.get("claim_id") == claim_id
        )

    current_claim = claimed.get(scope_key) if isinstance(claimed, Mapping) else None
    if (
        not isinstance(scope_key, tuple)
        or len(scope_key) != 2
        or not isinstance(claim_id, str)
        or not isinstance(current_claim, Mapping)
        or current_claim.get("claim_id") != claim_id
        or not _valid_before_handover()
    ):
        _release_claim()
        raise RecoverySubmissionDenied(
            "EV cleanup provider handover is no longer authorized"
        )

    submission_id = uuid4().hex
    handoff = {"handed_off": False}

    async def _run_cleanup() -> Any:
        if not _valid_before_handover():
            _release_claim()
            raise RecoverySubmissionDenied(
                "EV cleanup recovery authority changed before provider call"
            )
        if not callable(submit_callable):
            if inspect.iscoroutine(submit_callable):
                submit_callable.close()
            _release_claim()
            raise RecoverySubmissionDenied(
                "EV cleanup provider callback is not callable"
            )
        # The caller's callback performs its own final service-write guard.
        # This marker records that control crossed the core's last no-handover
        # fence; all later outcomes remain conservatively provider-owned.
        handoff["handed_off"] = True
        result = submit_callable()
        if inspect.isawaitable(result):
            return await result
        return result

    task = asyncio.ensure_future(_run_cleanup())
    submission_record = {
        "task": task,
        "vehicle_id": vid,
        "operation": "stop",
        "generation": generation,
        "cleanup_scope_key": scope_key,
        "handoff": handoff,
    }
    runtime.setdefault(_SUBMISSIONS_KEY, {})[submission_id] = submission_record

    def _child_done(_done: Any) -> None:
        tracked = runtime.get(_SUBMISSIONS_KEY, {})
        if isinstance(tracked, dict) and tracked.get(submission_id) is submission_record:
            tracked.pop(submission_id, None)
        _release_claim()

    task.add_done_callback(_child_done)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        async def _late_finalize() -> None:
            try:
                await asyncio.shield(task)
            except BaseException:
                pass
        # Keep the provider child and claim alive independently of the caller's
        # cancelled task.  The done callback performs the eventual release.
        asyncio.create_task(_late_finalize())
        raise


async def async_drain_submissions(
    hass: Any, entry: Any, *, vehicle_ids: Any = None,
    timeout: float | None = RECOVERY_DRAIN_TIMEOUT,
) -> bool:
    """Drain tracked provider tasks outside the journal lock.

    A provider task remains shielded and tracked when the bounded wait expires.
    Returning ``False`` keeps stock recovery in its held phase; it never treats
    a timeout as proof that a mutation was not handed to the provider.
    """
    runtime = _runtime(hass, entry)
    if runtime is None:
        return False
    tracked = runtime.get(_SUBMISSIONS_KEY) or {}
    state = runtime.get(_SIDECAR_STATE_KEY)
    records = state.get("submissions", {}) if isinstance(state, Mapping) else {}
    selected_vehicles = None
    if vehicle_ids is not None:
        selected_vehicles = {str(item) for item in _candidate_list(vehicle_ids)}
    tasks = []
    for submission_id, tracked_value in tracked.items():
        task = _tracked_submission_task(tracked_value)
        if task is None:
            continue
        record = records.get(submission_id) if isinstance(records, Mapping) else None
        if record is None and isinstance(tracked_value, Mapping):
            record = tracked_value
        # Unknown metadata is retained conservatively when no scope was
        # supplied.  For a targeted stock incident it cannot be safely
        # associated with another VIN, so keep it in the drain set rather than
        # claiming the selected producer is quiescent.
        if (selected_vehicles is not None and isinstance(record, Mapping)
                and str(record.get("vehicle_id")) not in selected_vehicles):
            continue
        tasks.append(task)
    if timeout is not None:
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            return False
    for task in tasks:
        try:
            shielded = asyncio.shield(task)
            if timeout is None:
                await shielded
            else:
                await asyncio.wait_for(shielded, timeout=timeout_value)
        except asyncio.TimeoutError:
            # wait_for cancels only the shield wrapper; the provider child is
            # still running and remains in the durable submission ledger.
            return False
        except BaseException:
            pass
    return all(task.done() for task in tasks)


async def _invoke_recovery_callback(callback: Any, *args: Any) -> Any:
    if not callable(callback):
        return None
    try:
        signature = inspect.signature(callback)
        positional = [parameter for parameter in signature.parameters.values()
                      if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
        accepts_varargs = any(parameter.kind == parameter.VAR_POSITIONAL
                              for parameter in signature.parameters.values())
        call_args = args if accepts_varargs or len(positional) >= len(args) else ()
    except (TypeError, ValueError):
        call_args = args
    result = callback(*call_args)
    if inspect.isawaitable(result):
        return await result
    return result


def _observation_value(record: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in record:
            return record.get(name)
    return None


def _observation_provenance(record: Mapping[str, Any], name: str) -> Any:
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get(name) is not None:
        return provenance.get(name)
    return record.get(f"{name}_provenance")


def _valid_reconciliation_observation(record: Any, *, observed_after: float) -> bool:
    if not isinstance(record, Mapping):
        return False
    values = {
        "requested_current": _observation_value(record, ("requested_current", "requested_amps")),
        "charge_limit": _observation_value(record, ("charge_limit", "limit")),
        "charging": _observation_value(record, ("charging", "is_charging", "charging_state")),
        "power": _observation_value(record, ("power", "power_kw", "charging_power")),
    }
    if values["requested_current"] is None or values["charge_limit"] is None:
        return False
    try:
        if not math.isfinite(float(values["requested_current"])):
            return False
        if not math.isfinite(float(values["charge_limit"])):
            return False
        if not math.isfinite(float(values["power"])):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    if values["charging"] is None:
        return False
    if any(_observation_provenance(record, name) in (None, "")
           for name in values):
        return False
    stamp = record.get("observed_at", record.get("source_timestamp"))
    try:
        observed = float(stamp)
        fresh = (math.isfinite(observed) and observed >= observed_after
                 and observed <= time.time() + 5)
    except (TypeError, ValueError, OverflowError):
        fresh = False
    return fresh


async def _mark_recovery_failure(
    hass: Any,
    entry: Any,
    runtime: dict[str, Any],
    incident_id: str,
    reason: str,
) -> None:
    try:
        async with _journal_lock(runtime):
            if not _sidecar_ready(runtime):
                return
            state = runtime[_SIDECAR_STATE_KEY]
            selected = _incident_for(state, incident_id)
            if selected is None:
                return
            _, incident = selected
            prior_phase = incident.get("phase")
            incident["last_error"] = str(reason)
            incident["last_error_at"] = time.time()
            if prior_phase in {"quiescing", "reset_pending", "reconciling"}:
                # A failed/cancelled phase is retryable.  Historical attempts
                # and retirement evidence stay untouched; only the workflow
                # obligation returns to its held gate.
                retry_token = uuid4().hex
                incident["phase"] = "held"
                incident["phase_token"] = retry_token
                incident["retryable"] = True
                obligation = state.get("obligations", {}).get(incident_id)
                if isinstance(obligation, dict):
                    obligation["phase"] = "held"
                    obligation["phase_token"] = retry_token
                    obligation["retryable"] = True
            _next_revision(state)
            await _save_sidecar(hass, entry, runtime, state)
    except BaseException:
        # The original provider/reset error remains the caller's result.  The
        # phase and durable liability stay conservative if this save fails.
        pass


def _retire_recovery_liabilities(
    runtime: dict[str, Any], refs: list[Mapping[str, Any]],
    incident_id: str, retirement: Mapping[str, Any],
) -> None:
    """Mark covered producer liabilities retired while keeping outcomes immutable."""
    recovery = _recovery(runtime, create=False)
    if not isinstance(recovery, Mapping):
        return
    marker = {
        "incident_id": incident_id,
        "state": "producer_retired_reconciled",
        "historical_outcome": "unknown",
        "retired_at": time.time(),
        "retirement_digest": _digest(retirement),
    }
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        ledger = recovery.get("vehicles", {}).get(str(ref.get("vehicle_id")))
        if not isinstance(ledger, dict):
            continue
        attempt_id = ref.get("attempt_id")
        hazard_id = ref.get("hazard_id")
        # A hazard reference may carry its originating attempt_id solely to
        # bind the reusable hazard key.  It must not also retire that attempt;
        # only a reference without hazard_id denotes an attempt record.
        if attempt_id is not None and hazard_id is None:
            record = ledger.get("attempts", {}).get(str(attempt_id))
            if isinstance(record, dict):
                record["recovery_retired"] = deepcopy(marker)
        if hazard_id is not None:
            hazard = ledger.get("hazards", {}).get(str(hazard_id))
            if isinstance(hazard, dict):
                # ``ble:<prefix>`` is a reusable key.  A late retirement for
                # an older quarantine must not retire a newer hazard that has
                # already replaced it in the live projection.
                current_attempt_id = hazard.get("attempt_id")
                referenced_attempt_id = ref.get("attempt_id")
                if ((current_attempt_id is None) != (referenced_attempt_id is None)
                        or (current_attempt_id is not None
                            and str(current_attempt_id) != str(referenced_attempt_id))):
                    continue
                for field in ("prefix", "producer_identity"):
                    expected = ref.get(field)
                    actual = hazard.get(field)
                    if expected is not None and actual is not None and str(expected) != str(actual):
                        break
                else:
                    hazard["recovery_retired"] = deepcopy(marker)


def _recovery_phase_current(
    state: Mapping[str, Any], incident_id: str, phase: str,
    phase_token: str, vehicles: list[str],
) -> bool:
    selected = _incident_for(state, incident_id)
    if selected is None:
        return False
    _, incident = selected
    obligation = state.get("obligations", {}).get(incident_id)
    if (incident.get("phase") != phase or incident.get("phase_token") != phase_token
            or not isinstance(obligation, Mapping)
            or obligation.get("phase") != phase
            or obligation.get("phase_token") != phase_token):
        return False
    generations = state.get("generations", {})
    expected = incident.get("generation", {})
    return all(
        isinstance(generations, Mapping) and isinstance(expected, Mapping)
        and generations.get(vid) == expected.get(vid)
        for vid in vehicles
    )


async def async_recover_stock(
    hass: Any,
    entry: Any,
    *,
    incident_id: str | None = None,
    expected_revision: int | None = None,
    profile: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    restart: Any = None,
    wait_disconnected: Any = None,
    wait_connected: Any = None,
    read_identity: Any = None,
    read_uptime: Any = None,
    observe: Any = None,
    reconcile: Any = None,
    quiesce: Any = None,
    sleep: Any = None,
) -> bool:
    """Run one idempotent stock-retirement and vehicle-reconciliation cycle."""
    from .tesla_ble_reset import StockResetError, async_execute_stock_reset

    runtime = _runtime(hass, entry)
    if runtime is None or not _sidecar_ready(runtime):
        return False
    async with _journal_lock(runtime):
        state = runtime[_SIDECAR_STATE_KEY]
        selected = _incident_for(state, incident_id)
        if selected is None:
            return False
        selected_id, incident = selected
        if expected_revision is not None and int(state.get("revision", 0)) != expected_revision:
            return False
        if incident.get("phase") == "released":
            return True
        if incident.get("phase") != "held" or incident.get("scope_closed") is not True:
            return False
        vehicles = [str(item) for item in _candidate_list(incident.get("current_candidate_vehicles"))]
        producers = _candidate_list(incident.get("current_candidate_producers"))
        if not vehicles or not producers or not _producer_scope_matches(producers, identity or {}):
            return False
        liability_refs = incident.get("liability_refs", [])
        if (not isinstance(liability_refs, list)
                or any(not _liability_matches_stock_scope(ref, producers[0])
                       for ref in liability_refs if isinstance(ref, Mapping))
                or any(not isinstance(ref, Mapping) for ref in liability_refs)):
            return False
        generations = state.setdefault("generations", {})
        for vid in vehicles:
            value = generations.get(vid, 0)
            generations[vid] = (value if type(value) is int and value >= 0 else 0) + 1
        incident["generation"] = {vid: generations[vid] for vid in vehicles}
        phase_token = uuid4().hex
        incident["phase"] = "quiescing"
        incident["phase_token"] = phase_token
        incident["quiesce_started_at"] = time.time()
        incident["producer_scope"] = deepcopy(producers)
        state.setdefault("obligations", {})[selected_id] = {
            "incident_id": selected_id, "phase": "quiescing", "vehicles": vehicles,
            "generations": deepcopy(incident["generation"]),
            "phase_token": phase_token,
            "historical_outcome": "unknown",
        }
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            return False
    try:
        drained = await async_drain_submissions(
            hass, entry, vehicle_ids=vehicles,
        )
        if not drained:
            await _mark_recovery_failure(hass, entry, runtime, selected_id, "submission_drain_failed")
            return False
        if callable(quiesce):
            result = await _invoke_recovery_callback(quiesce, tuple(producers), tuple(vehicles))
            if result is False:
                await _mark_recovery_failure(hass, entry, runtime, selected_id, "host_quiescence_refused")
                return False
    except asyncio.CancelledError:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, "cancelled_quiesce")
        raise
    except Exception as exc:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, type(exc).__name__)
        return False

    async with _journal_lock(runtime):
        state = runtime.get(_SIDECAR_STATE_KEY)
        selected = _incident_for(state, selected_id) if isinstance(state, Mapping) else None
        if not isinstance(state, dict) or selected is None:
            return False
        _, incident = selected
        if not _recovery_phase_current(
                state, selected_id, "quiescing", phase_token, vehicles):
            return False
        phase_token = uuid4().hex
        incident["phase"] = "reset_pending"
        incident["phase_token"] = phase_token
        incident["reset_started_at"] = time.time()
        state["obligations"][selected_id]["phase"] = "reset_pending"
        state["obligations"][selected_id]["phase_token"] = phase_token
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            return False
    try:
        retirement = await async_execute_stock_reset(
            profile=profile or {}, identity=identity or {}, restart=restart,
            wait_disconnected=wait_disconnected, wait_connected=wait_connected,
            read_identity=read_identity, read_uptime=read_uptime, sleep=sleep,
        )
    except asyncio.CancelledError:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, "cancelled_reset")
        raise
    except StockResetError as exc:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, str(exc))
        return False
    except Exception as exc:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, type(exc).__name__)
        return False

    async with _journal_lock(runtime):
        state = runtime.get(_SIDECAR_STATE_KEY)
        selected = _incident_for(state, selected_id) if isinstance(state, Mapping) else None
        if not isinstance(state, dict) or selected is None:
            return False
        _, incident = selected
        if not _recovery_phase_current(
                state, selected_id, "reset_pending", phase_token, vehicles):
            return False
        phase_token = uuid4().hex
        incident["retirement_evidence"].append(deepcopy(retirement))
        incident["phase"] = "reconciling"
        incident["phase_token"] = phase_token
        incident["reconcile_started_at"] = time.time()
        state["obligations"][selected_id]["phase"] = "reconciling"
        state["obligations"][selected_id]["phase_token"] = phase_token
        _next_revision(state)
        if not await _save_sidecar(hass, entry, runtime, state):
            return False
    try:
        observations: dict[str, Any] = {}
        observed_after = float(retirement.get("qualified_at", time.time()))
        for vid in vehicles:
            raw = await _invoke_recovery_callback(observe, vid)
            if isinstance(raw, Mapping) and vid in raw and isinstance(raw.get(vid), Mapping):
                raw = raw[vid]
            if not _valid_reconciliation_observation(raw, observed_after=observed_after):
                await _mark_recovery_failure(hass, entry, runtime, selected_id,
                                             f"invalid_reconciliation:{vid}")
                return False
            observations[vid] = deepcopy(dict(raw))
        if callable(reconcile):
            result = await _invoke_recovery_callback(reconcile, observations)
            if result is False:
                await _mark_recovery_failure(hass, entry, runtime, selected_id,
                                             "reconciliation_refused")
                return False
    except asyncio.CancelledError:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, "cancelled_reconcile")
        raise
    except Exception as exc:
        await _mark_recovery_failure(hass, entry, runtime, selected_id, type(exc).__name__)
        return False

    async with _journal_lock(runtime):
        state = runtime.get(_SIDECAR_STATE_KEY)
        selected = _incident_for(state, selected_id) if isinstance(state, Mapping) else None
        if not isinstance(state, dict) or selected is None:
            return False
        _, incident = selected
        if not _recovery_phase_current(
                state, selected_id, "reconciling", phase_token, vehicles):
            return False
        before_release = deepcopy(state)
        before_recovery = _snapshot_recovery(runtime)

        def rollback_release() -> None:
            _restore_sidecar_state(state, before_release)
            _restore_recovery(runtime, before_recovery)
            held = state["incidents"][selected_id]
            held["phase"] = "held"
            held["phase_token"] = uuid4().hex
            held["last_error"] = "release_not_persisted"
            state["obligations"][selected_id]["phase"] = "held"
            state["obligations"][selected_id]["phase_token"] = held["phase_token"]
            _next_revision(state)

        incident["reconciliation_evidence"] = observations
        _retire_recovery_liabilities(
            runtime, liability_refs, selected_id, retirement,
        )
        for vid in vehicles:
            ledger = _ledger(runtime, vid)
            if isinstance(ledger, dict) and isinstance(ledger.get("route"), Mapping):
                if ledger["route"].get("kind") == "ble":
                    # The retired producer must not be selected again before
                    # the caller supplies a fresh exact-provider route.
                    ledger["route"] = None
        incident["phase"] = "released"
        incident["phase_token"] = uuid4().hex
        incident["released_at"] = time.time()
        incident["historical_outcome"] = "unknown"
        state["obligations"][selected_id]["phase"] = "released"
        state["obligations"][selected_id]["phase_token"] = incident["phase_token"]
        liability = state.get("liabilities", {}).get(selected_id)
        if isinstance(liability, dict):
            liability["state"] = "retained_reconciled"
            liability["historical_outcome"] = "unknown"
        # The legacy projection was mutated above by retirement/route cleanup.
        # Copy those mutations into the durable sidecar before the final save;
        # otherwise a reload would resurrect the old BLE liability.
        if not _sync_sidecar_projection(runtime, state):
            rollback_release()
            return False
        _next_revision(state)
        try:
            saved = await _save_sidecar(hass, entry, runtime, state)
        except BaseException:
            rollback_release()
            raise
        if not saved:
            rollback_release()
            return False
        return True


def _compatible(hass: Any, entry: Any, runtime: dict[str, Any], vid: str,
                route: Mapping[str, Any]) -> bool:
    if not isinstance(route, Mapping):
        return False
    identity = _identity(hass, str(route.get("entity_id") or ""))
    if identity is None or identity != route.get("entity_identity"):
        return False
    if route.get("vehicle_id") not in (None, vid):
        return False
    session_anchor = route.get("session_anchor")
    if session_anchor is not None:
        if not isinstance(session_anchor, Mapping):
            return False
        anchor_entity = session_anchor.get("entity_id")
        anchor_identity = session_anchor.get("entity_identity")
        current_anchor = _identity(hass, str(anchor_entity or ""))
        if (current_anchor is None or current_anchor != anchor_identity
                or not isinstance(anchor_identity, Mapping)):
            return False
        # Start, Rate, Limit, and Stop use different HA entities on the same
        # native provider.  Carrying a session is safe only across the exact
        # provider config/device; unique IDs are entity-specific.
        if any(identity.get(field) != anchor_identity.get(field)
               for field in ("platform", "config_entry_id", "device_id")):
            return False
    expected = _pairing_digest(entry, route, identity=identity)
    if route.get("pairing_digest") != expected:
        return False
    # A native route retained for a charging session is valid only while the
    # same ownership lease still exists. Check this after the exact provider,
    # config-entry, device, and unique-id binding above.
    scope = route.get("session_scope")
    if scope is None:
        return True
    return isinstance(scope, Mapping) and dict(scope) == _session_scope(
        _owner_snapshot(hass, entry, vid)
    )


def _record_compatible(hass: Any, entry: Any, runtime: dict[str, Any], vid: str,
                       record: Mapping[str, Any]) -> bool:
    return _compatible(hass, entry, runtime, vid, record.get("route", {}))


def _restore_recovery(runtime: dict[str, Any], before: Any) -> None:
    if before is _MISSING:
        runtime.pop(RECOVERY_KEY, None)
    else:
        runtime[RECOVERY_KEY] = before


def _snapshot_recovery(runtime: dict[str, Any]) -> Any:
    value = runtime.get(RECOVERY_KEY, _MISSING)
    return _MISSING if value is _MISSING else deepcopy(value)


def _terminal_removal_proof(
    hass: Any,
    entry: Any,
    runtime: dict[str, Any],
    ticket: RecoveryTicket,
    record: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any] | None:
    """Build a finish proof only for the current exact attempt generation."""
    if outcome not in _TERMINAL_ATTEMPT_OUTCOMES:
        return None
    if ticket.runtime_ref is not None and runtime is not ticket.runtime_ref:
        return None
    if (record.get("attempt_id") != ticket.attempt_id
            or record.get("vehicle_id") != ticket.vehicle_id
            or not isinstance(record.get("route"), Mapping)
            or dict(record.get("route", {})) != dict(ticket.route)
            or record.get("outcome") == "sent_unknown"):
        return None
    # The ticket may be retained across an ownership/configuration change even
    # while the runtime object remains alive.  Do not turn that stale ticket
    # into durable evidence that an old physical attempt was terminal.
    if (record.get("runtime_digest") != _runtime_digest(runtime, entry)
            or record.get("setup_digest") != _setup_digest(runtime, entry)):
        return None
    identity = record.get("route", {}).get("entity_identity")
    binding_route = dict(record["route"])
    # Finish proof validates the exact physical entity/provider binding, while
    # admission authority belongs to the original ticket.  A known not_sent
    # result may arrive after its ownership lease or deadline has expired; a
    # current owner check here would retain a proven-unsent row indefinitely.
    binding_route.pop("session_scope", None)
    if (record.get("pairing_digest") != _pairing_digest(
            entry, record.get("route"), identity=identity
    ) or not _compatible(hass, entry, runtime, ticket.vehicle_id, binding_route)):
        return None
    return {
        "attempt_id": ticket.attempt_id,
        "vehicle_id": ticket.vehicle_id,
        "operation": str(record.get("operation") or ""),
        "entity_id": str(record.get("entity_id") or ""),
        "outcome": outcome,
        "route": deepcopy(dict(record["route"])),
        "record_digest": _attempt_record_digest(record),
        "runtime_digest": str(record["runtime_digest"]),
        "pairing_digest": str(record["pairing_digest"]),
        "setup_digest": str(record["setup_digest"]),
        "finished_at": time.time(),
    }


def _record_terminal_removal(
    state: dict[str, Any], proof: Mapping[str, Any],
) -> bool:
    # Sidecar state is authoritative when enrolled.  Legacy-only runtimes
    # still carry the same proof in their persisted recovery root so existing
    # installs retain terminal-attempt semantics until the sidecar is created.
    recovery = state.get("recovery") if "recovery" in state else state
    if not isinstance(recovery, dict) or not _terminal_removals_valid(
        recovery.get(_TERMINAL_REMOVALS_KEY)
    ):
        return False
    removals = recovery.setdefault(_TERMINAL_REMOVALS_KEY, {})
    if not isinstance(removals, dict):
        return False
    vehicle_id = proof.get("vehicle_id")
    attempt_id = proof.get("attempt_id")
    if not isinstance(vehicle_id, str) or not isinstance(attempt_id, str):
        return False
    vehicle_removals = removals.setdefault(vehicle_id, {})
    if not isinstance(vehicle_removals, dict):
        return False
    existing = vehicle_removals.get(attempt_id)
    if isinstance(existing, Mapping):
        # The first exact terminal finish is the durable removal authority;
        # never replace it with a later, weaker or contradictory proof.
        return dict(existing) == dict(proof)
    vehicle_removals[attempt_id] = deepcopy(dict(proof))
    return True


async def _restore_after_cancel(
    hass: Any, entry: Any, runtime: dict[str, Any], before: Any,
) -> None:
    """Restore memory and best-effort persist the compensating state."""
    _restore_recovery(runtime, before)
    try:
        await _persist(hass, entry, runtime)
    except BaseException:
        # The caller's CancelledError remains the externally visible result.
        pass


def _isolation_ready_for_route(hass: Any, ledger: Mapping[str, Any],
                               route: Mapping[str, Any]) -> bool:
    prefix = str(route.get("prefix") or "")
    if not prefix:
        return False
    for hazard in _hazards(ledger):
        if (hazard.get("kind") == "ble_quarantine"
                and str(hazard.get("prefix") or "") == prefix):
            return hazard.get("state") == "isolated" and _isolation_valid(hass, hazard)
    return False


def _ha_fallback_allowed(hass: Any, ledger: Mapping[str, Any],
                         route: Mapping[str, Any]) -> bool:
    """Permit a new HA claim only when every old BLE hazard is isolated."""
    if route.get("kind") != "ha":
        return False
    attempts = ledger.get("attempts", {})
    found = False
    for record in attempts.values() if isinstance(attempts, Mapping) else ():
        if (not isinstance(record, Mapping) or record.get("recovery_retired")
                or record.get("outcome", "pending") not in ("pending", "sent_unknown")):
            continue
        old_route = record.get("route", {})
        if isinstance(old_route, Mapping) and old_route.get("kind") == "ble":
            found = True
            if not _isolation_ready_for_route(hass, ledger, old_route):
                return False
    for hazard in _hazards(ledger):
        if (hazard.get("kind") == "ble_quarantine"
                and not hazard.get("recovery_retired")
                and hazard.get("state") == "pending"):
            found = True
            if hazard.get("state") != "isolated" or not _isolation_valid(hass, hazard):
                return False
    return found


async def async_begin_attempt(
    hass: Any, entry: Any, vehicle_id: Any, route: Mapping[str, Any], operation: str,
    entity_id: str, *, target: Any = None, deadline: Any = None,
    allow_persist_refused_cleanup: bool = False,
    cleanup_obligation_token: str | None = None,
) -> RecoveryTicket | None:
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    if runtime is None or not vid or not isinstance(route, Mapping) or not isinstance(operation, str) or not operation:
        return None
    if runtime.get(_RECOVERY_STOPPING_KEY) is True:
        return None
    operation_name = operation.strip().lower()
    route_for_pin = dict(route)
    # Only a native Start can establish a new session route. A standalone
    # native Rate/Limit command must remain ephemeral for the next session.
    if route_for_pin.get("kind") == "ha" and operation_name == "start":
        scope = _session_scope(_owner_snapshot(hass, entry, vid))
        if scope is not None:
            route_for_pin["session_scope"] = scope
    async with _journal_lock(runtime):
        before = _snapshot_recovery(runtime)
        ledger = _ledger(runtime, vid, create=True)
        if ledger is None:
            return None
        # Carry an existing native session pin into Rate/Limit/Stop attempts
        # across the provider's operation-specific entities. The exact
        # provider config/device and the original Start entity are retained as
        # a separate session anchor; a same-VIN entity from another provider
        # or device cannot inherit the session.
        existing_route = ledger.get("route")
        if (isinstance(existing_route, Mapping)
                and existing_route.get("cleanup_only") is True
                and operation_name != "stop"):
            # A cleanup handoff is deliberately narrower than a resumed
            # charging session.  Do this before any operation-specific entity
            # pinning so Rate, Limit, Start, and alternate-provider routes
            # cannot escape the persisted stop-only fence.
            return None
        if (
            route_for_pin.get("kind") == "ha"
            and isinstance(existing_route, Mapping)
            and existing_route.get("kind") == "ha"
            and isinstance(existing_route.get("session_scope"), Mapping)
            and "session_scope" not in route_for_pin
        ):
            old_identity = existing_route.get("entity_identity")
            new_identity = _identity(hass, entity_id)
            anchor = existing_route.get("session_anchor")
            if not isinstance(anchor, Mapping):
                anchor = {
                    "entity_id": existing_route.get("entity_id"),
                    "entity_identity": old_identity,
                }
            anchor_identity = anchor.get("entity_identity")
            old_anchor_current = _identity(
                hass, str(anchor.get("entity_id") or "")
            )
            same_provider_device = (
                isinstance(old_identity, Mapping)
                and isinstance(new_identity, Mapping)
                and all(old_identity.get(field) == new_identity.get(field)
                        for field in ("platform", "config_entry_id", "device_id"))
                and isinstance(anchor_identity, Mapping)
                and isinstance(old_anchor_current, Mapping)
                and dict(old_anchor_current) == dict(anchor_identity)
                and existing_route.get("vehicle_id") in (None, vid)
            )
            binding_route = dict(existing_route)
            if operation_name == "stop":
                # A revoked/replaced lease must not strand an exact-producer
                # compensating Stop. Keep its original session pin and every
                # physical binding check; only positive work needs that lease
                # to remain current. The caller owns Stop admission authority.
                binding_route.pop("session_scope", None)
            if same_provider_device and _compatible(
                hass, entry, runtime, vid, binding_route
            ):
                route_for_pin["session_scope"] = deepcopy(
                    existing_route["session_scope"]
                )
                route_for_pin["session_anchor"] = deepcopy(anchor)
                if existing_route.get("cleanup_only") is True:
                    route_for_pin["cleanup_only"] = True
            elif route_for_pin.get("kind") == "ha":
                # An active native session cannot be adopted by a different
                # provider/config/device, even when it exposes the same VIN.
                return None
        if (
            route_for_pin.get("kind") == "ha"
            and operation_name == "start"
            and isinstance(route_for_pin.get("session_scope"), Mapping)
            and "session_anchor" not in route_for_pin
        ):
            start_identity = _identity(hass, entity_id)
            if start_identity is not None:
                route_for_pin["session_anchor"] = {
                    "entity_id": entity_id,
                    "entity_identity": deepcopy(start_identity),
                }
        pinned = _pin(hass, entry, runtime, vid, route_for_pin, entity_id)
        if pinned is None:
            return None
        cleanup_scope = (
            operation_name == "stop"
            and allow_persist_refused_cleanup
            and _cleanup_scope_matches(
                runtime, vid, pinned, cleanup_obligation_token,
            )
        )
        unresolved = _unresolved_routes(ledger)
        # A prior unknown Start must never veto a compensating Stop.  The
        # caller still supplies the current route and performs its own
        # provider/authority checks; this journal only records the intent.
        if unresolved and operation_name != "stop":
            # BLE can move to the native HA route only after its radio has
            # been independently isolated.  Teslemetry command-only routes
            # have no equivalent producer fence, so physical readback alone
            # never permits a new conflicting command or cloud fallback.
            if (any(route.get("kind") != "ble" for route in unresolved)
                    or not _ha_fallback_allowed(hass, ledger, pinned)):
                return None
        attempt_id = uuid4().hex
        record = {
            "attempt_id": attempt_id, "vehicle_id": vid, "operation": operation,
            "entity_id": entity_id, "target": _target(target), "route": pinned,
            "outcome": "pending", "submitted_epoch": time.time(),
            "original_deadline": _epoch(deadline), "owner_snapshot": pinned["owner_snapshot"],
            "runtime_digest": pinned["runtime_digest"], "pairing_digest": pinned["pairing_digest"],
            "setup_digest": pinned["setup_digest"],
        }
        ledger["route"] = pinned
        ledger["attempts"][attempt_id] = record
        try:
            if not await _persist(hass, entry, runtime):
                _restore_recovery(runtime, before)
                cleanup_claim = None
                if (
                    cleanup_scope
                    and _cleanup_binding_current(hass, entry, runtime, vid, pinned)
                ):
                    cleanup_claim = _claim_best_effort_cleanup(
                        runtime, vid, pinned, cleanup_obligation_token,
                    )
                if cleanup_claim is not None:
                    scope_key, claim_id = cleanup_claim
                    refusal_route = deepcopy(pinned)
                    # These markers are added only after the durable cleanup
                    # scope and current binding have both been revalidated.
                    # The caller may therefore hand this typed refusal to the
                    # shared in-memory tracker, but cannot manufacture one
                    # from an ordinary persistence failure.
                    refusal_route["cleanup_scope_valid"] = True
                    refusal_route["cleanup_obligation_id"] = cleanup_obligation_token
                    refusal_route["_cleanup_scope_key"] = scope_key
                    refusal_route["_cleanup_claim_id"] = claim_id
                    raise RecoveryPersistenceRefused(
                        "EV cleanup Stop journal persistence was refused",
                        vehicle_id=vid,
                        route=refusal_route,
                    )
                return None
        except asyncio.CancelledError:
            await _restore_after_cancel(hass, entry, runtime, before)
            raise
        return RecoveryTicket(hass, entry, vid, attempt_id, deepcopy(pinned), runtime)


async def async_finish_attempt(ticket: RecoveryTicket, outcome: Any) -> bool:
    runtime = _runtime(ticket.hass, ticket.entry)
    normalized = str(getattr(outcome, "value", outcome) or "").lower().strip()
    if (runtime is None or runtime.get(_RECOVERY_STOPPING_KEY) is True
            or normalized not in OUTCOMES
            or (ticket.runtime_ref is not None and runtime is not ticket.runtime_ref)):
        return False
    async with _journal_lock(runtime):
        ledger = _ledger(runtime, ticket.vehicle_id) if runtime else None
        attempts = ledger.get("attempts", {}) if ledger else {}
        record = attempts.get(ticket.attempt_id) if isinstance(attempts, dict) else None
        if not isinstance(record, dict):
            return False
        before = _snapshot_recovery(runtime)
        if normalized in ("not_sent", "accepted_unconfirmed", "confirmed", "rejected"):
            state = runtime.get(_SIDECAR_STATE_KEY)
            if not isinstance(state, dict):
                state = _recovery(runtime, create=False)
            sidecar_before = (
                deepcopy(state)
                if runtime.get(_SIDECAR_STATE_KEY) is state and isinstance(state, dict)
                else None
            )
            proof = _terminal_removal_proof(
                ticket.hass, ticket.entry, runtime, ticket, record, normalized,
            )
            if not isinstance(state, dict) or proof is None or not _record_terminal_removal(state, proof):
                # A terminal result without a current exact ticket/runtime
                # proof must remain pending so the durable journal cannot lose
                # an unknown physical effect.
                return False
            if sidecar_before is not None:
                runtime[_TERMINAL_SIDECAR_BEFORE_KEY] = sidecar_before
                # The proof was added above; the sidecar writer must roll back
                # to the state that existed before this finish if its Store
                # write is refused.  Keep the marker internal to this lock so
                # monkeypatched legacy persistence remains compatible.
            attempts.pop(ticket.attempt_id, None)
            stop_route_cleared = (
                str(record.get("operation") or "").strip().lower() == "stop"
                and isinstance(record.get("route"), Mapping)
                and record["route"].get("cleanup_only") is True
                and isinstance(ledger.get("route"), Mapping)
                and dict(ledger["route"]) == dict(record["route"])
                and not attempts
                and not ledger.get("hazards")
            )
            session_route_retained = (
                normalized in ("accepted_unconfirmed", "confirmed")
                and str(record.get("operation") or "").strip().lower() != "stop"
                and isinstance(record.get("route"), Mapping)
                and isinstance(record["route"].get("session_scope"), Mapping)
            )
            if (
                record.get("route", {}).get("kind") == "ha"
                and isinstance(ledger.get("route"), Mapping)
                and ledger["route"].get("kind") == "ha"
                and not attempts
                and not ledger.get("hazards")
                and not session_route_retained
            ):
                # A completed native fallback has no outstanding producer to
                # pin. Let the next request try its configured BLE preference.
                # Retain HA routing when an older attempt or isolation hazard
                # still requires that exact producer.
                ledger["route"] = None
            elif stop_route_cleared:
                # A terminal cleanup Stop releases the negative-only route for
                # every exact producer kind. Full route equality and empty
                # liabilities prevent a replacement route or hazard from
                # inheriting the old Stop proof.
                ledger["route"] = None
        else:
            record["outcome"] = normalized
        try:
            if not await _persist(ticket.hass, ticket.entry, runtime):
                _restore_recovery(runtime, before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(ticket.hass, ticket.entry, runtime, before)
            raise
        finally:
            runtime.pop(_TERMINAL_SIDECAR_BEFORE_KEY, None)
        return True


async def async_bind_native_session_route(
    hass: Any,
    entry: Any,
    vehicle_id: Any,
    entity_id: str,
    *,
    expected_identity: Mapping[str, Any] | None = None,
) -> bool:
    """Persist an accepted native Start as the current session producer.

    Dynamic EV ownership is claimed after the physical Start and initial-rate
    steps. This explicit handoff bridges that ordering without allowing a
    standalone native Rate/Limit operation to create a durable route. The
    caller may provide the mutation-time registry identity so a rebind between
    Start and ownership claim cannot pin a different provider device.
    """
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    entity = str(entity_id or "")
    if runtime is None or not vid or not entity:
        return False
    scope = _session_scope(_owner_snapshot(hass, entry, vid))
    if scope is None:
        return False
    current_identity = _identity(hass, entity)
    if current_identity is None:
        return False
    if isinstance(expected_identity, Mapping):
        if any(current_identity.get(key) != expected_identity.get(key) for key in (
            "entity_id", "platform", "config_entry_id", "device_id", "unique_id",
        )):
            return False
    route = {"kind": "ha", "entity_id": entity, "session_scope": scope}
    async with _journal_lock(runtime):
        before = _snapshot_recovery(runtime)
        ledger = _ledger(runtime, vid, create=True)
        if ledger is None:
            return False
        unresolved = _unresolved_routes(ledger)
        if unresolved and (
            any(item.get("kind") != "ble" for item in unresolved)
            or not _ha_fallback_allowed(hass, ledger, route)
        ):
            return False
        pinned = _pin(hass, entry, runtime, vid, route, entity)
        if pinned is None:
            return False
        ledger["route"] = pinned
        try:
            if not await _persist(hass, entry, runtime):
                _restore_recovery(runtime, before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(hass, entry, runtime, before)
            raise
        return True


async def async_rebind_native_session_route(
    hass: Any,
    entry: Any,
    vehicle_id: Any,
    *,
    entity_id: str | None = None,
    expected_identity: Mapping[str, Any] | None = None,
    mode: str = "positive_resume",
) -> bool:
    """Rebind a persisted native producer to a recovered ownership lease.

    A native route is scoped to the ownership lease that created it.  Runtime
    restore creates a new lease, so the old route cannot be used for a resumed
    Rate/Limit or a cleanup Stop until this handoff has durably established the
    new lease as the owner of the *same* producer.  The caller must supply the
    exact entity identity captured with the recovered obligation when it has
    one; omitting it still requires the persisted route identity to be current.

    ``positive_resume`` refuses every unresolved transport attempt.  It is the
    only mode that can authorize subsequent positive mutations.  The
    ``cleanup_stop`` mode may rebind while an older attempt remains unknown,
    but it never removes or rewrites that hazard.  Neither mode creates a new
    attempt or changes the vehicle VIN binding.
    """
    if mode not in {"positive_resume", "cleanup_stop"}:
        return False
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    if runtime is None or not vid:
        return False

    current_scope = _session_scope(_owner_snapshot(hass, entry, vid))
    # A handoff without a current ownership lease would turn old producer
    # evidence into a general native fallback.  Cleanup callers must establish
    # their own stop authority before requesting this route repair.
    if current_scope is None:
        return False

    async with _journal_lock(runtime):
        before = _snapshot_recovery(runtime)
        ledger = _ledger(runtime, vid, create=False)
        if ledger is None or not isinstance(ledger.get("route"), Mapping):
            return False
        old_route = ledger["route"]
        if old_route.get("kind") != "ha":
            return False
        if old_route.get("vehicle_id") != vid:
            return False
        old_entity = str(old_route.get("entity_id") or "")
        entity = old_entity if entity_id is None else str(entity_id or "")
        if not entity or entity != old_entity:
            return False

        old_identity = old_route.get("entity_identity")
        current_identity = _identity(hass, entity)
        if current_identity is None or not isinstance(old_identity, Mapping):
            return False

        # Require every producer registry field.  ``disabled_by`` was added to
        # newer persisted routes; an omitted value in a caller-supplied
        # expected identity means "enabled", never a wildcard.
        identity_fields = (
            "entity_id", "platform", "config_entry_id", "device_id", "unique_id",
        )
        if any(old_identity.get(key) != current_identity.get(key)
               for key in identity_fields):
            return False
        if old_identity.get("disabled_by") is not None:
            return False
        if isinstance(expected_identity, Mapping):
            if any(expected_identity.get(key) != current_identity.get(key)
                   for key in identity_fields):
                return False
            if expected_identity.get("disabled_by") is not None:
                return False

        # Validate the persisted producer binding while ignoring only its old
        # lease scope.  This catches a provider/config/device rebind and a
        # changed pairing before the new ownership lease is written.
        binding_route = deepcopy(old_route)
        binding_route.pop("session_scope", None)
        if not _compatible(hass, entry, runtime, vid, binding_route):
            return False
        if old_route.get("producer_identity") != _producer_identity(
            old_route, current_identity
        ):
            return False

        unresolved = _unresolved_routes(ledger)
        if unresolved and mode == "positive_resume":
            # A native session may continue after a prior BLE producer has
            # been independently fenced.  The existing handoff rule permits
            # that exact HA producer only when every unresolved item is BLE
            # and the fresh bridge isolation proof still validates.  Any
            # unresolved native/other-provider attempt remains a hard stop.
            if (
                any(item.get("kind") != "ble" for item in unresolved)
                or not _ha_fallback_allowed(hass, ledger, old_route)
            ):
                return False

        # Keep the exact producer anchor and VIN.  Only the ownership lease
        # changes; _pin refreshes runtime diagnostics and recomputes the same
        # pairing digest after the binding checks above.
        rebound = deepcopy(old_route)
        rebound["session_scope"] = deepcopy(current_scope)
        rebound["owner_snapshot"] = _owner_snapshot(hass, entry, vid)
        if mode == "cleanup_stop":
            rebound["cleanup_only"] = True
        else:
            # A fully validated positive resume replaces, rather than
            # inherits, a prior cleanup-only handoff.  In practice this mode
            # only succeeds once no unresolved hazard remains.
            rebound.pop("cleanup_only", None)
        if not isinstance(rebound.get("session_anchor"), Mapping):
            rebound["session_anchor"] = {
                "entity_id": old_entity,
                "entity_identity": deepcopy(old_identity),
            }
        pinned = _pin(hass, entry, runtime, vid, rebound, entity)
        if pinned is None:
            return False
        ledger["route"] = pinned
        try:
            if not await _persist(hass, entry, runtime):
                _restore_recovery(runtime, before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(hass, entry, runtime, before)
            raise

        # Persistence yields to HA.  Recheck both sides of the binding before
        # the caller is allowed to dispatch a mutation; a concurrent lease or
        # registry replacement invalidates the handoff and is rolled back.
        post_identity = _identity(hass, entity)
        post_scope = _session_scope(_owner_snapshot(hass, entry, vid))
        post_route = ledger.get("route")
        post_compatible = (
            isinstance(post_route, Mapping)
            and _compatible(hass, entry, runtime, vid, post_route)
        )
        if (
            post_identity != current_identity
            or post_scope != current_scope
            or not post_compatible
            or bool(post_route.get("cleanup_only", False))
            != (mode == "cleanup_stop")
        ):
            _restore_recovery(runtime, before)
            try:
                await _persist(hass, entry, runtime)
            except asyncio.CancelledError:
                await _restore_after_cancel(hass, entry, runtime, before)
                raise
            return False
        return True


def _ble_binding(hass: Any, prefix: str, charger: str) -> dict[str, Any] | None:
    ids = (charger, f"switch.{prefix}_ble_connection", f"binary_sensor.{prefix}_ble_status")
    entities = [_identity(hass, item) for item in ids]
    if any(item is None or item.get("disabled_by") is not None for item in entities):
        return None
    keys = {(item["platform"], item["config_entry_id"], item["device_id"]) for item in entities}
    if len(keys) != 1 or next(iter(keys))[0] != "esphome":
        return None
    binding = {"entities": dict(zip(("charger", "control", "status"), entities))}
    # The legacy BLE status is commonly advertisement presence. It cannot
    # establish GATT disconnection or prove that a producer drained its queues.
    # Only a bridge implementing the explicit transport-fence contract can
    # authorize automatic handoff after an uncertain mutation.
    fence = _identity(hass, f"sensor.{prefix}_transport_fence")
    if (fence is not None and fence.get("disabled_by") is None
            and (fence["platform"], fence["config_entry_id"], fence["device_id"]) in keys):
        binding["entities"]["fence"] = fence
    else:
        binding["entities"]["fence"] = None
    request = _identity(hass, f"text.{prefix}_transport_isolation_request")
    if (request is not None and request.get("disabled_by") is None
            and (request["platform"], request["config_entry_id"], request["device_id"]) in keys):
        binding["entities"]["request"] = request
    else:
        binding["entities"]["request"] = None
    return binding


def _observed(hass: Any, state: Any) -> float | None:
    try:
        from .tesla_ble import get_tesla_ble_observed_at

        value = get_tesla_ble_observed_at(hass, state)
        return value.timestamp() if value is not None else None
    except Exception:
        return None


def _valid_fence_request(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 32
            and all(char in "0123456789abcdef" for char in value))


def _valid_fence_value(value: Any, *, isolated: bool = False) -> bool:
    if (not isinstance(value, Mapping) or type(value.get("protocol")) is not int
            or value["protocol"] != 1
            or not isinstance(value.get("boot_id"), str)
            or not 1 <= len(value["boot_id"]) <= 128
            or not isinstance(value.get("request_id"), str) or len(value["request_id"]) > 128
            or type(value.get("generation")) is not int or value["generation"] < 0
            or any(type(value.get(key)) is not bool for key in ("radio_enabled", "connected", "producer_stopped"))
            or any(type(value.get(key)) is not int or value[key] < 0
                   for key in ("queued_commands", "queued_reads", "queued_writes"))):
        return False
    return (not isolated or (_valid_fence_request(value["request_id"])
        and not value["radio_enabled"] and not value["connected"]
        and value["producer_stopped"]
        and all(value[key] == 0 for key in ("queued_commands", "queued_reads", "queued_writes"))))


def _fence_stamp(value: Any) -> float | None:
    return (float(value) if type(value) in (int, float) and math.isfinite(value)
            and 0 < value <= time.time() else None)


def _fence_receipt(hass: Any, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the versioned producer contract, never a proximity sensor."""
    fence = binding.get("entities", {}).get("fence")
    if not isinstance(fence, Mapping):
        return None
    try:
        state = hass.states.get(fence["entity_id"])
        value = json.loads(state.state)
        observed = _observed(hass, state)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if not _valid_fence_value(value) or _fence_stamp(observed) is None:
        return None
    return {**value, "observed_at": observed}


def _off_samples(hass: Any, binding: Mapping[str, Any], after: float = 0) -> dict[str, Any] | None:
    entity = binding["entities"]["control"]
    try:
        state = hass.states.get(entity["entity_id"])
    except Exception:
        return None
    stamp = _observed(hass, state) if state is not None else None
    if (state is None or str(getattr(state, "state", "")).lower() != "off"
            or _fence_stamp(stamp) is None):
        return None
    proof = _fence_receipt(hass, binding)
    if (proof is None or proof["observed_at"] <= after
            or proof["radio_enabled"] or proof["connected"] or not proof["producer_stopped"]
            or any(proof[key] != 0 for key in ("queued_commands", "queued_reads", "queued_writes"))):
        return None
    return {"control": stamp, "status": proof["observed_at"], "fence": proof}


def _hazards(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    hazards = ledger.get("hazards", {})
    return [item for item in hazards.values() if isinstance(item, Mapping)] if isinstance(hazards, Mapping) else []


def _blocked(vid: str, route: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    return {"kind": "blocked", "vehicle_id": vid, "reason": reason,
            "prefix": route.get("prefix") if isinstance(route, Mapping) else None,
            "entity_id": route.get("entity_id") if isinstance(route, Mapping) else None,
            "entity_identity": route.get("entity_identity") if isinstance(route, Mapping) else None}


def _isolation_valid(hass: Any, hazard: Mapping[str, Any]) -> bool:
    binding = hazard.get("binding")
    if not isinstance(binding, Mapping):
        return False
    entities = binding.get("entities", {})
    charger = entities.get("charger", {}) if isinstance(entities, Mapping) else {}
    prefix = str(hazard.get("prefix") or "")
    if not prefix or _ble_binding(hass, prefix, str(charger.get("entity_id") or "")) != binding:
        return False
    control_off_at = _fence_stamp(hazard.get("control_off_at"))
    status_off_at = _fence_stamp(hazard.get("status_off_at"))
    requested = _fence_stamp(hazard.get("requested_epoch"))
    proof = hazard.get("fence_proof")
    baseline = hazard.get("fence_baseline")
    request_id = hazard.get("request_id")
    if (control_off_at is None or status_off_at is None or requested is None
            or not _valid_fence_request(request_id)
            or not _valid_fence_value(proof, isolated=True)
            or not _valid_fence_value(baseline)
            or _fence_stamp(proof.get("observed_at")) != status_off_at
            or (baseline_stamp := _fence_stamp(baseline.get("observed_at"))) is None
            or not baseline_stamp <= requested < status_off_at
            or proof["boot_id"] != baseline["boot_id"]
            or proof["generation"] <= baseline["generation"]
            or proof["request_id"] != request_id):
        return False
    samples = _off_samples(hass, binding)
    if samples is None:
        return False
    return (samples["control"] >= control_off_at
            and samples["status"] >= status_off_at
            and samples["fence"]["boot_id"] == proof.get("boot_id")
            and samples["fence"]["generation"] == proof.get("generation")
            and samples["fence"]["request_id"] == request_id)


def _unresolved_routes(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return producer routes whose command effect remains unresolved."""
    attempts = ledger.get("attempts", {})
    routes = []
    for record in attempts.values() if isinstance(attempts, Mapping) else ():
        route = record.get("route", {}) if isinstance(record, Mapping) else {}
        if (isinstance(route, Mapping) and route.get("kind") in ROUTE_KINDS
                and not record.get("recovery_retired")
                and record.get("outcome", "pending") in ("pending", "sent_unknown")):
            routes.append(route)
    for hazard in _hazards(ledger):
        if (hazard.get("kind") == "ble_quarantine"
                and not hazard.get("recovery_retired")
                and hazard.get("state") == "pending"):
            routes.append(hazard)
    return routes


def _unresolved_ble(ledger: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for route in _unresolved_routes(ledger):
        if route.get("kind") == "ble":
            return route
    return None


def _unresolved_route_reason(routes: list[Mapping[str, Any]]) -> str:
    kinds = {route.get("kind") for route in routes}
    if kinds == {"ha"}:
        return "native_recovery"
    return "ble_recovery" if kinds == {"ble"} else "transport_recovery"


def ev_recovery_status(hass: Any, entry: Any, vehicle_id: Any) -> dict[str, Any]:
    """Inspect one VIN without migrating, refreshing or changing its history."""
    if not isinstance(vehicle_id, str) or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vehicle_id):
        raise ValueError("invalid_vehicle_vin")
    runtime = _runtime(hass, entry)
    if runtime is None:
        raise ValueError("control_entry_not_available")
    available = RECOVERY_KEY in runtime or LEGACY_RECOVERY_KEY in runtime
    malformed = _journal_malformed(runtime, vehicle_id)
    root = runtime.get(RECOVERY_KEY, runtime.get(LEGACY_RECOVERY_KEY, {}))
    vehicles = root.get("vehicles", {}) if isinstance(root, Mapping) else {}
    ledger = vehicles.get(vehicle_id, {}) if isinstance(vehicles, Mapping) else {}
    rows = []
    if isinstance(ledger, Mapping) and not malformed:
        for attempt_id, record in ledger.get("attempts", {}).items():
            if record.get("recovery_retired") or record.get("outcome", "pending") not in {"pending", "sent_unknown"}:
                continue
            route = record["route"]
            entity_id = str(record.get("entity_id") or "")
            expected = route.get("entity_identity")
            current = _identity(hass, entity_id) if route.get("kind") == "ha" else None
            registry = _registry(hass) if route.get("kind") == "ha" else None
            registration = registry.async_get(entity_id) if registry is not None else None
            rows.append({
                "attempt_id": attempt_id, "operation": record.get("operation"),
                "outcome": record.get("outcome", "pending"), "transport": route.get("kind"),
                "submitted_epoch": record.get("submitted_epoch"),
                "original_deadline": record.get("original_deadline"),
                "target": deepcopy(record.get("target")),
                "command_request": deepcopy(route.get("command_request")),
                "effect_bound": deepcopy(route.get("effect_bound")),
                "producer_identity": deepcopy(expected),
                "current_producer_identity": deepcopy(current),
                "producer_state": (
                    "not_native" if route.get("kind") != "ha" else
                    "missing" if registration is None else
                    "disabled" if getattr(registration, "disabled_by", None) is not None else
                    "current" if current == expected else "identity_changed"
                ),
                "producer_identity_matches": (
                    current == expected if isinstance(expected, Mapping) and current is not None else None
                ),
            })
    routes = _unresolved_routes(ledger) if isinstance(ledger, Mapping) and not malformed else []
    state = runtime.get(_SIDECAR_STATE_KEY)
    config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    reason = (
        "recovery_history_unavailable" if not available else
        "malformed_recovery" if malformed else
        _unresolved_route_reason(routes) if routes else "no_retained_history"
    )
    return {
        "success": available and not malformed, "controls_issued": False,
        "entry_id": entry.entry_id, "vehicle_id": vehicle_id,
        "configured_provider": config.get("ev_provider", "fleet_api"),
        "generation": current_vehicle_generation(hass, entry, vehicle_id),
        "journal_revision": state.get("revision") if isinstance(state, Mapping) else None,
        "retained_attempts": rows, "reason": reason,
        "recovery_required": malformed or bool(routes) if available else None,
        "native_retirement_available": False,
        "missing_evidence": (
            "loaded_recovery_history" if not available else
            "valid_retained_history" if malformed else
            "original_attempt_terminal_or_no_late_effect_proof" if routes else None
        ),
    }


def get_vehicle_route(hass: Any, entry: Any, vehicle_id: Any) -> dict[str, Any] | None:
    vid = str(vehicle_id or "")
    runtime = _runtime(hass, entry)
    if runtime is None:
        return None
    if _journal_malformed(runtime, vid):
        return _blocked(vid, None, "malformed_recovery")
    ledger = _ledger(runtime, vid) if runtime else None
    if ledger is None:
        return None
    route = ledger.get("route")
    if not isinstance(route, Mapping):
        unresolved = _unresolved_routes(ledger)
        return _blocked(vid, unresolved[0], _unresolved_route_reason(unresolved)) if unresolved else None
    if route.get("kind") not in ROUTE_KINDS:
        return _blocked(vid, route, "malformed_recovery")
    for hazard in _hazards(ledger):
        if hazard.get("kind") != "ble_quarantine":
            continue
        if hazard.get("recovery_retired"):
            continue
        if hazard.get("state") == "pending" or (
                hazard.get("state") == "isolated" and not _isolation_valid(hass, hazard)):
            return _blocked(vid, route, "ble_recovery")
        if hazard.get("state") not in ("pending", "isolated"):
            return _blocked(vid, route, "malformed_recovery")
        if hazard.get("state") == "isolated" and route.get("kind") == "ble":
            return _blocked(vid, route, "ble_recovery")
    unresolved = _unresolved_routes(ledger)
    if unresolved:
        if (route.get("kind") != "ha"
                or any(item.get("kind") != "ble" for item in unresolved)
                or not _ha_fallback_allowed(hass, ledger, route)):
            return _blocked(vid, unresolved[0], _unresolved_route_reason(unresolved))
    if not _compatible(hass, entry, runtime, vid, route):
        return _blocked(vid, route, "route_identity_changed")
    return deepcopy(dict(route))


def pending_ble_attempt(hass: Any, entry: Any, vehicle_id: Any) -> RecoveryTicket | None:
    vid = str(vehicle_id or "")
    runtime = _runtime(hass, entry)
    if runtime is None or _journal_malformed(runtime, vid):
        return None
    ledger = _ledger(runtime, vid) if runtime else None
    if ledger is None:
        return None
    attempts = ledger.get("attempts", {})
    records = [record for record in attempts.values() if isinstance(record, Mapping)
               and not record.get("recovery_retired")
               and record.get("outcome", "pending") in ("pending", "sent_unknown")
               and record.get("route", {}).get("kind") == "ble"] if isinstance(attempts, Mapping) else []
    for record in sorted(records, key=lambda item: _epoch(item.get("submitted_epoch")) or 0, reverse=True):
        route = record.get("route", {})
        prefix = str(route.get("prefix") or "")
        binding = _ble_binding(hass, prefix, str(route.get("entity_id") or "")) if prefix else None
        if (_record_compatible(hass, entry, runtime, vid, record) and binding is not None
                and binding["entities"]["charger"] == route.get("entity_identity")):
            return RecoveryTicket(
                hass, entry, vid, str(record["attempt_id"]), deepcopy(dict(route)), runtime
            )
    return None


def ble_quarantined(hass: Any, prefix: str) -> bool:
    data = getattr(hass, "data", {}).get(DOMAIN, {})
    if not isinstance(data, Mapping):
        return True
    for runtime in data.values():
        if not isinstance(runtime, Mapping):
            continue
        if RECOVERY_KEY not in runtime and LEGACY_RECOVERY_KEY not in runtime:
            continue
        if _journal_malformed(runtime):
            return True
        recovery = runtime.get(RECOVERY_KEY) if isinstance(runtime, Mapping) else None
        if not isinstance(recovery, Mapping) and isinstance(runtime, Mapping):
            recovery = runtime.get(LEGACY_RECOVERY_KEY)
        vehicles = recovery.get("vehicles", {}) if isinstance(recovery, Mapping) else {}
        for ledger in vehicles.values() if isinstance(vehicles, Mapping) else ():
            if not isinstance(ledger, Mapping):
                return True
            attempts = ledger.get("attempts", {})
            for record in attempts.values() if isinstance(attempts, Mapping) else ():
                route = record.get("route", {}) if isinstance(record, Mapping) else {}
                if (route.get("kind") == "ble" and not record.get("recovery_retired")
                        and str(route.get("prefix")) == str(prefix)
                        and record.get("outcome", "pending") in ("pending", "sent_unknown")):
                    return True
            for hazard in _hazards(ledger):
                if (hazard.get("kind") == "ble_quarantine" and not hazard.get("recovery_retired")
                        and str(hazard.get("prefix")) == str(prefix)
                        and hazard.get("state") in ("pending", "isolated")):
                    return True
    return False


async def async_isolate_ble(ticket: RecoveryTicket, *, authorize: Any) -> bool:
    runtime = _runtime(ticket.hass, ticket.entry)
    if (runtime is None
            or (ticket.runtime_ref is not None and runtime is not ticket.runtime_ref)):
        return False

    async with _journal_lock(runtime):
        ledger = _ledger(runtime, ticket.vehicle_id) if runtime else None
        record = ledger.get("attempts", {}).get(ticket.attempt_id) if ledger else None
        route = record.get("route") if isinstance(record, Mapping) else None
        if (ledger is None or not isinstance(record, Mapping)
                or not isinstance(route, Mapping) or route.get("kind") != "ble"
                or record.get("outcome", "pending") not in ("pending", "sent_unknown")
                or not _record_compatible(ticket.hass, ticket.entry, runtime,
                                           ticket.vehicle_id, record)):
            return False
        prefix = str(route.get("prefix") or "")
        binding = _ble_binding(ticket.hass, prefix, str(route.get("entity_id") or ""))
        if (not prefix or binding is None
                or binding["entities"]["charger"] != route.get("entity_identity")):
            return False
        hazards = ledger["hazards"]
        key = f"ble:{prefix}"
        existing = hazards.get(key)
        if (isinstance(existing, Mapping) and existing.get("state") == "isolated"
                and _isolation_valid(ticket.hass, existing)):
            return True
        before = _snapshot_recovery(runtime)
        fence_baseline = _fence_receipt(ticket.hass, binding)
        request_id = uuid4().hex
        hazards[key] = {
            "kind": "ble_quarantine", "state": "pending", "prefix": prefix,
            "attempt_id": ticket.attempt_id, "binding": deepcopy(binding),
            "producer_identity": route.get("producer_identity"),
            "requested_epoch": time.time(),
            "fence_baseline": deepcopy(fence_baseline),
            "request_id": request_id,
        }
        try:
            if not await _persist(ticket.hass, ticket.entry, runtime):
                _restore_recovery(runtime, before)
                return False
        except asyncio.CancelledError:
            # Keep the durable pending hazard if the initial save committed;
            # cancellation must not permit a later Start on this BLE path.
            try:
                await _persist(ticket.hass, ticket.entry, runtime)
            except BaseException:
                pass
            raise
        pending = _snapshot_recovery(runtime)

    try:
        if not await _gate(authorize) or _ble_binding(ticket.hass, prefix, route["entity_id"]) != binding:
            return False
        started = time.time()
        await ticket.hass.services.async_call(
            "switch", "turn_off", {"entity_id": binding["entities"]["control"]["entity_id"]}, blocking=True
        )
        request_binding = binding["entities"].get("request")
        if fence_baseline is None or not isinstance(request_binding, Mapping):
            # Keep containment and the journal, but a legacy bridge provides
            # no queue/GATT proof. Its presence sensor must never permit handoff.
            return False
        # An echoed nonce correlates proof to this disable. HA receipt time
        # alone cannot distinguish a delayed earlier fence notification.
        if not await _gate(authorize) or _ble_binding(ticket.hass, prefix, route["entity_id"]) != binding:
            return False
        request_started = time.time()
        await ticket.hass.services.async_call(
            "text", "set_value", {"entity_id": request_binding["entity_id"], "value": request_id}, blocking=True
        )
        deadline = time.monotonic() + BLE_ISOLATION_TIMEOUT
        samples = None
        while time.monotonic() < deadline:
            samples = _off_samples(ticket.hass, binding, started)
            if (samples is not None
                    and samples["fence"]["boot_id"] == fence_baseline["boot_id"]
                    and samples["fence"]["generation"] > fence_baseline["generation"]
                    and samples["fence"]["request_id"] == request_id
                    and samples["fence"]["observed_at"] > request_started):
                break
            samples = None
            await asyncio.sleep(0.1)
        if samples is None or not await _gate(authorize) or _ble_binding(ticket.hass, prefix, route["entity_id"]) != binding:
            return False
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    async with _journal_lock(runtime):
        # A reload or setup replacement during provider I/O invalidates this
        # in-flight generation.  Leave the old pending hazard for cleanup.
        if _runtime(ticket.hass, ticket.entry) is not runtime:
            return False
        ledger = _ledger(runtime, ticket.vehicle_id)
        if ledger is None:
            return False
        current = ledger.get("attempts", {}).get(ticket.attempt_id)
        current_route = current.get("route") if isinstance(current, Mapping) else None
        if (not isinstance(current, Mapping) or current_route != route
                or _ble_binding(ticket.hass, prefix, route["entity_id"]) != binding):
            return False
        current_samples = _off_samples(ticket.hass, binding, started)
        if (current_samples is None
                or current_samples["fence"]["boot_id"] != samples["fence"]["boot_id"]
                or current_samples["fence"]["generation"] != samples["fence"]["generation"]):
            return False
        samples = current_samples
        fallback = route.get("fallback")
        if (not isinstance(fallback, Mapping)
                or not _compatible(ticket.hass, ticket.entry, runtime, ticket.vehicle_id, fallback)):
            return False
        pinned = _pin(ticket.hass, ticket.entry, runtime, ticket.vehicle_id, fallback,
                      str(fallback.get("entity_id") or ""))
        if pinned is None:
            return False
        hazards = ledger["hazards"]
        hazard = hazards.get(key)
        if not isinstance(hazard, dict):
            return False
        final_before = _snapshot_recovery(runtime)
        hazard.update({
            "state": "isolated", "binding": deepcopy(binding),
            "control_off_at": samples["control"], "status_off_at": samples["status"],
            "fence_proof": deepcopy(samples["fence"]),
        })
        ledger["route"] = pinned
        try:
            if not await _persist(ticket.hass, ticket.entry, runtime):
                _restore_recovery(runtime, final_before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(ticket.hass, ticket.entry, runtime, pending)
            raise
        # Saving and authority checks yield to provider/registry updates. A
        # durable record never substitutes for live queue/connection proof.
        if not await _gate(authorize):
            return False
        return (_runtime(ticket.hass, ticket.entry) is runtime
                and _ble_binding(ticket.hass, prefix, route["entity_id"]) == binding
                and _isolation_valid(ticket.hass, hazard))


async def _gate(gate: Any) -> bool:
    result = gate() if callable(gate) else gate
    if inspect.isawaitable(result):
        result = await result
    return result is True


__all__ = [
    "RecoveryTicket", "RecoverySubmissionDenied", "RecoveryPersistenceRefused",
    "async_begin_attempt", "async_finish_attempt", "pending_ble_attempt",
    "get_vehicle_route", "ble_quarantined", "async_isolate_ble",
    "async_load_recovery_state", "async_shutdown_recovery_state",
    "recovery_sidecar_snapshot", "restore_recovery_projection",
    "async_persist_recovery_state", "async_update_recovery_obligations",
    "async_record_evidence_loss",
    "async_confirm_recovery_scope", "current_vehicle_generation",
    "recovery_control_allowed", "async_advance_recovery_generation",
    "async_track_submission", "async_track_cleanup_submission",
    "async_drain_submissions", "async_recover_stock",
]
