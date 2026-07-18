"""Provider-neutral measured-energy quota settlement.

Quota tariffs are settled from telemetry only.  Forecasts may reserve a marginal
bucket for planning, but they never mutate :class:`QuotaLedgerState`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Literal

Direction = Literal["import", "export"]
Confidence = Literal["authoritative", "estimated", "unknown"]

QUOTA_STATE_VERSION = 2
DEFAULT_CONTINUITY_SECONDS = 10 * 60
RESET_BASELINE_GRACE_SECONDS = 10 * 60


@dataclass(frozen=True)
class QuotaRule:
    """A capped marginal-price rule within a tariff day."""

    rule_id: str
    direction: Direction
    timezone_token: str
    windows: tuple[tuple[str, str], ...]
    daily_cap_kwh: float
    base_price_c_per_kwh: float
    bonus_price_c_per_kwh: float
    settlement_source: str = "pcc_energy"
    reset_policy: str = "tariff_day"

    def contains(self, value: datetime) -> bool:
        local = tariff_datetime(value, self.timezone_token)
        minute = local.hour * 60 + local.minute
        return any(_minute_in_window(minute, start, end) for start, end in self.windows)

    def effective_price_c_per_kwh(self, bonus_available: bool) -> float:
        if not bonus_available:
            return self.base_price_c_per_kwh
        if self.direction == "import":
            return max(0.0, self.base_price_c_per_kwh - self.bonus_price_c_per_kwh)
        return self.base_price_c_per_kwh + self.bonus_price_c_per_kwh


@dataclass(frozen=True)
class MarginalBucket:
    """Non-persistent planning view of a quota bucket."""

    rule_id: str
    direction: Direction
    cap_kwh: float
    settled_kwh: float
    remaining_kwh: float
    planned_kwh: float
    base_price_c_per_kwh: float
    bonus_price_c_per_kwh: float
    effective_price_c_per_kwh: float
    confidence: Confidence


@dataclass
class QuotaLedgerState:
    """Serializable settlement state for a single tariff day."""

    schema_version: int = QUOTA_STATE_VERSION
    tariff_day: str | None = None
    timezone_token: str = "AEST"
    confidence: Confidence = "unknown"
    settled_kwh: dict[str, float] = field(default_factory=dict)
    last_meter_kwh: dict[Direction, float | None] = field(
        default_factory=lambda: {"import": None, "export": None}
    )
    last_sample_at: dict[Direction, str | None] = field(
        default_factory=lambda: {"import": None, "export": None}
    )
    source_kind: dict[Direction, str | None] = field(
        default_factory=lambda: {"import": None, "export": None}
    )
    reset_seen: dict[Direction, bool] = field(
        default_factory=lambda: {"import": False, "export": False}
    )
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "QuotaLedgerState":
        raw = raw or {}
        state = cls(
            schema_version=QUOTA_STATE_VERSION,
            tariff_day=raw.get("tariff_day"),
            timezone_token=str(raw.get("timezone_token") or "AEST"),
            confidence=_confidence(raw.get("confidence")),
            settled_kwh={
                str(key): max(0.0, _float(value))
                for key, value in (raw.get("settled_kwh") or {}).items()
            },
            reason=raw.get("reason"),
        )
        for direction in ("import", "export"):
            value = (raw.get("last_meter_kwh") or {}).get(direction)
            state.last_meter_kwh[direction] = None if value is None else _float(value)
            state.last_sample_at[direction] = (raw.get("last_sample_at") or {}).get(direction)
            state.source_kind[direction] = (raw.get("source_kind") or {}).get(direction)
            state.reset_seen[direction] = bool(
                (raw.get("reset_seen") or {}).get(direction, False)
            )
        return state


class QuotaLedger:
    """Settle quota rules from cumulative energy meters or continuous power."""

    def __init__(
        self,
        rules: Iterable[QuotaRule],
        state: QuotaLedgerState | None = None,
        *,
        continuity_seconds: int = DEFAULT_CONTINUITY_SECONDS,
    ) -> None:
        self.rules = tuple(rules)
        self.state = state or QuotaLedgerState()
        self.continuity_seconds = max(60, int(continuity_seconds))
        if self.rules:
            self.state.timezone_token = self.rules[0].timezone_token

    def observe_cumulative(
        self,
        direction: Direction,
        total_kwh: float,
        observed_at: datetime,
    ) -> float:
        """Settle a monotonic PCC energy reading; return newly eligible kWh."""
        observed_at = _aware(observed_at)
        total_kwh = max(0.0, _float(total_kwh))
        self._rollover_if_needed(observed_at)
        previous_at = _parse_datetime(self.state.last_sample_at[direction])
        previous_total = self.state.last_meter_kwh[direction]

        if previous_at is not None and observed_at < previous_at:
            return 0.0
        if previous_at is not None and observed_at == previous_at:
            if previous_total is not None and abs(total_kwh - previous_total) > 1e-9:
                self._mark_unknown("corrected reading reused an existing timestamp")
            return 0.0

        self.state.source_kind[direction] = "total_increasing"

        if previous_total is None or previous_at is None:
            self.state.last_sample_at[direction] = observed_at.isoformat()
            self.state.last_meter_kwh[direction] = total_kwh
            self._establish_baseline(direction, observed_at, authoritative=True)
            return 0.0
        if abs(total_kwh - previous_total) <= 1e-12:
            # Polling an unchanged monotonic total proves the entity is
            # available (and can establish a new-day baseline), but it is not
            # a new energy timestamp. Retain the last changing sample so a
            # delayed update is apportioned across its real interval.
            return 0.0
        if total_kwh < previous_total - 1e-9:
            self._mark_unknown("cumulative energy meter reset or decreased")
            return 0.0

        delta = max(0.0, total_kwh - previous_total)
        self.state.last_sample_at[direction] = observed_at.isoformat()
        self.state.last_meter_kwh[direction] = total_kwh
        return self._settle_interval(direction, previous_at, observed_at, delta)

    def observe_power(
        self,
        direction: Direction,
        power_w: float,
        observed_at: datetime,
    ) -> float:
        """Settle a power-integrated estimate while samples remain continuous."""
        observed_at = _aware(observed_at)
        self._rollover_if_needed(observed_at)
        previous_at = _parse_datetime(self.state.last_sample_at[direction])
        self.state.last_sample_at[direction] = observed_at.isoformat()
        self.state.last_meter_kwh[direction] = None
        self.state.source_kind[direction] = "power_integrated"
        if self.state.confidence == "authoritative":
            self.state.confidence = "estimated"
            self.state.reason = "quota settlement switched to integrated power"

        if previous_at is None:
            self._establish_baseline(direction, observed_at, authoritative=False)
            return 0.0
        elapsed = (observed_at - previous_at).total_seconds()
        if elapsed <= 0:
            return 0.0
        if elapsed > self.continuity_seconds:
            self._mark_unknown("power telemetry gap")
            return 0.0
        energy_kwh = max(0.0, _float(power_w)) * elapsed / 3_600_000.0
        return self._settle_interval(direction, previous_at, observed_at, energy_kwh)

    def remaining_kwh(self, rule_id: str) -> float:
        rule = self._rule(rule_id)
        return max(0.0, rule.daily_cap_kwh - self.state.settled_kwh.get(rule_id, 0.0))

    def advance_to(self, observed_at: datetime) -> None:
        """Advance the ledger to the tariff day containing ``observed_at``.

        Forecasting and status reads can happen before the first telemetry
        sample of a new day.  Advancing explicitly prevents yesterday's quota
        balance from leaking into those reads while preserving the rule that a
        fresh baseline is required before any new-day bonus is trusted.
        """
        self._rollover_if_needed(_aware(observed_at))

    def mark_unknown(self, reason: str) -> None:
        """Conservatively disable marginal benefits after telemetry loss."""
        self._mark_unknown(reason)

    def bucket(self, rule_id: str, planned_kwh: float = 0.0) -> MarginalBucket:
        rule = self._rule(rule_id)
        settled = min(rule.daily_cap_kwh, self.state.settled_kwh.get(rule_id, 0.0))
        remaining = max(0.0, rule.daily_cap_kwh - settled)
        planned = min(remaining, max(0.0, _float(planned_kwh)))
        bonus_available = self.state.confidence != "unknown" and remaining > 1e-9
        return MarginalBucket(
            rule_id=rule.rule_id,
            direction=rule.direction,
            cap_kwh=rule.daily_cap_kwh,
            settled_kwh=settled,
            remaining_kwh=remaining,
            planned_kwh=planned,
            base_price_c_per_kwh=rule.base_price_c_per_kwh,
            bonus_price_c_per_kwh=rule.bonus_price_c_per_kwh,
            effective_price_c_per_kwh=rule.effective_price_c_per_kwh(bonus_available),
            confidence=self.state.confidence,
        )

    def _settle_interval(
        self,
        direction: Direction,
        start: datetime,
        end: datetime,
        energy_kwh: float,
    ) -> float:
        duration = (end - start).total_seconds()
        if duration <= 0 or energy_kwh <= 0:
            return 0.0
        newly_eligible = 0.0
        for rule in self.rules:
            if rule.direction != direction:
                continue
            seconds = _window_overlap_seconds(start, end, rule)
            if seconds <= 0:
                continue
            allocated = energy_kwh * min(1.0, seconds / duration)
            old = self.state.settled_kwh.get(rule.rule_id, 0.0)
            new = min(rule.daily_cap_kwh, old + allocated)
            self.state.settled_kwh[rule.rule_id] = new
            newly_eligible += max(0.0, new - old)
        return newly_eligible

    def _rollover_if_needed(self, observed_at: datetime) -> None:
        day = tariff_datetime(observed_at, self.state.timezone_token).date().isoformat()
        if self.state.tariff_day == day:
            return
        self.state.tariff_day = day
        self.state.settled_kwh = {rule.rule_id: 0.0 for rule in self.rules}
        self.state.last_meter_kwh = {"import": None, "export": None}
        self.state.last_sample_at = {"import": None, "export": None}
        self.state.source_kind = {"import": None, "export": None}
        self.state.reset_seen = {"import": False, "export": False}
        self.state.confidence = "unknown"
        self.state.reason = "awaiting tariff-day baseline"

    def _establish_baseline(
        self,
        direction: Direction,
        observed_at: datetime,
        *,
        authoritative: bool,
    ) -> None:
        local = tariff_datetime(observed_at, self.state.timezone_token)
        day_start = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
        seconds_after_reset = (local - day_start).total_seconds()
        eligible_usage_could_have_elapsed = any(
            rule.direction == direction
            and _window_overlap_seconds(day_start, local, rule) > 0
            for rule in self.rules
        )
        if (
            seconds_after_reset <= RESET_BASELINE_GRACE_SECONDS
            or not eligible_usage_could_have_elapsed
        ):
            self.state.reset_seen[direction] = True
            if all(self.state.reset_seen.values()):
                previously_estimated = self.state.confidence == "estimated"
                all_authoritative = all(
                    self.state.source_kind.get(item) == "total_increasing"
                    for item in ("import", "export")
                )
                self.state.confidence = (
                    "authoritative"
                    if all_authoritative and not previously_estimated
                    else "estimated"
                )
                if not previously_estimated:
                    self.state.reason = None
        else:
            self._mark_unknown(
                "first sample arrived after eligible quota usage could begin"
            )

    def _mark_unknown(self, reason: str) -> None:
        self.state.confidence = "unknown"
        self.state.reason = reason
        # Once an established baseline is invalidated, a later first sample
        # from the other direction must not accidentally restore confidence.
        # A new tariff-day rollover is the only safe way to re-arm both flags.
        self.state.reset_seen = {"import": False, "export": False}

    def _rule(self, rule_id: str) -> QuotaRule:
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise KeyError(rule_id)


def import_legacy_settled_state(
    state: QuotaLedgerState,
    legacy: dict[str, Any] | None,
    mapping: dict[str, str],
) -> QuotaLedgerState:
    """Idempotently import legacy counters into an empty v2 ledger."""
    if not legacy:
        return state
    for rule_id, legacy_key in mapping.items():
        if rule_id in state.settled_kwh:
            continue
        value = legacy.get(legacy_key)
        if value is not None:
            state.settled_kwh[rule_id] = max(0.0, _float(value))
    return state


def tariff_datetime(value: datetime, token: str) -> datetime:
    """Convert to the tariff clock.  AEST is fixed UTC+10, never Adelaide DST."""
    value = _aware(value)
    if str(token).upper() == "AEST":
        return value.astimezone(timezone(timedelta(hours=10), name="AEST"))
    return value


def _window_overlap_seconds(start: datetime, end: datetime, rule: QuotaRule) -> float:
    if end <= start:
        return 0.0
    local_start = tariff_datetime(start, rule.timezone_token)
    local_end = tariff_datetime(end, rule.timezone_token)
    cursor = local_start.date() - timedelta(days=1)
    last_day = local_end.date()
    overlap = 0.0
    while cursor <= last_day:
        day_start = datetime.combine(cursor, time.min, tzinfo=local_start.tzinfo)
        for start_text, end_text in rule.windows:
            start_min = _hhmm(start_text)
            end_min = _hhmm(end_text)
            window_start = day_start + timedelta(minutes=start_min)
            window_end = day_start + timedelta(minutes=end_min)
            if end_min <= start_min:
                window_end += timedelta(days=1)
            overlap += max(
                0.0,
                (min(local_end, window_end) - max(local_start, window_start)).total_seconds(),
            )
        cursor += timedelta(days=1)
    return min((local_end - local_start).total_seconds(), overlap)


def _minute_in_window(minute: int, start: str, end: str) -> bool:
    start_min = _hhmm(start)
    end_min = _hhmm(end)
    if end_min <= start_min:
        return minute >= start_min or minute < end_min
    return start_min <= minute < end_min


def _hhmm(value: str) -> int:
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _confidence(value: Any) -> Confidence:
    return value if value in ("authoritative", "estimated", "unknown") else "unknown"
