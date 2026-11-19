"""Provider-neutral learning for effective battery round-trip efficiency.

The optimiser models aggregate AC-side battery power.  With generic SOC and
power telemetry, usable capacity and separate directional efficiencies are not
jointly identifiable.  This module therefore learns only effective round-trip
efficiency from completed charge/discharge loops that return to the same SOC
band.  It is deliberately pure: Home Assistant persistence and optimiser
application are owned by the coordinator.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any


STORE_VERSION = 1
HISTORY_DAYS = 30
MAX_HISTORY_SAMPLES = 60
MAX_REJECTION_REASONS = 24

DEFAULT_ONE_WAY_EFFICIENCY = 0.92
DEFAULT_ROUND_TRIP_EFFICIENCY = DEFAULT_ONE_WAY_EFFICIENCY**2
MIN_ROUND_TRIP_EFFICIENCY = 0.75
MAX_ROUND_TRIP_EFFICIENCY = 0.98

MIN_VALID_CYCLES = 5
FULL_CONFIDENCE_CYCLES = 10
MIN_VALID_DAYS = 3
FULL_CONFIDENCE_DAYS = 7
MIN_EQUIVALENT_FULL_CYCLES = 1.5
FULL_CONFIDENCE_EQUIVALENT_CYCLES = 4.0

MIN_SOC_EXCURSION = 0.10
RETURN_SOC_TOLERANCE = 0.015
ANCHOR_SOC_MIN = 0.05
ANCHOR_SOC_MAX = 0.95
EXCLUDED_SOC_MIN = 0.02
ANCHOR_POWER_KW = 0.15
MIN_DIRECTIONAL_ENERGY_KWH = 0.25
MIN_DIRECTIONAL_CAPACITY_FRACTION = 0.08
RETURN_DWELL_SECONDS = 5 * 60
MIN_SAMPLE_GAP_SECONDS = 30
MAX_SAMPLE_GAP_SECONDS = 15 * 60
MAX_CYCLE_DURATION = timedelta(hours=72)

MAX_UPWARD_ONE_WAY_STEP = 0.002
MAX_DOWNWARD_ONE_WAY_STEP = 0.004
ONE_WAY_QUANTUM = 0.001


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _weighted_quantile(
    values: list[tuple[float, float]],
    fraction: float,
) -> float | None:
    """Return a deterministic weighted empirical quantile."""
    valid = sorted(
        (value, weight)
        for value, weight in values
        if math.isfinite(value) and math.isfinite(weight) and weight > 0
    )
    if not valid:
        return None
    total = sum(weight for _, weight in valid)
    threshold = _clamp(fraction, 0.0, 1.0) * total
    cumulative = 0.0
    for value, weight in valid:
        cumulative += weight
        if cumulative + 1e-12 >= threshold:
            return value
    return valid[-1][0]


@dataclass(frozen=True)
class ResolvedOptimizerParameters:
    """Immutable physical and economic values used by one optimiser solve."""

    charge_efficiency: float = DEFAULT_ONE_WAY_EFFICIENCY
    discharge_efficiency: float = DEFAULT_ONE_WAY_EFFICIENCY
    physical_round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY
    economic_round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY
    battery_efficiency_confidence: float = 0.0
    battery_efficiency_source: str = "legacy"
    battery_efficiency_candidate_rte: float | None = None

    @classmethod
    def legacy(
        cls,
        one_way: float = DEFAULT_ONE_WAY_EFFICIENCY,
    ) -> "ResolvedOptimizerParameters":
        efficiency = _clamp(float(one_way), 0.01, 1.0)
        physical_rte = efficiency**2
        return cls(
            charge_efficiency=efficiency,
            discharge_efficiency=efficiency,
            physical_round_trip_efficiency=physical_rte,
            economic_round_trip_efficiency=min(
                physical_rte,
                DEFAULT_ROUND_TRIP_EFFICIENCY,
            ),
        )


@dataclass(frozen=True)
class BatteryEfficiencySample:
    """One accepted closed-cycle summary."""

    timestamp: str
    local_date: str
    round_trip_efficiency: float
    charge_kwh: float
    discharge_kwh: float
    capacity_kwh: float
    soc_span: float
    duration_hours: float
    energy_source: str = "integrated_ac_power"

    @property
    def equivalent_cycles(self) -> float:
        if self.capacity_kwh <= 0:
            return 0.0
        return min(self.charge_kwh, self.discharge_kwh) / self.capacity_kwh

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "local_date": self.local_date,
            "round_trip_efficiency": round(self.round_trip_efficiency, 6),
            "charge_kwh": round(self.charge_kwh, 6),
            "discharge_kwh": round(self.discharge_kwh, 6),
            "capacity_kwh": round(self.capacity_kwh, 6),
            "soc_span": round(self.soc_span, 6),
            "duration_hours": round(self.duration_hours, 6),
            "energy_source": self.energy_source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "BatteryEfficiencySample | None":
        if not isinstance(data, dict):
            return None
        timestamp = _parse_datetime(data.get("timestamp"))
        rte = _finite(data.get("round_trip_efficiency"))
        charge = _finite(data.get("charge_kwh"))
        discharge = _finite(data.get("discharge_kwh"))
        capacity = _finite(data.get("capacity_kwh"))
        soc_span = _finite(data.get("soc_span"))
        duration = _finite(data.get("duration_hours"))
        if (
            timestamp is None
            or rte is None
            or charge is None
            or discharge is None
            or capacity is None
            or soc_span is None
            or duration is None
            or not MIN_ROUND_TRIP_EFFICIENCY <= rte <= MAX_ROUND_TRIP_EFFICIENCY
            or min(charge, discharge, capacity, duration) <= 0
        ):
            return None
        return cls(
            timestamp=timestamp.isoformat(),
            local_date=str(data.get("local_date") or timestamp.date().isoformat()),
            round_trip_efficiency=rte,
            charge_kwh=charge,
            discharge_kwh=discharge,
            capacity_kwh=capacity,
            soc_span=max(0.0, soc_span),
            duration_hours=duration,
            energy_source=str(data.get("energy_source") or "integrated_ac_power"),
        )


@dataclass
class _ActiveCycle:
    """Non-persisted state for one possible closed SOC loop."""

    state: str
    anchor_timestamp: datetime
    anchor_soc: float
    last_timestamp: datetime
    last_soc: float
    last_power_kw: float
    capacity_kwh: float
    topology_fingerprint: str
    energy_source: str
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0
    min_soc: float = 1.0
    max_soc: float = 0.0
    return_started_at: datetime | None = None


@dataclass(frozen=True)
class ObservationResult:
    changed: bool
    accepted_cycle: bool = False
    rejection_reason: str | None = None


@dataclass
class BatteryEfficiencyLearner:
    """Learn effective AC round-trip efficiency from closed battery cycles."""

    history: list[BatteryEfficiencySample] = field(default_factory=list)
    rejections: Counter[str] = field(default_factory=Counter)
    applied_one_way_efficiency: float = DEFAULT_ONE_WAY_EFFICIENCY
    topology_fingerprint: str | None = None
    last_accepted_cycle: str | None = None
    last_rejection_reason: str | None = None
    _active: _ActiveCycle | None = field(default=None, init=False, repr=False)

    @property
    def state(self) -> str:
        return self._active.state if self._active is not None else "idle"

    def _reject(self, reason: str) -> ObservationResult:
        reason = reason or "invalid_observation"
        self.rejections[reason] += 1
        self.last_rejection_reason = reason
        self._active = None
        if len(self.rejections) > MAX_REJECTION_REASONS:
            for key, _ in self.rejections.most_common()[MAX_REJECTION_REASONS:]:
                del self.rejections[key]
        return ObservationResult(True, False, reason)

    def ensure_topology(self, topology_fingerprint: str) -> bool:
        """Reset persisted evidence when the aggregate battery boundary changes."""
        if not topology_fingerprint:
            return False
        if self.topology_fingerprint is None:
            self.topology_fingerprint = topology_fingerprint
            return True
        if self.topology_fingerprint == topology_fingerprint:
            return False
        self.history.clear()
        self.applied_one_way_efficiency = DEFAULT_ONE_WAY_EFFICIENCY
        self.topology_fingerprint = topology_fingerprint
        self._active = None
        self.rejections["topology_changed"] += 1
        self.last_rejection_reason = "topology_changed"
        return True

    def _start_anchor(
        self,
        *,
        timestamp: datetime,
        soc: float,
        power_kw: float,
        capacity_kwh: float,
        topology_fingerprint: str,
        energy_source: str,
    ) -> bool:
        if not ANCHOR_SOC_MIN <= soc <= ANCHOR_SOC_MAX:
            return False
        if abs(power_kw) > ANCHOR_POWER_KW:
            return False
        self._active = _ActiveCycle(
            state="anchored",
            anchor_timestamp=timestamp,
            anchor_soc=soc,
            last_timestamp=timestamp,
            last_soc=soc,
            last_power_kw=power_kw,
            capacity_kwh=capacity_kwh,
            topology_fingerprint=topology_fingerprint,
            energy_source=energy_source,
            min_soc=soc,
            max_soc=soc,
        )
        return True

    def _accept_cycle(
        self,
        active: _ActiveCycle,
        timestamp: datetime,
    ) -> ObservationResult:
        rte = active.discharge_kwh / active.charge_kwh
        if not MIN_ROUND_TRIP_EFFICIENCY <= rte <= MAX_ROUND_TRIP_EFFICIENCY:
            return self._reject("implausible_efficiency")
        duration_hours = (timestamp - active.anchor_timestamp).total_seconds() / 3600.0
        sample = BatteryEfficiencySample(
            timestamp=timestamp.isoformat(),
            local_date=timestamp.date().isoformat(),
            round_trip_efficiency=rte,
            charge_kwh=active.charge_kwh,
            discharge_kwh=active.discharge_kwh,
            capacity_kwh=active.capacity_kwh,
            soc_span=active.max_soc - active.min_soc,
            duration_hours=duration_hours,
            energy_source=active.energy_source,
        )
        self.history.append(sample)
        self.history = self.history[-MAX_HISTORY_SAMPLES:]
        self.last_accepted_cycle = sample.timestamp
        self.last_rejection_reason = None
        self._active = None
        self._update_applied_efficiency(timestamp)
        return ObservationResult(True, True, None)

    def observe(
        self,
        *,
        timestamp: datetime,
        soc: Any,
        battery_power_kw: Any,
        capacity_kwh: Any,
        topology_fingerprint: str,
        energy_source: str = "integrated_ac_power",
        valid: bool = True,
        skip_reason: str | None = None,
    ) -> ObservationResult:
        """Observe one normalized aggregate AC battery telemetry sample."""
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        soc_value = _finite(soc)
        power_kw = _finite(battery_power_kw)
        capacity = _finite(capacity_kwh)
        if not valid:
            return self._reject(skip_reason or "invalid_observation")
        if (
            soc_value is None
            or power_kw is None
            or capacity is None
            or capacity <= 0
        ):
            return self._reject(skip_reason or "invalid_telemetry")
        soc_value = soc_value / 100.0 if soc_value > 1.0 else soc_value
        if not 0.0 <= soc_value <= 1.0:
            return self._reject("invalid_soc")
        if not topology_fingerprint or energy_source != "integrated_ac_power":
            return self._reject("unknown_measurement_boundary")

        previous_topology = self.topology_fingerprint
        topology_changed = self.ensure_topology(topology_fingerprint)
        if topology_changed and previous_topology is not None:
            return ObservationResult(True, False, "topology_changed")

        self._prune(timestamp)
        # A full charge can be part of an interior-to-interior loop. Keep
        # counting AC energy through the plateau without anchoring there.
        if soc_value <= EXCLUDED_SOC_MIN:
            return self._reject("soc_boundary")

        active = self._active
        if active is None:
            self._start_anchor(
                timestamp=timestamp,
                soc=soc_value,
                power_kw=power_kw,
                capacity_kwh=capacity,
                topology_fingerprint=topology_fingerprint,
                energy_source=energy_source,
            )
            # Open-cycle state is deliberately not persisted. Only the first
            # topology fingerprint changes serialized state here.
            return ObservationResult(topology_changed)

        if timestamp == active.last_timestamp:
            return ObservationResult(False)
        if timestamp < active.last_timestamp:
            return self._reject("backwards_timestamp")
        gap_seconds = (timestamp - active.last_timestamp).total_seconds()
        if gap_seconds < MIN_SAMPLE_GAP_SECONDS:
            return ObservationResult(False)
        if gap_seconds > MAX_SAMPLE_GAP_SECONDS:
            return self._reject("telemetry_gap")
        if timestamp - active.anchor_timestamp > MAX_CYCLE_DURATION:
            return self._reject("cycle_timeout")
        if (
            active.topology_fingerprint != topology_fingerprint
            or active.energy_source != energy_source
        ):
            return self._reject("measurement_source_changed")

        average_power_kw = (active.last_power_kw + power_kw) * 0.5
        soc_delta = soc_value - active.last_soc
        if (
            abs(soc_delta) >= 0.002
            and abs(average_power_kw) > ANCHOR_POWER_KW
            and average_power_kw * soc_delta > 0
        ):
            return self._reject("power_soc_direction_mismatch")

        dt_hours = gap_seconds / 3600.0
        active.charge_kwh += (
            max(0.0, -active.last_power_kw) + max(0.0, -power_kw)
        ) * 0.5 * dt_hours
        active.discharge_kwh += (
            max(0.0, active.last_power_kw) + max(0.0, power_kw)
        ) * 0.5 * dt_hours
        active.last_timestamp = timestamp
        active.last_soc = soc_value
        active.last_power_kw = power_kw
        active.min_soc = min(active.min_soc, soc_value)
        active.max_soc = max(active.max_soc, soc_value)

        excursion = max(
            abs(active.max_soc - active.anchor_soc),
            abs(active.min_soc - active.anchor_soc),
        )
        if active.state == "anchored" and excursion >= MIN_SOC_EXCURSION:
            active.state = "excursion"

        min_energy = max(
            MIN_DIRECTIONAL_ENERGY_KWH,
            active.capacity_kwh * MIN_DIRECTIONAL_CAPACITY_FRACTION,
        )
        evidence_ready = (
            excursion >= MIN_SOC_EXCURSION
            and active.charge_kwh >= min_energy
            and active.discharge_kwh >= min_energy
        )
        inside_return_band = abs(soc_value - active.anchor_soc) <= RETURN_SOC_TOLERANCE

        if active.state == "excursion" and evidence_ready and inside_return_band:
            active.state = "return_pending"
            active.return_started_at = timestamp
        elif active.state == "return_pending":
            if not inside_return_band:
                active.state = "excursion"
                active.return_started_at = None
            elif (
                active.return_started_at is not None
                and (timestamp - active.return_started_at).total_seconds()
                >= RETURN_DWELL_SECONDS
                and abs(power_kw) <= ANCHOR_POWER_KW
            ):
                return self._accept_cycle(active, timestamp)

        # Integrated open-cycle state is not serialized.
        return ObservationResult(False)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=HISTORY_DAYS)
        self.history = [
            sample
            for sample in self.history
            if (
                _parse_datetime(sample.timestamp)
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            >= cutoff
        ][-MAX_HISTORY_SAMPLES:]

    def candidate_round_trip_efficiency(
        self,
        now: datetime | None = None,
    ) -> float | None:
        if now is not None:
            self._prune(now)
        weighted = [
            (sample.round_trip_efficiency, max(sample.equivalent_cycles, 1e-6))
            for sample in self.history
        ]
        candidate = _weighted_quantile(weighted, 0.25)
        if candidate is None:
            return None
        return _clamp(
            candidate,
            MIN_ROUND_TRIP_EFFICIENCY,
            MAX_ROUND_TRIP_EFFICIENCY,
        )

    def evidence(self, now: datetime | None = None) -> tuple[int, int, float]:
        if now is not None:
            self._prune(now)
        return (
            len(self.history),
            len({sample.local_date for sample in self.history}),
            sum(sample.equivalent_cycles for sample in self.history),
        )

    def confidence(self, now: datetime | None = None) -> float:
        cycles, days, equivalent_cycles = self.evidence(now)
        if (
            cycles < MIN_VALID_CYCLES
            or days < MIN_VALID_DAYS
            or equivalent_cycles < MIN_EQUIVALENT_FULL_CYCLES
        ):
            return 0.0
        return min(
            1.0,
            cycles / FULL_CONFIDENCE_CYCLES,
            days / FULL_CONFIDENCE_DAYS,
            equivalent_cycles / FULL_CONFIDENCE_EQUIVALENT_CYCLES,
        )

    def _update_applied_efficiency(self, now: datetime) -> None:
        confidence = self.confidence(now)
        candidate = self.candidate_round_trip_efficiency(now)
        if confidence <= 0 or candidate is None:
            self.applied_one_way_efficiency = DEFAULT_ONE_WAY_EFFICIENCY
            return
        target_rte = (
            DEFAULT_ROUND_TRIP_EFFICIENCY * (1.0 - confidence)
            + candidate * confidence
        )
        target = math.sqrt(_clamp(
            target_rte,
            MIN_ROUND_TRIP_EFFICIENCY,
            MAX_ROUND_TRIP_EFFICIENCY,
        ))
        current = _clamp(
            self.applied_one_way_efficiency,
            math.sqrt(MIN_ROUND_TRIP_EFFICIENCY),
            math.sqrt(MAX_ROUND_TRIP_EFFICIENCY),
        )
        if target > current:
            applied = min(target, current + MAX_UPWARD_ONE_WAY_STEP)
        else:
            applied = max(target, current - MAX_DOWNWARD_ONE_WAY_STEP)
        self.applied_one_way_efficiency = round(
            applied / ONE_WAY_QUANTUM
        ) * ONE_WAY_QUANTUM

    def resolved_parameters(
        self,
        *,
        application_enabled: bool = True,
        now: datetime | None = None,
    ) -> ResolvedOptimizerParameters:
        now = now or datetime.now(timezone.utc)
        candidate = self.candidate_round_trip_efficiency(now)
        confidence = self.confidence(now)
        if not application_enabled:
            return ResolvedOptimizerParameters(
                battery_efficiency_confidence=confidence,
                battery_efficiency_source="disabled",
                battery_efficiency_candidate_rte=candidate,
            )
        if confidence <= 0 or candidate is None:
            return ResolvedOptimizerParameters(
                battery_efficiency_confidence=0.0,
                battery_efficiency_source=(
                    "learning" if self.history else "cold_start"
                ),
                battery_efficiency_candidate_rte=candidate,
            )
        one_way = _clamp(
            self.applied_one_way_efficiency,
            math.sqrt(MIN_ROUND_TRIP_EFFICIENCY),
            math.sqrt(MAX_ROUND_TRIP_EFFICIENCY),
        )
        physical_rte = one_way**2
        return ResolvedOptimizerParameters(
            charge_efficiency=one_way,
            discharge_efficiency=one_way,
            physical_round_trip_efficiency=physical_rte,
            economic_round_trip_efficiency=min(
                physical_rte,
                DEFAULT_ROUND_TRIP_EFFICIENCY,
            ),
            battery_efficiency_confidence=confidence,
            battery_efficiency_source="learned",
            battery_efficiency_candidate_rte=candidate,
        )

    def diagnostics(
        self,
        *,
        application_enabled: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        resolved = self.resolved_parameters(
            application_enabled=application_enabled,
            now=now,
        )
        cycles, days, equivalent_cycles = self.evidence(now)
        return {
            "state": resolved.battery_efficiency_source,
            "cycle_state": self.state,
            "application_enabled": application_enabled,
            "default_one_way_efficiency": DEFAULT_ONE_WAY_EFFICIENCY,
            "applied_one_way_efficiency": round(resolved.charge_efficiency, 3),
            "physical_round_trip_efficiency": round(
                resolved.physical_round_trip_efficiency,
                4,
            ),
            "economic_round_trip_efficiency": round(
                resolved.economic_round_trip_efficiency,
                4,
            ),
            "candidate_round_trip_efficiency": (
                round(resolved.battery_efficiency_candidate_rte, 4)
                if resolved.battery_efficiency_candidate_rte is not None
                else None
            ),
            "confidence": round(resolved.battery_efficiency_confidence, 3),
            "valid_cycles": cycles,
            "distinct_days": days,
            "equivalent_full_cycles": round(equivalent_cycles, 3),
            "last_accepted_cycle": self.last_accepted_cycle,
            "last_rejection_reason": self.last_rejection_reason,
            "rejections": dict(self.rejections.most_common(MAX_REJECTION_REASONS)),
            "energy_source": "integrated_ac_power",
            "measurement_boundary": "ac_system",
            "topology_fingerprint": self.topology_fingerprint,
            "store_version": STORE_VERSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "history": [
                sample.to_dict()
                for sample in self.history[-MAX_HISTORY_SAMPLES:]
            ],
            "rejections": dict(self.rejections.most_common(MAX_REJECTION_REASONS)),
            "applied_one_way_efficiency": round(self.applied_one_way_efficiency, 6),
            "topology_fingerprint": self.topology_fingerprint,
            "last_accepted_cycle": self.last_accepted_cycle,
            "last_rejection_reason": self.last_rejection_reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "BatteryEfficiencyLearner":
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return cls()
        raw_history = data.get("history")
        if not isinstance(raw_history, list):
            raw_history = []
        history = [
            sample
            for raw in raw_history[-MAX_HISTORY_SAMPLES:]
            if (sample := BatteryEfficiencySample.from_dict(raw)) is not None
        ]
        applied = _finite(data.get("applied_one_way_efficiency"))
        if applied is None or not (
            math.sqrt(MIN_ROUND_TRIP_EFFICIENCY)
            <= applied
            <= math.sqrt(MAX_ROUND_TRIP_EFFICIENCY)
        ):
            applied = DEFAULT_ONE_WAY_EFFICIENCY
        raw_rejections = data.get("rejections")
        rejections = Counter()
        if isinstance(raw_rejections, dict):
            for reason, count in raw_rejections.items():
                try:
                    parsed = max(0, int(count))
                except (TypeError, ValueError):
                    continue
                if parsed:
                    rejections[str(reason)] = parsed
        learner = cls(
            history=history,
            rejections=Counter(
                dict(rejections.most_common(MAX_REJECTION_REASONS))
            ),
            applied_one_way_efficiency=applied,
            topology_fingerprint=(
                str(data.get("topology_fingerprint"))
                if data.get("topology_fingerprint")
                else None
            ),
            last_accepted_cycle=(
                str(data.get("last_accepted_cycle"))
                if data.get("last_accepted_cycle")
                else None
            ),
            last_rejection_reason=(
                str(data.get("last_rejection_reason"))
                if data.get("last_rejection_reason")
                else None
            ),
        )
        # Open cycles are intentionally never serialized or restored.
        return learner
