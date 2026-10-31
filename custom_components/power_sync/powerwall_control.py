"""Site-scoped Tesla mutation ordering and observable effect receipts.

This module has no device I/O and owns no restoration journal.  Existing
licensing, route, settings and recovery admission remains authoritative.  An
operation only records intent; the write lock is acquired at the final I/O
boundary, after those existing locks.  Status reads never acquire that lock.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import inspect
import time
from typing import Any
from uuid import uuid4

from .powerwall_local.control_method import ControlTargetKind
from .powerwall_local.route_lock import BatteryRouteLock

REGISTRY_KEY = "power_sync_tesla_control"


def canonical_site_id(value: object) -> str:
    """Use one physical Tesla identity across providers and config entries."""
    if isinstance(value, bool) or value is None:
        raise ValueError("A Tesla energy site identity is required")
    if not isinstance(value, (str, int)):
        raise ValueError("Invalid Tesla energy site identity")
    result = str(value).strip()
    if not result:
        raise ValueError("A Tesla energy site identity is required")
    # Tesla providers return the same numeric ID as either JSON int or str.
    return str(int(result)) if result.isascii() and result.isdigit() else result


def _entry_site_id(entry: Any) -> object:
    data = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    return data.get("tesla_energy_site_id") or data.get("powerwall_local_energy_site_id")


def coordinator_for(hass: Any, entry: Any = None, site_id: object = None) -> PowerwallSiteControl:
    """Return the shared coordinator; a missing site never gets an entry lock."""
    identity = canonical_site_id(site_id if site_id is not None else _entry_site_id(entry))
    registry = hass.data.setdefault(REGISTRY_KEY, {})
    if identity not in registry:
        registry[identity] = PowerwallSiteControl(identity)
    return registry[identity]


def foreign_site_owner(hass: Any, entry_id: str | None, site_id: object) -> str | None:
    """Find another entry's authoritative control/recovery obligation, read-only.

    Expiry is deliberately not a release: the original owner must finish its
    cleanup and retire its existing record. An unknown caller owns no entry.
    """
    target = canonical_site_id(site_id)
    control = hass.data.get(REGISTRY_KEY, {}).get(target)
    reservation = getattr(control, "route_entry_id", None)
    if reservation is not None and reservation != entry_id:
        return reservation
    for owner_id, runtime in hass.data.get("power_sync", {}).items():
        if not isinstance(runtime, Mapping) or (entry_id is not None and owner_id == entry_id):
            continue
        identities = set()
        control = runtime.get("powerwall_site_control")
        candidates = [getattr(control, "site_id", None)]
        entry = runtime.get("entry")
        config = {}
        if entry is None:
            lookup = getattr(getattr(hass, "config_entries", None), "async_get_entry", None)
            entry = lookup(owner_id) if lookup is not None else None
        if entry is not None:
            config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
            candidates += [config.get("tesla_energy_site_id"), config.get("powerwall_local_energy_site_id")]
        native = runtime.get("powerwall_native_max_charge")
        dispatch = runtime.get("powerwall_battery_dispatch")
        candidates += [getattr(getattr(native, "adapter", None), "site", None),
                       getattr(runtime.get("tesla_coordinator"), "site_id", None)]
        candidates += [record.get("site_id") for key in
                       ("powerwall_settings_pending", "tesla_settings_pending", "pending_tesla_restore")
                       if isinstance(record := runtime.get(key), Mapping)]
        for candidate in candidates:
            try:
                identities.add(canonical_site_id(candidate))
            except (TypeError, ValueError):
                pass

        def affects(record=None):
            identity = record.get("site_id") if isinstance(record, Mapping) else None
            if identity is not None:
                try:
                    return canonical_site_id(identity) == target
                except (TypeError, ValueError):
                    pass
            # Unidentifiable obligations cannot be proven unrelated.
            return not identities or target in identities

        for key in ("powerwall_settings_pending", "tesla_settings_pending", "pending_tesla_restore"):
            record = runtime.get(key)
            if record and affects(record):
                return str(owner_id)
        generic_applies = bool(identities) or config.get("battery_system") in (None, "tesla")
        active = generic_applies and any(isinstance(runtime.get(key), Mapping) and
                     (runtime[key].get("active") or runtime[key].get("pending"))
                     for key in ("force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state"))
        recovery = any(runtime.get(key) for key in (
            "native_max_startup_pending", "native_max_recovery_unavailable",
            "powerwall_local_recovery_pending", "powerwall_dispatch_fallback_pending", "powerwall_settings_recovering"))
        recovery = recovery or (generic_applies and any(runtime.get(key) for key in
                    ("optimizer_force_restart_restore_pending", "ev_curtailment_override")))
        event = generic_applies and (any(key in runtime and runtime[key] is not None for key in
                     ("max_backup_end_ts", "max_backup_saved_reserve"))
                 or runtime.get("max_backup_local_event") or runtime.get("max_backup_schedule"))
        if (active or recovery or event or getattr(native, "pending", False)
                or getattr(dispatch, "pending", False)) and affects():
            return str(owner_id)
        for key, active_flag in (("aemo_spike_manager", "_in_spike_mode"),
                                 ("saving_session_tariff_manager", "_in_session_mode")):
            manager = runtime.get(key)
            if manager is not None and any(getattr(manager, flag, False) for flag in
                    (active_flag, "_restore_pending", "_recovery_unreadable")):
                if affects({"site_id": getattr(manager, "site_id", None)}):
                    return str(owner_id)
    return None


@dataclass(frozen=True)
class NativeMaxTarget:
    direction: str
    kind: ControlTargetKind = ControlTargetKind.NATIVE_MAX

    def __post_init__(self):
        if self.direction not in ("charge", "discharge") or self.kind is not ControlTargetKind.NATIVE_MAX:
            raise ValueError("Native Max requires a charge or discharge direction")


def _validate_watts(watts: float, *, signed: bool = False) -> None:
    if type(watts) not in (int, float) or not math.isfinite(watts) or (not signed and watts < 0):
        raise ValueError("A finite watt target is required")


@dataclass(frozen=True)
class BatteryPowerTarget:
    """Signed battery watts: positive discharge, negative charge."""
    watts: float
    kind: ControlTargetKind = ControlTargetKind.BATTERY_WATTS

    def __post_init__(self):
        _validate_watts(self.watts, signed=True)
        if self.kind is not ControlTargetKind.BATTERY_WATTS:
            raise ValueError("Invalid battery target kind")


@dataclass(frozen=True)
class GridExportTarget:
    watts: float
    kind: ControlTargetKind = ControlTargetKind.GRID_EXPORT_WATTS

    def __post_init__(self):
        _validate_watts(self.watts)
        if self.kind is not ControlTargetKind.GRID_EXPORT_WATTS:
            raise ValueError("Invalid grid export target kind")


@dataclass(frozen=True)
class SiteImportTarget:
    watts: float
    kind: ControlTargetKind = ControlTargetKind.SITE_IMPORT_WATTS

    def __post_init__(self):
        _validate_watts(self.watts)
        if self.kind is not ControlTargetKind.SITE_IMPORT_WATTS:
            raise ValueError("Invalid site import target kind")


ControlTarget = NativeMaxTarget | BatteryPowerTarget | GridExportTarget | SiteImportTarget


@dataclass(frozen=True)
class ControlRequest:
    id: str
    site_id: str
    label: str
    target: ControlTarget | None
    created_at: float
    deadline: float | None
    entry_id: str | None = None


@dataclass(frozen=True)
class EffectReceipt:
    """Identifies a submitted callback, not device acceptance or physical effect."""
    id: str
    request_id: str
    site_id: str
    label: str
    submitted_at: float


class ControlWriteRejected(RuntimeError):
    """A final mutation fence rejected the request before submission."""


class ControlRequestStale(ControlWriteRejected):
    pass


class ControlIdentityChanged(ControlWriteRejected):
    pass


class ControlDeadlineExpired(ControlWriteRejected):
    pass


@dataclass(frozen=True)
class _Scope:
    request: ControlRequest
    guards: tuple[Callable[[], bool], ...] = ()
    identities: tuple[Callable[[], object], ...] = ()
    deadline: float | None = None
    restoring: bool = False
    apply_guards: tuple[Callable[[], bool], ...] = ()


def _earliest(*deadlines: float | None) -> float | None:
    values = [value for value in deadlines if value is not None]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("Deadline must be an absolute monotonic time")
    return min(values) if values else None


class PowerwallSiteControl:
    """One physical site's short intent state and final mutation serialization.

    Task identity, rather than a ContextVar, grants nested authority.  Ordinary
    child tasks therefore cannot inherit a parent's lock or operation.  Only
    run_write's explicitly supervised direct-I/O task receives that authority.
    """

    def __init__(self, site_id: object, *, clock: Callable[[], float] = time.monotonic):
        self.site_id = canonical_site_id(site_id)
        self.route_lock = BatteryRouteLock()
        self._write_lock = BatteryRouteLock()
        self._write_owner = None
        self._write_depth = 0
        self._clock = clock
        self._scopes: dict[asyncio.Task, _Scope] = {}
        self._operations: dict[str, dict] = {}
        self._effects: dict[str, dict] = {}
        self._jobs: dict[asyncio.Task, dict] = {}
        self._latest: str | None = None
        self._open_operations: set[str] = set()
        self._obligations: dict[str, Any] = {}
        self.route_entry_id: str | None = None

    @asynccontextmanager
    async def route_scope(self, entry_id: str):
        """Reserve admission while its existing route lock protects preparation."""
        if not entry_id:
            raise ControlRequestStale("A route reservation requires an owning entry")
        async with self.route_lock:
            previous = self.route_entry_id
            if previous is not None and previous != entry_id:
                raise ControlRequestStale("A nested route cannot change its owning entry")
            self.route_entry_id = entry_id
            try:
                yield self
            finally:
                self.route_entry_id = previous

    def request(self, label: str, target: ControlTarget | None = None,
                deadline: float | None = None, *, entry_id: str | None = None) -> ControlRequest:
        if target is not None and not isinstance(target, (NativeMaxTarget, BatteryPowerTarget, GridExportTarget, SiteImportTarget)):
            raise TypeError("A typed Powerwall control target is required")
        return ControlRequest(uuid4().hex, self.site_id, label, target, self._clock(), _earliest(deadline), entry_id)

    def current_entry_id(self) -> str | None:
        """Return only this exact task's explicit operation owner."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return None
        scope = self._scopes.get(task)
        return scope.request.entry_id if scope is not None else None

    @asynccontextmanager
    async def operation(self, request: ControlRequest | None = None, *, label: str = "control",
                        target: ControlTarget | None = None, deadline: float | None = None,
                        is_current: Callable[[], bool] | None = None,
                        identity: Callable[[], object] | None = None, restoring: bool = False,
                        apply_guard: Callable[[], bool] | None = None, entry_id: str | None = None):
        """Record logical intent without holding a lock across orchestration."""
        task = asyncio.current_task()
        parent = self._scopes.get(task)
        if parent is not None and entry_id is not None and entry_id != parent.request.entry_id:
            raise ControlRequestStale("A nested operation cannot change its owning entry")
        if request is not None and entry_id is not None and request.entry_id != entry_id:
            raise ControlRequestStale("Control request entry identity does not match")
        if restoring and (target is not None or deadline is not None or request is not None):
            raise ControlRequestStale("Restoration cannot create a forcing target or deadline")
        if parent is not None and parent.restoring and (target is not None or (request is not None and request.target is not None)):
            raise ControlRequestStale("A restoration scope cannot renew a forcing request")
        if parent is not None and not restoring and request is not None and request != parent.request:
            raise ControlRequestStale("A nested operation cannot replace its parent request")
        owner = parent.request.entry_id if parent is not None else entry_id
        request = self.request(label, entry_id=owner) if restoring else parent.request if parent else request or self.request(label, target, deadline, entry_id=owner)
        if canonical_site_id(request.site_id) != self.site_id:
            raise ControlIdentityChanged("Control request belongs to another Tesla site")
        # Explicitly passing an active request to a child is not reentrancy.
        if parent is None and any(scope.request.id == request.id for scope in self._scopes.values()):
            raise ControlRequestStale("Control request is owned by another task")
        scope = _Scope(request,
                       (parent.guards if parent else ()) + ((is_current,) if is_current else ()),
                       (parent.identities if parent else ()) + ((identity,) if identity else ()),
                       None if restoring else _earliest(request.deadline, parent.deadline if parent else None, deadline),
                       restoring or bool(parent and parent.restoring),
                       (() if restoring or parent is None else parent.apply_guards)
                       + ((apply_guard,) if apply_guard else ()))
        self._scopes[task] = scope
        owns_operation = parent is None or restoring
        if owns_operation:
            self._open_operations.add(request.id)
            self._latest = request.id
            self._operations[request.id] = {"request": request, "status": "running"}
            self._prune()
        try:
            yield request
        except BaseException as exc:
            if owns_operation:
                self._operations[request.id]["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            raise
        else:
            if owns_operation:
                self._operations[request.id]["status"] = "completed"
        finally:
            if owns_operation:
                self._open_operations.discard(request.id)
            if parent is None:
                self._scopes.pop(task, None)
            else:
                self._scopes[task] = parent

    def _check(self, scope: _Scope) -> None:
        if canonical_site_id(scope.request.site_id) != self.site_id:
            raise ControlIdentityChanged("Control request belongs to another Tesla site")
        for identity in scope.identities:
            try:
                matches = canonical_site_id(identity()) == self.site_id
            except (TypeError, ValueError):
                matches = False
            if not matches:
                raise ControlIdentityChanged("Tesla site identity changed before submission")
        for guard in (*scope.guards, *scope.apply_guards):
            current = guard()
            if current is not True:
                # Async admission cannot be made atomic with this final I/O
                # boundary.  Reject it rather than treating a coroutine as a
                # truthy permission and accidentally submitting a command.
                if inspect.iscoroutine(current):
                    current.close()
                raise ControlRequestStale("Control request is no longer current")
        if scope.deadline is not None and self._clock() >= scope.deadline:
            raise ControlDeadlineExpired("Control request expired before submission")

    def current_fence(self, *, required: bool = True) -> Callable[[], None] | None:
        """Capture final checks for an explicitly supervised raw I/O task.

        Calling the fence grants no lock/operation ownership to that task.
        """
        scope = self._scopes.get(asyncio.current_task())
        if scope is None:
            if not required:
                return None
            raise ControlRequestStale("No current control operation")
        return lambda: self._check(scope)

    @asynccontextmanager
    async def write_context(self, label: str, *, is_current: Callable[[], bool] | None = None,
                            deadline: float | None = None, identity: Callable[[], object] | None = None,
                            request: ControlRequest | None = None, prepare: Callable | None = None,
                            entry_id: str | None = None):
        """Fence and serialize only the final mutation; retain uncertain exits.

        Use around direct I/O.  A cancelled context cannot prove whether a
        remote effect happened, so its receipt remains uncertain until an
        explicit reconciliation.  Existing durable journals are untouched.
        """
        task = asyncio.current_task()
        parent = self._scopes.get(task)
        if parent is None:
            async with self.operation(request, label=label, deadline=deadline,
                                      is_current=is_current, identity=identity, entry_id=entry_id):
                async with self.write_context(label, prepare=prepare) as receipt:
                    yield receipt
            return
        if request is not None and request != parent.request:
            raise ControlRequestStale("A nested write cannot replace its parent request")
        if entry_id is not None and entry_id != parent.request.entry_id:
            raise ControlRequestStale("A nested write cannot change its owning entry")
        scope = _Scope(parent.request, parent.guards + ((is_current,) if is_current else ()),
                       parent.identities + ((identity,) if identity else ()),
                       _earliest(parent.deadline, deadline), parent.restoring, parent.apply_guards)
        # Timeout only lock acquisition.  Cancelling a submitted I/O on expiry
        # would turn a known in-flight effect into an unknown one.
        remaining = None if scope.deadline is None else max(0, scope.deadline - self._clock())
        try:
            async with asyncio.timeout(remaining):
                await self._write_lock.__aenter__()
        except TimeoutError as exc:
            raise ControlDeadlineExpired("Control request expired while queued") from exc
        try:
            self._check(scope)
            if prepare is not None:
                await prepare()
                self._check(scope)
            receipt = EffectReceipt(uuid4().hex, scope.request.id, self.site_id, label, self._clock())
            self._effects[receipt.id] = {"receipt": receipt, "status": "submitted", "reason": None}
            self._scopes[task] = scope
            self._write_owner = task
            self._write_depth += 1
            try:
                yield receipt
            except BaseException:
                if self._effects[receipt.id]["status"] != "not_submitted":
                    self.mark_effect_uncertain(receipt, "mutation_interrupted")
                raise
            else:
                effect = self._effects[receipt.id]
                if effect["status"] == "submitted":
                    self.resolve_effect(receipt)
            finally:
                self._scopes[task] = parent
                self._write_depth -= 1
                if not self._write_depth:
                    self._write_owner = None
                self._prune()
        finally:
            await self._write_lock.__aexit__(None, None, None)

    async def run_write(self, label: str, callback: Callable, *, is_current=None,
                        deadline=None, identity=None, request=None, prepare=None, entry_id=None):
        """Supervise direct I/O independently of cancellation of its caller.

        A queued cancelled caller cancels its worker before any submission.
        An already submitted worker remains tracked, and its eventual result
        reconciles its own receipt without changing a newer operation's state.
        """
        task = asyncio.current_task()
        scope = self._scopes.get(task)
        if scope is None:
            async with self.operation(request, label=label, deadline=deadline,
                                      is_current=is_current, identity=identity, entry_id=entry_id):
                return await self.run_write(label, callback, prepare=prepare)
        if request is not None and request != scope.request:
            raise ControlRequestStale("A nested write cannot replace its parent request")
        if entry_id is not None and entry_id != scope.request.entry_id:
            raise ControlRequestStale("A nested write cannot change its owning entry")

        async def invoke(job):
            async with self.write_context(label, is_current=is_current, deadline=deadline, identity=identity, prepare=prepare) as receipt:
                job["receipt"] = receipt
                job["status"] = "submitted"
                result = await callback()
                # A caller's cancellation is resolved by this worker's known
                # completion.  An explicitly supplied unknown remains unknown.
                effect = self._effects[receipt.id]
                if effect["reason"] == "caller_cancelled":
                    self.resolve_effect(receipt)
                return result

        # Same-task cleanup inside a submitted callback keeps the same owner.
        if task is self._write_owner:
            return await invoke({})
        job = {"request_id": scope.request.id, "label": label, "status": "queued", "receipt": None}

        async def supervise():
            worker = asyncio.current_task()
            self._scopes[worker] = scope  # Explicit delegation, never inheritance.
            try:
                return await invoke(job)
            finally:
                self._scopes.pop(worker, None)

        worker = asyncio.create_task(supervise(), name=f"power_sync_tesla_write:{label}")
        self._jobs[worker] = job

        def done(completed):
            # Retrieving exceptions also handles detached cancelled callers.
            if not completed.cancelled():
                completed.exception()
            self._jobs.pop(completed, None)

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if job["receipt"] is None:
                worker.cancel()
            elif not worker.done():
                self.mark_effect_uncertain(job["receipt"], "caller_cancelled")
            raise

    def mark_effect_uncertain(self, receipt: EffectReceipt, reason: str = "unknown") -> None:
        """Retain uncertainty; this is diagnostic state, never a restore claim."""
        effect = self._effects[receipt.id]
        effect.update(status="uncertain", reason=reason)

    def resolve_effect(self, receipt: EffectReceipt) -> None:
        """Record a known callback completion, without asserting physical success."""
        self._effects[receipt.id].update(status="returned", reason=None)

    def mark_effect_not_submitted(self, receipt: EffectReceipt) -> None:
        """Record proven rejection before invoking the raw driver, never an unknown."""
        effect = self._effects[receipt.id]
        if effect["status"] == "submitted":
            effect.update(status="not_submitted", reason=None)

    def set_obligations(self, source: str, value: Any) -> None:
        """Copy an authoritative journal's observed state without changing it."""
        self._obligations[source] = deepcopy(value)

    def snapshot(self) -> dict:
        """Return isolated, responsive diagnostics even while device I/O blocks."""
        operations = [{**asdict(item["request"]), "status": item["status"]}
                      for item in self._operations.values()]
        effects = [{**asdict(item["receipt"]), "status": item["status"], "reason": item["reason"]}
                   for item in self._effects.values()]
        return {"site_id": self.site_id,
                "operation": next((item for item in operations if item["id"] == self._latest), None),
                "operations": operations,
                "effects": effects,
                "uncertain_effects": [item for item in effects if item["status"] == "uncertain"],
                "submitted_tasks": [{key: value for key, value in job.items() if key != "receipt"}
                                    for job in self._jobs.values()],
                "obligations": deepcopy(self._obligations)}

    def _prune(self):
        # Unknown effects are never discarded.  This bounds ordinary successful
        # traffic without turning a timeout into an implicit recovery decision.
        for key in list(self._effects):
            if len(self._effects) <= 64:
                break
            if self._effects[key]["status"] in {"returned", "not_submitted"}:
                del self._effects[key]
        active = self._open_operations | {scope.request.id for scope in self._scopes.values()}
        active.update(effect["receipt"].request_id for effect in self._effects.values()
                      if effect["status"] not in {"returned", "not_submitted"})
        for key in list(self._operations):
            if len(self._operations) <= 32:
                break
            if key not in active and key != self._latest:
                del self._operations[key]
