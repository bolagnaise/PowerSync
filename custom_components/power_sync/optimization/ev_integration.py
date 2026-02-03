"""
EV charging integration for battery optimization.

Extends the optimization problem to include EV charging as a controllable load.
The optimiser can schedule EV charging during optimal price periods while
respecting:
- EV battery capacity and current SOC
- Charger power limits
- Departure time constraints
- Priority between home battery and EV charging

This enables whole-home energy optimization including vehicles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# Optional dependency
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)


class EVChargingPriority(Enum):
    """Priority for EV charging relative to home battery."""
    EV_FIRST = "ev_first"           # Charge EV before storing in home battery
    BATTERY_FIRST = "battery_first"  # Fill home battery first
    EQUAL = "equal"                  # No priority, optimize jointly
    SOLAR_ONLY = "solar_only"        # Only charge EV from excess solar


class EVChargerType(Enum):
    """Type of EV charger."""
    TESLA_WALL_CONNECTOR = "tesla_wall_connector"
    OCPP = "ocpp"
    GENERIC = "generic"


@dataclass
class EVConfig:
    """Configuration for an EV in the optimization."""
    vehicle_id: str
    name: str

    # Battery specs
    battery_capacity_kwh: float = 75.0    # Total battery capacity
    current_soc: float = 0.5              # Current state of charge (0-1)
    target_soc: float = 0.8               # Target SOC to reach
    min_soc: float = 0.2                  # Minimum SOC to maintain

    # Charger specs
    charger_type: EVChargerType = EVChargerType.GENERIC
    max_charge_kw: float = 7.4            # Max charging power (single phase)
    min_charge_kw: float = 1.4            # Minimum charging power (when active)
    charger_efficiency: float = 0.92      # Charging efficiency

    # Constraints
    departure_time: datetime | None = None  # When EV needs to be ready
    must_reach_target: bool = True          # Hard constraint vs soft target
    allow_grid_charging: bool = True        # Allow charging from grid
    solar_only: bool = False                # Only charge from solar surplus

    # Priority
    priority: EVChargingPriority = EVChargingPriority.EQUAL

    def energy_needed_kwh(self) -> float:
        """Calculate energy needed to reach target SOC."""
        if self.current_soc >= self.target_soc:
            return 0.0
        return (self.target_soc - self.current_soc) * self.battery_capacity_kwh

    def time_needed_hours(self) -> float:
        """Estimate hours needed to reach target at max charge rate."""
        energy = self.energy_needed_kwh()
        if energy <= 0 or self.max_charge_kw <= 0:
            return 0.0
        return energy / (self.max_charge_kw * self.charger_efficiency)


@dataclass
class EVChargingSchedule:
    """Optimized EV charging schedule."""
    vehicle_id: str
    success: bool
    status: str

    # Schedule (per interval)
    charge_power_w: list[float] = field(default_factory=list)
    soc_trajectory: list[float] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)

    # Metrics
    total_energy_kwh: float = 0.0
    solar_energy_kwh: float = 0.0
    grid_energy_kwh: float = 0.0
    total_cost: float = 0.0
    average_price: float = 0.0
    final_soc: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "vehicle_id": self.vehicle_id,
            "success": self.success,
            "status": self.status,
            "schedule": {
                "charge_power_w": self.charge_power_w,
                "soc": self.soc_trajectory,
                "timestamps": [t.isoformat() for t in self.timestamps],
            },
            "summary": {
                "total_energy_kwh": round(self.total_energy_kwh, 2),
                "solar_energy_kwh": round(self.solar_energy_kwh, 2),
                "grid_energy_kwh": round(self.grid_energy_kwh, 2),
                "total_cost": round(self.total_cost, 2),
                "average_price": round(self.average_price, 4),
                "final_soc": round(self.final_soc, 3),
            },
        }

    def should_charge_at(self, check_time: datetime, min_power_w: float = 100.0) -> tuple[bool, float]:
        """
        Check if charging should occur at the given time.

        Args:
            check_time: The time to check
            min_power_w: Minimum power threshold to consider as "charging"

        Returns:
            Tuple of (should_charge, power_w)
        """
        if not self.success or not self.timestamps or not self.charge_power_w:
            return False, 0.0

        # Find the interval that contains check_time
        for i, ts in enumerate(self.timestamps):
            # Check if check_time falls within this interval
            # Intervals are typically 30 minutes
            if i + 1 < len(self.timestamps):
                interval_end = self.timestamps[i + 1]
            else:
                # Last interval - assume same duration as previous
                if len(self.timestamps) > 1:
                    interval_duration = self.timestamps[1] - self.timestamps[0]
                else:
                    interval_duration = timedelta(minutes=30)
                interval_end = ts + interval_duration

            if ts <= check_time < interval_end:
                power_w = self.charge_power_w[i]
                should_charge = power_w >= min_power_w
                return should_charge, power_w

        return False, 0.0

    def get_next_charging_window(self, after_time: datetime, min_power_w: float = 100.0) -> tuple[datetime | None, datetime | None, float]:
        """
        Get the next charging window after the given time.

        Returns:
            Tuple of (start_time, end_time, avg_power_w) or (None, None, 0) if no window found
        """
        if not self.success or not self.timestamps or not self.charge_power_w:
            return None, None, 0.0

        window_start = None
        window_powers = []

        for i, ts in enumerate(self.timestamps):
            if ts < after_time:
                continue

            power_w = self.charge_power_w[i]
            is_charging = power_w >= min_power_w

            if is_charging and window_start is None:
                window_start = ts
                window_powers = [power_w]
            elif is_charging and window_start is not None:
                window_powers.append(power_w)
            elif not is_charging and window_start is not None:
                # End of window found
                avg_power = sum(window_powers) / len(window_powers) if window_powers else 0
                return window_start, ts, avg_power

        # If window extends to end of schedule
        if window_start is not None and window_powers:
            if len(self.timestamps) > 1:
                interval_duration = self.timestamps[1] - self.timestamps[0]
            else:
                interval_duration = timedelta(minutes=30)
            window_end = self.timestamps[-1] + interval_duration
            avg_power = sum(window_powers) / len(window_powers)
            return window_start, window_end, avg_power

        return None, None, 0.0


class EVOptimiser:
    """
    EV charging optimiser.

    Can be used standalone or integrated with the main battery optimiser.
    Schedules EV charging to minimize cost while meeting departure constraints.
    """

    def __init__(self, interval_minutes: int = 30):
        """Initialize the EV optimiser."""
        self.interval_minutes = interval_minutes
        self._solver_available = self._check_solver()

    def _check_solver(self) -> bool:
        """Check if CVXPY is available."""
        try:
            import cvxpy as cp
            return len(cp.installed_solvers()) > 0
        except ImportError:
            return False

    def optimize_single_ev(
        self,
        ev_config: EVConfig,
        prices_import: list[float],      # $/kWh
        solar_surplus: list[float],      # W available for EV (after home load)
        start_time: datetime,
    ) -> EVChargingSchedule:
        """
        Optimize charging schedule for a single EV.

        Args:
            ev_config: EV configuration and constraints
            prices_import: Import prices in $/kWh for each interval
            solar_surplus: Available solar surplus in Watts
            start_time: Start time of optimization

        Returns:
            EVChargingSchedule with optimal charging plan
        """
        n_intervals = len(prices_import)
        dt_hours = self.interval_minutes / 60.0

        if not self._solver_available:
            return self._heuristic_schedule(
                ev_config, prices_import, solar_surplus, start_time, n_intervals, dt_hours
            )

        try:
            return self._solve_ev_lp(
                ev_config, prices_import, solar_surplus, start_time, n_intervals, dt_hours
            )
        except Exception as e:
            _LOGGER.error(f"EV optimization failed: {e}")
            return EVChargingSchedule(
                vehicle_id=ev_config.vehicle_id,
                success=False,
                status=f"Solver error: {str(e)}",
            )

    def _solve_ev_lp(
        self,
        ev: EVConfig,
        prices: list[float],
        solar: list[float],
        start_time: datetime,
        n_intervals: int,
        dt_hours: float,
    ) -> EVChargingSchedule:
        """Solve EV charging LP problem."""
        import cvxpy as cp

        # Convert to arrays
        p_import = np.array(prices)
        solar_w = np.array(solar)

        # EV parameters
        capacity_wh = ev.battery_capacity_kwh * 1000
        max_charge_w = ev.max_charge_kw * 1000
        min_charge_w = ev.min_charge_kw * 1000

        # Decision variables
        ev_charge = cp.Variable(n_intervals, nonneg=True)  # EV charge power (W)
        ev_soc = cp.Variable(n_intervals + 1)              # EV SOC at each step
        grid_for_ev = cp.Variable(n_intervals, nonneg=True)  # Grid power for EV

        constraints = []

        # Initial SOC
        constraints.append(ev_soc[0] == ev.current_soc)

        # SOC dynamics
        for t in range(n_intervals):
            energy_in = ev_charge[t] * ev.charger_efficiency * dt_hours
            delta_soc = energy_in / capacity_wh
            constraints.append(ev_soc[t + 1] == ev_soc[t] + delta_soc)

        # SOC bounds
        for t in range(n_intervals + 1):
            constraints.append(ev_soc[t] >= ev.min_soc)
            constraints.append(ev_soc[t] <= 1.0)

        # Target SOC constraint
        if ev.must_reach_target:
            # Find departure interval
            if ev.departure_time:
                departure_interval = min(
                    n_intervals,
                    int((ev.departure_time - start_time).total_seconds() / (self.interval_minutes * 60))
                )
                if departure_interval > 0:
                    constraints.append(ev_soc[departure_interval] >= ev.target_soc)
            else:
                # Must reach target by end of horizon
                constraints.append(ev_soc[n_intervals] >= ev.target_soc)

        # Charging power limits
        constraints.append(ev_charge <= max_charge_w)

        # Grid/solar split for EV charging
        for t in range(n_intervals):
            # EV charge can come from solar surplus or grid
            if ev.solar_only:
                constraints.append(ev_charge[t] <= solar_w[t])
                constraints.append(grid_for_ev[t] == 0)
            elif not ev.allow_grid_charging:
                constraints.append(grid_for_ev[t] == 0)
                constraints.append(ev_charge[t] <= solar_w[t])
            else:
                # ev_charge = solar_used + grid_for_ev
                solar_for_ev = cp.minimum(ev_charge[t], solar_w[t])
                # Note: CVXPY min creates non-linear constraint, use different formulation
                constraints.append(grid_for_ev[t] >= ev_charge[t] - solar_w[t])

        # Objective: Minimize cost of grid charging
        charging_cost = cp.sum(cp.multiply(p_import, grid_for_ev)) * dt_hours / 1000

        # Add penalty for not reaching target (soft constraint when not must_reach)
        if not ev.must_reach_target:
            shortfall_penalty = 100 * cp.pos(ev.target_soc - ev_soc[n_intervals])
            objective = cp.Minimize(charging_cost + shortfall_penalty)
        else:
            objective = cp.Minimize(charging_cost)

        # Solve
        problem = cp.Problem(objective, constraints)
        try:
            problem.solve(solver=cp.HIGHS if "HIGHS" in cp.installed_solvers() else None, verbose=False)
        except:
            problem.solve(verbose=False)

        if problem.status not in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE]:
            return EVChargingSchedule(
                vehicle_id=ev.vehicle_id,
                success=False,
                status=f"Solver status: {problem.status}",
            )

        # Extract results
        charge_w = np.maximum(ev_charge.value, 0).tolist()
        soc_values = np.clip(ev_soc.value, 0, 1).tolist()
        grid_w = np.maximum(grid_for_ev.value, 0).tolist()

        timestamps = [
            start_time + timedelta(minutes=self.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate metrics
        total_energy = sum(charge_w) * dt_hours / 1000
        grid_energy = sum(grid_w) * dt_hours / 1000
        solar_energy = total_energy - grid_energy
        total_cost = sum(p * e * dt_hours / 1000 for p, e in zip(p_import, grid_w))
        avg_price = total_cost / grid_energy if grid_energy > 0 else 0

        return EVChargingSchedule(
            vehicle_id=ev.vehicle_id,
            success=True,
            status="optimal",
            charge_power_w=charge_w,
            soc_trajectory=soc_values,
            timestamps=timestamps,
            total_energy_kwh=total_energy,
            solar_energy_kwh=solar_energy,
            grid_energy_kwh=grid_energy,
            total_cost=total_cost,
            average_price=avg_price,
            final_soc=soc_values[-1] if soc_values else ev.current_soc,
        )

    def _heuristic_schedule(
        self,
        ev: EVConfig,
        prices: list[float],
        solar: list[float],
        start_time: datetime,
        n_intervals: int,
        dt_hours: float,
    ) -> EVChargingSchedule:
        """Generate heuristic EV charging schedule."""
        capacity_wh = ev.battery_capacity_kwh * 1000
        max_charge_w = ev.max_charge_kw * 1000

        # Calculate energy needed
        energy_needed_wh = ev.energy_needed_kwh() * 1000

        # Find cheapest periods
        price_indices = sorted(range(n_intervals), key=lambda i: prices[i])

        charge_w = [0.0] * n_intervals
        soc_values = [ev.current_soc]
        current_soc = ev.current_soc
        energy_added = 0.0

        # First pass: use solar surplus
        for t in range(n_intervals):
            if current_soc >= ev.target_soc:
                break

            solar_available = solar[t]
            if solar_available > 0:
                charge_power = min(max_charge_w, solar_available)
                energy = charge_power * ev.charger_efficiency * dt_hours
                new_soc = current_soc + energy / capacity_wh

                if new_soc <= 1.0:
                    charge_w[t] = charge_power
                    current_soc = new_soc
                    energy_added += energy

        # Second pass: cheapest grid periods
        if current_soc < ev.target_soc and ev.allow_grid_charging:
            for t in price_indices:
                if current_soc >= ev.target_soc:
                    break

                if charge_w[t] < max_charge_w:
                    remaining_capacity = max_charge_w - charge_w[t]
                    energy = remaining_capacity * ev.charger_efficiency * dt_hours
                    new_soc = current_soc + energy / capacity_wh

                    if new_soc <= 1.0:
                        charge_w[t] += remaining_capacity
                        current_soc = new_soc
                        energy_added += energy

        # Build SOC trajectory
        soc = ev.current_soc
        for t in range(n_intervals):
            energy = charge_w[t] * ev.charger_efficiency * dt_hours
            soc += energy / capacity_wh
            soc = min(1.0, soc)
            soc_values.append(soc)

        timestamps = [
            start_time + timedelta(minutes=self.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate metrics
        total_energy = sum(charge_w) * dt_hours / 1000
        solar_energy = min(total_energy, sum(min(c, s) for c, s in zip(charge_w, solar)) * dt_hours / 1000)
        grid_energy = total_energy - solar_energy
        total_cost = sum(prices[t] * max(0, charge_w[t] - solar[t]) * dt_hours / 1000 for t in range(n_intervals))

        return EVChargingSchedule(
            vehicle_id=ev.vehicle_id,
            success=True,
            status="heuristic",
            charge_power_w=charge_w,
            soc_trajectory=soc_values,
            timestamps=timestamps,
            total_energy_kwh=total_energy,
            solar_energy_kwh=solar_energy,
            grid_energy_kwh=grid_energy,
            total_cost=total_cost,
            average_price=total_cost / grid_energy if grid_energy > 0 else 0,
            final_soc=soc_values[-1] if soc_values else ev.current_soc,
        )


def integrate_ev_with_home_battery(
    home_optimiser,
    ev_configs: list[EVConfig],
    prices_import: list[float],
    prices_export: list[float],
    solar_forecast: list[float],
    load_forecast: list[float],
    initial_home_soc: float,
    start_time: datetime,
) -> tuple[Any, list[EVChargingSchedule]]:
    """
    Joint optimization of home battery and EV charging.

    This is a higher-level function that coordinates the home battery
    optimiser with EV charging to find a globally optimal solution.

    Args:
        home_optimiser: BatteryOptimiser instance
        ev_configs: List of EV configurations
        prices_import: Import prices
        prices_export: Export prices
        solar_forecast: Solar generation forecast
        load_forecast: Base load forecast (excluding EVs)
        initial_home_soc: Current home battery SOC
        start_time: Start time

    Returns:
        Tuple of (home_battery_result, list of ev_schedules)
    """
    n_intervals = len(prices_import)
    dt_hours = home_optimiser.config.interval_minutes / 60.0

    # Strategy: Iterative optimization
    # 1. First optimize home battery without EVs
    # 2. Calculate available surplus for EVs
    # 3. Optimize each EV
    # 4. Re-optimize home battery with EV loads
    # 5. Iterate until convergence

    ev_optimiser = EVOptimiser(home_optimiser.config.interval_minutes)

    # Step 1: Initial home battery optimization
    home_result = home_optimiser.optimize(
        prices_import=prices_import,
        prices_export=prices_export,
        solar_forecast=solar_forecast,
        load_forecast=load_forecast,
        initial_soc=initial_home_soc,
        start_time=start_time,
    )

    # Step 2: Calculate surplus for EVs
    # Surplus = solar - home_load - battery_charge + battery_discharge - grid_export
    ev_schedules = []

    if home_result.success:
        surplus = []
        for t in range(n_intervals):
            available = (
                solar_forecast[t]
                - load_forecast[t]
                - home_result.charge_schedule_w[t]
                + home_result.discharge_schedule_w[t]
            )
            surplus.append(max(0, available))

        # Step 3: Optimize each EV (in priority order)
        sorted_evs = sorted(ev_configs, key=lambda e: e.priority.value)

        for ev in sorted_evs:
            ev_schedule = ev_optimiser.optimize_single_ev(
                ev_config=ev,
                prices_import=prices_import,
                solar_surplus=surplus,
                start_time=start_time,
            )
            ev_schedules.append(ev_schedule)

            # Update surplus after this EV
            if ev_schedule.success:
                for t in range(n_intervals):
                    surplus[t] = max(0, surplus[t] - ev_schedule.charge_power_w[t])

        # Step 4: Re-optimize home battery including EV loads
        total_ev_load = [0.0] * n_intervals
        for schedule in ev_schedules:
            if schedule.success:
                for t in range(n_intervals):
                    if t < len(schedule.charge_power_w):
                        total_ev_load[t] += schedule.charge_power_w[t]

        combined_load = [load_forecast[t] + total_ev_load[t] for t in range(n_intervals)]

        home_result = home_optimiser.optimize(
            prices_import=prices_import,
            prices_export=prices_export,
            solar_forecast=solar_forecast,
            load_forecast=combined_load,
            initial_soc=initial_home_soc,
            start_time=start_time,
        )

    return home_result, ev_schedules
