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
            if same_provider_device and _compatible(
                hass, entry, runtime, vid, existing_route
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
        fence_baseline = _fence_receipt(ticket.hass, binding)
        request_id = uuid4().hex
        hazards[key] = {
            "kind": "ble_quarantine", "state": "pending", "prefix": prefix,
            "attempt_id": ticket.attempt_id, "binding": deepcopy(binding),
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
    "RecoveryTicket", "async_begin_attempt", "async_finish_attempt", "pending_ble_attempt",
    "get_vehicle_route", "ble_quarantined", "async_isolate_ble",
]
