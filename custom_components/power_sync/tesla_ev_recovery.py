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
BLE_ISOLATION_TIMEOUT = 10.0
_MISSING = object()
_JOURNAL_LOCK_KEY = "_ev_transport_recovery_lock"


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


async def _persist(hass: Any, entry: Any, runtime: dict[str, Any]) -> bool:
    try:
        from .automations.ev_ownership import persist_ev_runtime_state

        result = persist_ev_runtime_state(hass, entry, runtime.get("automation_store"))
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
        return result is True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _compatible(hass: Any, entry: Any, runtime: dict[str, Any], vid: str,
                route: Mapping[str, Any]) -> bool:
    if not isinstance(route, Mapping):
        return False
    identity = _identity(hass, str(route.get("entity_id") or ""))
    if identity is None or identity != route.get("entity_identity"):
        return False
    expected = _pairing_digest(entry, route, identity=identity)
    return route.get("pairing_digest") == expected


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
        if not isinstance(record, Mapping) or record.get("outcome", "pending") not in ("pending", "sent_unknown"):
            continue
        old_route = record.get("route", {})
        if isinstance(old_route, Mapping) and old_route.get("kind") == "ble":
            found = True
            if not _isolation_ready_for_route(hass, ledger, old_route):
                return False
    for hazard in _hazards(ledger):
        if hazard.get("kind") == "ble_quarantine" and hazard.get("state") == "pending":
            found = True
            if hazard.get("state") != "isolated" or not _isolation_valid(hass, hazard):
                return False
    return found


async def async_begin_attempt(
    hass: Any, entry: Any, vehicle_id: Any, route: Mapping[str, Any], operation: str,
    entity_id: str, *, target: Any = None, deadline: Any = None,
) -> RecoveryTicket | None:
    runtime = _runtime(hass, entry)
    vid = str(vehicle_id or "")
    if runtime is None or not vid or not isinstance(route, Mapping) or not isinstance(operation, str) or not operation:
        return None
    pinned = _pin(hass, entry, runtime, vid, route, entity_id)
    if pinned is None:
        return None
    async with _journal_lock(runtime):
        before = _snapshot_recovery(runtime)
        ledger = _ledger(runtime, vid, create=True)
        if ledger is None:
            return None
        unresolved = _unresolved_routes(ledger)
        # A prior unknown Start must never veto a compensating Stop.  The
        # caller still supplies the current route and performs its own
        # provider/authority checks; this journal only records the intent.
        if unresolved and operation.strip().lower() != "stop":
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
                return None
        except asyncio.CancelledError:
            await _restore_after_cancel(hass, entry, runtime, before)
            raise
        return RecoveryTicket(hass, entry, vid, attempt_id, deepcopy(pinned), runtime)


async def async_finish_attempt(ticket: RecoveryTicket, outcome: Any) -> bool:
    runtime = _runtime(ticket.hass, ticket.entry)
    normalized = str(getattr(outcome, "value", outcome) or "").lower().strip()
    if (runtime is None or normalized not in OUTCOMES
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
            attempts.pop(ticket.attempt_id, None)
        else:
            record["outcome"] = normalized
        try:
            if not await _persist(ticket.hass, ticket.entry, runtime):
                _restore_recovery(runtime, before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(ticket.hass, ticket.entry, runtime, before)
            raise
        return True


def _ble_binding(hass: Any, prefix: str, charger: str) -> dict[str, Any] | None:
    ids = (charger, f"switch.{prefix}_ble_connection", f"binary_sensor.{prefix}_ble_status")
    entities = [_identity(hass, item) for item in ids]
    if any(item is None or item.get("disabled_by") is not None for item in entities):
        return None
    keys = {(item["platform"], item["config_entry_id"], item["device_id"]) for item in entities}
    if len(keys) != 1 or next(iter(keys))[0] != "esphome":
        return None
    return {"entities": dict(zip(("charger", "control", "status"), entities))}


def _observed(hass: Any, state: Any) -> float | None:
    try:
        from .tesla_ble import get_tesla_ble_observed_at

        value = get_tesla_ble_observed_at(hass, state)
        return value.timestamp() if value is not None else None
    except Exception:
        return None


def _off_samples(hass: Any, binding: Mapping[str, Any], after: float = 0) -> dict[str, float] | None:
    samples = {}
    for name in ("control", "status"):
        entity = binding["entities"][name]
        try:
            state = hass.states.get(entity["entity_id"])
        except Exception:
            state = None
        stamp = _observed(hass, state) if state is not None else None
        if state is None or str(getattr(state, "state", "")).lower() != "off" or stamp is None or stamp <= after:
            return None
        samples[name] = stamp
    return samples


def _hazards(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    hazards = ledger.get("hazards", {})
    return [item for item in hazards.values() if isinstance(item, Mapping)] if isinstance(hazards, Mapping) else []


def _blocked(vid: str, route: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    return {"kind": "blocked", "vehicle_id": vid, "reason": reason,
            "prefix": route.get("prefix") if isinstance(route, Mapping) else None,
            "entity_id": route.get("entity_id") if isinstance(route, Mapping) else None}


def _isolation_valid(hass: Any, hazard: Mapping[str, Any]) -> bool:
    binding = hazard.get("binding")
    if not isinstance(binding, Mapping):
        return False
    entities = binding.get("entities", {})
    charger = entities.get("charger", {}) if isinstance(entities, Mapping) else {}
    prefix = str(hazard.get("prefix") or "")
    if not prefix or _ble_binding(hass, prefix, str(charger.get("entity_id") or "")) != binding:
        return False
    control_off_at = _epoch(hazard.get("control_off_at"))
    status_off_at = _epoch(hazard.get("status_off_at"))
    if control_off_at is None or status_off_at is None:
        return False
    samples = _off_samples(hass, binding)
    if samples is None:
        return False
    return (samples["control"] >= control_off_at
            and samples["status"] >= status_off_at)


def _unresolved_routes(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return producer routes whose command effect remains unresolved."""
    attempts = ledger.get("attempts", {})
    routes = []
    for record in attempts.values() if isinstance(attempts, Mapping) else ():
        route = record.get("route", {}) if isinstance(record, Mapping) else {}
        if (isinstance(route, Mapping) and route.get("kind") in ROUTE_KINDS
                and record.get("outcome", "pending") in ("pending", "sent_unknown")):
            routes.append(route)
    for hazard in _hazards(ledger):
        if hazard.get("kind") == "ble_quarantine" and hazard.get("state") == "pending":
            routes.append(hazard)
    return routes


def _unresolved_ble(ledger: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for route in _unresolved_routes(ledger):
        if route.get("kind") == "ble":
            return route
    return None


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
        return _blocked(vid, unresolved[0] if unresolved else None, "ble_recovery") if unresolved else None
    if route.get("kind") not in ROUTE_KINDS:
        return _blocked(vid, route, "malformed_recovery")
    for hazard in _hazards(ledger):
        if hazard.get("kind") != "ble_quarantine":
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
            return _blocked(vid, unresolved[0], "ble_recovery")
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
                if (route.get("kind") == "ble" and str(route.get("prefix")) == str(prefix)
                        and record.get("outcome", "pending") in ("pending", "sent_unknown")):
                    return True
            for hazard in _hazards(ledger):
                if (hazard.get("kind") == "ble_quarantine"
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
        hazards[key] = {
            "kind": "ble_quarantine", "state": "pending", "prefix": prefix,
            "attempt_id": ticket.attempt_id, "binding": deepcopy(binding),
            "requested_epoch": time.time(),
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
        deadline = time.monotonic() + BLE_ISOLATION_TIMEOUT
        samples = None
        while time.monotonic() < deadline:
            samples = _off_samples(ticket.hass, binding, started)
            if samples is not None:
                break
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
        })
        ledger["route"] = pinned
        try:
            if not await _persist(ticket.hass, ticket.entry, runtime):
                _restore_recovery(runtime, final_before)
                return False
        except asyncio.CancelledError:
            await _restore_after_cancel(ticket.hass, ticket.entry, runtime, pending)
            raise
        return True


async def _gate(gate: Any) -> bool:
    result = gate() if callable(gate) else gate
    if inspect.isawaitable(result):
        result = await result
    return result is True


__all__ = [
    "RecoveryTicket", "async_begin_attempt", "async_finish_attempt", "pending_ble_attempt",
    "get_vehicle_route", "ble_quarantined", "async_isolate_ble",
]
