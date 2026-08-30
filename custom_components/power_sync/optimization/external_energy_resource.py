"""Pure planning support for finite external energy resources.

This module deliberately has no Home Assistant or provider dependencies.  An
external resource is an *assumption* supplied to the optimizer: it may offset
native-home import, but it cannot charge the stationary battery, export to the
grid, or control an EV.  Provider observation and execution belong to later
layers.

Energy is represented in kWh in the resolved planning types and in Wh in the
configuration/ledger persistence boundary.  Power is represented in kW by the
resolved session and in W by :class:`ExternalEnergyPlan`, matching the
existing optimizer's result convention.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import math
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PLANNING_MODE_IMPORT_OFFSET_ONLY = "import_offset_only"
CONTROL_CAPABILITY_PLANNING_ASSUMPTION = "planning_assumption"
SINK_MODE_IMPORT_OFFSET_ONLY = "import_offset_only"
LEDGER_SCHEMA_VERSION = 1
_EPSILON_KWH = 1e-9
_UTC = timezone.utc


def _finite(value: object, default: float = 0.0) -> float:
    """Return a finite float, or ``default`` for malformed values."""

    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _non_negative(value: object, default: float = 0.0) -> float:
    return max(0.0, _finite(value, default))


def _aware_utc(value: object) -> datetime | None:
    """Normalize an aware datetime to UTC; reject naive/malformed values."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(_UTC)
    except (OverflowError, ValueError):
        return None


def _resolve_timezone(value: object) -> tzinfo | None:
    if isinstance(value, tzinfo):
        return value
    if value is None:
        return _UTC
    token = str(value).strip()
    if not token:
        return None
    if token.upper() == "UTC":
        return _UTC
    try:
        return ZoneInfo(token)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse_local_time(value: object) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        return None
    token = value.strip()
    try:
        parsed = time.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # A recurring local time must not carry an independent offset.
        return None
    return parsed


def _slot_hours(value: object, default: float = 5.0 / 60.0) -> float:
    parsed = _finite(value, default)
    return parsed if parsed > 0 else default


@dataclass(frozen=True, init=False)
class ExternalEnergyResourceConfig:
    """Recurring local-time configuration for one planning-only resource.

    ``usable_energy_wh`` is the maximum net AC energy granted once per
    occurrence of the local-time window.  ``max_power_w`` is the maximum AC
    discharge power.  The aliases accepted by ``__init__`` preserve a small,
    stable boundary for callers while keeping the persisted canonical fields
    unambiguous.
    """

    resource_id: str
    usable_energy_wh: float
    max_power_w: float
    start_local: str | time
    end_local: str | time
    timezone: str | tzinfo = "UTC"
    enabled: bool = True
    loadpoint_id: str | None = None
    config_entry_id: str | None = None
    planning_mode: str = PLANNING_MODE_IMPORT_OFFSET_ONLY
    control_capability: str = CONTROL_CAPABILITY_PLANNING_ASSUMPTION
    sink_mode: str = SINK_MODE_IMPORT_OFFSET_ONLY
    recurrence: str = "daily"

    def __init__(
        self,
        resource_id: str = "",
        usable_energy_wh: float | None = None,
        max_power_w: float | None = None,
        start_local: str | time | None = None,
        end_local: str | time | None = None,
        timezone: str | tzinfo = "UTC",
        enabled: bool = True,
        loadpoint_id: str | None = None,
        config_entry_id: str | None = None,
        planning_mode: str = PLANNING_MODE_IMPORT_OFFSET_ONLY,
        control_capability: str = CONTROL_CAPABILITY_PLANNING_ASSUMPTION,
        sink_mode: str = SINK_MODE_IMPORT_OFFSET_ONLY,
        recurrence: str = "daily",
        *,
        # Compatibility aliases used by early architecture callers.
        usable_ac_kwh: float | None = None,
        max_discharge_w: float | None = None,
        max_discharge_kw: float | None = None,
        max_power_kw: float | None = None,
        start_local_time: str | time | None = None,
        end_local_time: str | time | None = None,
        timezone_token: str | tzinfo | None = None,
        tz: str | tzinfo | None = None,
    ) -> None:
        if usable_energy_wh is None and usable_ac_kwh is not None:
            usable_energy_wh = _finite(usable_ac_kwh) * 1000.0
        if max_power_w is None:
            if max_discharge_w is not None:
                max_power_w = max_discharge_w
            elif max_discharge_kw is not None:
                max_power_w = _finite(max_discharge_kw) * 1000.0
            elif max_power_kw is not None:
                max_power_w = _finite(max_power_kw) * 1000.0
        if start_local is None:
            start_local = start_local_time
        if end_local is None:
            end_local = end_local_time
        if timezone == "UTC":
            timezone = timezone_token if timezone_token is not None else (tz or timezone)
        object.__setattr__(self, "resource_id", str(resource_id or ""))
        object.__setattr__(self, "usable_energy_wh", _finite(usable_energy_wh, float("nan")))
        object.__setattr__(self, "max_power_w", _finite(max_power_w, float("nan")))
        object.__setattr__(self, "start_local", start_local if start_local is not None else "")
        object.__setattr__(self, "end_local", end_local if end_local is not None else "")
        object.__setattr__(self, "timezone", timezone)
        object.__setattr__(self, "enabled", bool(enabled))
        object.__setattr__(self, "loadpoint_id", loadpoint_id)
        object.__setattr__(self, "config_entry_id", config_entry_id)
        object.__setattr__(self, "planning_mode", str(planning_mode or ""))
        object.__setattr__(self, "control_capability", str(control_capability or ""))
        object.__setattr__(self, "sink_mode", str(sink_mode or ""))
        object.__setattr__(self, "recurrence", str(recurrence or "daily").lower())

    @property
    def usable_ac_kwh(self) -> float:
        return max(0.0, self.usable_energy_wh) / 1000.0

    @property
    def max_discharge_kw(self) -> float:
        return max(0.0, self.max_power_w) / 1000.0

    @property
    def max_power_kw(self) -> float:
        return self.max_discharge_kw

    @property
    def max_discharge_w(self) -> float:
        return max(0.0, self.max_power_w)

    def validate(self) -> str | None:
        """Return a reason for invalid input, or ``None`` when valid."""

        if not self.resource_id.strip():
            return "missing_resource_id"
        if not math.isfinite(self.usable_energy_wh) or self.usable_energy_wh < 0:
            return "invalid_usable_energy_wh"
        if not math.isfinite(self.max_power_w) or self.max_power_w <= 0:
            return "invalid_max_power_w"
        if _parse_local_time(self.start_local) is None:
            return "invalid_start_local"
        if _parse_local_time(self.end_local) is None:
            return "invalid_end_local"
        if _resolve_timezone(self.timezone) is None:
            return "invalid_timezone"
        if self.planning_mode != PLANNING_MODE_IMPORT_OFFSET_ONLY:
            return "unsupported_planning_mode"
        if self.sink_mode != SINK_MODE_IMPORT_OFFSET_ONLY:
            return "unsupported_sink_mode"
        if self.recurrence != "daily":
            return "unsupported_recurrence"
        return None


@dataclass(frozen=True)
class ResolvedExternalEnergySession:
    """Immutable, horizon-aligned planning input for one session occurrence."""

    resource_id: str = ""
    session_id: str = ""
    loadpoint_id: str | None = None
    planning_mode: str = PLANNING_MODE_IMPORT_OFFSET_ONLY
    control_capability: str = CONTROL_CAPABILITY_PLANNING_ASSUMPTION
    sink_mode: str = SINK_MODE_IMPORT_OFFSET_ONLY
    remaining_ac_kwh: float = 0.0
    available_slots: tuple[bool, ...] = ()
    max_discharge_kw: tuple[float, ...] = ()
    observation_quality: str = "assumed"
    source_updated_at: datetime | None = None
    session_start_utc: datetime | None = None
    session_end_utc: datetime | None = None
    slot_starts_utc: tuple[datetime, ...] = ()
    slot_hours: tuple[float, ...] = ()
    configured_usable_energy_wh: float | None = None
    config_entry_id: str | None = None

    @property
    def remaining_energy_wh(self) -> float:
        return max(0.0, _finite(self.remaining_ac_kwh)) * 1000.0

    @property
    def max_power_kw(self) -> tuple[float, ...]:
        return self.max_discharge_kw

    @property
    def start_utc(self) -> datetime | None:
        return self.session_start_utc

    @property
    def end_utc(self) -> datetime | None:
        return self.session_end_utc

    def ledger_key(self) -> str:
        return external_energy_session_key(
            self.resource_id,
            self.session_start_utc,
            config_entry_id=self.config_entry_id,
        )

    def valid(self, n_slots: int | None = None) -> bool:
        if not self.resource_id.strip() or not self.session_id.strip():
            return False
        if self.planning_mode != PLANNING_MODE_IMPORT_OFFSET_ONLY:
            return False
        if self.sink_mode != SINK_MODE_IMPORT_OFFSET_ONLY:
            return False
        if not math.isfinite(self.remaining_ac_kwh) or self.remaining_ac_kwh < 0:
            return False
        if self.session_start_utc is not None and _aware_utc(self.session_start_utc) is None:
            return False
        if self.session_end_utc is not None and _aware_utc(self.session_end_utc) is None:
            return False
        if (
            self.session_start_utc is not None
            and self.session_end_utc is not None
            and _aware_utc(self.session_end_utc) <= _aware_utc(self.session_start_utc)
        ):
            return False
        lengths = {len(self.available_slots), len(self.max_discharge_kw)}
        if self.slot_starts_utc:
            lengths.add(len(self.slot_starts_utc))
        if self.slot_hours:
            lengths.add(len(self.slot_hours))
        if n_slots is not None:
            lengths.add(n_slots)
        if len(lengths) != 1:
            return False
        if any(not isinstance(value, bool) for value in self.available_slots):
            return False
        if any(
            not math.isfinite(_finite(value, float("nan")))
            or _finite(value, float("nan")) < 0
            for value in self.max_discharge_kw
        ):
            return False
        if any(
            not math.isfinite(_finite(value, float("nan"))) or _finite(value, float("nan")) < 0
            for value in self.slot_hours
        ):
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        for key in ("source_updated_at", "session_start_utc", "session_end_utc"):
            value = values.get(key)
            if isinstance(value, datetime):
                values[key] = value.isoformat()
        values["available_slots"] = list(self.available_slots)
        values["max_discharge_kw"] = list(self.max_discharge_kw)
        values["slot_starts_utc"] = [
            value.isoformat() for value in self.slot_starts_utc
        ]
        values["slot_hours"] = list(self.slot_hours)
        return values


# Shorter spelling for callers that do not need to distinguish resolved input.
ExternalEnergySession = ResolvedExternalEnergySession


@dataclass(frozen=True)
class ExternalEnergyPlan:
    """Planning output kept separate from stationary-battery actions."""

    resource_id: str = ""
    session_id: str = ""
    planned_discharge_w: tuple[float, ...] = ()
    planned_energy_kwh: float = 0.0
    remaining_after_plan_kwh: float = 0.0
    planning_mode: str = PLANNING_MODE_IMPORT_OFFSET_ONLY
    control_capability: str = CONTROL_CAPABILITY_PLANNING_ASSUMPTION
    reason: str | None = None
    session_start_utc: datetime | None = None

    @property
    def planned_discharge_kw(self) -> tuple[float, ...]:
        return tuple(max(0.0, value) / 1000.0 for value in self.planned_discharge_w)

    @property
    def planned_energy_wh(self) -> float:
        return max(0.0, self.planned_energy_kwh) * 1000.0

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        if isinstance(values.get("session_start_utc"), datetime):
            values["session_start_utc"] = values["session_start_utc"].isoformat()
        values["planned_discharge_w"] = list(self.planned_discharge_w)
        values["planned_discharge_kw"] = list(self.planned_discharge_kw)
        return values


@dataclass(frozen=True)
class ExternalEnergyAllocationResult:
    """Aggregate second-stage allocation and diagnostics."""

    plans: tuple[ExternalEnergyPlan, ...] = ()
    external_power_kw: tuple[float, ...] = ()
    eligible_native_home_import_kw: tuple[float, ...] = ()
    grid_import_without_resource_kw: tuple[float, ...] = ()
    grid_import_with_resource_kw: tuple[float, ...] = ()
    grid_export_without_resource_kw: tuple[float, ...] = ()
    grid_export_with_resource_kw: tuple[float, ...] = ()
    reason: str | None = None

    @property
    def external_energy_kwh(self) -> float:
        # The result does not retain per-session slot lengths; plans carry the
        # authoritative session totals.  Summing them avoids unit ambiguity.
        return sum(plan.planned_energy_kwh for plan in self.plans)

    @property
    def grid_import_reduction_kw(self) -> tuple[float, ...]:
        return tuple(
            max(0.0, before - after)
            for before, after in zip(
                self.grid_import_without_resource_kw,
                self.grid_import_with_resource_kw,
                strict=False,
            )
        )

    def plan_for(self, resource_id: str, session_id: str | None = None) -> ExternalEnergyPlan | None:
        for plan in self.plans:
            if plan.resource_id == resource_id and (
                session_id is None or plan.session_id == session_id
            ):
                return plan
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plans": [plan.as_dict() for plan in self.plans],
            "external_power_kw": list(self.external_power_kw),
            "eligible_native_home_import_kw": list(self.eligible_native_home_import_kw),
            "grid_import_without_resource_kw": list(self.grid_import_without_resource_kw),
            "grid_import_with_resource_kw": list(self.grid_import_with_resource_kw),
            "grid_export_without_resource_kw": list(self.grid_export_without_resource_kw),
            "grid_export_with_resource_kw": list(self.grid_export_with_resource_kw),
            "external_energy_kwh": self.external_energy_kwh,
            "reason": self.reason,
        }


def external_energy_session_key(
    resource_id: object,
    session_start_utc: object,
    *,
    config_entry_id: object | None = None,
) -> str:
    """Return the durable identity for one recurring session occurrence."""

    start = _aware_utc(session_start_utc)
    start_token = start.isoformat() if start is not None else "invalid"
    prefix = str(config_entry_id or "")
    return f"{prefix}|{str(resource_id or '')}|{start_token}"


def _local_session_bounds(
    local_date: date,
    start: time,
    end: time,
    local_zone: tzinfo,
) -> tuple[datetime, datetime] | None:
    start_local = datetime.combine(local_date, start).replace(tzinfo=local_zone)
    end_date = local_date if (end.hour, end.minute, end.second, end.microsecond) > (
        start.hour,
        start.minute,
        start.second,
        start.microsecond,
    ) else local_date + timedelta(days=1)
    end_local = datetime.combine(end_date, end).replace(tzinfo=local_zone)
    start_utc = _aware_utc(start_local)
    end_utc = _aware_utc(end_local)
    if start_utc is None or end_utc is None or end_utc <= start_utc:
        return None
    return start_utc, end_utc


def _date_range_for_horizon(start_utc: datetime, end_utc: datetime, zone: tzinfo) -> range:
    local_start = start_utc.astimezone(zone).date() - timedelta(days=1)
    local_end = end_utc.astimezone(zone).date() + timedelta(days=1)
    return range((local_end - local_start).days + 1)


def expand_external_energy_sessions(
    config: ExternalEnergyResourceConfig,
    horizon_start: datetime,
    horizon_end: datetime,
    *,
    slot_duration: timedelta = timedelta(minutes=5),
) -> tuple[ResolvedExternalEnergySession, ...]:
    """Expand daily local-time windows into UTC-keyed horizon sessions.

    A session is returned when it intersects the horizon, including an active
    cross-midnight session whose local start predates the horizon.  All output
    slot arrays are aligned to the supplied horizon, so multiple resources can
    be safely passed to one allocator.
    """

    reason = config.validate()
    start_utc = _aware_utc(horizon_start)
    end_utc = _aware_utc(horizon_end)
    if reason is not None or not config.enabled or start_utc is None or end_utc is None:
        return ()
    if end_utc <= start_utc or slot_duration.total_seconds() <= 0:
        return ()
    local_zone = _resolve_timezone(config.timezone)
    start_local = _parse_local_time(config.start_local)
    end_local = _parse_local_time(config.end_local)
    if local_zone is None or start_local is None or end_local is None:
        return ()

    step_seconds = slot_duration.total_seconds()
    n_slots = max(0, math.ceil((end_utc - start_utc).total_seconds() / step_seconds))
    slot_starts = tuple(
        start_utc + slot_duration * index for index in range(n_slots)
    )
    slot_hours = tuple(
        max(0.0, min(slot_duration.total_seconds(), (end_utc - value).total_seconds()))
        / 3600.0
        for value in slot_starts
    )
    sessions: list[ResolvedExternalEnergySession] = []
    local_anchor = start_utc.astimezone(local_zone).date() - timedelta(days=1)
    day_count = len(_date_range_for_horizon(start_utc, end_utc, local_zone))
    for offset in range(day_count):
        bounds = _local_session_bounds(
            local_anchor + timedelta(days=offset),
            start_local,
            end_local,
            local_zone,
        )
        if bounds is None:
            continue
        session_start, session_end = bounds
        if session_end <= start_utc or session_start >= end_utc:
            continue
        available: list[bool] = []
        powers: list[float] = []
        session_slot_hours: list[float] = []
        for slot_start, horizon_slot_hours in zip(slot_starts, slot_hours, strict=True):
            slot_end = slot_start + slot_duration
            overlap_seconds = max(
                0.0,
                (min(slot_end, session_end, end_utc) - max(slot_start, session_start, start_utc)).total_seconds(),
            )
            overlap_hours = overlap_seconds / 3600.0
            enabled = overlap_hours > 0.0
            available.append(enabled)
            powers.append(config.max_discharge_kw if enabled else 0.0)
            session_slot_hours.append(overlap_hours if enabled else 0.0)
        session_id = f"{config.resource_id}:{session_start.isoformat()}"
        sessions.append(
            ResolvedExternalEnergySession(
                resource_id=config.resource_id,
                session_id=session_id,
                loadpoint_id=config.loadpoint_id,
                planning_mode=config.planning_mode,
                control_capability=config.control_capability,
                sink_mode=config.sink_mode,
                remaining_ac_kwh=config.usable_ac_kwh,
                available_slots=tuple(available),
                max_discharge_kw=tuple(powers),
                observation_quality="assumed",
                session_start_utc=session_start,
                session_end_utc=session_end,
                slot_starts_utc=slot_starts,
                slot_hours=tuple(session_slot_hours),
                configured_usable_energy_wh=config.usable_energy_wh,
                config_entry_id=config.config_entry_id,
            )
        )
    return tuple(sorted(sessions, key=lambda item: (item.session_start_utc or datetime.min.replace(tzinfo=_UTC), item.resource_id, item.session_id)))


def resolve_external_energy_sessions(
    configs: Iterable[ExternalEnergyResourceConfig],
    horizon_start: datetime,
    horizon_end: datetime,
    *,
    slot_duration: timedelta = timedelta(minutes=5),
    ledger: "ExternalEnergyLedgerState | Mapping[str, Any] | None" = None,
) -> tuple[ResolvedExternalEnergySession, ...]:
    """Expand configs and apply persisted per-session inventory, if supplied."""

    state = ExternalEnergyLedgerState.from_dict(ledger) if isinstance(ledger, Mapping) else ledger
    sessions: list[ResolvedExternalEnergySession] = []
    for config in configs:
        for session in expand_external_energy_sessions(
            config, horizon_start, horizon_end, slot_duration=slot_duration
        ):
            if state is not None:
                remaining = state.remaining_ac_kwh(session)
                session = replace(session, remaining_ac_kwh=remaining)
            sessions.append(session)
    return tuple(sessions)


# Compatibility names used by coordinator prototypes during the planning-only
# slice.  They intentionally point at the same pure implementations.
ExternalEnergyResource = ExternalEnergyResourceConfig


def _normalized_series(values: Sequence[object] | None, n_slots: int, *, default: float = 0.0) -> tuple[float, ...]:
    if values is None:
        return tuple(default for _ in range(n_slots))
    return tuple(
        max(0.0, _finite(value, default)) if math.isfinite(_finite(value, default)) else default
        for value in list(values)[:n_slots]
    ) + tuple(default for _ in range(max(0, n_slots - len(values))))


def allocate_external_energy(
    sessions: Iterable[ResolvedExternalEnergySession],
    eligible_native_home_import_kw: Sequence[object] | None = None,
    avoided_import_price: Sequence[object] | None = None,
    *,
    slot_duration_hours: float = 5.0 / 60.0,
    planned_ev_charge_kw: Sequence[object] | None = None,
    native_home_import_kw: Sequence[object] | None = None,
    eligible_import_kw: Sequence[object] | None = None,
    avoided_import_price_c_per_kwh: Sequence[object] | None = None,
    grid_import_without_resource_kw: Sequence[object] | None = None,
    grid_export_without_resource_kw: Sequence[object] | None = None,
) -> ExternalEnergyAllocationResult:
    """Allocate finite resources against eligible native-home import only.

    Slots are ordered by avoided import price descending, then chronologically,
    with stable resource/session identity as a final tie-breaker.  Overlapping
    sessions have independent budgets but share the native-home import
    capacity, preventing two resources from covering the same load twice.
    """

    resolved = tuple(sessions)
    n_slots = 0
    for session in resolved:
        n_slots = max(n_slots, len(session.available_slots), len(session.max_discharge_kw))
    if eligible_import_kw is not None:
        eligible_native_home_import_kw = eligible_import_kw
    if avoided_import_price_c_per_kwh is not None:
        avoided_import_price = avoided_import_price_c_per_kwh
    if native_home_import_kw is not None:
        eligible_native_home_import_kw = native_home_import_kw
    if n_slots == 0:
        return ExternalEnergyAllocationResult(reason="empty_horizon")
    eligible = list(_normalized_series(eligible_native_home_import_kw, n_slots))
    # A caller providing a raw native-home series can still provide EV demand;
    # subtracting it is conservative and guarantees EV load is never supplied.
    if planned_ev_charge_kw is not None:
        ev = _normalized_series(planned_ev_charge_kw, n_slots)
        eligible = [max(0.0, value - ev[index]) for index, value in enumerate(eligible)]
    prices = _normalized_series(avoided_import_price, n_slots)
    base_import = _normalized_series(grid_import_without_resource_kw, n_slots)
    base_export = _normalized_series(grid_export_without_resource_kw, n_slots)
    aggregate = [0.0] * n_slots
    allocations: dict[str, list[float]] = {}
    valid_sessions: list[ResolvedExternalEnergySession] = []
    for session in resolved:
        if session.valid(n_slots) and session.remaining_ac_kwh > _EPSILON_KWH:
            valid_sessions.append(session)
            allocations[session.ledger_key()] = [0.0] * n_slots

    candidates: list[tuple[float, int, str, ResolvedExternalEnergySession]] = []
    for session in valid_sessions:
        key = session.ledger_key()
        for index, (available, max_power) in enumerate(
            zip(session.available_slots, session.max_discharge_kw, strict=True)
        ):
            if available and _finite(max_power) > 0 and eligible[index] > 0:
                candidates.append((-prices[index], index, key, session))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, index, key, session in candidates:
        session_hours = (
            _slot_hours(session.slot_hours[index])
            if session.slot_hours and index < len(session.slot_hours)
            else _slot_hours(slot_duration_hours)
        )
        if session_hours <= 0:
            continue
        already_planned_kwh = sum(
            power * (
                _slot_hours(session.slot_hours[position])
                if session.slot_hours and position < len(session.slot_hours)
                else _slot_hours(slot_duration_hours)
            )
            for position, power in enumerate(allocations[key])
        )
        remaining_kwh = max(0.0, session.remaining_ac_kwh - already_planned_kwh)
        if remaining_kwh <= _EPSILON_KWH:
            continue
        import_headroom_kw = max(0.0, eligible[index] - aggregate[index])
        power_kw = min(
            max(0.0, _finite(session.max_discharge_kw[index])),
            import_headroom_kw,
            remaining_kwh / session_hours,
        )
        if power_kw <= 0:
            continue
        allocations[key][index] = power_kw
        aggregate[index] += power_kw

    plans: list[ExternalEnergyPlan] = []
    for session in resolved:
        key = session.ledger_key()
        powers_kw = allocations.get(key, [0.0] * n_slots)
        powers_w = tuple(max(0.0, power) * 1000.0 for power in powers_kw)
        total_kwh = sum(
            power * (
                _slot_hours(session.slot_hours[index])
                if session.slot_hours and index < len(session.slot_hours)
                else _slot_hours(slot_duration_hours)
            )
            for index, power in enumerate(powers_kw)
        )
        if not session.valid(n_slots):
            reason = "invalid_session"
            total_kwh = 0.0
            powers_w = tuple(0.0 for _ in range(n_slots))
        elif session.planning_mode != PLANNING_MODE_IMPORT_OFFSET_ONLY or session.sink_mode != SINK_MODE_IMPORT_OFFSET_ONLY:
            reason = "unsupported_sink_mode"
            total_kwh = 0.0
            powers_w = tuple(0.0 for _ in range(n_slots))
        elif total_kwh <= _EPSILON_KWH:
            reason = "no_eligible_native_home_import"
        else:
            reason = None
        plans.append(
            ExternalEnergyPlan(
                resource_id=session.resource_id,
                session_id=session.session_id,
                planned_discharge_w=powers_w,
                planned_energy_kwh=total_kwh,
                remaining_after_plan_kwh=max(0.0, session.remaining_ac_kwh - total_kwh),
                planning_mode=session.planning_mode,
                control_capability=session.control_capability,
                reason=reason,
                session_start_utc=session.session_start_utc,
            )
        )
    with_import = tuple(max(0.0, base_import[index] - aggregate[index]) for index in range(n_slots))
    return ExternalEnergyAllocationResult(
        plans=tuple(plans),
        external_power_kw=tuple(aggregate),
        eligible_native_home_import_kw=tuple(eligible),
        grid_import_without_resource_kw=tuple(base_import),
        grid_import_with_resource_kw=with_import,
        grid_export_without_resource_kw=tuple(base_export),
        grid_export_with_resource_kw=tuple(base_export),
        reason=None,
    )


@dataclass(frozen=True)
class ExternalEnergyLedgerEntry:
    """Monotonic persisted settlement for one resource/session identity."""

    key: str
    resource_id: str
    session_id: str
    session_start_utc: str | None
    configured_energy_wh: float
    consumed_energy_wh: float = 0.0
    settled_slots: tuple[tuple[str, float], ...] = ()
    planned_slots: tuple[tuple[str, float], ...] = ()
    measured_slots: tuple[str, ...] = ()
    corrupt: bool = False
    reason: str | None = None

    @property
    def remaining_energy_wh(self) -> float:
        if self.corrupt:
            return 0.0
        return max(0.0, self.configured_energy_wh - self.consumed_energy_wh)

    @property
    def consumed_wh(self) -> float:
        return self.consumed_energy_wh

    @property
    def used_energy_wh(self) -> float:
        return self.consumed_energy_wh

    @property
    def remaining_ac_kwh(self) -> float:
        return self.remaining_energy_wh / 1000.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "resource_id": self.resource_id,
            "session_id": self.session_id,
            "session_start_utc": self.session_start_utc,
            "configured_energy_wh": self.configured_energy_wh,
            "consumed_energy_wh": self.consumed_energy_wh,
            "settled_slots": {slot: energy for slot, energy in self.settled_slots},
            "planned_slots": {slot: energy for slot, energy in self.planned_slots},
            "measured_slots": list(self.measured_slots),
            "corrupt": self.corrupt,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExternalEnergyLedgerState:
    """Serializable immutable ledger containing independent session entries."""

    schema_version: int = LEDGER_SCHEMA_VERSION
    entries: tuple[ExternalEnergyLedgerEntry, ...] = ()
    corrupt: bool = False
    reason: str | None = None

    def entry_for(self, session: ResolvedExternalEnergySession) -> ExternalEnergyLedgerEntry | None:
        key = session.ledger_key()
        return next((entry for entry in self.entries if entry.key == key), None)

    def remaining_ac_kwh(self, session: ResolvedExternalEnergySession) -> float:
        entry = self.entry_for(session)
        if self.corrupt or (entry is not None and entry.corrupt):
            return 0.0
        return session.remaining_ac_kwh if entry is None else entry.remaining_ac_kwh

    def remaining_energy_wh(self, session: ResolvedExternalEnergySession) -> float:
        return self.remaining_ac_kwh(session) * 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": {entry.key: entry.as_dict() for entry in self.entries},
            "corrupt": self.corrupt,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(
        cls,
        raw: "ExternalEnergyLedgerState | Mapping[str, Any] | None",
    ) -> "ExternalEnergyLedgerState":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            return cls(corrupt=True, reason="invalid_ledger_state")
        version = raw.get("schema_version", LEDGER_SCHEMA_VERSION)
        if version != LEDGER_SCHEMA_VERSION:
            return cls(corrupt=True, reason="unsupported_ledger_schema")
        raw_entries = raw.get("entries", {})
        if isinstance(raw_entries, Mapping):
            values = list(raw_entries.values())
        elif isinstance(raw_entries, Sequence) and not isinstance(raw_entries, (str, bytes, bytearray)):
            values = list(raw_entries)
        else:
            return cls(corrupt=True, reason="invalid_ledger_entries")
        entries: list[ExternalEnergyLedgerEntry] = []
        corrupt = bool(raw.get("corrupt", False))
        reason = raw.get("reason")
        for item in values:
            if not isinstance(item, Mapping):
                corrupt = True
                reason = reason or "invalid_ledger_entry"
                continue
            try:
                key = str(item.get("key") or "")
                resource_id = str(item.get("resource_id") or "")
                session_id = str(item.get("session_id") or "")
                configured = _finite(item.get("configured_energy_wh"), float("nan"))
                consumed = _finite(item.get("consumed_energy_wh"), float("nan"))
                if not key or not resource_id or not session_id or not math.isfinite(configured) or configured < 0 or not math.isfinite(consumed) or consumed < 0:
                    raise ValueError("invalid ledger entry values")
                settled = _parse_slot_energy(item.get("settled_slots", {}))
                planned = _parse_slot_energy(item.get("planned_slots", {}))
                raw_measured = item.get("measured_slots", ())
                if isinstance(raw_measured, str) or not isinstance(raw_measured, Sequence):
                    raise ValueError("invalid measured slot list")
                measured_slots = tuple(sorted({str(slot) for slot in raw_measured if str(slot)}))
                entries.append(
                    ExternalEnergyLedgerEntry(
                        key=key,
                        resource_id=resource_id,
                        session_id=session_id,
                        session_start_utc=item.get("session_start_utc"),
                        configured_energy_wh=configured,
                        consumed_energy_wh=min(configured, consumed),
                        settled_slots=tuple(sorted(settled.items())),
                        planned_slots=tuple(sorted(planned.items())),
                        measured_slots=measured_slots,
                        corrupt=bool(item.get("corrupt", False)),
                        reason=item.get("reason"),
                    )
                )
            except (TypeError, ValueError):
                corrupt = True
                reason = reason or "invalid_ledger_entry"
        return cls(entries=tuple(entries), corrupt=corrupt, reason=reason)


# The shorter name is convenient for code that treats the state as a ledger.
ExternalEnergyLedger = ExternalEnergyLedgerState


def _parse_slot_energy(raw: object) -> dict[str, float]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        values = raw.items()
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        values = raw
    else:
        raise ValueError("invalid slot energy map")
    result: dict[str, float] = {}
    for item in values:
        if isinstance(raw, Mapping):
            slot, energy = item
        else:
            if not isinstance(item, Sequence) or len(item) != 2:
                raise ValueError("invalid slot energy item")
            slot, energy = item
        token = str(slot)
        parsed = _finite(energy, float("nan"))
        if not token or not math.isfinite(parsed) or parsed < 0:
            raise ValueError("invalid slot energy")
        result[token] = parsed
    return result


def _entry_for_state(
    state: ExternalEnergyLedgerState,
    session: ResolvedExternalEnergySession,
) -> ExternalEnergyLedgerEntry | None:
    return state.entry_for(session)


def _slot_key(session: ResolvedExternalEnergySession, index: int, slot_duration_hours: float) -> str:
    if session.slot_starts_utc and index < len(session.slot_starts_utc):
        value = _aware_utc(session.slot_starts_utc[index])
    elif session.session_start_utc is not None:
        value = _aware_utc(session.session_start_utc) + timedelta(hours=slot_duration_hours * index)
    else:
        value = None
    return value.isoformat() if value is not None else str(index)


def _energy_series(
    values: Sequence[object] | Mapping[object, object] | None,
    session: ResolvedExternalEnergySession,
    n_slots: int,
    slot_duration_hours: float,
) -> dict[str, float]:
    if values is None:
        return {}
    result: dict[str, float] = {}
    if isinstance(values, Mapping):
        items = values.items()
        for slot, value in items:
            parsed = _finite(value, float("nan"))
            if math.isfinite(parsed) and parsed >= 0:
                result[str(slot)] = parsed
        return result
    for index, value in enumerate(list(values)[:n_slots]):
        parsed = _finite(value, float("nan"))
        if math.isfinite(parsed) and parsed >= 0:
            result[_slot_key(session, index, slot_duration_hours)] = parsed
    return result


def _elapsed_indices(
    session: ResolvedExternalEnergySession,
    n_slots: int,
    now: datetime | None,
    slot_duration_hours: float,
) -> tuple[int, ...]:
    if now is None:
        return tuple(range(n_slots))
    now_utc = _aware_utc(now)
    if now_utc is None:
        return ()
    elapsed: list[int] = []
    for index in range(n_slots):
        if session.slot_starts_utc and index < len(session.slot_starts_utc):
            slot_start = _aware_utc(session.slot_starts_utc[index])
        elif session.session_start_utc is not None:
            start = _aware_utc(session.session_start_utc)
            slot_start = start + timedelta(hours=slot_duration_hours * index) if start else None
        else:
            slot_start = None
        if slot_start is None:
            continue
        slot_hours = (
            _slot_hours(session.slot_hours[index])
            if session.slot_hours and index < len(session.slot_hours) and session.slot_hours[index] > 0
            else _slot_hours(slot_duration_hours)
        )
        if slot_start + timedelta(hours=slot_hours) <= now_utc:
            elapsed.append(index)
    return tuple(elapsed)


def _replace_entry(
    state: ExternalEnergyLedgerState,
    entry: ExternalEnergyLedgerEntry,
) -> ExternalEnergyLedgerState:
    entries = [item for item in state.entries if item.key != entry.key]
    entries.append(entry)
    entries.sort(key=lambda item: item.key)
    return replace(state, entries=tuple(entries))


def reduce_external_energy_ledger(
    session: ResolvedExternalEnergySession | ExternalEnergyLedgerState | Mapping[str, Any],
    state: ExternalEnergyLedgerState | ResolvedExternalEnergySession | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    planned_discharge_w: Sequence[object] | Mapping[object, object] | None = None,
    planned_energy_wh: Sequence[object] | Mapping[object, object] | None = None,
    measured_energy_wh: Sequence[object] | Mapping[object, object] | None = None,
    measured_power_w: Sequence[object] | Mapping[object, object] | None = None,
    slot_duration_hours: float = 5.0 / 60.0,
) -> ExternalEnergyLedgerState:
    """Reduce one session's durable consumption ledger without side effects.

    For each elapsed slot, validated measured energy supersedes the prior
    planned estimate.  If no measurement exists, the previous/current plan is
    conservatively settled.  Once settled, energy is monotonic: a delayed
    lower measurement cannot replenish inventory or undo the fallback.

    The argument order accepts both ``(session, state)`` and ``(state,
    session)`` to keep migration callers simple.  Malformed active state is
    retained as corrupt and resolves to zero remaining energy.
    """

    if isinstance(session, (ExternalEnergyLedgerState, Mapping)) and isinstance(state, ResolvedExternalEnergySession):
        session, state = state, session
    if not isinstance(session, ResolvedExternalEnergySession):
        return ExternalEnergyLedgerState(corrupt=True, reason="invalid_session")
    ledger = ExternalEnergyLedgerState.from_dict(state if state is not None else None)
    if ledger.corrupt:
        existing = ledger.entry_for(session)
        if existing is None:
            configured = session.configured_usable_energy_wh
            configured = session.remaining_energy_wh if configured is None else max(0.0, _finite(configured))
            existing = ExternalEnergyLedgerEntry(
                key=session.ledger_key(),
                resource_id=session.resource_id,
                session_id=session.session_id,
                session_start_utc=(session.session_start_utc.isoformat() if session.session_start_utc else None),
                configured_energy_wh=configured,
                corrupt=True,
                reason=ledger.reason or "corrupt_active_ledger",
            )
        elif not existing.corrupt:
            existing = replace(existing, corrupt=True, reason=ledger.reason or "corrupt_active_ledger")
        return _replace_entry(ledger, existing)
    if not session.valid(len(session.available_slots)):
        existing = ledger.entry_for(session)
        if existing is None:
            existing = ExternalEnergyLedgerEntry(
                key=session.ledger_key(),
                resource_id=session.resource_id,
                session_id=session.session_id,
                session_start_utc=(session.session_start_utc.isoformat() if session.session_start_utc else None),
                configured_energy_wh=max(0.0, session.remaining_energy_wh),
                corrupt=True,
                reason="invalid_session",
            )
        else:
            existing = replace(existing, corrupt=True, reason="invalid_session")
        return _replace_entry(ledger, existing)

    n_slots = len(session.available_slots)
    slot_hours_default = _slot_hours(slot_duration_hours)
    current_planned = _energy_series(planned_energy_wh, session, n_slots, slot_hours_default)
    if planned_discharge_w is not None:
        if isinstance(planned_discharge_w, Mapping):
            current_planned = {
                str(slot): max(0.0, _finite(power)) * slot_hours_default
                for slot, power in planned_discharge_w.items()
                if math.isfinite(_finite(power)) and _finite(power) >= 0
            }
        else:
            current_planned = {}
            for index, power in enumerate(list(planned_discharge_w)[:n_slots]):
                parsed = _finite(power, float("nan"))
                if math.isfinite(parsed) and parsed >= 0:
                    hours = (
                        _slot_hours(session.slot_hours[index])
                        if session.slot_hours and index < len(session.slot_hours)
                        else slot_hours_default
                    )
                    current_planned[_slot_key(session, index, slot_hours_default)] = parsed * hours
    measured = _energy_series(measured_energy_wh, session, n_slots, slot_hours_default)
    if measured_power_w is not None:
        power_measurements = _energy_series(measured_power_w, session, n_slots, slot_hours_default)
        measured = {
            slot: power * (
                _slot_hours(session.slot_hours[index])
                if session.slot_hours and index < len(session.slot_hours)
                else slot_hours_default
            )
            for index, slot in enumerate(
                _slot_key(session, position, slot_hours_default) for position in range(n_slots)
            )
            if slot in power_measurements
            for power in (power_measurements[slot],)
        }

    existing = ledger.entry_for(session)
    configured = max(0.0, _finite(session.configured_usable_energy_wh, session.remaining_energy_wh))
    if existing is not None:
        # A config reduction may lower remaining inventory, but a later larger
        # setting never grants energy back to an existing session occurrence.
        configured = min(configured, existing.configured_energy_wh)
    settled = dict(existing.settled_slots) if existing is not None else {}
    previous_planned = dict(existing.planned_slots) if existing is not None else {}
    measured_slots = set(existing.measured_slots) if existing is not None else set()
    # A supplied plan replaces only future assumptions.  Already-settled slots
    # stay immutable and absent input preserves the previous conservative plan.
    has_current_plan = planned_discharge_w is not None or planned_energy_wh is not None
    merged_planned = previous_planned if not has_current_plan else current_planned
    settlement_planned = dict(previous_planned)
    settlement_planned.update(current_planned)
    elapsed = _elapsed_indices(session, n_slots, now, slot_hours_default)
    for index in elapsed:
        slot = _slot_key(session, index, slot_hours_default)
        if slot in measured:
            candidate = measured[slot]
            measured_slots.add(slot)
        elif slot in measured_slots:
            # A measured value already settled for this slot remains the
            # authoritative value.  A later solve with no telemetry must not
            # replace it with the old planned fallback.
            continue
        else:
            candidate = settlement_planned.get(slot, 0.0)
        candidate = max(0.0, _finite(candidate))
        # max() makes settlement monotonic and prevents delayed telemetry from
        # undoing a conservative planned fallback.
        settled[slot] = max(settled.get(slot, 0.0), candidate)
    # A rolling horizon drops its oldest slot on every solve. Settle any
    # previously planned timestamp that has elapsed even when it is no longer
    # present in the newly aligned session arrays; otherwise the active session
    # would quietly regain that energy on every re-plan.
    now_utc = _aware_utc(now)
    if now_utc is not None:
        for slot, candidate in previous_planned.items():
            try:
                slot_start = datetime.fromisoformat(slot)
            except (TypeError, ValueError):
                continue
            slot_start = _aware_utc(slot_start)
            if (
                slot_start is None
                or slot_start + timedelta(hours=slot_hours_default) > now_utc
                or slot in measured_slots
            ):
                continue
            settled[slot] = max(
                settled.get(slot, 0.0),
                max(0.0, _finite(candidate)),
            )
    consumed = min(configured, sum(max(0.0, value) for value in settled.values()))
    entry = ExternalEnergyLedgerEntry(
        key=session.ledger_key(),
        resource_id=session.resource_id,
        session_id=session.session_id,
        session_start_utc=(session.session_start_utc.isoformat() if session.session_start_utc else None),
        configured_energy_wh=configured,
        consumed_energy_wh=consumed,
        settled_slots=tuple(sorted(settled.items())),
        planned_slots=tuple(sorted(merged_planned.items())),
        measured_slots=tuple(sorted(measured_slots)),
        corrupt=False,
        reason=None,
    )
    return _replace_entry(ledger, entry)


def settle_external_energy_ledger(*args: Any, **kwargs: Any) -> ExternalEnergyLedgerState:
    """Alias emphasizing that the reducer is a settlement operation."""

    return reduce_external_energy_ledger(*args, **kwargs)


def reduce_external_energy_state(*args: Any, **kwargs: Any) -> ExternalEnergyLedgerState:
    return reduce_external_energy_ledger(*args, **kwargs)


expand_sessions = expand_external_energy_sessions
resolve_sessions = resolve_external_energy_sessions
allocate_external_energy_resource = allocate_external_energy


__all__ = [
    "CONTROL_CAPABILITY_PLANNING_ASSUMPTION",
    "ExternalEnergyAllocationResult",
    "ExternalEnergyLedger",
    "ExternalEnergyLedgerEntry",
    "ExternalEnergyLedgerState",
    "ExternalEnergyPlan",
    "ExternalEnergyResourceConfig",
    "ExternalEnergyResource",
    "ExternalEnergySession",
    "LEDGER_SCHEMA_VERSION",
    "PLANNING_MODE_IMPORT_OFFSET_ONLY",
    "ResolvedExternalEnergySession",
    "SINK_MODE_IMPORT_OFFSET_ONLY",
    "allocate_external_energy",
    "allocate_external_energy_resource",
    "expand_external_energy_sessions",
    "expand_sessions",
    "external_energy_session_key",
    "reduce_external_energy_ledger",
    "reduce_external_energy_state",
    "resolve_external_energy_sessions",
    "resolve_sessions",
    "settle_external_energy_ledger",
]
