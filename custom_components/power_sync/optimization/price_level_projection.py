"""Pure Price-Level Charging policy and forward projection helpers.

The projection is advisory.  It never owns a loadpoint or authorises a charger
command; the live PriceLevelChargingExecutor remains the command authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math
from typing import Any, Sequence


FULL_EV_SOC = 100.0
CHARGING_EFFICIENCY = 0.9


def manual_force_block_reason(force_state: Any) -> str | None:
    """Return a projection block only for a non-optimizer force state."""
    if not isinstance(force_state, dict) or not force_state.get("active"):
        return None
    if force_state.get("type") not in {"charge", "discharge"}:
        return None
    if (
        str(force_state.get("source") or "").strip().lower() == "optimizer"
        or str(force_state.get("scope") or "").strip().lower() == "optimizer"
    ):
        return None
    return f"Manual force {force_state.get('type')} is active"


@dataclass(frozen=True)
class PriceLevelPolicyDecision:
    """Deterministic price/SOC classification shared by live and forecast paths."""

    should_charge: bool
    reason: str
    mode: str = ""

    @property
    def trigger(self) -> str | None:
        if self.mode == "price_level_recovery":
            return "recovery"
        if self.mode == "price_level_opportunity":
            return "opportunity"
        return None


def classify_price_level_policy(
    *,
    ev_soc_percent: float | None,
    price_cents: float | None,
    recovery_soc: float,
    recovery_price_cents: float,
    opportunity_price_cents: float,
) -> PriceLevelPolicyDecision:
    """Return the existing Price-Level decision after live gates have passed."""

    soc_known = ev_soc_percent is not None
    if soc_known and ev_soc_percent >= FULL_EV_SOC:
        return PriceLevelPolicyDecision(
            False,
            f"EV {ev_soc_percent:g}% >= {FULL_EV_SOC:g}%, already full",
        )
    if price_cents is None or not math.isfinite(float(price_cents)):
        return PriceLevelPolicyDecision(False, "No price data available")

    price = float(price_cents)
    if soc_known and ev_soc_percent < recovery_soc:
        if price <= recovery_price_cents:
            return PriceLevelPolicyDecision(
                True,
                f"Recovery: EV {ev_soc_percent:g}% < {recovery_soc:g}%, "
                f"price {price:.1f}c <= {recovery_price_cents:g}c",
                "price_level_recovery",
            )
        return PriceLevelPolicyDecision(
            False,
            f"Recovery: EV {ev_soc_percent:g}% < {recovery_soc:g}%, "
            f"but price {price:.1f}c > {recovery_price_cents:g}c",
        )

    soc_label = f"{ev_soc_percent:g}%" if soc_known else "unknown"
    if price <= opportunity_price_cents:
        return PriceLevelPolicyDecision(
            True,
            f"Opportunity: EV {soc_label}, price {price:.1f}c <= "
            f"{opportunity_price_cents:g}c",
            "price_level_opportunity",
        )
    if not soc_known and price <= recovery_price_cents:
        return PriceLevelPolicyDecision(
            True,
            f"Recovery fallback: EV SOC unknown, price {price:.1f}c <= "
            f"{recovery_price_cents:g}c",
            "price_level_recovery",
        )
    if soc_known:
        reason = (
            f"EV {soc_label} >= {recovery_soc:g}%, price {price:.1f}c > "
            f"{opportunity_price_cents:g}c"
        )
    else:
        reason = (
            f"EV SOC unknown, price {price:.1f}c > recovery price "
            f"{recovery_price_cents:g}c"
        )
    return PriceLevelPolicyDecision(False, reason)


@dataclass(frozen=True)
class PriceLevelVehicleSnapshot:
    """Read-only live facts used to decide projection confidence."""

    vehicle_id: str
    loadpoint_id: str
    display_name: str
    ev_soc_percent: float | None
    location: str
    plugged_in: bool | None
    home_battery_soc_percent: float | None
    charger_power_w: float | None
    charger_power_known: bool
    battery_capacity_kwh: float | None
    battery_capacity_source: str | None = None
    provider: str | None = None
    observation_quality: str = "high"
    options: tuple[tuple[str, Any], ...] = ()
    blocked_by: str | None = None
    blocking_reason: str | None = None
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceLevelProjectionWindow:
    """One contiguous expected, conditional, or suppressed Price-Level range."""

    vehicle_id: str
    loadpoint_id: str
    display_name: str
    start: datetime
    end: datetime
    trigger: str
    classification: str
    power_cap_w: float
    expected_energy_wh: float | None
    included_in_optimizer: bool
    confidence: str
    price_threshold_cents: float
    assumptions: tuple[str, ...] = ()
    suppressed_by: str | None = None
    blocking_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": (
                f"price_level:{self.loadpoint_id}:"
                f"{self.start.isoformat()}:{self.trigger}:{self.classification}"
            ),
            "source": "price_level",
            "vehicle_id": self.vehicle_id,
            "loadpoint_id": self.loadpoint_id,
            "display_name": self.display_name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "trigger": self.trigger,
            "classification": self.classification,
            "power_cap_w": round(self.power_cap_w, 3),
            "expected_energy_wh": (
                round(self.expected_energy_wh, 3)
                if self.expected_energy_wh is not None
                else None
            ),
            "included_in_optimizer": self.included_in_optimizer,
            "confidence": self.confidence,
            "price_threshold_cents": round(self.price_threshold_cents, 3),
            "assumptions": list(self.assumptions),
            "suppressed_by": self.suppressed_by,
            "blocking_reason": self.blocking_reason,
        }


@dataclass(frozen=True)
class PriceLevelProjection:
    """Aligned Price-Level result before Smart Schedule/external arbitration."""

    expected_w: tuple[float, ...]
    conditional_cap_w: tuple[float, ...]
    expected_by_loadpoint: dict[str, tuple[float, ...]]
    windows: tuple[PriceLevelProjectionWindow, ...]
    warnings: tuple[str, ...] = ()

    @classmethod
    def empty(cls, length: int, warning: str | None = None) -> "PriceLevelProjection":
        return cls(
            expected_w=tuple(0.0 for _ in range(length)),
            conditional_cap_w=tuple(0.0 for _ in range(length)),
            expected_by_loadpoint={},
            windows=(),
            warnings=(warning,) if warning else (),
        )

    def with_suppressed_expected(
        self,
        *,
        suppressed_by: str,
        reason: str,
    ) -> "PriceLevelProjection":
        """Keep provenance while removing every expected optimiser contribution."""

        windows = tuple(
            replace(
                window,
                classification="suppressed",
                expected_energy_wh=None,
                included_in_optimizer=False,
                confidence="low",
                suppressed_by=suppressed_by,
                blocking_reason=reason,
            )
            if window.classification == "expected"
            else window
            for window in self.windows
        )
        return replace(
            self,
            expected_w=tuple(0.0 for _ in self.expected_w),
            expected_by_loadpoint={},
            windows=windows,
        )


@dataclass
class _WindowAccumulator:
    vehicle: PriceLevelVehicleSnapshot
    start: datetime
    end: datetime
    trigger: str
    classification: str
    power_cap_w: float
    expected_energy_wh: float | None
    included_in_optimizer: bool
    confidence: str
    price_threshold_cents: float
    assumptions: tuple[str, ...]
    suppressed_by: str | None
    blocking_reason: str | None

    def extend(self, end: datetime, *, expected_energy_wh: float | None) -> None:
        self.end = end
        if expected_energy_wh is not None:
            self.expected_energy_wh = (self.expected_energy_wh or 0.0) + expected_energy_wh

    def freeze(self) -> PriceLevelProjectionWindow:
        return PriceLevelProjectionWindow(
            vehicle_id=self.vehicle.vehicle_id,
            loadpoint_id=self.vehicle.loadpoint_id,
            display_name=self.vehicle.display_name,
            start=self.start,
            end=self.end,
            trigger=self.trigger,
            classification=self.classification,
            power_cap_w=self.power_cap_w,
            expected_energy_wh=self.expected_energy_wh,
            included_in_optimizer=self.included_in_optimizer,
            confidence=self.confidence,
            price_threshold_cents=self.price_threshold_cents,
            assumptions=self.assumptions,
            suppressed_by=self.suppressed_by,
            blocking_reason=self.blocking_reason,
        )


def _slot_end(
    timestamps: Sequence[datetime],
    index: int,
    interval_minutes: int,
) -> datetime | None:
    start = timestamps[index]
    if not isinstance(start, datetime) or start.tzinfo is None:
        return None
    if index + 1 < len(timestamps):
        end = timestamps[index + 1]
        if not isinstance(end, datetime) or end.tzinfo is None or end <= start:
            return None
        return end
    return start + timedelta(minutes=max(1, interval_minutes))


def _expected_snapshot_reason(
    vehicle: PriceLevelVehicleSnapshot,
    *,
    home_battery_minimum: float,
    preserve_home_battery: bool,
    no_grid_import: bool,
) -> str | None:
    if vehicle.blocked_by:
        return vehicle.blocking_reason or f"{vehicle.blocked_by} currently owns this loadpoint"
    if vehicle.observation_quality != "high":
        return "Current vehicle observations are incomplete or uncertain"
    if vehicle.location != "home":
        return "Vehicle must still be at home"
    if vehicle.plugged_in is not True:
        return "Vehicle must still be plugged in"
    if vehicle.ev_soc_percent is None:
        return "EV SOC is unavailable"
    if vehicle.battery_capacity_kwh is None or vehicle.battery_capacity_kwh <= 0:
        return "Usable EV battery capacity is unavailable"
    if not vehicle.charger_power_known or not vehicle.charger_power_w or vehicle.charger_power_w <= 0:
        return "Configured charger capability is unavailable"
    if preserve_home_battery:
        return "Home-battery preservation depends on the live optimiser result"
    if no_grid_import:
        return "No-grid-import charging depends on live site headroom"
    if home_battery_minimum > 0:
        if vehicle.home_battery_soc_percent is None:
            return "Home battery SOC is unavailable"
        if vehicle.home_battery_soc_percent < home_battery_minimum:
            return (
                f"Home battery {vehicle.home_battery_soc_percent:g}% is below "
                f"the {home_battery_minimum:g}% minimum"
            )
    return None


def build_price_level_projection(
    *,
    timestamps: Sequence[datetime],
    prices_cents: Sequence[float | None],
    vehicles: Sequence[PriceLevelVehicleSnapshot],
    enabled: bool,
    recovery_soc: float,
    recovery_price_cents: float,
    opportunity_price_cents: float,
    home_battery_minimum: float,
    preserve_home_battery: bool,
    no_grid_import: bool,
    demand_blocked: Sequence[bool] | None = None,
    valid_price_slots: Sequence[bool] | None = None,
    interval_minutes: int = 5,
) -> PriceLevelProjection:
    """Project Price-Level intent without performing or authorising any action."""

    length = len(timestamps)
    if not enabled or length == 0:
        return PriceLevelProjection.empty(length)

    expected = [0.0] * length
    conditional_by_loadpoint: dict[str, list[float]] = {}
    by_loadpoint: dict[str, list[float]] = {}
    finished_windows: list[PriceLevelProjectionWindow] = []
    warnings: list[str] = []

    for vehicle in vehicles:
        if not vehicle.loadpoint_id:
            warnings.append(f"Skipped Price-Level projection for {vehicle.vehicle_id}: no loadpoint identity")
            continue
        power_w = max(0.0, float(vehicle.charger_power_w or 0.0))
        expected_block_reason = _expected_snapshot_reason(
            vehicle,
            home_battery_minimum=home_battery_minimum,
            preserve_home_battery=preserve_home_battery,
            no_grid_import=no_grid_import,
        )
        energy_remaining_wh: float | None = None
        if (
            expected_block_reason is None
            and vehicle.ev_soc_percent is not None
            and vehicle.ev_soc_percent < recovery_soc
            and vehicle.battery_capacity_kwh is not None
        ):
            energy_remaining_wh = (
                (recovery_soc - vehicle.ev_soc_percent)
                / 100.0
                * vehicle.battery_capacity_kwh
                * 1000.0
                / CHARGING_EFFICIENCY
            )

        active: _WindowAccumulator | None = None
        loadpoint_values = by_loadpoint.setdefault(vehicle.loadpoint_id, [0.0] * length)
        conditional_values = conditional_by_loadpoint.setdefault(
            vehicle.loadpoint_id, [0.0] * length
        )
        for index, start in enumerate(timestamps):
            end = _slot_end(timestamps, index, interval_minutes)
            if end is None:
                if active:
                    finished_windows.append(active.freeze())
                    active = None
                continue
            price_valid = (
                index < len(prices_cents)
                and (valid_price_slots is None or index >= len(valid_price_slots) or valid_price_slots[index])
            )
            try:
                price = float(prices_cents[index]) if price_valid and prices_cents[index] is not None else None
            except (TypeError, ValueError):
                price = None
            if price is None or not math.isfinite(price):
                if active:
                    finished_windows.append(active.freeze())
                    active = None
                continue

            projected_soc = vehicle.ev_soc_percent
            if energy_remaining_wh is not None and energy_remaining_wh <= 1e-6:
                projected_soc = recovery_soc
            decision = classify_price_level_policy(
                ev_soc_percent=projected_soc,
                price_cents=price,
                recovery_soc=recovery_soc,
                recovery_price_cents=recovery_price_cents,
                opportunity_price_cents=opportunity_price_cents,
            )
            if not decision.should_charge or decision.trigger is None:
                if active:
                    finished_windows.append(active.freeze())
                    active = None
                continue

            slot_suppressed_by = vehicle.blocked_by
            slot_block_reason = vehicle.blocking_reason
            if demand_blocked is not None and index < len(demand_blocked) and demand_blocked[index]:
                slot_suppressed_by = "demand_window"
                slot_block_reason = "Grid charging is blocked during this demand window"

            slot_energy_wh: float | None = None
            slot_power_w = power_w
            classification = "conditional"
            confidence = "low"
            included = False
            assumptions = tuple(vehicle.assumptions)
            if slot_suppressed_by:
                classification = "suppressed"
                slot_power_w = power_w
            elif (
                decision.trigger == "recovery"
                and energy_remaining_wh is not None
                and energy_remaining_wh > 1e-6
                and expected_block_reason is None
                and power_w > 0
            ):
                duration_hours = (end - start).total_seconds() / 3600.0
                max_slot_energy_wh = power_w * duration_hours
                slot_energy_wh = min(energy_remaining_wh, max_slot_energy_wh)
                slot_power_w = slot_energy_wh / duration_hours if duration_hours > 0 else 0.0
                energy_remaining_wh = max(0.0, energy_remaining_wh - slot_energy_wh)
                classification = "expected"
                confidence = "high"
                included = slot_power_w > 0
                expected[index] += slot_power_w
                loadpoint_values[index] = max(loadpoint_values[index], slot_power_w)
            else:
                if expected_block_reason:
                    assumptions = tuple(dict.fromkeys((*assumptions, expected_block_reason)))
                conditional_values[index] = max(
                    conditional_values[index], power_w
                )

            threshold = (
                recovery_price_cents
                if decision.trigger == "recovery"
                else opportunity_price_cents
            )
            window_key = (
                decision.trigger,
                classification,
                included,
                slot_suppressed_by,
                slot_block_reason,
                assumptions,
                round(slot_power_w, 3),
            )
            active_key = None
            if active is not None:
                active_key = (
                    active.trigger,
                    active.classification,
                    active.included_in_optimizer,
                    active.suppressed_by,
                    active.blocking_reason,
                    active.assumptions,
                    round(active.power_cap_w, 3),
                )
            if active is not None and active.end == start and active_key == window_key:
                active.extend(end, expected_energy_wh=slot_energy_wh)
            else:
                if active:
                    finished_windows.append(active.freeze())
                active = _WindowAccumulator(
                    vehicle=vehicle,
                    start=start,
                    end=end,
                    trigger=decision.trigger,
                    classification=classification,
                    power_cap_w=slot_power_w,
                    expected_energy_wh=slot_energy_wh,
                    included_in_optimizer=included,
                    confidence=confidence,
                    price_threshold_cents=threshold,
                    assumptions=assumptions,
                    suppressed_by=slot_suppressed_by,
                    blocking_reason=slot_block_reason,
                )
        if active:
            finished_windows.append(active.freeze())

    conditional = [
        sum(values[index] for values in conditional_by_loadpoint.values())
        for index in range(length)
    ]
    return PriceLevelProjection(
        expected_w=tuple(expected),
        conditional_cap_w=tuple(conditional),
        expected_by_loadpoint={
            key: tuple(values)
            for key, values in by_loadpoint.items()
            if any(value > 0 for value in values)
        },
        windows=tuple(sorted(finished_windows, key=lambda window: (window.start, window.loadpoint_id))),
        warnings=tuple(warnings),
    )
