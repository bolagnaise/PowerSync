"""
Built-in LP Battery Optimizer for PowerSync.

Replaces external HAEO dependency with a direct scipy-based Linear Programming
optimizer. Falls back to a greedy heuristic if scipy is unavailable.

Action model:
- CHARGE: Force grid → battery (LP detects grid_import > load)
- EXPORT: Force battery → grid for profit (LP detects grid_export > 0 AND battery_discharge > 0)
- IDLE: Hold battery at current SOC (set backup reserve = current SOC to prevent discharge)
- SELF_CONSUMPTION: Everything else — battery operates naturally (solar charging, home loads)

We only FORCE the battery when it needs to do something it wouldn't do naturally.
Grid charging and grid exporting require force commands. Everything else is natural
self-consumption behavior.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .schedule_reader import ScheduleAction, OptimizationSchedule

_LOGGER = logging.getLogger(__name__)

# Try to import scipy; fall back to greedy if unavailable
try:
    from scipy.optimize import linprog

    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    _LOGGER.warning(
        "scipy not available — using greedy fallback optimizer. "
        "Install scipy for optimal LP-based scheduling."
    )

# Action detection threshold (W) — below this, treat as idle to avoid rapid switching
ACTION_THRESHOLD_W = 100.0

# Default fallback prices when no price data is available ($/kWh)
DEFAULT_IMPORT_PRICE = 0.30
DEFAULT_EXPORT_PRICE = 0.08

# Battery round-trip efficiency
DEFAULT_EFFICIENCY = 0.92


@dataclass
class OptimizerResult:
    """Result from the LP optimizer."""

    schedule: OptimizationSchedule
    solve_time_s: float = 0.0
    objective_value: float = 0.0
    solver_used: str = "greedy"
    feasible: bool = True
    grid_import_w: list[float] = field(default_factory=list)
    grid_export_w: list[float] = field(default_factory=list)


class BatteryOptimizer:
    """
    LP-based battery optimizer using scipy.optimize.linprog.

    Solves a cost-minimization (or self-consumption) LP over a forecast horizon
    and maps the result to battery actions.
    """

    def __init__(
        self,
        capacity_wh: float = 13500,
        max_charge_w: float = 5000,
        max_discharge_w: float = 5000,
        efficiency: float = DEFAULT_EFFICIENCY,
        backup_reserve: float = 0.20,
        interval_minutes: int = 5,
        horizon_hours: int = 48,
    ):
        self.capacity_wh = capacity_wh
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.efficiency = efficiency
        self.backup_reserve = backup_reserve
        self.interval_minutes = interval_minutes
        self.horizon_hours = horizon_hours

        # Derived
        self.capacity_kwh = capacity_wh / 1000.0
        self.max_charge_kw = max_charge_w / 1000.0
        self.max_discharge_kw = max_discharge_w / 1000.0
        self.dt_hours = interval_minutes / 60.0  # time step in hours

    def update_config(
        self,
        capacity_wh: float | None = None,
        max_charge_w: float | None = None,
        max_discharge_w: float | None = None,
        efficiency: float | None = None,
        backup_reserve: float | None = None,
    ) -> None:
        """Update optimizer configuration."""
        if capacity_wh is not None:
            self.capacity_wh = capacity_wh
            self.capacity_kwh = capacity_wh / 1000.0
        if max_charge_w is not None:
            self.max_charge_w = max_charge_w
            self.max_charge_kw = max_charge_w / 1000.0
        if max_discharge_w is not None:
            self.max_discharge_w = max_discharge_w
            self.max_discharge_kw = max_discharge_w / 1000.0
        if efficiency is not None:
            self.efficiency = efficiency
        if backup_reserve is not None:
            self.backup_reserve = backup_reserve

    def optimize(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        current_soc: float,
        cost_function: str = "cost",
    ) -> OptimizerResult:
        """
        Run the LP optimization.

        Args:
            import_prices: Import price per kWh for each time step ($/kWh)
            export_prices: Export price per kWh for each time step ($/kWh)
            solar_forecast: Solar generation per time step (kW)
            load_forecast: Home load per time step (kW)
            current_soc: Current battery SOC (0-1)
            cost_function: Optimization objective (only "cost" is supported)

        Returns:
            OptimizerResult with schedule and metadata
        """
        start_time = time.monotonic()

        # Align all arrays to the same length
        n_steps = self._align_forecasts(
            import_prices, export_prices, solar_forecast, load_forecast
        )

        if n_steps == 0:
            _LOGGER.warning("No forecast data available, returning empty schedule")
            return self._empty_result()

        # Pad/truncate arrays
        import_prices = self._pad_array(import_prices, n_steps, DEFAULT_IMPORT_PRICE)
        export_prices = self._pad_array(export_prices, n_steps, DEFAULT_EXPORT_PRICE)
        solar_forecast = self._pad_array(solar_forecast, n_steps, 0.0)
        load_forecast = self._pad_array(load_forecast, n_steps, 0.0)

        if SCIPY_AVAILABLE:
            try:
                result = self._solve_lp(
                    n_steps,
                    import_prices,
                    export_prices,
                    solar_forecast,
                    load_forecast,
                    current_soc,
                    cost_function,
                )
                result.solve_time_s = time.monotonic() - start_time
                return result
            except Exception as e:
                _LOGGER.error(f"LP solver failed, falling back to greedy: {e}")

        # Greedy fallback
        result = self._solve_greedy(
            n_steps,
            import_prices,
            export_prices,
            solar_forecast,
            load_forecast,
            current_soc,
            cost_function,
        )
        result.solve_time_s = time.monotonic() - start_time
        return result

    def _align_forecasts(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
    ) -> int:
        """Determine the number of time steps from available data."""
        lengths = [
            len(arr)
            for arr in [import_prices, export_prices, solar_forecast, load_forecast]
            if arr
        ]
        if not lengths:
            return 0

        max_steps = int(self.horizon_hours * 60 / self.interval_minutes)
        return min(max(lengths), max_steps)

    def _pad_array(
        self, arr: list[float] | None, target_len: int, default: float
    ) -> list[float]:
        """Pad or truncate array to target length."""
        if not arr:
            return [default] * target_len
        if len(arr) >= target_len:
            return arr[:target_len]
        # Pad with last known value
        pad_value = arr[-1] if arr else default
        return arr + [pad_value] * (target_len - len(arr))

    def _solve_lp(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        cost_function: str,
    ) -> OptimizerResult:
        """
        Solve the LP formulation using scipy.optimize.linprog.

        Variables per time step (4 * n total):
            x[0..n-1]   = grid_import[t]  (kW, >= 0)
            x[n..2n-1]  = grid_export[t]  (kW, >= 0)
            x[2n..3n-1] = battery_charge[t] (kW, >= 0)
            x[3n..4n-1] = battery_discharge[t] (kW, >= 0)
        """
        dt = self.dt_hours

        # === Objective function: cost minimization ===
        # minimize SUM(import_price * grid_import - export_price * grid_export) * dt
        c = [0.0] * (4 * n)
        for t in range(n):
            c[t] = import_prices[t] * dt        # grid_import cost
            c[n + t] = -export_prices[t] * dt   # grid_export revenue (negative = profit)

        # === Equality constraints: power balance ===
        # solar[t] + grid_import[t] + battery_discharge[t] = load[t] + grid_export[t] + battery_charge[t]
        # Rearranged: grid_import[t] - grid_export[t] - battery_charge[t] + battery_discharge[t] = load[t] - solar[t]
        A_eq = []
        b_eq = []

        for t in range(n):
            row = [0.0] * (4 * n)
            row[t] = 1.0          # grid_import
            row[n + t] = -1.0     # grid_export
            row[2 * n + t] = -1.0  # battery_charge
            row[3 * n + t] = 1.0   # battery_discharge
            A_eq.append(row)
            b_eq.append(load[t] - solar[t])

        # === Inequality constraints: SOC bounds ===
        # soc[t] = soc_0 + SUM_{i<=t}(charge[i]*eff - discharge[i]/eff) * dt / capacity_kwh
        # We need: backup_reserve <= soc[t] <= 1.0
        #
        # Upper bound: soc[t] <= 1.0
        #   SUM_{i<=t}(charge[i]*eff - discharge[i]/eff) * dt / cap <= 1.0 - soc_0
        #
        # Lower bound: soc[t] >= backup_reserve
        #   -SUM_{i<=t}(charge[i]*eff - discharge[i]/eff) * dt / cap <= soc_0 - backup_reserve
        A_ub = []
        b_ub = []

        eff = self.efficiency
        cap = self.capacity_kwh

        for t in range(n):
            # Upper SOC bound: cumulative energy <= (1.0 - soc_0) * cap
            row_upper = [0.0] * (4 * n)
            for i in range(t + 1):
                row_upper[2 * n + i] = eff * dt / cap      # charge adds SOC
                row_upper[3 * n + i] = -dt / (eff * cap)    # discharge removes SOC
            A_ub.append(row_upper)
            b_ub.append(1.0 - soc_0)

            # Lower SOC bound: -cumulative energy <= (soc_0 - backup_reserve)
            row_lower = [0.0] * (4 * n)
            for i in range(t + 1):
                row_lower[2 * n + i] = -eff * dt / cap
                row_lower[3 * n + i] = dt / (eff * cap)
            A_ub.append(row_lower)
            b_ub.append(soc_0 - self.backup_reserve)

        # === Variable bounds ===
        # Cap grid at 100 kW (generous safety limit; prevents unbounded LP
        # if a price accidentally goes negative or zero).
        max_grid_kw = 100.0
        bounds = []
        for t in range(n):
            bounds.append((0, max_grid_kw))  # grid_import
        for t in range(n):
            bounds.append((0, max_grid_kw))  # grid_export
        for t in range(n):
            bounds.append((0, self.max_charge_kw))  # battery_charge
        for t in range(n):
            bounds.append((0, self.max_discharge_kw))  # battery_discharge

        # === Solve ===
        _LOGGER.debug(f"Solving LP: {n} time steps, {4*n} variables")

        result = linprog(
            c,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
            options={"time_limit": 10.0},
        )

        if not result.success:
            _LOGGER.warning(f"LP solver status: {result.message}")
            if "infeasible" in result.message.lower() and not getattr(self, '_relaxing', False):
                # Try relaxing constraints (guard prevents infinite recursion)
                return self._solve_lp_relaxed(
                    n, import_prices, export_prices, solar, load, soc_0, cost_function
                )
            # Fall back to greedy
            return self._solve_greedy(
                n, import_prices, export_prices, solar, load, soc_0, cost_function
            )

        # === Extract solution ===
        x = result.x
        # Clamp tiny negative values to 0
        x = [max(0.0, v) for v in x]

        grid_import = [x[t] for t in range(n)]
        grid_export = [x[n + t] for t in range(n)]
        battery_charge = [x[2 * n + t] for t in range(n)]
        battery_discharge = [x[3 * n + t] for t in range(n)]

        # Build schedule with action mapping
        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0
        )

        # Calculate costs for first 24 hours only (display as daily cost)
        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * dt
            - export_prices[t] * grid_export[t] * dt
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h, import_prices, export_prices, solar, load
        )
        predicted_savings = baseline_cost - predicted_cost

        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(predicted_savings, 2)

        return OptimizerResult(
            schedule=schedule,
            objective_value=result.fun,
            solver_used="highs",
            feasible=True,
            grid_import_w=[v * 1000 for v in grid_import],
            grid_export_w=[v * 1000 for v in grid_export],
        )

    def _solve_lp_relaxed(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        cost_function: str,
    ) -> OptimizerResult:
        """Retry LP with relaxed SOC constraints (lower backup reserve)."""
        _LOGGER.warning(
            "LP infeasible — relaxing backup reserve from %.0f%% to 5%%",
            self.backup_reserve * 100,
        )
        original_reserve = self.backup_reserve
        self.backup_reserve = 0.05  # Minimal reserve
        self._relaxing = True  # Guard against infinite recursion

        try:
            result = self._solve_lp(
                n, import_prices, export_prices, solar, load, soc_0, cost_function
            )
            result.feasible = False  # Mark as relaxed
            return result
        except Exception:
            # Complete failure — use greedy
            return self._solve_greedy(
                n, import_prices, export_prices, solar, load, soc_0, cost_function
            )
        finally:
            self.backup_reserve = original_reserve
            self._relaxing = False

    def _solve_greedy(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        cost_function: str,
    ) -> OptimizerResult:
        """
        Greedy fallback optimizer.

        Sort time steps by price spread and greedily assign charge/discharge
        while tracking SOC constraints.
        """
        dt = self.dt_hours
        eff = self.efficiency
        cap = self.capacity_kwh

        grid_import = [0.0] * n
        grid_export = [0.0] * n
        battery_charge = [0.0] * n
        battery_discharge = [0.0] * n

        # Price-based greedy: sort by price spread
        # Charge during cheapest import, discharge during most profitable export
        spreads = []
        for t in range(n):
            net_load = load[t] - solar[t]
            spread = export_prices[t] - import_prices[t]
            spreads.append((spread, t, net_load))

        # Sort: most profitable export first (highest spread)
        spreads.sort(key=lambda x: -x[0])

        # Two-pass: first assign exports (top spread), then imports (bottom spread)
        soc = soc_0
        actions = {}  # t -> (charge_kw, discharge_kw)

        # Pass 1: assign discharge/export to highest-spread periods
        soc_tracker = soc_0
        for spread, t, net_load in spreads:
            if spread > 0:
                # Profitable to discharge
                discharge_room = (soc_tracker - self.backup_reserve) * cap * eff / dt
                discharge_kw = min(self.max_discharge_kw, max(0, discharge_room))
                if discharge_kw > 0.01:
                    actions[t] = (0.0, discharge_kw)
                    soc_tracker -= discharge_kw * dt / (eff * cap)
            else:
                # Cheap to charge
                charge_room = (1.0 - soc_tracker) * cap / (eff * dt)
                charge_kw = min(self.max_charge_kw, max(0, charge_room))
                if charge_kw > 0.01:
                    actions[t] = (charge_kw, 0.0)
                    soc_tracker += charge_kw * eff * dt / cap

        # Now compute grid flows in time order
        soc = soc_0
        for t in range(n):
            net_load = load[t] - solar[t]
            charge_kw, discharge_kw = actions.get(t, (0.0, 0.0))

            battery_charge[t] = charge_kw
            battery_discharge[t] = discharge_kw

            # Power balance: grid_import + solar + discharge = load + grid_export + charge
            net_grid = net_load + charge_kw - discharge_kw
            if net_grid > 0:
                grid_import[t] = net_grid
            else:
                grid_export[t] = -net_grid

            soc += (charge_kw * eff - discharge_kw / eff) * dt / cap
            soc = max(0.0, min(1.0, soc))

        # Build schedule
        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0
        )

        # Calculate costs for first 24 hours only (display as daily cost)
        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * dt
            - export_prices[t] * grid_export[t] * dt
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h, import_prices, export_prices, solar, load
        )

        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(baseline_cost - predicted_cost, 2)

        return OptimizerResult(
            schedule=schedule,
            solver_used="greedy",
            feasible=True,
            grid_import_w=[v * 1000 for v in grid_import],
            grid_export_w=[v * 1000 for v in grid_export],
        )

    def _build_schedule(
        self,
        n: int,
        grid_import: list[float],
        grid_export: list[float],
        battery_charge: list[float],
        battery_discharge: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
    ) -> OptimizationSchedule:
        """
        Map LP solution to battery actions.

        Action mapping:
        - CHARGE: grid → battery. Detected when battery_charge > threshold AND
          grid_import > load (charging from grid, not just from solar excess).
        - EXPORT: battery → grid. Detected when grid_export > threshold AND
          battery_discharge > threshold.
        - IDLE: Hold SOC. Detected when battery is neither charging nor discharging
          significantly, AND there is grid import (home drawing from grid while
          battery holds). Implemented by setting backup reserve = current SOC.
        - SELF_CONSUMPTION: Everything else. Battery charges from solar excess and
          discharges to serve home load naturally.
        """
        dt = self.dt_hours
        eff = self.efficiency
        cap = self.capacity_kwh
        # Snap to previous interval boundary so schedule timestamps
        # align with hour/TOU boundaries (e.g. :00, :05, :10 for 5-min)
        raw_now = dt_util.now()
        now = raw_now.replace(
            minute=(raw_now.minute // self.interval_minutes) * self.interval_minutes,
            second=0, microsecond=0,
        )
        threshold_kw = ACTION_THRESHOLD_W / 1000.0

        actions = []
        soc = soc_0

        for t in range(n):
            ts = now + timedelta(minutes=t * self.interval_minutes)

            charge_kw = battery_charge[t]
            discharge_kw = battery_discharge[t]
            import_kw = grid_import[t]
            export_kw = grid_export[t]

            # Update SOC
            soc += (charge_kw * eff - discharge_kw / eff) * dt / cap
            soc = max(0.0, min(1.0, soc))

            # Determine action
            if charge_kw > threshold_kw and import_kw > (load[t] + threshold_kw):
                # Grid is providing more than load needs → grid charging battery
                action = "charge"
                power_w = charge_kw * 1000
            elif export_kw > threshold_kw and discharge_kw > threshold_kw:
                # Battery discharging AND power going to grid → exporting
                action = "export"
                power_w = discharge_kw * 1000
            elif (
                charge_kw < threshold_kw
                and discharge_kw < threshold_kw
                and import_kw > threshold_kw
            ):
                # Battery idle while home draws from grid — hold SOC
                action = "idle"
                power_w = 0.0
            else:
                # Natural self-consumption: solar charging or battery serving load
                action = "self_consumption"
                if discharge_kw > threshold_kw:
                    power_w = discharge_kw * 1000
                elif charge_kw > threshold_kw:
                    power_w = charge_kw * 1000
                else:
                    power_w = 0.0

            actions.append(ScheduleAction(
                timestamp=ts,
                action=action,
                power_w=round(power_w, 1),
                soc=round(soc, 4),
                battery_charge_w=round(charge_kw * 1000, 1),
                battery_discharge_w=round(discharge_kw * 1000, 1),
            ))

        return OptimizationSchedule(
            actions=actions,
            predicted_cost=0.0,
            predicted_savings=0.0,
            last_updated=now,
        )

    def _calculate_baseline_cost(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
    ) -> float:
        """
        Calculate baseline cost without battery.

        All load from grid, all excess solar exported.
        """
        dt = self.dt_hours
        cost = 0.0

        for t in range(n):
            net = load[t] - solar[t]
            if net > 0:
                cost += import_prices[t] * net * dt
            else:
                cost -= export_prices[t] * (-net) * dt

        return round(cost, 2)

    def _empty_result(self) -> OptimizerResult:
        """Return an empty result when no data is available."""
        return OptimizerResult(
            schedule=OptimizationSchedule(
                actions=[],
                predicted_cost=0.0,
                predicted_savings=0.0,
                last_updated=dt_util.now(),
            ),
            solver_used="none",
            feasible=False,
        )
