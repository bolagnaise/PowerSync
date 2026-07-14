"""Read-only network operating envelopes from certified site equipment.

PowerSync is not a CSIP-AUS client.  This module consumes limits already
enforced by certified equipment exposed through Home Assistant.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import inspect
import logging
from typing import Any, Awaitable, Callable, Iterable, Literal

_LOGGER = logging.getLogger(__name__)

NETWORK_ENVELOPE_SCHEMA_VERSION = 1
# Release B exposes the frozen read-only monitoring contract.  Flip this only
# after the required seven-day SAPN site soak and staged fallback replay have
# completed; both config flow and runtime enforce the gate independently.
NETWORK_EXPORT_ACTIVE_MODE_RELEASED = False
MIN_SAFETY_MARGIN_W = 250.0
DEFAULT_SOURCE_MAX_AGE_SECONDS = 10 * 60
DEFAULT_PCC_MAX_AGE_SECONDS = 2 * 60

EnvelopeMode = Literal["off", "monitoring", "active"]
EnvelopeScope = Literal["aggregate_pcc", "per_phase"]


@dataclass(frozen=True)
class EnvelopeSchedulePoint:
    start: datetime
    end: datetime
    limit_w: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "limit_w": self.limit_w,
        }


@dataclass(frozen=True)
class NetworkExportEnvelope:
    schema_version: int = NETWORK_ENVELOPE_SCHEMA_VERSION
    mode: EnvelopeMode = "off"
    source_kind: str | None = None
    scope: EnvelopeScope | None = None
    current_limit_w: float | None = None
    per_phase_limits_w: dict[str, float] | None = None
    fallback_limit_w: float | None = None
    effective_limit_w: float | None = None
    source_status: str | None = None
    source_updated_at: datetime | None = None
    received_at: datetime | None = None
    expires_at: datetime | None = None
    next_change_at: datetime | None = None
    schedule: tuple[EnvelopeSchedulePoint, ...] = ()
    snapshot_version: int = 0
    active_export_permitted: bool = False
    reason: str | None = None
    fault: str | None = None
    safety_margin_w: float | None = None
    source_entity_id: str | None = None
    provenance_valid: bool = False
    fresh_post_subscription: bool = False
    # Internal resolved scalar cap. It is intentionally omitted from the
    # public schema; consumers receive only the already-resolved limits.
    _static_limit_w: float | None = None
    _configured_safety_margin_w: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("source_updated_at", "received_at", "expires_at", "next_change_at"):
            item = getattr(self, key)
            value[key] = item.isoformat() if item else None
        value["schedule"] = [item.to_dict() for item in self.schedule]
        value["next_limit_w"] = (
            self.limit_for_interval(
                self.next_change_at,
                self.next_change_at + timedelta(seconds=1),
            )
            if self.next_change_at is not None
            else None
        )
        value.pop("_static_limit_w", None)
        value.pop("_configured_safety_margin_w", None)
        return value

    def limit_for_interval(self, start: datetime, end: datetime) -> float | None:
        """Return the minimum limit covering a complete optimizer slot."""
        if self.mode == "off":
            return None
        fallback = self.fallback_limit_w
        if fallback is None:
            fallback = 0.0
        controls = [item for item in self.schedule if item.end > start and item.start < end]
        if not controls:
            if self.schedule:
                result = max(0.0, fallback)
                if self._static_limit_w is not None:
                    result = min(result, self._static_limit_w)
                return result
            return self.effective_limit_w if self.effective_limit_w is not None else fallback

        boundaries = {start, end}
        for item in controls:
            boundaries.add(max(start, item.start))
            boundaries.add(min(end, item.end))
        ordered = sorted(boundaries)
        limits: list[float] = []
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            midpoint = left + (right - left) / 2
            active = [item.limit_w for item in controls if item.start <= midpoint < item.end]
            limits.append(min(active) if active else fallback)
        if not limits:
            limits.append(fallback)
        result = min(limits)
        if self._static_limit_w is not None:
            result = min(result, self._static_limit_w)
        return max(0.0, result)


@dataclass(frozen=True)
class ProvenanceResult:
    valid: bool
    reason: str | None = None


def resolve_effective_limit_w(
    static_limit_w: float | None,
    live_limit_w: float | None,
    fallback_limit_w: float | None,
    *,
    live_valid: bool,
) -> float:
    """Fail closed and preserve numeric zero as a valid control."""
    selected = live_limit_w if live_valid and live_limit_w is not None else fallback_limit_w
    if selected is None:
        selected = 0.0
    selected = max(0.0, float(selected))
    if static_limit_w is not None:
        selected = min(selected, max(0.0, float(static_limit_w)))
    return selected


def safety_margin_w(effective_limit_w: float | None, configured_w: float | None = None) -> float:
    effective = max(0.0, float(effective_limit_w or 0.0))
    minimum = max(MIN_SAFETY_MARGIN_W, effective * 0.05)
    try:
        configured = float(configured_w) if configured_w is not None else minimum
    except (TypeError, ValueError):
        configured = minimum
    return max(minimum, configured)


def parse_schedule(raw: Any) -> tuple[EnvelopeSchedulePoint, ...]:
    """Normalize, order and retain overlaps (resolution takes their minimum)."""
    if not isinstance(raw, (list, tuple)):
        return ()
    parsed: list[EnvelopeSchedulePoint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = _parse_datetime(item.get("start") or item.get("valid_from"))
        end = _parse_datetime(item.get("end") or item.get("valid_until"))
        limit = _nullable_non_negative(
            item.get("limit_w", item.get("export_limit_w", item.get("value")))
        )
        if start is None or end is None or limit is None or end <= start:
            continue
        parsed.append(EnvelopeSchedulePoint(start=start, end=end, limit_w=limit))
    return tuple(sorted(parsed, key=lambda item: (item.start, item.end, item.limit_w)))


def normalize_envelope(
    *,
    mode: EnvelopeMode,
    scope: EnvelopeScope,
    current_limit_w: Any,
    fallback_limit_w: Any,
    static_limit_w: float | None,
    source_status: str | None,
    source_updated_at: datetime | None,
    received_at: datetime | None,
    expires_at: datetime | None,
    schedule: Iterable[EnvelopeSchedulePoint] = (),
    snapshot_version: int = 0,
    source_entity_id: str | None = None,
    per_phase_limits_w: dict[str, Any] | None = None,
    provenance: ProvenanceResult = ProvenanceResult(False, "source provenance unavailable"),
    fresh_post_subscription: bool = False,
    attested_all_der_covered: bool = False,
    site_phase_count: int = 1,
    pcc_fresh: bool = False,
    configured_safety_margin_w: float | None = None,
    now: datetime | None = None,
    source_max_age_seconds: int = DEFAULT_SOURCE_MAX_AGE_SECONDS,
) -> NetworkExportEnvelope:
    """Build the atomic public snapshot and evaluate active-mode gates."""
    now = _aware(now or datetime.now(timezone.utc))
    current = _nullable_non_negative(current_limit_w)
    fallback = _nullable_non_negative(fallback_limit_w)
    phase_limits = _phase_limits(per_phase_limits_w)
    raw_schedule = tuple(schedule)
    status = str(source_status or "unknown").lower()
    invalid_statuses = ("invalid", "unavailable", "unknown", "expired", "error", "offline")
    status_valid = not any(status == value or status.startswith(f"{value}_") for value in invalid_statuses)
    age_valid = (
        received_at is not None
        and (_aware(now) - _aware(received_at)).total_seconds() <= source_max_age_seconds
    )
    expiry_valid = expires_at is None or _aware(expires_at) > now
    live_valid = current is not None and status_valid and age_valid and expiry_valid
    # Future controls belong to the same certified source as the current
    # limit. A stale/invalid source cannot leave a stale-high schedule armed;
    # enforcement falls back and the next fresh source update republishes it.
    schedule_tuple = raw_schedule if live_valid else ()
    effective = None if mode == "off" else (
        0.0
        if mode == "active" and fallback is None
        else resolve_effective_limit_w(
            static_limit_w, current, fallback, live_valid=live_valid
        )
    )
    reason = None
    active_permitted = mode == "active"
    gates = (
        (fallback is not None, "site-approved fallback is required"),
        (fresh_post_subscription, "awaiting a fresh post-subscription source update"),
        (provenance.valid, provenance.reason or "source provenance is not trusted"),
        (live_valid, "network envelope source is stale or invalid"),
        (pcc_fresh, "PCC telemetry is stale or unavailable"),
        (attested_all_der_covered, "all exporting DER coverage has not been attested"),
        (
            site_phase_count <= 1 or scope == "aggregate_pcc",
            "multi-phase per-phase sources are monitoring-only",
        ),
    )
    if mode == "active":
        for valid, message in gates:
            if not valid:
                active_permitted = False
                reason = message
                break
    elif mode == "monitoring":
        reason = "monitoring mode suppresses intentional PowerSync export"
    else:
        reason = "network envelope is off"

    # A limit changes at either edge of a control. Publishing only future
    # starts hides the return-to-fallback transition at the end of a window.
    next_change = min(
        (
            boundary
            for item in schedule_tuple
            for boundary in (item.start, item.end)
            if boundary > now
        ),
        default=None,
    )
    margin = safety_margin_w(effective, configured_safety_margin_w) if mode != "off" else None
    envelope = NetworkExportEnvelope(
        mode=mode,
        source_kind="ha_entity" if source_entity_id else None,
        scope=scope,
        current_limit_w=current,
        per_phase_limits_w=phase_limits,
        fallback_limit_w=fallback,
        effective_limit_w=effective,
        source_status=source_status,
        source_updated_at=source_updated_at,
        received_at=received_at,
        expires_at=expires_at,
        next_change_at=next_change,
        schedule=schedule_tuple,
        snapshot_version=snapshot_version,
        active_export_permitted=active_permitted,
        reason=reason,
        safety_margin_w=margin,
        source_entity_id=source_entity_id,
        provenance_valid=provenance.valid,
        fresh_post_subscription=fresh_post_subscription,
        _static_limit_w=(
            max(0.0, float(static_limit_w))
            if static_limit_w is not None
            else None
        ),
        _configured_safety_margin_w=_nullable_non_negative(
            configured_safety_margin_w
        ),
    )
    if mode != "off" and schedule_tuple:
        current_effective = envelope.limit_for_interval(
            now,
            now + timedelta(seconds=1),
        )
        envelope = replace(
            envelope,
            effective_limit_w=current_effective,
            safety_margin_w=safety_margin_w(
                current_effective,
                envelope._configured_safety_margin_w,
            ),
        )
    return envelope


class HANetworkEnvelopeManager:
    """Atomic HA-entity adapter for a certified controller's read-only limit."""

    def __init__(self, hass: Any, entry: Any, static_limit_getter: Callable[[], float | None]):
        self.hass = hass
        self.entry = entry
        self._static_limit_getter = static_limit_getter
        self._snapshot = NetworkExportEnvelope()
        self._version = 0
        self._fresh_post_subscription = False
        self._last_received_at: datetime | None = None
        self._last_source_updated_at: datetime | None = None
        self._source_order_valid = True
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[NetworkExportEnvelope, NetworkExportEnvelope], Any]] = []
        self._fault: str | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> NetworkExportEnvelope:
        return self._snapshot

    def add_listener(
        self, callback: Callable[[NetworkExportEnvelope, NetworkExportEnvelope], Any]
    ) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    async def async_start(self) -> None:
        from homeassistant.helpers.event import (
            async_track_state_change_event,
            async_track_time_interval,
        )

        entities = [value for value in self._configured_entities() if value]
        if entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, entities, self._async_state_changed)
            )
        # Expiry, source/PCC freshness and scheduled control boundaries are
        # time driven.  They must fail closed even when no HA entity emits a
        # new state at the boundary.
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_periodic_refresh,
                timedelta(seconds=15),
            )
        )
        await self.async_refresh(fresh_event=False)

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    async def _async_state_changed(self, event: Any) -> None:
        new_state = event.data.get("new_state") if event else None
        entity_id = str(event.data.get("entity_id") or "") if event else ""
        settings = dict(getattr(self.entry, "data", {}) or {})
        settings.update(getattr(self.entry, "options", {}) or {})
        limit_entity = str(settings.get("network_export_limit_entity") or "")
        fresh_limit_event = new_state is not None and entity_id == limit_entity
        if fresh_limit_event:
            updated_at = _aware(getattr(new_state, "last_updated", None))
            if (
                self._last_source_updated_at is not None
                and updated_at <= self._last_source_updated_at
            ):
                self._source_order_valid = False
                fresh_limit_event = False
            else:
                self._last_source_updated_at = updated_at
                self._source_order_valid = True
                self._fresh_post_subscription = True
                self._last_received_at = datetime.now(timezone.utc)
        await self.async_refresh(fresh_event=fresh_limit_event)

    async def _async_periodic_refresh(self, _now: datetime) -> None:
        await self.async_refresh(fresh_event=False)

    async def async_refresh(self, *, fresh_event: bool = False) -> NetworkExportEnvelope:
        async with self._lock:
            if fresh_event:
                self._fresh_post_subscription = True
            old = self._snapshot
            new = await self._build_snapshot()
            self._version += 1
            new = replace(new, snapshot_version=self._version, fault=self._fault)
            if self._fault:
                new = replace(
                    new,
                    active_export_permitted=False,
                    reason=self._fault,
                )
            self._snapshot = new
        for callback in tuple(self._listeners):
            result = callback(old, new)
            if inspect.isawaitable(result):
                self.hass.async_create_task(result)
        return new

    async def _build_snapshot(self) -> NetworkExportEnvelope:
        settings = dict(getattr(self.entry, "data", {}) or {})
        settings.update(getattr(self.entry, "options", {}) or {})
        requested_mode = str(settings.get("network_export_mode") or "off")
        if requested_mode not in ("off", "monitoring", "active"):
            requested_mode = "off"
        active_release_blocked = (
            requested_mode == "active" and not NETWORK_EXPORT_ACTIVE_MODE_RELEASED
        )
        mode = "monitoring" if active_release_blocked else requested_mode
        scope = str(settings.get("network_export_scope") or "aggregate_pcc")
        if scope not in ("aggregate_pcc", "per_phase"):
            scope = "aggregate_pcc"
        source_entity = str(settings.get("network_export_limit_entity") or "")
        state = self.hass.states.get(source_entity) if source_entity else None
        provenance = await self._provenance(source_entity)
        now = datetime.now(timezone.utc)
        current = _state_power_w(state)
        attributes = dict(getattr(state, "attributes", {}) or {})
        status_entity = str(settings.get("network_export_status_entity") or "")
        status_state = self.hass.states.get(status_entity) if status_entity else None
        if status_entity and (
            status_state is None
            or getattr(status_state, "state", None) in (None, "", "unknown", "unavailable")
        ):
            status = "invalid_status_source"
        else:
            status = _state_text(
                self.hass,
                status_entity,
                attributes.get("status", "valid" if current is not None else "unavailable"),
            )
        if not self._source_order_valid:
            status = "invalid_out_of_order"
        expiry_entity = str(settings.get("network_export_expiry_entity") or "")
        expiry_state = self.hass.states.get(expiry_entity) if expiry_entity else None
        expiry = _state_datetime(
            self.hass,
            expiry_entity,
            attributes.get("expires_at") or attributes.get("valid_until"),
        )
        if expiry_entity and (
            expiry_state is None
            or getattr(expiry_state, "state", None) in (None, "", "unknown", "unavailable")
            or expiry is None
        ):
            status = "invalid_expiry_source"
        schedule_state = self.hass.states.get(settings.get("network_export_schedule_entity"))
        schedule_raw = (
            (getattr(schedule_state, "attributes", {}) or {}).get("schedule")
            if schedule_state is not None
            else attributes.get("schedule")
        )
        pcc_entity = str(settings.get("network_export_pcc_power_entity") or "")
        pcc_state = self.hass.states.get(pcc_entity) if pcc_entity else None
        pcc_fresh = _state_is_fresh(
            pcc_state,
            now,
            int(settings.get("network_export_pcc_max_age_seconds") or DEFAULT_PCC_MAX_AGE_SECONDS),
        ) and _state_power_w(pcc_state, allow_negative=True) is not None
        snapshot = normalize_envelope(
            mode=mode,
            scope=scope,
            current_limit_w=current,
            fallback_limit_w=settings.get("network_export_fallback_limit_w"),
            static_limit_w=self._static_limit_getter(),
            source_status=status,
            source_updated_at=_aware(getattr(state, "last_updated", now)) if state else None,
            received_at=self._last_received_at,
            expires_at=expiry,
            schedule=parse_schedule(schedule_raw),
            snapshot_version=self._version,
            source_entity_id=source_entity or None,
            per_phase_limits_w=attributes.get("per_phase_limits_w"),
            provenance=provenance,
            fresh_post_subscription=self._fresh_post_subscription,
            attested_all_der_covered=bool(settings.get("network_export_all_der_attested", False)),
            site_phase_count=max(1, int(settings.get("network_export_site_phase_count") or 1)),
            pcc_fresh=pcc_fresh,
            configured_safety_margin_w=settings.get("network_export_safety_margin_w"),
            now=now,
            source_max_age_seconds=int(
                settings.get("network_export_source_max_age_seconds")
                or DEFAULT_SOURCE_MAX_AGE_SECONDS
            ),
        )
        if active_release_blocked:
            snapshot = replace(
                snapshot,
                active_export_permitted=False,
                reason=(
                    "active mode is held behind the SAPN monitoring soak "
                    "release gate"
                ),
            )
        return snapshot

    async def _provenance(self, entity_id: str) -> ProvenanceResult:
        if not entity_id:
            return ProvenanceResult(False, "network limit entity is not configured")
        from homeassistant.helpers import entity_registry as er

        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if registry_entry is None:
            return ProvenanceResult(False, "network limit source is not in the entity registry")
        platform = str(getattr(registry_entry, "platform", "") or "").lower()
        if platform in {"template", "power_sync"}:
            return ProvenanceResult(False, "template and PowerSync entities cannot arm active export")
        if not getattr(registry_entry, "unique_id", None):
            return ProvenanceResult(False, "network limit source has no stable unique_id")
        if not (
            getattr(registry_entry, "device_id", None)
            or getattr(registry_entry, "config_entry_id", None)
            or getattr(registry_entry, "config_entry_id", None)
        ):
            return ProvenanceResult(False, "network limit source has no device/config-entry owner")
        if getattr(registry_entry, "config_entry_id", None) == getattr(self.entry, "entry_id", None):
            return ProvenanceResult(False, "PowerSync-owned entities cannot arm active export")
        return ProvenanceResult(True)

    def _configured_entities(self) -> tuple[str, ...]:
        settings = dict(getattr(self.entry, "data", {}) or {})
        settings.update(getattr(self.entry, "options", {}) or {})
        return tuple(
            str(settings.get(key) or "")
            for key in (
                "network_export_limit_entity",
                "network_export_status_entity",
                "network_export_expiry_entity",
                "network_export_schedule_entity",
                "network_export_pcc_power_entity",
            )
        )

    def pcc_export_w(self) -> tuple[float | None, datetime | None]:
        settings = dict(getattr(self.entry, "data", {}) or {})
        settings.update(getattr(self.entry, "options", {}) or {})
        state = self.hass.states.get(settings.get("network_export_pcc_power_entity"))
        value = _state_power_w(state, allow_negative=True)
        if value is None:
            return None, None
        # PCC entity uses the PowerSync convention: positive import, negative export.
        return max(0.0, -value), _aware(getattr(state, "last_updated", None))

    async def async_set_fault(self, reason: str | None) -> None:
        self._fault = reason
        await self.async_refresh()


class ExportGuard:
    """Central fail-closed runtime guard for export-increasing actuator writes."""

    def __init__(
        self,
        manager: HANetworkEnvelopeManager,
        *,
        stop_export: Callable[[], Awaitable[bool]] | None = None,
        pcc_max_age_seconds: int = DEFAULT_PCC_MAX_AGE_SECONDS,
    ) -> None:
        self.manager = manager
        self.stop_export = stop_export
        self.pcc_max_age_seconds = pcc_max_age_seconds
        self._approved_limit_w = 0.0
        self._approved_safety_margin_w = MIN_SAFETY_MARGIN_W
        self._approved_snapshot_version: int | None = None

    @property
    def approved_limit_w(self) -> float:
        """Return the last successfully optimized instantaneous cap."""
        return self._approved_limit_w

    def approve_reoptimized_snapshot(self, snapshot_version: int) -> bool:
        """Authorize an upward limit only after a successful reoptimization."""
        snapshot = self.manager.snapshot
        if (
            snapshot.snapshot_version != snapshot_version
            or snapshot.mode != "active"
            or not snapshot.active_export_permitted
            or snapshot.fault
        ):
            return False
        now = datetime.now(timezone.utc)
        active_limit = snapshot.limit_for_interval(now, now + timedelta(seconds=1))
        self._approved_limit_w = max(0.0, float(active_limit or 0.0))
        self._approved_safety_margin_w = safety_margin_w(
            self._approved_limit_w,
            snapshot._configured_safety_margin_w,
        )
        self._approved_snapshot_version = snapshot_version
        return True

    def reset_reoptimization_approval(self) -> None:
        self._approved_limit_w = 0.0
        self._approved_safety_margin_w = MIN_SAFETY_MARGIN_W
        self._approved_snapshot_version = None

    async def clamp_requested_export_w(
        self,
        requested_w: float,
        *,
        current_controlled_export_w: float = 0.0,
    ) -> float:
        snapshot = self.manager.snapshot
        if snapshot.mode == "off":
            return max(0.0, float(requested_w))
        if (
            snapshot.mode == "monitoring"
            or snapshot.fault
            or not snapshot.active_export_permitted
        ):
            return 0.0
        pcc_export_w, observed_at = self.manager.pcc_export_w()
        now = datetime.now(timezone.utc)
        if (
            pcc_export_w is None
            or observed_at is None
            or (now - observed_at).total_seconds() > self.pcc_max_age_seconds
        ):
            await self._fault_and_stop("PCC telemetry is stale or unavailable")
            return 0.0
        active_limit = snapshot.limit_for_interval(now, now + timedelta(seconds=1))
        limit = max(0.0, float(active_limit or 0.0))
        # Downward controls take effect immediately and latch. An upward
        # control remains at the last lower authorization until the coordinator
        # has solved a schedule against the new snapshot and approves it.
        if limit < self._approved_limit_w:
            self._approved_limit_w = limit
            self._approved_safety_margin_w = safety_margin_w(
                limit,
                snapshot._configured_safety_margin_w,
            )
            self._approved_snapshot_version = snapshot.snapshot_version
        limit = min(limit, self._approved_limit_w)
        margin = safety_margin_w(limit, self._approved_safety_margin_w)
        safe_limit = max(0.0, limit - margin)
        unmanaged = max(0.0, pcc_export_w - max(0.0, current_controlled_export_w))
        if pcc_export_w > safe_limit + 1e-6:
            await self._fault_and_stop("PCC export exceeded the guarded network limit")
            return 0.0
        return min(max(0.0, float(requested_w)), max(0.0, safe_limit - unmanaged))

    async def async_guard_write(
        self,
        requested_w: float,
        writer: Callable[[float], Awaitable[bool]],
        *,
        current_controlled_export_w: float = 0.0,
    ) -> bool:
        first = self.manager.snapshot
        clamped = await self.clamp_requested_export_w(
            requested_w,
            current_controlled_export_w=current_controlled_export_w,
        )
        second = self.manager.snapshot
        if second.snapshot_version != first.snapshot_version:
            clamped = await self.clamp_requested_export_w(
                clamped,
                current_controlled_export_w=current_controlled_export_w,
            )
            second = self.manager.snapshot
        if second.mode != "off" and (
            second.mode == "monitoring"
            or second.fault
            or not second.active_export_permitted
        ):
            return False
        if clamped <= 0 and requested_w > 0:
            return False
        return bool(await writer(clamped))

    async def _fault_and_stop(self, reason: str) -> None:
        await self.manager.async_set_fault(reason)
        if self.stop_export is not None:
            try:
                stopped = await self.stop_export()
            except Exception:
                stopped = False
            if not stopped:
                await self.manager.async_set_fault(f"{reason}; export stop command failed")


def optimizer_slot_limits(
    snapshot: NetworkExportEnvelope,
    timestamps: list[datetime],
    interval_minutes: int,
) -> list[float | None]:
    limits = [
        snapshot.limit_for_interval(
            timestamp,
            timestamp + timedelta(minutes=interval_minutes),
        )
        for timestamp in timestamps
    ]
    return [
        None
        if limit is None
        else max(
            0.0,
            limit - safety_margin_w(limit, snapshot._configured_safety_margin_w),
        )
        for limit in limits
    ]


def _state_power_w(state: Any, *, allow_negative: bool = False) -> float | None:
    if state is None or getattr(state, "state", None) in (None, "unknown", "unavailable", ""):
        return None
    try:
        value = float(getattr(state, "state", None))
    except (TypeError, ValueError):
        return None
    if not allow_negative and value < 0:
        return None
    unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement") or "W").lower()
    return value * 1000.0 if unit in {"kw", "kilowatt", "kilowatts"} else value


def _state_text(hass: Any, entity_id: Any, fallback: Any) -> str | None:
    state = hass.states.get(entity_id) if entity_id else None
    value = getattr(state, "state", fallback)
    return None if value is None else str(value)


def _state_datetime(hass: Any, entity_id: Any, fallback: Any) -> datetime | None:
    state = hass.states.get(entity_id) if entity_id else None
    return _parse_datetime(getattr(state, "state", fallback))


def _state_is_fresh(state: Any, now: datetime, max_age_seconds: int) -> bool:
    updated = getattr(state, "last_updated", None) if state is not None else None
    return updated is not None and (now - _aware(updated)).total_seconds() <= max_age_seconds


def _phase_limits(raw: dict[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    result = {
        str(key): value
        for key, raw_value in raw.items()
        if (value := _nullable_non_negative(raw_value)) is not None
    }
    return result or None


def _nullable_non_negative(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _aware(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
