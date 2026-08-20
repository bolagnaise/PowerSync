"""EV charging demand expressed for the LP battery optimizer.

The home-load forecast deliberately excludes EV charging
(``load_estimator._subtract_ev_buckets``), so the optimizer has historically
planned battery charge windows as if the whole grid import limit were
available. This module carries the missing demand in two forms:

* :func:`ev_charge_bounds_kw` — per-period charging capability, used to bound
  the LP's ``ev_charge`` decision variable so the solver co-optimizes the car
  against the battery and the site import limit.
* :func:`expected_ev_load_kw` — a deterministic as-soon-as-possible profile
  used where co-optimization is not available (the greedy fallback), so the
  battery plan still accounts for the car rather than over-committing import
  headroom.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


# Charging that is scheduled but unfinished is worth more than any realistic
# import price, so the LP fills the car unless doing so is physically or
# limit-infeasible. Expressed in $/kWh against import prices in the same unit.
EV_SHORTFALL_PENALTY_PER_KWH = 100.0


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@dataclass(frozen=True)
class EVChargePlan:
    """One vehicle's charging demand over the optimization horizon.

    ``max_power_kw`` is per schedule slot and already encodes availability:
    a slot the car cannot charge in (unplugged, outside its allowed window,
    past its deadline) carries 0.0.
    """

    vehicle_id: str
    max_power_kw: tuple[float, ...]
    energy_needed_kwh: float
    charge_efficiency: float = 0.9
    min_power_kw: float = 0.0
    # Cumulative energy that must be delivered by a given slot, as
    # ``(last_usable_slot, kwh_by_then)`` sorted by slot. Empty means the one
    # implicit stage every single-vehicle plan has: all of its energy by its
    # own deadline. Combining vehicles is the only thing that produces more
    # than one stage, and it is the whole reason the field exists -- summing
    # two cars into one energy figure otherwise discards the earlier car's
    # deadline, letting the solver satisfy the total in a cheap window that
    # car can no longer use.
    deadline_requirements: tuple[tuple[int, float], ...] = ()

    @property
    def staged_requirements(self) -> tuple[tuple[int, float], ...]:
        """Return the cumulative delivery stages, deriving the implicit one."""
        if self.deadline_requirements:
            return self.deadline_requirements
        deadline = self.deadline_index
        if deadline < 0:
            return ()
        return ((deadline, self.energy_needed_kwh),)

    @property
    def active(self) -> bool:
        """Return whether this plan asks for any schedulable energy."""
        return (
            self.energy_needed_kwh > 1e-6
            and any(power > 1e-6 for power in self.max_power_kw)
        )

    @property
    def deadline_index(self) -> int:
        """Return the last slot that can contribute, or -1 when none can."""
        for index in range(len(self.max_power_kw) - 1, -1, -1):
            if self.max_power_kw[index] > 1e-6:
                return index
        return -1


def normalize_ev_charge_plan(
    plan: EVChargePlan | None,
    n_slots: int,
) -> EVChargePlan | None:
    """Return a plan padded or truncated to the solve horizon.

    A plan whose window falls entirely outside the horizon returns ``None`` so
    callers can skip every EV code path instead of carrying an empty block.
    """
    if plan is None or n_slots <= 0:
        return None

    powers = [max(0.0, _finite(value)) for value in plan.max_power_kw[:n_slots]]
    powers.extend([0.0] * (n_slots - len(powers)))
    efficiency = _finite(plan.charge_efficiency, 0.9)
    if not 0.1 <= efficiency <= 1.0:
        efficiency = 0.9

    # A stage whose deadline falls past the horizon is clamped to the last
    # slot rather than dropped: the energy is still required, and dropping it
    # would quietly relax the requirement to zero.
    stages: list[tuple[int, float]] = []
    for raw_slot, raw_kwh in plan.deadline_requirements or ():
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            continue
        kwh = max(0.0, _finite(raw_kwh))
        if kwh <= 1e-9:
            continue
        stages.append((max(0, min(n_slots - 1, slot)), kwh))
    stages.sort(key=lambda stage: stage[0])

    normalized = EVChargePlan(
        vehicle_id=str(plan.vehicle_id or "_default"),
        max_power_kw=tuple(powers),
        energy_needed_kwh=max(0.0, _finite(plan.energy_needed_kwh)),
        charge_efficiency=efficiency,
        min_power_kw=max(0.0, _finite(plan.min_power_kw)),
        deadline_requirements=tuple(stages),
    )
    return normalized if normalized.active else None


def combine_ev_charge_plans(
    plans: Iterable[EVChargePlan | None],
    n_slots: int,
) -> EVChargePlan | None:
    """Merge every vehicle into one aggregate demand for the solver.

    The LP models total EV load against the shared site import limit, so
    multiple vehicles combine into a single block: capability adds per slot and
    required energy sums. Per-vehicle allocation stays with the EV controller,
    which already arbitrates a shared site budget between loadpoints.
    """
    normalized = [
        normalized_plan
        for normalized_plan in (
            normalize_ev_charge_plan(plan, n_slots) for plan in plans
        )
        if normalized_plan is not None
    ]
    if not normalized:
        return None
    if len(normalized) == 1:
        return normalized[0]

    combined_power = tuple(
        sum(plan.max_power_kw[index] for plan in normalized)
        for index in range(n_slots)
    )
    total_energy = sum(plan.energy_needed_kwh for plan in normalized)
    # Energy-weighted efficiency keeps the delivered-kWh accounting honest when
    # vehicles charge at different efficiencies.
    weighted_efficiency = (
        sum(plan.charge_efficiency * plan.energy_needed_kwh for plan in normalized)
        / total_energy
        if total_energy > 1e-9
        else normalized[0].charge_efficiency
    )
    # Each vehicle's own deadline becomes a cumulative stage. By the earliest
    # deadline the aggregate must already have delivered everything owed to the
    # vehicles that expire then -- otherwise the solver is free to park the
    # whole total in a later cheap window those vehicles cannot reach.
    energy_by_deadline: dict[int, float] = {}
    for plan in normalized:
        for slot, kwh in plan.staged_requirements:
            energy_by_deadline[slot] = energy_by_deadline.get(slot, 0.0) + kwh
    cumulative = 0.0
    stages: list[tuple[int, float]] = []
    for slot in sorted(energy_by_deadline):
        cumulative += energy_by_deadline[slot]
        stages.append((slot, cumulative))

    return EVChargePlan(
        vehicle_id="_combined",
        max_power_kw=combined_power,
        energy_needed_kwh=total_energy,
        charge_efficiency=weighted_efficiency,
        min_power_kw=min(plan.min_power_kw for plan in normalized),
        deadline_requirements=tuple(stages),
    )


def ev_chart_series(overlay_w, solved_ev_w, n_slots: int):
    """Return the planned-EV-load series to publish, or None.

    The load overlay and the LP's ``ev_charge`` decision variable model the
    same demand and are mutually exclusive: whenever the solver co-optimizes
    the car, the overlay is deliberately zeroed. Publishing only the overlay
    therefore showed no planned EV load at all for exactly the sites whose car
    the solver had placed.
    """
    if overlay_w:
        return list(overlay_w[:n_slots])
    solved = [float(value or 0.0) for value in (solved_ev_w or [])[:n_slots]]
    if any(value > 0 for value in solved):
        return solved
    return None


def _match_awareness(value, reference):
    """Return ``value`` made comparable with ``reference``.

    A naive boundary is read as being on the reference's own clock, which is
    what the EV planner means by it: both are HA-local.
    """
    if value is None or reference is None:
        return value
    reference_tz = getattr(reference, "tzinfo", None)
    value_tz = getattr(value, "tzinfo", None)
    if reference_tz is not None and value_tz is None:
        return value.replace(tzinfo=reference_tz)
    if reference_tz is None and value_tz is not None:
        return value.replace(tzinfo=None)
    return value


def ev_plan_from_demand(
    *,
    vehicle_id: str,
    energy_needed_kwh: float,
    charger_power_kw: float,
    schedule_timestamps: Sequence,
    deadline=None,
    available_from=None,
    charge_efficiency: float = 0.9,
    min_power_kw: float = 0.0,
) -> EVChargePlan | None:
    """Build one vehicle's LP demand from the EV planner's figures.

    Availability is the *physical* envelope — from when the car can start
    until its deadline, at the charger's rate — not the windows the EV planner
    already picked. Handing the solver the physical envelope is the whole
    point: it chooses the timing against prices and the import limit, where
    the EV planner could only choose against prices.
    """
    energy_kwh = max(0.0, _finite(energy_needed_kwh))
    power_kw = max(0.0, _finite(charger_power_kw))
    if energy_kwh <= 1e-6 or power_kw <= 1e-6 or not schedule_timestamps:
        return None

    # The EV planner stores its deadlines as HA-local *naive* times while the
    # schedule timestamps are aware. Align them here so the availability
    # comparisons below cannot raise TypeError on a caller that forgot to.
    reference = schedule_timestamps[0]
    deadline = _match_awareness(deadline, reference)
    available_from = _match_awareness(available_from, reference)

    powers: list[float] = []
    for timestamp in schedule_timestamps:
        available = True
        if available_from is not None and timestamp < available_from:
            available = False
        if deadline is not None and timestamp >= deadline:
            available = False
        powers.append(power_kw if available else 0.0)

    return normalize_ev_charge_plan(
        EVChargePlan(
            vehicle_id=str(vehicle_id or "_default"),
            max_power_kw=tuple(powers),
            energy_needed_kwh=energy_kwh,
            charge_efficiency=_finite(charge_efficiency, 0.9),
            min_power_kw=max(0.0, _finite(min_power_kw)),
        ),
        len(schedule_timestamps),
    )


def ev_charge_bounds_kw(
    plan: EVChargePlan,
    periods: Sequence[tuple[int, int]],
) -> list[float]:
    """Return the per-LP-period charging capability for one plan.

    The LP coarsens slots into periods; a period's capability is the mean of
    its slots so the period's energy budget (``power * period_hours``) matches
    the slot-level budget it stands for.
    """
    bounds: list[float] = []
    for start, end in periods:
        span = max(1, end - start)
        window = plan.max_power_kw[start:end]
        bounds.append(sum(window) / span if window else 0.0)
    return bounds


def expected_ev_load_kw(
    plan: EVChargePlan | None,
    n_slots: int,
    dt_hours: float,
) -> list[float]:
    """Return an as-soon-as-possible EV draw profile in kW per slot.

    Used where the solver cannot co-optimize the car. Charging as early as the
    window allows is the conservative assumption for a battery plan: it never
    understates the import headroom the car will occupy while the battery is
    trying to charge.
    """
    profile = [0.0] * max(0, n_slots)
    normalized = normalize_ev_charge_plan(plan, n_slots)
    if normalized is None or dt_hours <= 0:
        return profile

    remaining_kwh = normalized.energy_needed_kwh
    for index in range(n_slots):
        if remaining_kwh <= 1e-9:
            break
        available_kw = normalized.max_power_kw[index]
        if available_kw <= 1e-9:
            continue
        # Grid-side kW needed to land ``remaining_kwh`` in the pack.
        needed_kw = remaining_kwh / (dt_hours * normalized.charge_efficiency)
        draw_kw = min(available_kw, needed_kw)
        profile[index] = draw_kw
        remaining_kwh -= draw_kw * dt_hours * normalized.charge_efficiency
    return profile


def unmet_ev_energy_kwh(
    plan: EVChargePlan | None,
    delivered_kw: Sequence[float],
    dt_hours: float,
) -> float:
    """Return the plan energy a solved profile failed to deliver."""
    if plan is None or dt_hours <= 0:
        return 0.0
    delivered_kwh = sum(
        max(0.0, _finite(value)) * dt_hours * plan.charge_efficiency
        for value in delivered_kw
    )
    return max(0.0, plan.energy_needed_kwh - delivered_kwh)
