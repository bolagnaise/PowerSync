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
from dataclasses import asdict, dataclass, field
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
ANCHOR_SOC_MIN = 0.05
ANCHOR_SOC_MAX = 0.95
ANCHOR_POWER_KW = 0.15
MIN_DIRECTIONAL_ENERGY_KWH = 0.25
MIN_DIRECTIONAL_CAPACITY_FRACTION = 0.08
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


def _integrate_nonnegative_linear(
    start: float,
    end: float,
    duration_hours: float,
) -> float:
    """Integrate the positive part of a linearly changing signal.

    Power telemetry can change sign between two observations.  Integrating
    ``max(start, 0)`` and ``max(end, 0)`` as one trapezoid counts both sides of
    that sign change as if the signal were positive for the full interval.
    Split the interval at the analytic zero crossing instead.
    """
    if duration_hours <= 0.0:
        return 0.0
    if start <= 0.0 and end <= 0.0:
        return 0.0
    if start >= 0.0 and end >= 0.0:
        return (start + end) * 0.5 * duration_hours
    if start < 0.0 < end:
        zero_fraction = -start / (end - start)
        return end * (1.0 - zero_fraction) * 0.5 * duration_hours
    if start > 0.0 > end:
        zero_fraction = start / (start - end)
        return start * zero_fraction * 0.5 * duration_hours
    # One endpoint is exactly zero.  The same-sign branches above already
    # handle it, but retain a finite fallback for unusual float inputs.
    return max(0.0, start, end) * 0.5 * duration_hours


def _integrate_directional_energy(
    start_power_kw: float,
    end_power_kw: float,
    duration_hours: float,
) -> tuple[float, float]:
    """Return charge and discharge energy for one linear power interval."""
    return (
        _integrate_nonnegative_linear(
            -start_power_kw,
            -end_power_kw,
            duration_hours,
        ),
        _integrate_nonnegative_linear(
            start_power_kw,
            end_power_kw,
            duration_hours,
        ),
    )


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
    """Measurements accumulated toward one closed SOC loop."""

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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["anchor_timestamp"] = self.anchor_timestamp.isoformat()
        data["last_timestamp"] = self.last_timestamp.isoformat()
        return data

    @classmethod
    def from_dict(
        cls, data: Any, topology_fingerprint: str | None
    ) -> "_ActiveCycle | None":
        if (
            not isinstance(data, dict)
            or not topology_fingerprint
            or data.get("topology_fingerprint") != topology_fingerprint
            or data.get("energy_source") != "integrated_ac_power"
            or data.get("state") not in ("anchored", "excursion")
        ):
            return None
        try:
            anchor_timestamp = datetime.fromisoformat(data["anchor_timestamp"])
            last_timestamp = datetime.fromisoformat(data["last_timestamp"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            anchor_timestamp.tzinfo is None
            or last_timestamp.tzinfo is None
            or not timedelta(0) <= last_timestamp - anchor_timestamp <= MAX_CYCLE_DURATION
        ):
            return None

        anchor_soc = _finite(data.get("anchor_soc"))
        last_soc = _finite(data.get("last_soc"))
        last_power_kw = _finite(data.get("last_power_kw"))
        capacity_kwh = _finite(data.get("capacity_kwh"))
        charge_kwh = _finite(data.get("charge_kwh"))
        discharge_kwh = _finite(data.get("discharge_kwh"))
        min_soc = _finite(data.get("min_soc"))
        max_soc = _finite(data.get("max_soc"))
        if (
            anchor_soc is None
            or last_soc is None
            or last_power_kw is None
            or capacity_kwh is None
            or charge_kwh is None
            or discharge_kwh is None
            or min_soc is None
            or max_soc is None
            or not ANCHOR_SOC_MIN <= anchor_soc <= ANCHOR_SOC_MAX
            or not 0.0 <= min_soc <= min(anchor_soc, last_soc)
            or not max(anchor_soc, last_soc) <= max_soc <= 1.0
            or capacity_kwh <= 0
            or min(charge_kwh, discharge_kwh) < 0
        ):
            return None
        return cls(
            state=data["state"],
            anchor_timestamp=anchor_timestamp,
            anchor_soc=anchor_soc,
            last_timestamp=last_timestamp,
            last_soc=last_soc,
            last_power_kw=last_power_kw,
            capacity_kwh=capacity_kwh,
            topology_fingerprint=topology_fingerprint,
            energy_source=data["energy_source"],
            charge_kwh=charge_kwh,
            discharge_kwh=discharge_kwh,
            min_soc=min_soc,
            max_soc=max_soc,
        )


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

    @staticmethod
    def _return_crossing_fraction(
        active: _ActiveCycle,
        soc: float,
    ) -> float | None:
        """Return the interval fraction at which the anchor SOC is crossed.

        A return is directional: after an upward excursion, the previous
        sample must be above the anchor and the current sample must be at or
        below it.  The downward case is the mirror image.  Merely being near
        the anchor therefore cannot close a cycle.
        """
        anchor = active.anchor_soc
        if active.max_soc - anchor + 1e-9 >= MIN_SOC_EXCURSION:
            if active.last_soc > anchor and soc <= anchor:
                denominator = active.last_soc - soc
                if denominator > 0.0:
                    return _clamp(
                        (active.last_soc - anchor) / denominator,
                        0.0,
                        1.0,
                    )
        if anchor - active.min_soc + 1e-9 >= MIN_SOC_EXCURSION:
            if active.last_soc < anchor and soc >= anchor:
                denominator = soc - active.last_soc
                if denominator > 0.0:
                    return _clamp(
                        (anchor - active.last_soc) / denominator,
                        0.0,
                        1.0,
                    )
        return None

    def _accept_and_reanchor(
        self,
        active: _ActiveCycle,
        *,
        crossing_timestamp: datetime,
        current_timestamp: datetime,
        current_soc: float,
        current_power_kw: float,
    ) -> ObservationResult:
        """Accept one loop and seed the next loop from the current sample.

        ``crossing_timestamp`` is the exact end of the accepted loop.  When
        the current sample overshoots that crossing, the remainder is
        deliberately discarded and the next loop starts at the current
        observation; this avoids reusing energy in two accepted loops.
        """
        result = self._accept_cycle(active, crossing_timestamp)
        if result.accepted_cycle and ANCHOR_SOC_MIN <= current_soc <= ANCHOR_SOC_MAX:
            self._start_anchor(
                timestamp=current_timestamp,
                soc=current_soc,
                power_kw=current_power_kw,
                capacity_kwh=active.capacity_kwh,
                topology_fingerprint=active.topology_fingerprint,
                energy_source=active.energy_source,
            )
        return result

    def _reject_and_reanchor(
        self,
        reason: str,
        *,
        timestamp: datetime,
        soc: float,
        power_kw: float,
        capacity_kwh: float,
        topology_fingerprint: str,
        energy_source: str,
    ) -> ObservationResult:
        """Discard a stale loop while seeding a new loop from this sample."""
        result = self._reject(reason)
        self._start_anchor(
            timestamp=timestamp,
            soc=soc,
            power_kw=power_kw,
            capacity_kwh=capacity_kwh,
            topology_fingerprint=topology_fingerprint,
            energy_source=energy_source,
        )
        return result

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
        active = self._active
        if active is None:
            if not self._start_anchor(
                timestamp=timestamp,
                soc=soc_value,
                power_kw=power_kw,
                capacity_kwh=capacity,
                topology_fingerprint=topology_fingerprint,
                energy_source=energy_source,
            ):
                # Clipped SOC samples are useful once a cycle is active, but
                # cannot establish an anchor for a new cycle.
                return self._reject("soc_boundary")
            return ObservationResult(True)

        if timestamp == active.last_timestamp:
            return ObservationResult(False)
        if timestamp < active.last_timestamp:
            return self._reject("backwards_timestamp")
        gap_seconds = (timestamp - active.last_timestamp).total_seconds()
        if gap_seconds < MIN_SAMPLE_GAP_SECONDS:
            return ObservationResult(False)
        if gap_seconds > MAX_SAMPLE_GAP_SECONDS:
            return self._reject_and_reanchor(
                "telemetry_gap",
                timestamp=timestamp,
                soc=soc_value,
                power_kw=power_kw,
                capacity_kwh=capacity,
                topology_fingerprint=topology_fingerprint,
                energy_source=energy_source,
            )
        if timestamp - active.anchor_timestamp > MAX_CYCLE_DURATION:
            return self._reject_and_reanchor(
                "cycle_timeout",
                timestamp=timestamp,
                soc=soc_value,
                power_kw=power_kw,
                capacity_kwh=capacity,
                topology_fingerprint=topology_fingerprint,
                energy_source=energy_source,
            )
        if (
            active.topology_fingerprint != topology_fingerprint
            or active.energy_source != energy_source
        ):
            return self._reject("measurement_source_changed")

        average_power_kw = (active.last_power_kw + power_kw) * 0.5
        soc_delta = soc_value - active.last_soc
        if active.state == "anchored":
            prior_excursion = max(
                abs(active.max_soc - active.anchor_soc),
                abs(active.min_soc - active.anchor_soc),
            )
            if prior_excursion + 1e-9 >= MIN_SOC_EXCURSION:
                active.state = "excursion"
        crossing_fraction = (
            self._return_crossing_fraction(active, soc_value)
            if active.state == "excursion"
            else None
        )
        # Validate the same interval prefix whose energy will be recorded.
        # At a sign change, the final power alone cannot describe the SOC
        # movement accumulated since the previous acquisition.
        direction_power_kw = average_power_kw
        if crossing_fraction is not None:
            crossing_power = active.last_power_kw + (
                power_kw - active.last_power_kw
            ) * crossing_fraction
            direction_power_kw = (active.last_power_kw + crossing_power) * 0.5
        if (
            abs(soc_delta) >= 0.002
            and abs(direction_power_kw) > ANCHOR_POWER_KW
            and direction_power_kw * soc_delta > 0
        ):
            return self._reject("power_soc_direction_mismatch")

        dt_hours = gap_seconds / 3600.0
        if crossing_fraction is not None:
            crossing_timestamp = active.last_timestamp + timedelta(
                seconds=gap_seconds * crossing_fraction
            )
            crossing_power_kw = active.last_power_kw + (
                power_kw - active.last_power_kw
            ) * crossing_fraction
            charge_kwh, discharge_kwh = _integrate_directional_energy(
                active.last_power_kw,
                crossing_power_kw,
                dt_hours * crossing_fraction,
            )
            min_energy = max(
                MIN_DIRECTIONAL_ENERGY_KWH,
                active.capacity_kwh * MIN_DIRECTIONAL_CAPACITY_FRACTION,
            )
            if (
                active.charge_kwh + charge_kwh >= min_energy
                and active.discharge_kwh + discharge_kwh >= min_energy
            ):
                active.charge_kwh += charge_kwh
                active.discharge_kwh += discharge_kwh
                # Do not integrate or retain the post-crossing remainder in
                # this cycle.  _accept_and_reanchor starts a fresh cycle from
                # the current observation, so accepted loops cannot overlap.
                return self._accept_and_reanchor(
                    active,
                    crossing_timestamp=crossing_timestamp,
                    current_timestamp=timestamp,
                    current_soc=soc_value,
                    current_power_kw=power_kw,
                )

        charge_kwh, discharge_kwh = _integrate_directional_energy(
            active.last_power_kw,
            power_kw,
            dt_hours,
        )
        active.charge_kwh += charge_kwh
        active.discharge_kwh += discharge_kwh
        active.last_timestamp = timestamp
        active.last_soc = soc_value
        active.last_power_kw = power_kw
        active.min_soc = min(active.min_soc, soc_value)
        active.max_soc = max(active.max_soc, soc_value)

        excursion = max(
            abs(active.max_soc - active.anchor_soc),
            abs(active.min_soc - active.anchor_soc),
        )
        if (
            active.state == "anchored"
            and excursion + 1e-9 >= MIN_SOC_EXCURSION
        ):
            active.state = "excursion"

        return ObservationResult(True)

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
        planning_parameters: ResolvedOptimizerParameters | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        resolved = self.resolved_parameters(
            application_enabled=application_enabled,
            now=now,
        )
        cycles, days, equivalent_cycles = self.evidence(now)
        # A loop completed during a solve becomes eligible on the next solve.
        # Show the frozen conversion values used by the published plan while
        # retaining the newest measured estimate and evidence below.
        applied = planning_parameters or resolved
        return {
            "state": resolved.battery_efficiency_source,
            "cycle_state": self.state,
            "application_enabled": application_enabled,
            "default_one_way_efficiency": DEFAULT_ONE_WAY_EFFICIENCY,
            "applied_one_way_efficiency": round(applied.charge_efficiency, 3),
            "physical_round_trip_efficiency": round(
                applied.physical_round_trip_efficiency,
                4,
            ),
            "economic_round_trip_efficiency": round(
                applied.economic_round_trip_efficiency,
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
            "active_cycle": self._active.to_dict() if self._active is not None else None,
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
        learner._active = _ActiveCycle.from_dict(
            data.get("active_cycle"), learner.topology_fingerprint
        )
        return learner
