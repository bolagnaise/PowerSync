"""
Multi-battery optimization support.

Extends the optimization engine to handle multiple battery systems,
such as:
- Multiple Tesla Powerwalls
- Tesla + Sigenergy
- Sigenergy + Sungrow
- Any combination of supported battery systems

The optimiser coordinates charging/discharging across all batteries
to achieve global cost minimization.
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

from .engine import CostFunction, OptimizationConfig, OptimizationResult

_LOGGER = logging.getLogger(__name__)


class BatterySystemType(Enum):
    """Supported battery system types."""
    TESLA = "tesla"
    SIGENERGY = "sigenergy"
    SUNGROW = "sungrow"
    GENERIC = "generic"


@dataclass
class BatteryConfig:
    """Configuration for a single battery in the multi-battery system."""
    battery_id: str
    name: str
    system_type: BatterySystemType

    # Capacity and power limits
    capacity_wh: float = 13500.0         # Battery capacity in Wh
    max_charge_w: float = 5000.0         # Max charge power in W
    max_discharge_w: float = 5000.0      # Max discharge power in W
    charge_efficiency: float = 0.90      # Charging efficiency
    discharge_efficiency: float = 0.90   # Discharging efficiency

    # Current state
    current_soc: float = 0.5             # Current SOC (0-1)

    # Constraints
    min_soc: float = 0.0                 # Minimum SOC
    max_soc: float = 1.0                 # Maximum SOC
    backup_reserve: float = 0.20         # Minimum reserve to maintain

    # Cost factors
    cycle_cost: float = 0.0              # Cost per kWh cycled (degradation)
    priority: int = 1                    # Priority (lower = discharge first)

    # Control capabilities
    can_charge_from_grid: bool = True    # Can charge from grid
    can_export_to_grid: bool = True      # Can export to grid


@dataclass
class MultiBatteryResult:
    """Result from multi-battery optimization."""
    success: bool
    status: str

    # Per-battery schedules
    battery_schedules: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # Format: {battery_id: {charge_w: [...], discharge_w: [...], soc: [...]}}

    # Aggregated schedules
    total_charge_w: list[float] = field(default_factory=list)
    total_discharge_w: list[float] = field(default_factory=list)
    grid_import_w: list[float] = field(default_factory=list)
    grid_export_w: list[float] = field(default_factory=list)

    # Timestamps
    timestamps: list[datetime] = field(default_factory=list)

    # Metrics
    total_cost: float = 0.0
    total_import_kwh: float = 0.0
    total_export_kwh: float = 0.0
    baseline_cost: float = 0.0
    savings: float = 0.0

    # Per-battery metrics
    battery_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "status": self.status,
            "batteries": {
                bid: {
                    "schedule": schedule,
                    "metrics": self.battery_metrics.get(bid, {}),
                }
                for bid, schedule in self.battery_schedules.items()
            },
            "aggregated": {
                "total_charge_w": self.total_charge_w,
                "total_discharge_w": self.total_discharge_w,
                "grid_import_w": self.grid_import_w,
                "grid_export_w": self.grid_export_w,
                "timestamps": [t.isoformat() for t in self.timestamps],
            },
            "summary": {
                "total_cost": round(self.total_cost, 2),
                "total_import_kwh": round(self.total_import_kwh, 2),
                "total_export_kwh": round(self.total_export_kwh, 2),
                "baseline_cost": round(self.baseline_cost, 2),
                "savings": round(self.savings, 2),
            },
        }


class MultiBatteryOptimiser:
    """
    Optimiser for multiple battery systems.

    Uses CVXPY to solve a joint optimization problem that coordinates
    charging and discharging across all batteries to minimize total cost.
    """

    def __init__(
        self,
        batteries: list[BatteryConfig],
        interval_minutes: int = 5,
        horizon_hours: int = 48,
        cost_function: CostFunction = CostFunction.COST_MINIMIZATION,
    ):
        """
        Initialize the multi-battery optimiser.

        Args:
            batteries: List of battery configurations
            interval_minutes: Time interval in minutes
            horizon_hours: Optimization horizon in hours
            cost_function: Optimization objective
        """
        self.batteries = {b.battery_id: b for b in batteries}
        self.interval_minutes = interval_minutes
        self.horizon_hours = horizon_hours
        self.cost_function = cost_function
        self._solver_available = self._check_solver()

    def _check_solver(self) -> bool:
        """Check if CVXPY is available."""
        try:
            import cvxpy as cp
            return len(cp.installed_solvers()) > 0
        except ImportError:
            return False

    def optimize(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        start_time: datetime,
        battery_states: dict[str, float] | None = None,  # {battery_id: current_soc}
    ) -> MultiBatteryResult:
        """
        Run multi-battery optimization.

        Args:
            prices_import: Import prices in $/kWh
            prices_export: Export prices in $/kWh
            solar_forecast: Solar generation in Watts
            load_forecast: Load consumption in Watts
            start_time: Start time of optimization
            battery_states: Optional override of current SOC for each battery

        Returns:
            MultiBatteryResult with optimal schedules for all batteries
        """
        n_intervals = len(prices_import)
        dt_hours = self.interval_minutes / 60.0

        # Update battery states if provided
        if battery_states:
            for bid, soc in battery_states.items():
                if bid in self.batteries:
                    self.batteries[bid].current_soc = soc

        if not self._solver_available:
            return self._heuristic_schedule(
                prices_import, prices_export, solar_forecast, load_forecast,
                start_time, n_intervals, dt_hours
            )

        try:
            return self._solve_multi_battery_lp(
                prices_import, prices_export, solar_forecast, load_forecast,
                start_time, n_intervals, dt_hours
            )
        except Exception as e:
            _LOGGER.error(f"Multi-battery optimization failed: {e}", exc_info=True)
            return MultiBatteryResult(
                success=False,
                status=f"Solver error: {str(e)}",
            )

    def _solve_multi_battery_lp(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        start_time: datetime,
        n_intervals: int,
        dt_hours: float,
    ) -> MultiBatteryResult:
        """Solve the multi-battery LP problem."""
        import cvxpy as cp

        p_import = np.array(prices_import)
        p_export = np.array(prices_export)
        solar = np.array(solar_forecast)
        load = np.array(load_forecast)

        # Create variables for each battery
        battery_vars = {}
        constraints = []

        for bid, batt in self.batteries.items():
            # Decision variables
            charge = cp.Variable(n_intervals, nonneg=True)
            discharge = cp.Variable(n_intervals, nonneg=True)
            soc = cp.Variable(n_intervals + 1)

            battery_vars[bid] = {
                "charge": charge,
                "discharge": discharge,
                "soc": soc,
            }

            # Initial SOC
            constraints.append(soc[0] == batt.current_soc)

            # SOC dynamics
            for t in range(n_intervals):
                energy_in = charge[t] * batt.charge_efficiency * dt_hours
                energy_out = discharge[t] / batt.discharge_efficiency * dt_hours
                delta_soc = (energy_in - energy_out) / batt.capacity_wh
                constraints.append(soc[t + 1] == soc[t] + delta_soc)

            # SOC bounds
            min_soc = max(batt.min_soc, batt.backup_reserve)
            for t in range(n_intervals + 1):
                constraints.append(soc[t] >= min_soc)
                constraints.append(soc[t] <= batt.max_soc)

            # Power limits
            constraints.append(charge <= batt.max_charge_w)
            constraints.append(discharge <= batt.max_discharge_w)

        # Grid variables
        grid_import = cp.Variable(n_intervals, nonneg=True)
        grid_export = cp.Variable(n_intervals, nonneg=True)

        # Power balance for each interval
        for t in range(n_intervals):
            total_charge = sum(battery_vars[bid]["charge"][t] for bid in self.batteries)
            total_discharge = sum(battery_vars[bid]["discharge"][t] for bid in self.batteries)

            power_in = solar[t] + grid_import[t] + total_discharge
            power_out = load[t] + grid_export[t] + total_charge
            constraints.append(power_in == power_out)

        # Objective function
        import_cost = cp.sum(cp.multiply(p_import, grid_import)) * dt_hours / 1000
        export_revenue = cp.sum(cp.multiply(p_export, grid_export)) * dt_hours / 1000

        if self.cost_function == CostFunction.COST_MINIMIZATION:
            objective = cp.Minimize(import_cost - export_revenue)
        elif self.cost_function == CostFunction.PROFIT_MAXIMIZATION:
            objective = cp.Maximize(export_revenue - import_cost)
        else:  # SELF_CONSUMPTION
            import_penalty = cp.sum(grid_import) * 1000
            objective = cp.Minimize(import_penalty + import_cost)

        # Add cycle costs for each battery
        total_cycle_cost = 0
        for bid, batt in self.batteries.items():
            if batt.cycle_cost > 0:
                vars = battery_vars[bid]
                cycle_penalty = batt.cycle_cost * cp.sum(vars["charge"] + vars["discharge"]) * dt_hours / 1000
                total_cycle_cost += cycle_penalty

        if total_cycle_cost != 0:
            objective = cp.Minimize(objective.args[0] + total_cycle_cost)

        # Solve
        problem = cp.Problem(objective, constraints)
        try:
            if "HIGHS" in cp.installed_solvers():
                problem.solve(solver=cp.HIGHS, verbose=False)
            else:
                problem.solve(verbose=False)
        except:
            problem.solve(verbose=False)

        if problem.status not in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE]:
            return MultiBatteryResult(
                success=False,
                status=f"Solver status: {problem.status}",
            )

        # Extract results
        battery_schedules = {}
        battery_metrics = {}

        total_charge_w = [0.0] * n_intervals
        total_discharge_w = [0.0] * n_intervals

        for bid in self.batteries:
            vars = battery_vars[bid]
            charge_w = np.maximum(vars["charge"].value, 0).tolist()
            discharge_w = np.maximum(vars["discharge"].value, 0).tolist()
            soc_vals = np.clip(vars["soc"].value, 0, 1).tolist()

            battery_schedules[bid] = {
                "charge_w": charge_w,
                "discharge_w": discharge_w,
                "soc": soc_vals,
            }

            # Accumulate totals
            for t in range(n_intervals):
                total_charge_w[t] += charge_w[t]
                total_discharge_w[t] += discharge_w[t]

            # Calculate per-battery metrics
            batt = self.batteries[bid]
            charge_kwh = sum(charge_w) * dt_hours / 1000
            discharge_kwh = sum(discharge_w) * dt_hours / 1000

            battery_metrics[bid] = {
                "charge_kwh": round(charge_kwh, 2),
                "discharge_kwh": round(discharge_kwh, 2),
                "cycles": round(discharge_kwh / (batt.capacity_wh / 1000), 2),
                "final_soc": round(soc_vals[-1], 3),
            }

        import_w = np.maximum(grid_import.value, 0).tolist()
        export_w = np.maximum(grid_export.value, 0).tolist()

        timestamps = [
            start_time + timedelta(minutes=self.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate summary metrics
        total_import_kwh = sum(import_w) * dt_hours / 1000
        total_export_kwh = sum(export_w) * dt_hours / 1000
        total_import_cost = sum(p * e * dt_hours / 1000 for p, e in zip(p_import, import_w))
        total_export_revenue = sum(p * e * dt_hours / 1000 for p, e in zip(p_export, export_w))
        total_cost = total_import_cost - total_export_revenue

        baseline_cost = self._calculate_baseline_cost(p_import, p_export, solar, load, dt_hours)

        return MultiBatteryResult(
            success=True,
            status="optimal",
            battery_schedules=battery_schedules,
            total_charge_w=total_charge_w,
            total_discharge_w=total_discharge_w,
            grid_import_w=import_w,
            grid_export_w=export_w,
            timestamps=timestamps,
            total_cost=total_cost,
            total_import_kwh=total_import_kwh,
            total_export_kwh=total_export_kwh,
            baseline_cost=baseline_cost,
            savings=baseline_cost - total_cost,
            battery_metrics=battery_metrics,
        )

    def _calculate_baseline_cost(
        self,
        prices_import: np.ndarray,
        prices_export: np.ndarray,
        solar: np.ndarray,
        load: np.ndarray,
        dt_hours: float,
    ) -> float:
        """Calculate baseline cost without battery optimization."""
        total_cost = 0.0
        for t in range(len(prices_import)):
            net_load = load[t] - solar[t]
            if net_load > 0:
                energy_kwh = net_load * dt_hours / 1000
                total_cost += prices_import[t] * energy_kwh
            else:
                energy_kwh = -net_load * dt_hours / 1000
                total_cost -= prices_export[t] * energy_kwh
        return total_cost

    def _heuristic_schedule(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        start_time: datetime,
        n_intervals: int,
        dt_hours: float,
    ) -> MultiBatteryResult:
        """Generate heuristic schedule for multiple batteries."""
        # Simple strategy: treat all batteries as one combined battery
        # and distribute charge/discharge proportionally

        # Calculate combined capacity
        total_capacity = sum(b.capacity_wh for b in self.batteries.values())
        total_max_charge = sum(b.max_charge_w for b in self.batteries.values())
        total_max_discharge = sum(b.max_discharge_w for b in self.batteries.values())

        # Average current SOC weighted by capacity
        weighted_soc = sum(
            b.current_soc * b.capacity_wh for b in self.batteries.values()
        ) / total_capacity if total_capacity > 0 else 0.5

        avg_backup_reserve = sum(b.backup_reserve for b in self.batteries.values()) / len(self.batteries)

        # Price thresholds
        avg_import = sum(prices_import) / n_intervals
        avg_export = sum(prices_export) / n_intervals

        # Generate combined schedule
        total_charge_w = []
        total_discharge_w = []
        import_w = []
        export_w = []
        current_soc = weighted_soc

        battery_schedules = {bid: {"charge_w": [], "discharge_w": [], "soc": [b.current_soc]}
                           for bid, b in self.batteries.items()}

        for t in range(n_intervals):
            solar = solar_forecast[t]
            load = load_forecast[t]
            net_load = load - solar

            charge = 0.0
            discharge = 0.0

            # Low price - charge
            if prices_import[t] < avg_import * 0.7 and current_soc < 0.9:
                max_energy = (0.9 - current_soc) * total_capacity
                charge = min(total_max_charge, max_energy / dt_hours)

            # High export price - discharge
            elif prices_export[t] > avg_export * 1.3 and current_soc > avg_backup_reserve + 0.1:
                available = (current_soc - avg_backup_reserve) * total_capacity
                discharge = min(total_max_discharge, available / dt_hours)

            # Update SOC
            energy_in = charge * 0.9 * dt_hours
            energy_out = discharge / 0.9 * dt_hours
            current_soc += (energy_in - energy_out) / total_capacity
            current_soc = max(avg_backup_reserve, min(0.9, current_soc))

            # Grid flows
            net_with_battery = net_load + charge - discharge
            grid_in = max(0, net_with_battery)
            grid_out = max(0, -net_with_battery)

            total_charge_w.append(charge)
            total_discharge_w.append(discharge)
            import_w.append(grid_in)
            export_w.append(grid_out)

            # Distribute to individual batteries proportionally
            for bid, batt in self.batteries.items():
                ratio = batt.capacity_wh / total_capacity if total_capacity > 0 else 1 / len(self.batteries)
                b_charge = charge * ratio
                b_discharge = discharge * ratio

                battery_schedules[bid]["charge_w"].append(b_charge)
                battery_schedules[bid]["discharge_w"].append(b_discharge)

                # Update individual SOC
                b_soc = battery_schedules[bid]["soc"][-1]
                b_energy_in = b_charge * batt.charge_efficiency * dt_hours
                b_energy_out = b_discharge / batt.discharge_efficiency * dt_hours
                b_soc += (b_energy_in - b_energy_out) / batt.capacity_wh
                b_soc = max(batt.backup_reserve, min(batt.max_soc, b_soc))
                battery_schedules[bid]["soc"].append(b_soc)

        timestamps = [
            start_time + timedelta(minutes=self.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate metrics
        total_import_kwh = sum(import_w) * dt_hours / 1000
        total_export_kwh = sum(export_w) * dt_hours / 1000
        total_cost = sum(p * e * dt_hours / 1000 for p, e in zip(prices_import, import_w))
        total_cost -= sum(p * e * dt_hours / 1000 for p, e in zip(prices_export, export_w))

        baseline_cost = self._calculate_baseline_cost(
            np.array(prices_import), np.array(prices_export),
            np.array(solar_forecast), np.array(load_forecast), dt_hours
        )

        # Per-battery metrics
        battery_metrics = {}
        for bid, batt in self.batteries.items():
            schedule = battery_schedules[bid]
            charge_kwh = sum(schedule["charge_w"]) * dt_hours / 1000
            discharge_kwh = sum(schedule["discharge_w"]) * dt_hours / 1000
            battery_metrics[bid] = {
                "charge_kwh": round(charge_kwh, 2),
                "discharge_kwh": round(discharge_kwh, 2),
                "cycles": round(discharge_kwh / (batt.capacity_wh / 1000), 2),
                "final_soc": round(schedule["soc"][-1], 3),
            }

        return MultiBatteryResult(
            success=True,
            status="heuristic",
            battery_schedules=battery_schedules,
            total_charge_w=total_charge_w,
            total_discharge_w=total_discharge_w,
            grid_import_w=import_w,
            grid_export_w=export_w,
            timestamps=timestamps,
            total_cost=total_cost,
            total_import_kwh=total_import_kwh,
            total_export_kwh=total_export_kwh,
            baseline_cost=baseline_cost,
            savings=baseline_cost - total_cost,
            battery_metrics=battery_metrics,
        )

    def add_battery(self, battery: BatteryConfig) -> None:
        """Add a battery to the optimiser."""
        self.batteries[battery.battery_id] = battery

    def remove_battery(self, battery_id: str) -> None:
        """Remove a battery from the optimiser."""
        if battery_id in self.batteries:
            del self.batteries[battery_id]

    def update_battery_state(self, battery_id: str, current_soc: float) -> None:
        """Update the current SOC of a battery."""
        if battery_id in self.batteries:
            self.batteries[battery_id].current_soc = current_soc

    def get_total_capacity(self) -> float:
        """Get total capacity of all batteries in Wh."""
        return sum(b.capacity_wh for b in self.batteries.values())

    def get_total_power(self) -> tuple[float, float]:
        """Get total charge and discharge power limits in W."""
        total_charge = sum(b.max_charge_w for b in self.batteries.values())
        total_discharge = sum(b.max_discharge_w for b in self.batteries.values())
        return total_charge, total_discharge
