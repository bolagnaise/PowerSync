"""
Battery optimization engine using Linear Programming.

Solves the optimal battery charge/discharge schedule to minimize electricity costs
or maximize profit/self-consumption based on price forecasts and solar predictions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# Optional dependency - optimization won't work without it
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)


class CostFunction(Enum):
    """Optimization objective functions."""
    COST_MINIMIZATION = "cost"           # Minimize total electricity cost
    PROFIT_MAXIMIZATION = "profit"       # Maximize profit from grid trading
    SELF_CONSUMPTION = "self_consumption"  # Maximize solar self-consumption


@dataclass
class OptimizationConfig:
    """Configuration for the optimization problem."""
    # Battery parameters
    battery_capacity_wh: float = 13500.0  # Total battery capacity in Wh
    max_charge_w: float = 5000.0          # Maximum charge power in W
    max_discharge_w: float = 5000.0       # Maximum discharge power in W
    charge_efficiency: float = 0.90       # Round-trip efficiency for charging
    discharge_efficiency: float = 0.90    # Round-trip efficiency for discharging

    # Constraints
    backup_reserve: float = 0.20          # Minimum SOC to maintain (0-1)
    target_end_soc: float | None = None   # Optional target SOC at end of horizon
    min_soc: float = 0.0                  # Minimum allowed SOC (0-1)
    max_soc: float = 1.0                  # Maximum allowed SOC (0-1)

    # Grid constraints
    max_grid_import_w: float | None = None   # Max grid import power (None = unlimited)
    max_grid_export_w: float | None = None   # Max grid export power (None = unlimited)

    # Optimization settings
    cost_function: CostFunction = CostFunction.COST_MINIMIZATION
    interval_minutes: int = 5             # Time interval in minutes
    horizon_hours: int = 48               # Optimization horizon in hours

    # Degradation penalty (optional)
    cycle_cost: float = 0.0               # Cost per kWh cycled (for battery wear)


@dataclass
class OptimizationResult:
    """Result from the optimization solver."""
    success: bool
    status: str

    # Schedules (per interval) - legacy/aggregate fields
    charge_schedule_w: list[float] = field(default_factory=list)    # Total charge power (W)
    discharge_schedule_w: list[float] = field(default_factory=list) # Total discharge power (W)
    grid_import_w: list[float] = field(default_factory=list)        # Total grid import (W)
    grid_export_w: list[float] = field(default_factory=list)        # Total grid export (W)
    soc_trajectory: list[float] = field(default_factory=list)       # SOC at each interval (0-1)

    # Detailed power flow breakdown (new)
    battery_consume_w: list[float] = field(default_factory=list)    # Battery → Load (W)
    battery_export_w: list[float] = field(default_factory=list)     # Battery → Grid (W)
    solar_to_load_w: list[float] = field(default_factory=list)      # Solar → Load (W)
    solar_to_battery_w: list[float] = field(default_factory=list)   # Solar → Battery (W)
    solar_to_grid_w: list[float] = field(default_factory=list)      # Solar → Grid (W)
    grid_to_load_w: list[float] = field(default_factory=list)       # Grid → Load (W)
    grid_to_battery_w: list[float] = field(default_factory=list)    # Grid → Battery (W)

    # Timestamps for each interval
    timestamps: list[datetime] = field(default_factory=list)

    # Summary metrics
    total_cost: float = 0.0               # Total electricity cost ($)
    total_import_kwh: float = 0.0         # Total grid import (kWh)
    total_export_kwh: float = 0.0         # Total grid export (kWh)
    total_charge_kwh: float = 0.0         # Total battery charge (kWh)
    total_discharge_kwh: float = 0.0      # Total battery discharge (kWh)
    average_import_price: float = 0.0     # Average import price ($/kWh)
    average_export_price: float = 0.0     # Average export price ($/kWh)

    # Detailed metrics (new)
    total_battery_consume_kwh: float = 0.0  # Battery used to power home (kWh)
    total_battery_export_kwh: float = 0.0   # Battery exported to grid (kWh)
    total_solar_consumed_kwh: float = 0.0   # Solar used (load + battery) (kWh)
    total_solar_exported_kwh: float = 0.0   # Solar exported to grid (kWh)

    # Comparison metrics
    baseline_cost: float = 0.0            # Cost without optimization
    savings: float = 0.0                  # Savings vs baseline ($)

    # Solver info
    solve_time_ms: float = 0.0
    solver_name: str = ""

    def get_action_at_index(self, index: int) -> dict[str, Any]:
        """Get the recommended action for a specific interval."""
        if index < 0 or index >= len(self.charge_schedule_w):
            return {"action": "idle", "power_w": 0}

        charge_w = self.charge_schedule_w[index]
        discharge_w = self.discharge_schedule_w[index]

        # Get detailed breakdown if available
        consume_w = self.battery_consume_w[index] if index < len(self.battery_consume_w) else 0
        export_w = self.battery_export_w[index] if index < len(self.battery_export_w) else 0

        if charge_w > 10:  # Small threshold to avoid floating point noise
            return {"action": "charge", "power_w": charge_w}
        elif discharge_w > 10:
            # Determine if primarily consuming (powering home) or exporting (to grid)
            if consume_w > export_w:
                return {"action": "consume", "power_w": discharge_w, "to_load_w": consume_w, "to_grid_w": export_w}
            elif export_w > 10:
                return {"action": "export", "power_w": discharge_w, "to_load_w": consume_w, "to_grid_w": export_w}
            else:
                return {"action": "consume", "power_w": discharge_w, "to_load_w": consume_w, "to_grid_w": export_w}
        else:
            return {"action": "idle", "power_w": 0}

    def get_next_actions(self, count: int = 5) -> list[dict[str, Any]]:
        """Get the next N actions from the schedule."""
        actions = []
        for i in range(min(count, len(self.charge_schedule_w))):
            action = self.get_action_at_index(i)
            action["timestamp"] = self.timestamps[i].isoformat() if i < len(self.timestamps) else None
            action["soc"] = self.soc_trajectory[i] if i < len(self.soc_trajectory) else None
            actions.append(action)
        return actions

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "status": self.status,
            "schedule": {
                "charge_w": self.charge_schedule_w,
                "discharge_w": self.discharge_schedule_w,
                "grid_import_w": self.grid_import_w,
                "grid_export_w": self.grid_export_w,
                "soc": self.soc_trajectory,
                "timestamps": [t.isoformat() for t in self.timestamps],
            },
            "summary": {
                "total_cost": round(self.total_cost, 2),
                "total_import_kwh": round(self.total_import_kwh, 2),
                "total_export_kwh": round(self.total_export_kwh, 2),
                "total_charge_kwh": round(self.total_charge_kwh, 2),
                "total_discharge_kwh": round(self.total_discharge_kwh, 2),
                "average_import_price": round(self.average_import_price, 4),
                "average_export_price": round(self.average_export_price, 4),
                "baseline_cost": round(self.baseline_cost, 2),
                "savings": round(self.savings, 2),
            },
            "solver": {
                "solve_time_ms": round(self.solve_time_ms, 2),
                "solver_name": self.solver_name,
            },
            "next_actions": self.get_next_actions(5),
        }


class BatteryOptimiser:
    """
    Linear Programming optimiser for battery scheduling.

    Uses CVXPY with HiGHS solver to find the optimal charge/discharge schedule
    that minimizes electricity costs while respecting battery constraints.
    """

    def __init__(self, config: OptimizationConfig | None = None):
        """Initialize the optimiser with configuration."""
        self.config = config or OptimizationConfig()
        self._solver_available = self._check_solver()

    def _check_solver(self) -> bool:
        """Check if CVXPY, numpy, and HiGHS solver are available."""
        if not NUMPY_AVAILABLE:
            _LOGGER.info("NumPy not installed locally - will use PowerSync Optimiser add-on if available")
            return False

        try:
            import cvxpy as cp
            # Check if HiGHS is available
            if "HIGHS" in cp.installed_solvers():
                _LOGGER.info("CVXPY with HiGHS solver available")
                return True
            # Fall back to other solvers
            available = cp.installed_solvers()
            _LOGGER.warning(f"HiGHS not available, using fallback. Available: {available}")
            return len(available) > 0
        except ImportError:
            _LOGGER.info("CVXPY not installed locally - will use PowerSync Optimiser add-on if available")
            return False

    @property
    def is_available(self) -> bool:
        """Check if optimization is available."""
        return self._solver_available

    def optimize(
        self,
        prices_import: list[float],      # $/kWh for each interval
        prices_export: list[float],      # $/kWh for each interval
        solar_forecast: list[float],     # Watts for each interval
        load_forecast: list[float],      # Watts for each interval
        initial_soc: float,              # Current SOC (0-1)
        start_time: datetime,            # Start time of optimization
        config: OptimizationConfig | None = None,
    ) -> OptimizationResult:
        """
        Run the battery optimization.

        Args:
            prices_import: Import prices in $/kWh for each interval
            prices_export: Export prices in $/kWh for each interval (positive = you get paid)
            solar_forecast: Solar generation forecast in Watts
            load_forecast: Load consumption forecast in Watts
            initial_soc: Current battery state of charge (0-1)
            start_time: Start time of the optimization horizon
            config: Optional override configuration

        Returns:
            OptimizationResult with optimal schedule
        """
        import time
        start_solve = time.time()

        cfg = config or self.config

        # Validate inputs
        n_intervals = len(prices_import)
        if not all(len(x) == n_intervals for x in [prices_export, solar_forecast, load_forecast]):
            return OptimizationResult(
                success=False,
                status="Input arrays must have same length",
            )

        if n_intervals == 0:
            return OptimizationResult(
                success=False,
                status="No intervals provided",
            )

        if not self._solver_available:
            return self._fallback_schedule(
                prices_import, prices_export, solar_forecast, load_forecast,
                initial_soc, start_time, cfg
            )

        try:
            result = self._solve_lp(
                prices_import, prices_export, solar_forecast, load_forecast,
                initial_soc, start_time, cfg, n_intervals
            )
            result.solve_time_ms = (time.time() - start_solve) * 1000
            return result

        except Exception as e:
            _LOGGER.error(f"Optimization failed: {e}", exc_info=True)
            return OptimizationResult(
                success=False,
                status=f"Solver error: {str(e)}",
            )

    def _solve_lp(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        initial_soc: float,
        start_time: datetime,
        cfg: OptimizationConfig,
        n_intervals: int,
    ) -> OptimizationResult:
        """Solve the LP optimization problem using CVXPY."""
        import cvxpy as cp

        # Convert to numpy arrays
        p_import = np.array(prices_import)  # $/kWh
        p_export = np.array(prices_export)  # $/kWh
        solar = np.array(solar_forecast)    # W
        load = np.array(load_forecast)      # W

        # Time interval in hours
        dt_hours = cfg.interval_minutes / 60.0

        # Capacity in Wh
        capacity_wh = cfg.battery_capacity_wh

        # Decision variables
        charge = cp.Variable(n_intervals, nonneg=True)      # Charge power (W)
        discharge = cp.Variable(n_intervals, nonneg=True)   # Discharge power (W)
        grid_import = cp.Variable(n_intervals, nonneg=True) # Grid import (W)
        grid_export = cp.Variable(n_intervals, nonneg=True) # Grid export (W)
        soc = cp.Variable(n_intervals + 1)                  # SOC at each step (0-1)

        constraints = []

        # Initial SOC constraint
        constraints.append(soc[0] == initial_soc)

        # SOC dynamics: soc[t+1] = soc[t] + (charge*eta - discharge/eta) * dt / capacity
        for t in range(n_intervals):
            energy_in = charge[t] * cfg.charge_efficiency * dt_hours  # Wh
            energy_out = discharge[t] / cfg.discharge_efficiency * dt_hours  # Wh
            delta_soc = (energy_in - energy_out) / capacity_wh
            constraints.append(soc[t + 1] == soc[t] + delta_soc)

        # SOC bounds
        min_soc = max(cfg.min_soc, cfg.backup_reserve)
        for t in range(n_intervals + 1):
            constraints.append(soc[t] >= min_soc)
            constraints.append(soc[t] <= cfg.max_soc)

        # Target end SOC if specified
        if cfg.target_end_soc is not None:
            constraints.append(soc[n_intervals] >= cfg.target_end_soc)

        # Power limits
        constraints.append(charge <= cfg.max_charge_w)
        constraints.append(discharge <= cfg.max_discharge_w)

        # Grid limits if specified
        if cfg.max_grid_import_w is not None:
            constraints.append(grid_import <= cfg.max_grid_import_w)
        if cfg.max_grid_export_w is not None:
            constraints.append(grid_export <= cfg.max_grid_export_w)

        # Power balance: solar + grid_import + discharge = load + grid_export + charge
        for t in range(n_intervals):
            power_in = solar[t] + grid_import[t] + discharge[t]
            power_out = load[t] + grid_export[t] + charge[t]
            constraints.append(power_in == power_out)

        # Objective function
        if cfg.cost_function == CostFunction.COST_MINIMIZATION:
            # Minimize: sum of (import_price * grid_import - export_price * grid_export)
            # Convert W to kWh: power * dt_hours / 1000
            import_cost = cp.sum(cp.multiply(p_import, grid_import)) * dt_hours / 1000
            export_revenue = cp.sum(cp.multiply(p_export, grid_export)) * dt_hours / 1000
            objective = cp.Minimize(import_cost - export_revenue)

        elif cfg.cost_function == CostFunction.PROFIT_MAXIMIZATION:
            # Maximize: export revenue - import cost
            import_cost = cp.sum(cp.multiply(p_import, grid_import)) * dt_hours / 1000
            export_revenue = cp.sum(cp.multiply(p_export, grid_export)) * dt_hours / 1000
            objective = cp.Maximize(export_revenue - import_cost)

        else:  # SELF_CONSUMPTION
            # Minimize grid import (high weight) with small cost consideration
            import_penalty = cp.sum(grid_import) * 1000  # Heavy penalty on imports
            import_cost = cp.sum(cp.multiply(p_import, grid_import)) * dt_hours / 1000
            objective = cp.Minimize(import_penalty + import_cost)

        # Add cycle cost if specified
        if cfg.cycle_cost > 0:
            cycle_penalty = cfg.cycle_cost * cp.sum(charge + discharge) * dt_hours / 1000
            objective = cp.Minimize(objective.args[0] + cycle_penalty)

        # Solve the problem
        problem = cp.Problem(objective, constraints)

        # Try HiGHS first, fall back to other solvers
        solver_name = "HIGHS"
        try:
            if "HIGHS" in cp.installed_solvers():
                problem.solve(solver=cp.HIGHS, verbose=False)
            else:
                # Fall back to available solver
                problem.solve(verbose=False)
                solver_name = "default"
        except Exception as e:
            _LOGGER.warning(f"Primary solver failed: {e}, trying fallback")
            problem.solve(verbose=False)
            solver_name = "fallback"

        # Check solution status
        if problem.status not in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE]:
            return OptimizationResult(
                success=False,
                status=f"Solver status: {problem.status}",
                solver_name=solver_name,
            )

        # Extract results
        charge_w = np.maximum(charge.value, 0).tolist()
        discharge_w = np.maximum(discharge.value, 0).tolist()
        import_w = np.maximum(grid_import.value, 0).tolist()
        export_w = np.maximum(grid_export.value, 0).tolist()
        soc_values = np.clip(soc.value, 0, 1).tolist()

        # Generate timestamps
        timestamps = [
            start_time + timedelta(minutes=cfg.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate summary metrics
        total_import_kwh = sum(import_w) * dt_hours / 1000
        total_export_kwh = sum(export_w) * dt_hours / 1000
        total_charge_kwh = sum(charge_w) * dt_hours / 1000
        total_discharge_kwh = sum(discharge_w) * dt_hours / 1000

        total_import_cost = sum(p * e * dt_hours / 1000 for p, e in zip(p_import, import_w))
        total_export_revenue = sum(p * e * dt_hours / 1000 for p, e in zip(p_export, export_w))
        total_cost = total_import_cost - total_export_revenue

        avg_import_price = total_import_cost / total_import_kwh if total_import_kwh > 0 else 0
        avg_export_price = total_export_revenue / total_export_kwh if total_export_kwh > 0 else 0

        # Calculate baseline (no battery optimization - direct pass-through)
        baseline_cost = self._calculate_baseline_cost(
            p_import, p_export, solar, load, dt_hours
        )

        return OptimizationResult(
            success=True,
            status="optimal",
            charge_schedule_w=charge_w,
            discharge_schedule_w=discharge_w,
            grid_import_w=import_w,
            grid_export_w=export_w,
            soc_trajectory=soc_values,
            timestamps=timestamps,
            total_cost=total_cost,
            total_import_kwh=total_import_kwh,
            total_export_kwh=total_export_kwh,
            total_charge_kwh=total_charge_kwh,
            total_discharge_kwh=total_discharge_kwh,
            average_import_price=avg_import_price,
            average_export_price=avg_export_price,
            baseline_cost=baseline_cost,
            savings=baseline_cost - total_cost,
            solver_name=solver_name,
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
                # Need to import
                energy_kwh = net_load * dt_hours / 1000
                total_cost += prices_import[t] * energy_kwh
            else:
                # Excess solar - export
                energy_kwh = -net_load * dt_hours / 1000
                total_cost -= prices_export[t] * energy_kwh
        return total_cost

    def _fallback_schedule(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        initial_soc: float,
        start_time: datetime,
        cfg: OptimizationConfig,
    ) -> OptimizationResult:
        """
        Generate a simple heuristic schedule when LP solver is unavailable.

        Strategy: Charge during lowest price periods, discharge during highest.
        """
        n_intervals = len(prices_import)
        dt_hours = cfg.interval_minutes / 60.0
        capacity_wh = cfg.battery_capacity_wh

        # Simple threshold-based strategy
        avg_import = sum(prices_import) / n_intervals
        avg_export = sum(prices_export) / n_intervals

        charge_w = []
        discharge_w = []
        import_w = []
        export_w = []
        soc_values = [initial_soc]

        current_soc = initial_soc

        for t in range(n_intervals):
            solar = solar_forecast[t]
            load = load_forecast[t]
            net_load = load - solar

            charge_power = 0.0
            discharge_power = 0.0

            # Low price - charge if battery not full
            if prices_import[t] < avg_import * 0.7 and current_soc < cfg.max_soc - 0.1:
                available_capacity = (cfg.max_soc - current_soc) * capacity_wh
                max_energy = cfg.max_charge_w * dt_hours
                charge_power = min(cfg.max_charge_w, available_capacity / dt_hours)

            # High export price - discharge if battery not at reserve
            elif prices_export[t] > avg_export * 1.3 and current_soc > cfg.backup_reserve + 0.1:
                available_energy = (current_soc - cfg.backup_reserve) * capacity_wh
                discharge_power = min(cfg.max_discharge_w, available_energy / dt_hours)

            # Update SOC
            energy_in = charge_power * cfg.charge_efficiency * dt_hours
            energy_out = discharge_power / cfg.discharge_efficiency * dt_hours
            current_soc += (energy_in - energy_out) / capacity_wh
            current_soc = max(cfg.backup_reserve, min(cfg.max_soc, current_soc))

            # Calculate grid flows
            net_with_battery = net_load + charge_power - discharge_power
            grid_in = max(0, net_with_battery)
            grid_out = max(0, -net_with_battery)

            charge_w.append(charge_power)
            discharge_w.append(discharge_power)
            import_w.append(grid_in)
            export_w.append(grid_out)
            soc_values.append(current_soc)

        timestamps = [
            start_time + timedelta(minutes=cfg.interval_minutes * i)
            for i in range(n_intervals)
        ]

        # Calculate costs
        total_import = sum(import_w) * dt_hours / 1000
        total_export = sum(export_w) * dt_hours / 1000
        total_charge = sum(charge_w) * dt_hours / 1000
        total_discharge = sum(discharge_w) * dt_hours / 1000

        total_cost = sum(p * e * dt_hours / 1000 for p, e in zip(prices_import, import_w))
        total_cost -= sum(p * e * dt_hours / 1000 for p, e in zip(prices_export, export_w))

        baseline_cost = self._calculate_baseline_cost(
            np.array(prices_import), np.array(prices_export),
            np.array(solar_forecast), np.array(load_forecast), dt_hours
        )

        return OptimizationResult(
            success=True,
            status="heuristic (solver unavailable)",
            charge_schedule_w=charge_w,
            discharge_schedule_w=discharge_w,
            grid_import_w=import_w,
            grid_export_w=export_w,
            soc_trajectory=soc_values,
            timestamps=timestamps,
            total_cost=total_cost,
            total_import_kwh=total_import,
            total_export_kwh=total_export,
            total_charge_kwh=total_charge,
            total_discharge_kwh=total_discharge,
            baseline_cost=baseline_cost,
            savings=baseline_cost - total_cost,
            solver_name="heuristic",
        )
