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
        hardware_reserve: float = 0.0,
        interval_minutes: int = 5,
        horizon_hours: int = 48,
        terminal_weight: float = 1.0,
    ):
        self.capacity_wh = capacity_wh
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.efficiency = efficiency
        self.backup_reserve = backup_reserve
        self.hardware_reserve = hardware_reserve
        self.interval_minutes = interval_minutes
        self.horizon_hours = horizon_hours
        self.terminal_weight = terminal_weight
        # Set by coordinator when a user-triggered force discharge is active so
        # that the below-reserve adjustment fires at INFO instead of WARNING.
        # (SOC below reserve is expected during intentional force discharge.)
        self.suppress_reserve_warning: bool = False

        # Pre-window SOC floor: enforce soc[pre_window_slot - 1] >= target.
        # Used by the coordinator to guarantee the battery is filled before
        # high-value export windows (e.g. Flow Power Happy Hour) when
        # profit_max mode is on. The LP rolling horizon otherwise tends to
        # defer grid-charging to the globally cheapest slots, missing the
        # window for today's HH.
        self.pre_window_soc_target: float = 0.0
        self.pre_window_slot: int | None = None

        # Terminal valuation units. The original LP wrote terminal coefficients
        # as `terminal_price * eff * dt / cap`, which is dimensionally wrong:
        # `terminal_price` is $/kWh, so the correct per-kW objective coefficient
        # is `terminal_price * eff * dt` (no `/cap`). The `/cap` was an
        # artefact of treating terminal_price as "$ per SoC unit" while it's
        # actually "$ per kWh of stored energy"; the cap belongs in the SoC
        # bound *constraints* (which already have it correctly), not the
        # objective. Default True now that the unit error is fixed; kept as
        # a flag so tests can compare behavior. Solar-equipped users see no
        # regression because terminal_price is set from solar export prices
        # (typically ~5c) when solar is in horizon, which keeps the
        # discharge penalty well below avoided-import savings.
        self.use_per_kwh_terminal: bool = True

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

    def update_hardware_reserve(self, hardware_reserve: float) -> None:
        """Update hardware reserve (from manufacturer's app setting)."""
        self.hardware_reserve = hardware_reserve

    def optimize(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        current_soc: float,
        cost_function: str = "cost",
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: bool | list[bool] = True,
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
            allow_battery_export: Whether battery-to-grid export is permitted.
                A per-step list restricts export to explicit windows while still
                allowing solar surplus export.

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
        allow_battery_export = self._normalize_battery_export_flags(
            allow_battery_export, n_steps
        )

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
                    acquisition_cost_kwh,
                    allow_battery_export,
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
            acquisition_cost_kwh,
            allow_battery_export,
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

    def _normalize_battery_export_flags(
        self,
        allow_battery_export: bool | list[bool],
        target_len: int,
    ) -> list[bool]:
        """Normalize battery export permission into one flag per time step."""
        if isinstance(allow_battery_export, bool):
            return [allow_battery_export] * target_len

        flags = [bool(v) for v in allow_battery_export[:target_len]]
        if len(flags) < target_len:
            flags.extend([False] * (target_len - len(flags)))
        return flags

    def _solve_lp(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        cost_function: str,
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: list[bool] | None = None,
    ) -> OptimizerResult:
        """
        Solve the LP formulation using scipy.optimize.linprog.

        Variables per time step (4 * n total):
            x[0..n-1]   = grid_import[t]  (kW, >= 0)
            x[n..2n-1]  = grid_export[t]  (kW, >= 0)
            x[2n..3n-1] = battery_charge[t] (kW, >= 0)
            x[3n..4n-1] = battery_discharge[t] (kW, >= 0)
        """
        # If SOC is below backup_reserve, the LP is guaranteed infeasible
        # (cannot satisfy soc[0] >= backup_reserve when starting below it
        # and max charge can't bridge the gap in one timestep).
        # Temporarily lower the effective reserve to current SOC so the LP
        # can still produce a useful schedule (it will plan to charge back
        # up towards the real reserve rather than failing outright).
        _soc_below_reserve = soc_0 < self.backup_reserve
        _saved_reserve = self.backup_reserve
        if _soc_below_reserve:
            # Use current SOC as the floor (no further discharge allowed).
            # Using soc-1% caused cascading drain: each cycle allowed 1% more
            # discharge, so the battery drained 3%+ per 5-minute LP cycle.
            effective_reserve = soc_0
            log = _LOGGER.info if self.suppress_reserve_warning else _LOGGER.warning
            log(
                "SOC (%.1f%%) below backup reserve (%.0f%%) — using effective "
                "reserve %.1f%% to avoid infeasibility",
                soc_0 * 100, self.backup_reserve * 100, effective_reserve * 100,
            )
            self.backup_reserve = effective_reserve

        try:
            return self._solve_lp_inner(
                n, import_prices, export_prices, solar, load, soc_0,
                cost_function, acquisition_cost_kwh, allow_battery_export or [True] * n,
            )
        finally:
            if _soc_below_reserve:
                self.backup_reserve = _saved_reserve

    def _solve_lp_inner(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        cost_function: str,
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: list[bool] | None = None,
    ) -> OptimizerResult:
        """Inner LP solver (separated for SOC-below-reserve guard in _solve_lp)."""
        dt = self.dt_hours
        eff = self.efficiency
        cap = self.capacity_kwh
        allow_battery_export = allow_battery_export or [True] * n

        # === Objective function: cost minimization ===
        # minimize SUM(import_price * grid_import - export_price * grid_export) * dt
        c = [0.0] * (4 * n)
        # Tiny time-preference epsilon to break LP degeneracy.  When multiple
        # timesteps have the same price (e.g. flat TOU rate across a window),
        # the LP is indifferent about which ones to use and HiGHS may scatter
        # actions across non-contiguous timesteps (charge-SC-charge-SC…).
        # Adding a monotonic epsilon concentrates actions into contiguous blocks:
        #   - Exports: prefer earlier (decreasing eps) → discharge first, then SC
        #   - Imports/charging: depends on whether a SOC deadline is binding
        # 1e-7 per step is ~5e-5 across 576 steps — negligible vs real prices.
        eps = 1e-7

        # Deadline mode: when pre_window_soc_target is binding (e.g. must reach
        # 100% before today's Flow Power Happy Hour), flip the import bias so
        # ties resolve to EARLIER charging.  Otherwise the legacy prefer-later
        # behavior eats the entire pre-window slack and starts charging right
        # against the deadline — any Modbus latency, BMS taper, or forecast
        # jitter then leaves the user below target at window start.
        # Solar-SC users (no deadline) keep the prefer-later default so grid
        # imports happen after solar has had a chance to fill the battery.
        deadline_mode = (
            self.pre_window_slot is not None
            and self.pre_window_slot > 0
            and self.pre_window_soc_target > 0.0
        )

        # Pre-compute free charging bonus: use median non-free import price
        # so the LP sees free charging as "saving" that future import cost.
        # See use_per_kwh_terminal field: legacy form divides by `cap` (a unit
        # error; attenuates the bonus to noise on large batteries), corrected
        # form drops the `/cap`. _build_schedule has a hard override that
        # forces max charge during 0c periods regardless of solver output —
        # the corrected coefficient just lets the LP arrive at the same
        # answer through its own economics.
        _nonzero_prices = sorted(p for p in import_prices if p > 0.01)
        _terminal_unit_divisor = 1.0 if self.use_per_kwh_terminal else cap
        _free_charge_bonus = (
            _nonzero_prices[len(_nonzero_prices) // 2] * eff * dt / _terminal_unit_divisor
            if _nonzero_prices else 0.0
        )

        for t in range(n):
            # Import/charge tie-breaker: prefer EARLIER when a deadline is
            # binding, prefer LATER otherwise (see deadline_mode comment above).
            import_eps = eps * (t if deadline_mode else (n - t))
            c[t] = (import_prices[t] + import_eps) * dt
            if export_prices[t] > 0:
                c[n + t] = -(export_prices[t] + eps * (n - t)) * dt  # grid_export: prefer earlier
            else:
                # Exporting at 0c costs the same as importing — any energy pushed out
                # at 0c must be bought back at the import rate, so it's never worthwhile
                # to intentionally discharge for 0c export (e.g. Flow Power non-happy-hour).
                c[n + t] = max(0.01, import_prices[t]) * dt

            # Free electricity: strongly incentivize charging.
            # Without this, the LP may idle during free windows because
            # the near-zero import cost doesn't overcome terminal valuation.
            if import_prices[t] <= 0.001 and _free_charge_bonus > 0:
                c[2 * n + t] -= _free_charge_bonus

        # === Terminal valuation: incentivize keeping charge at end of horizon ===
        # Use the cheapest available recharge price as the replacement cost.
        # The battery will recharge during the cheapest period in the horizon,
        # so min is the correct marginal cost. Using median over-penalizes
        # discharge when free/cheap charging windows exist (e.g. GloBird
        # FOUR4FREE has 4 hours at 0c — median would be ~31c, causing the LP
        # to prefer grid import over battery discharge at 31c partial-peak
        # because the efficiency-adjusted penalty 31/0.9=34.4c > 31c import).
        #
        # Solar recharging: when solar is available, the battery can recharge
        # at the opportunity cost of export (foregone export revenue), which
        # is typically much cheaper than grid import. Without this, flat-rate
        # users see terminal_price = import_price, making the efficiency-
        # adjusted penalty > import_price, so the LP prefers IDLE (grid
        # import) over self-consumption — exactly wrong.
        half_n = n // 2
        second_half_prices = import_prices[half_n:] if half_n < n else import_prices
        min_grid_recharge = min(second_half_prices) if second_half_prices else 0.0

        # Check if solar can recharge the battery in the second half of horizon.
        # If so, the marginal recharge cost is the export price (opportunity cost).
        solar_recharge_costs = [
            export_prices[t]
            for t in range(half_n, n)
            if solar[t] > 0.1  # Meaningful solar available
        ]
        if solar_recharge_costs:
            min_solar_recharge = min(solar_recharge_costs)
            terminal_price = max(0.001, min(min_grid_recharge, min_solar_recharge))
        else:
            terminal_price = max(0.001, min_grid_recharge) if min_grid_recharge > 0 else 0.0

        # Floor: even when recharging is free (e.g. GloBird SUPER_OFF_PEAK 0c),
        # round-trip efficiency losses mean discharge isn't free. Use a minimum
        # terminal price so the LP doesn't dump battery energy at 0c sell price
        # just because it can recharge for free later.
        if terminal_price < 0.01:
            # Use efficiency-adjusted median import as minimum replacement cost.
            # This reflects the real cost of the energy already stored.
            all_nonzero = [p for p in import_prices if p > 0.01]
            if all_nonzero:
                median_price = sorted(all_nonzero)[len(all_nonzero) // 2]
                terminal_price = max(terminal_price, median_price * (1 - eff))

        terminal_price *= self.terminal_weight

        if terminal_price > 0:
            # See use_per_kwh_terminal field for the unit-error history.
            # Correct coefficients are terminal_price * eff * dt (no /cap):
            # terminal_price is $/kWh, so a per-kW objective coefficient over
            # dt hours produces $ — adding /cap would give $·h/kWh², garbage.
            # Solar-equipped users see no behavior change because solar
            # export sets terminal_price low (~5c FiT), keeping the
            # discharge penalty well under avoided-import savings.
            for t in range(n):
                # Charging adds SOC → subtract cost (incentivize keeping charge)
                c[2 * n + t] -= terminal_price * eff * dt / _terminal_unit_divisor
                # Discharging removes SOC → add cost (penalize draining)
                c[3 * n + t] += terminal_price * dt / (eff * _terminal_unit_divisor)

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

            # Export must be backed by physical energy from solar surplus or
            # battery discharge. This prevents impossible simultaneous
            # grid-import -> grid-export passthrough arbitrage in the LP.
            row_export_source = [0.0] * (4 * n)
            row_export_source[n + t] = 1.0
            row_export_source[3 * n + t] = -1.0
            A_ub.append(row_export_source)
            b_ub.append(max(0.0, solar[t] - load[t]))

        # === Pre-window SOC floor ===
        # Force soc[pre_window_slot - 1] >= target so the battery is filled
        # before a known high-value export window (e.g. Flow Power Happy Hour).
        # The 48 h rolling horizon otherwise places grid-charge slots at the
        # globally cheapest periods, which often misses today's HH entirely.
        # Cap target at what's physically reachable to keep the LP feasible.
        if (
            self.pre_window_slot is not None
            and self.pre_window_slot > 0
            and self.pre_window_slot <= n
            and self.pre_window_soc_target > 0.0
            and not getattr(self, "_relaxing", False)
        ):
            slots_to_window = self.pre_window_slot
            max_soc_gain = (
                self.max_charge_kw * eff * dt * slots_to_window / cap
            )
            max_reachable = min(1.0, soc_0 + max_soc_gain)
            # 0.5% buffer so a tight LP doesn't flip infeasible from rounding
            effective_target = min(self.pre_window_soc_target, max_reachable - 0.005)

            if effective_target > soc_0:
                row = [0.0] * (4 * n)
                for i in range(slots_to_window):
                    row[2 * n + i] = -eff * dt / cap
                    row[3 * n + i] = dt / (eff * cap)
                A_ub.append(row)
                b_ub.append(soc_0 - effective_target)
                _LOGGER.debug(
                    "Pre-window SOC floor: target=%.1f%% (capped from %.1f%%) "
                    "at slot %d (%.1f h ahead), current=%.1f%%",
                    effective_target * 100,
                    self.pre_window_soc_target * 100,
                    self.pre_window_slot,
                    self.pre_window_slot * dt,
                    soc_0 * 100,
                )

        # === Variable bounds ===
        # Cap grid at 100 kW (generous safety limit; prevents unbounded LP
        # if a price accidentally goes negative or zero).
        max_grid_kw = 100.0
        bounds = []
        for t in range(n):
            bounds.append((0, max_grid_kw))  # grid_import

        # Grid export is always allowed for solar surplus. When battery export is
        # disabled, cap export to exogenous surplus so the LP cannot invent
        # grid-import -> grid-export or battery -> grid arbitrage.
        for t in range(n):
            if allow_battery_export[t]:
                bounds.append((0, max_grid_kw))  # grid_export
            else:
                solar_surplus_kw = max(0.0, solar[t] - load[t])
                bounds.append((0, min(max_grid_kw, solar_surplus_kw)))

        for t in range(n):
            bounds.append((0, self.max_charge_kw))  # battery_charge

        for t in range(n):
            restrict_to_self_consumption = (
                not allow_battery_export[t]
                or (acquisition_cost_kwh > 0 and export_prices[t] < acquisition_cost_kwh)
            )
            if restrict_to_self_consumption:
                # Allow discharge only for self-consumption (serving home load)
                net_load_kw = max(0.0, load[t] - solar[t])
                max_self_consumption = net_load_kw
                bounds.append((0, min(self.max_discharge_kw, max_self_consumption)))
            else:
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
                    n, import_prices, export_prices, solar, load, soc_0, cost_function,
                    acquisition_cost_kwh,
                    allow_battery_export,
                )
            # Fall back to greedy
            return self._solve_greedy(
                n, import_prices, export_prices, solar, load, soc_0, cost_function,
                acquisition_cost_kwh,
                allow_battery_export,
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
            solar, load, soc_0, import_prices, export_prices
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
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: list[bool] | None = None,
    ) -> OptimizerResult:
        """Retry LP with relaxed SOC constraints (lower backup reserve)."""
        _LOGGER.warning(
            "LP infeasible — relaxing backup reserve from %.0f%% to 5%%",
            self.backup_reserve * 100,
        )
        original_reserve = self.backup_reserve
        self.backup_reserve = 0.05  # Minimal reserve
        self._relaxing = True  # Guard against infinite recursion
        allow_battery_export = allow_battery_export or [True] * n

        try:
            result = self._solve_lp(
                n, import_prices, export_prices, solar, load, soc_0, cost_function,
                acquisition_cost_kwh,
                allow_battery_export,
            )
            result.feasible = False  # Mark as relaxed
            return result
        except Exception:
            # Complete failure — use greedy
            return self._solve_greedy(
                n, import_prices, export_prices, solar, load, soc_0, cost_function,
                acquisition_cost_kwh,
                allow_battery_export,
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
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: list[bool] | None = None,
    ) -> OptimizerResult:
        """
        Greedy fallback optimizer.

        Sort time steps by price spread and greedily assign charge/discharge
        while tracking SOC constraints.
        """
        dt = self.dt_hours
        eff = self.efficiency
        cap = self.capacity_kwh
        allow_battery_export = allow_battery_export or [True] * n

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
                # Profitable to discharge; cap to home load when battery export
                # is not explicitly permitted or export is below acquisition cost.
                discharge_limit = self.max_discharge_kw
                if (
                    not allow_battery_export[t]
                    or (
                        acquisition_cost_kwh > 0
                        and export_prices[t] < acquisition_cost_kwh
                    )
                ):
                    discharge_limit = min(discharge_limit, max(0.0, net_load))
                discharge_room = (soc_tracker - self.backup_reserve) * cap * eff / dt
                discharge_kw = min(discharge_limit, max(0, discharge_room))
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
            soc = max(self.backup_reserve, min(1.0, soc))

        # Build schedule
        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0, import_prices, export_prices
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
        import_prices: list[float] | None = None,
        export_prices: list[float] | None = None,
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

            # Update SOC — override charge rate during free electricity
            # so the SOC projection matches the forced max charge action
            effective_charge_kw = charge_kw
            if import_prices is not None and import_prices[t] <= 0.001 and soc < 0.99:
                effective_charge_kw = max(charge_kw, self.max_charge_kw)
            soc += (effective_charge_kw * eff - discharge_kw / eff) * dt / cap
            soc = max(self.backup_reserve, min(1.0, soc))

            # Determine action
            if import_prices is not None and import_prices[t] <= 0.001 and soc < 0.99 and (charge_kw > 0 or effective_charge_kw > 0):
                # Free electricity and battery not full — always force charge
                # to maximize free grid intake (don't oscillate with SC)
                action = "charge"
                power_w = max(charge_kw * 1000, self.max_charge_w)
            elif charge_kw > threshold_kw and import_kw > (load[t] + threshold_kw):
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
                # Battery idle while home draws from grid.
                # Only use IDLE when there's a clear profit from holding
                # battery for a future export window. Otherwise, prefer
                # self_consumption — the battery naturally serves load,
                # avoiding expensive grid import.
                meaningful_hold = soc > self.backup_reserve + 0.05
                if meaningful_hold and export_prices is not None and import_prices is not None:
                    # Check if upcoming export prices justify holding battery
                    # over letting it serve load (avoiding import cost).
                    # Need: export_price > import_price / efficiency
                    # (export revenue must exceed the avoided import after losses)
                    cur_import = import_prices[t]
                    min_export_premium = cur_import / eff + 0.02  # +2c/kWh buffer
                    # Look ahead up to 6 hours for a worthwhile export window
                    lookahead = min(n, t + 6 * 60 // self.interval_minutes)
                    best_export = max(
                        (export_prices[k] for k in range(t, lookahead)),
                        default=0,
                    )
                    if best_export >= min_export_premium:
                        action = "idle"
                    else:
                        action = "self_consumption"
                elif meaningful_hold:
                    action = "idle"
                else:
                    action = "self_consumption"
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
