"""
Built-in LP Battery Optimizer for PowerSync.

Uses the HiGHS Linear Programming solver directly (via highspy). Falls back to a
greedy heuristic if highspy is unavailable.

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
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .schedule_reader import ScheduleAction, OptimizationSchedule

_LOGGER = logging.getLogger(__name__)

BELOW_RESERVE_RECOVERY_HOLD_MARGIN_SOC = 0.02
PRE_WINDOW_REACHABLE_TARGET_MARGIN_SOC = 0.005
PRE_WINDOW_REACHABILITY_MARGIN_SOC = 1e-6
# Most horizons stabilize in two passes. A realistic two-day provider window
# can need six as the natural self-use boundary advances across adjacent base
# slots, so keep a small bounded allowance before using the projected plan.
MODE_PROJECTION_MAX_ITERATIONS = 8

# Try to import the HiGHS solver; fall back to greedy if unavailable.
try:
    import highspy

    HIGHS_AVAILABLE = True
except ImportError:
    HIGHS_AVAILABLE = False
    highspy = None
    _LOGGER.warning(
        "highspy not available — using greedy fallback optimizer. "
        "Install highspy for optimal LP-based scheduling."
    )


class _LpMatrix:
    """Minimal row-oriented sparse matrix for building LP constraints.

    Implements just the subset of ``scipy.sparse.lil_matrix`` the optimizer
    relies on — ``shape``, ``m[i, j] = v`` assignment, ``m[i, j]`` lookup,
    ``.nnz``, ``.tocsr()`` (a no-op) and per-row iteration — so we can build the
    constraint matrices and feed HiGHS directly without depending on scipy.
    """

    __slots__ = ("shape", "_rows")

    def __init__(self, shape, dtype=float):
        rows, cols = int(shape[0]), int(shape[1])
        self.shape = (rows, cols)
        self._rows: list[dict[int, float]] = [dict() for _ in range(rows)]

    def __setitem__(self, key, value) -> None:
        i, j = key
        value = float(value)
        if value == 0.0:
            self._rows[i].pop(j, None)
        else:
            self._rows[i][j] = value

    def __getitem__(self, key) -> float:
        i, j = key
        return self._rows[i].get(j, 0.0)

    @property
    def nnz(self) -> int:
        return sum(len(r) for r in self._rows)

    def tocsr(self) -> "_LpMatrix":
        return self

    def iter_rows(self):
        """Yield (row_index, [col indices], [values]) for non-trivial use."""
        for i, row in enumerate(self._rows):
            yield i, list(row.keys()), list(row.values())


class _HighsResult:
    """linprog-compatible result wrapper so the solve call site is unchanged."""

    __slots__ = ("x", "success", "message", "status", "fun")

    def __init__(self, x, success, message, status, fun):
        self.x = x
        self.success = success
        self.message = message
        self.status = status
        self.fun = fun


def _solve_lp_highs(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
    """Solve a standard-form LP with HiGHS and return a linprog-like result.

    minimize  c·x   s.t.   A_ub·x <= b_ub,  A_eq·x == b_eq,  bounds[j] on x[j].

    Mirrors ``scipy.optimize.linprog(method="highs")``: an optimal solve sets
    ``success=True``, and so does a time/iteration-limited solve that still
    holds a feasible incumbent (that incumbent is returned instead of being
    discarded); infeasible/unbounded — and limited solves with no feasible
    incumbent — report success=False with a message string (``"infeasible"``
    substring preserved so the caller's self-consumption fallback still
    triggers).
    """
    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("log_to_console", False)
    h.setOptionValue("time_limit", float(time_limit))

    # Columns carry the objective coefficients and variable bounds; constraint
    # coefficients are supplied row-by-row below, so each column starts empty.
    for j in range(len(c)):
        lo, hi = bounds[j]
        lo = -inf if lo is None else float(lo)
        hi = inf if hi is None else float(hi)
        h.addCol(float(c[j]), lo, hi, 0, [], [])

    # Equality rows: lower == upper == b_eq[i].
    for i, idx, val in A_eq.iter_rows():
        rhs = float(b_eq[i])
        h.addRow(rhs, rhs, len(idx), idx, val)

    # Inequality rows: -inf <= row·x <= b_ub[i].
    for i, idx, val in A_ub.iter_rows():
        h.addRow(-inf, float(b_ub[i]), len(idx), idx, val)

    h.run()

    model_status = h.getModelStatus()
    message = h.modelStatusToString(model_status)
    optimal = model_status == highspy.HighsModelStatus.kOptimal

    # A time/iteration-limited solve can still hold a usable feasible
    # incumbent — use it instead of discarding it and forcing the caller's
    # greedy fallback.
    usable_incumbent = False
    if not optimal and model_status in (
        highspy.HighsModelStatus.kTimeLimit,
        highspy.HighsModelStatus.kIterationLimit,
    ):
        info = h.getInfo()
        usable_incumbent = (
            getattr(info, "primal_solution_status", None)
            == highspy.kSolutionStatusFeasible
        )

    success = optimal or usable_incumbent
    if success:
        x = list(h.getSolution().col_value)
        fun = float(h.getObjectiveValue())
    else:
        x = None
        fun = None
    return _HighsResult(
        x=x,
        success=success,
        message=message,
        status=int(model_status),
        fun=fun,
    )

# Action detection threshold (W) — below this, treat as idle to avoid rapid switching
ACTION_THRESHOLD_W = 100.0

# Default fallback prices when no price data is available ($/kWh)
DEFAULT_IMPORT_PRICE = 0.30
DEFAULT_EXPORT_PRICE = 0.08

# Battery ONE-WAY (per-direction) efficiency. The LP and every downstream
# consumer apply this once on charge (charge*eff) and once on discharge
# (discharge/eff), so the modelled round-trip efficiency is eff**2 (~0.85 at
# 0.92). This is deliberately conservative — it declines arbitrage below a
# ~18% spread, which broadly offsets unmodelled LFP cycle-degradation cost.
# NOTE: this is NOT the round-trip figure; to target a specific round trip R,
# set this to sqrt(R). Changing it shifts arbitrage aggressiveness for every
# user, so treat it as an economic tuning decision, not a units fix.
DEFAULT_EFFICIENCY = 0.92

# HiGHS can legitimately need more than 10s for 48h/5min plans on HA hardware.
LP_SOLVER_TIME_LIMIT_SECONDS = 30.0

# Internal tiered period aggregation. The public schedule remains fixed at
# `interval_minutes`; only the LP model coarsens the far horizon.
LP_NEAR_HORIZON_HOURS = 6
LP_MID_HORIZON_HOURS = 24
LP_MID_PERIOD_MINUTES = 30
LP_FAR_PERIOD_MINUTES = 60
LP_PRICE_SPLIT_THRESHOLD = 0.02
LP_POWER_SPLIT_THRESHOLD_KW = ACTION_THRESHOLD_W / 1000.0

# Profit Max prefill guard: count most, but not all, forecast net solar before
# the export window and keep a small SOC buffer for forecast error.
PRE_WINDOW_SOLAR_CREDIT_FACTOR = 0.80
PRE_WINDOW_SOLAR_BUFFER_SOC = 0.03

_UNSET = object()


@dataclass(frozen=True)
class _LpPeriod:
    """Internal LP period mapped to a range of base schedule slots."""

    start: int
    end: int
    import_price: float
    export_price: float
    export_bonus_price: float
    import_bonus_price: float
    solar_kw: float
    load_kw: float
    allow_battery_export: bool
    block_battery_charge: bool
    grid_charge_allowed: bool
    priority_export: bool
    mode: str | None = None
    required_self_use_kw: float = 0.0

    @property
    def slot_count(self) -> int:
        return self.end - self.start


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
    lp_stats: dict[str, Any] = field(default_factory=dict)
    reserve_recommendation: dict[str, Any] = field(default_factory=dict)
    modeled_backup_reserve: float | None = None
    modeled_export_reserve_floor: float | None = None
    modeled_export_reserve_floor_slots: list[float] | None = None


class BatteryOptimizer:
    """
    LP-based battery optimizer using the HiGHS solver (highspy).

    Solves a cost-minimization (or self-consumption) LP over a forecast horizon
    and maps the result to battery actions.
    """

    def __init__(
        self,
        capacity_wh: float = 13500,
        max_charge_w: float = 5000,
        max_discharge_w: float = 5000,
        max_grid_import_w: float | None = None,
        max_grid_export_w: float | None = None,
        max_battery_export_w: float | None = None,
        efficiency: float = DEFAULT_EFFICIENCY,
        backup_reserve: float = 0.20,
        hardware_reserve: float | None = None,
        grid_charge_soc_cap: float = 1.0,
        interval_minutes: int = 5,
        horizon_hours: int = 48,
        terminal_weight: float = 1.0,
    ):
        self.capacity_wh = capacity_wh
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.max_grid_import_w = self._normalize_optional_power_w(max_grid_import_w)
        self.max_grid_export_w = self._normalize_optional_export_power_w(max_grid_export_w)
        self.max_battery_export_w = self._normalize_optional_export_power_w(
            max_battery_export_w
        )
        self.efficiency = efficiency
        self.backup_reserve = backup_reserve
        self.hardware_reserve = max(0.0, min(1.0, float(hardware_reserve or 0.0)))
        self.hardware_reserve_known = hardware_reserve is not None
        self.grid_charge_soc_cap = max(
            0.0,
            min(1.0, float(grid_charge_soc_cap if grid_charge_soc_cap is not None else 1.0)),
        )
        self.interval_minutes = interval_minutes
        self.horizon_hours = horizon_hours
        self.terminal_weight = terminal_weight
        # Set by coordinator when a user-triggered force discharge is active so
        # that the below-reserve adjustment fires at INFO instead of WARNING.
        # (SOC below reserve is expected during intentional force discharge.)
        self.suppress_reserve_warning: bool = False
        self._below_reserve_recovery_target: float | None = None
        self.export_reserve_floor: float = 0.0
        self.export_reserve_floor_slots: list[float] | None = None

        # Pre-window SOC floor: enforce soc[pre_window_slot - 1] >= target.
        # Used by the coordinator to guarantee the battery is filled before
        # high-value export windows (e.g. Flow Power Happy Hour) when
        # profit_max mode is on. The LP rolling horizon otherwise tends to
        # defer grid-charging to the globally cheapest slots, missing the
        # window for today's HH.
        self.pre_window_soc_target: float = 0.0
        self.pre_window_slot: int | None = None
        self.pre_window_solar_credit_factor: float = PRE_WINDOW_SOLAR_CREDIT_FACTOR
        self.pre_window_solar_buffer_soc: float = PRE_WINDOW_SOLAR_BUFFER_SOC

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
        self.max_grid_import_kw = (
            self.max_grid_import_w / 1000.0
            if self.max_grid_import_w is not None
            else None
        )
        self.max_battery_export_kw = (
            self.max_battery_export_w / 1000.0
            if self.max_battery_export_w is not None
            else None
        )
        self.dt_hours = interval_minutes / 60.0  # time step in hours

    def update_config(
        self,
        capacity_wh: float | None = None,
        max_charge_w: float | None = None,
        max_discharge_w: float | None = None,
        max_grid_import_w: float | None | object = _UNSET,
        max_grid_export_w: float | None | object = _UNSET,
        max_battery_export_w: float | None | object = _UNSET,
        efficiency: float | None = None,
        backup_reserve: float | None = None,
        grid_charge_soc_cap: float | None = None,
        horizon_hours: int | None = None,
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
        if max_grid_import_w is not _UNSET:
            self.max_grid_import_w = self._normalize_optional_power_w(max_grid_import_w)
            self.max_grid_import_kw = (
                self.max_grid_import_w / 1000.0
                if self.max_grid_import_w is not None
                else None
            )
        if max_grid_export_w is not _UNSET:
            self.max_grid_export_w = self._normalize_optional_export_power_w(max_grid_export_w)
        if max_battery_export_w is not _UNSET:
            self.max_battery_export_w = self._normalize_optional_export_power_w(
                max_battery_export_w
            )
            self.max_battery_export_kw = (
                self.max_battery_export_w / 1000.0
                if self.max_battery_export_w is not None
                else None
            )
        if efficiency is not None:
            self.efficiency = efficiency
        if backup_reserve is not None:
            self.backup_reserve = backup_reserve
        if grid_charge_soc_cap is not None:
            self.grid_charge_soc_cap = max(
                0.0,
                min(1.0, float(grid_charge_soc_cap)),
            )
        if horizon_hours is not None:
            try:
                parsed_horizon = int(float(horizon_hours))
            except (TypeError, ValueError):
                parsed_horizon = None
            if parsed_horizon is not None and parsed_horizon > 0:
                self.horizon_hours = parsed_horizon

    @staticmethod
    def _normalize_optional_power_w(value: float | None | object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _normalize_optional_export_power_w(value: float | None | object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def _charge_limit_kw(
        self,
        load_kw: float,
        solar_kw: float,
        allow_grid_charge: bool,
    ) -> float:
        """Return feasible battery charge power for a slot."""
        charge_limit = self.max_charge_kw
        if not allow_grid_charge:
            charge_limit = min(charge_limit, max(0.0, solar_kw - load_kw))
        elif self.max_grid_import_kw is not None:
            charge_limit = min(
                charge_limit,
                max(0.0, self.max_grid_import_kw - load_kw + solar_kw),
            )
        return max(0.0, charge_limit)

    def update_hardware_reserve(self, hardware_reserve: float) -> None:
        """Update hardware reserve (from manufacturer's app setting)."""
        self.hardware_reserve = max(0.0, min(1.0, float(hardware_reserve or 0.0)))
        self.hardware_reserve_known = True

    def _natural_self_consumption_floor(self, soc_0: float) -> float:
        """SOC floor for displayed natural home-load battery use."""
        optimizer_reserve = max(0.0, min(1.0, self.backup_reserve))
        current_soc = max(0.0, min(1.0, float(soc_0)))
        if not getattr(self, "hardware_reserve_known", False):
            # Do not invent energy when telemetry is already below the
            # conservative fallback floor. Hold at the observed SOC until an
            # economically selected recovery charge can raise it.
            return min(current_soc, optimizer_reserve)
        hardware_reserve = max(0.0, min(1.0, self.hardware_reserve))
        return min(current_soc, hardware_reserve)

    def _configured_export_reserve_floor(self) -> float:
        """Return the transient reserve floor for forced battery export."""
        slot_floors = getattr(self, "export_reserve_floor_slots", None)
        slot_floor = max(slot_floors) if slot_floors else 0.0
        return max(
            0.0,
            min(1.0, float(getattr(self, "export_reserve_floor", 0.0) or 0.0)),
            max(0.0, min(1.0, float(slot_floor or 0.0))),
        )

    def _configured_export_reserve_floor_for_range(self, start: int, end: int) -> float:
        """Return the transient export floor active for a base-slot range."""
        floor = max(
            0.0,
            min(1.0, float(getattr(self, "export_reserve_floor", 0.0) or 0.0)),
        )
        slot_floors = getattr(self, "export_reserve_floor_slots", None)
        if slot_floors:
            active = slot_floors[max(0, start):max(0, end)]
            if active:
                floor = max(floor, max(0.0, min(1.0, max(active))))
        return floor

    def optimize(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        current_soc: float,
        cost_function: str = "cost",
        acquisition_cost_kwh: float = 0.0,
        allow_battery_export: bool | list[bool] = False,
        block_battery_charge: bool | list[bool] = False,
        allow_grid_charge: bool = True,
        grid_charge_allowed: bool | list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        export_reserve_floor: float | list[float] | None = None,
        schedule_timestamps: list[datetime] | None = None,
        priority_export_slots: bool | list[bool] | None = None,
        priority_export_enabled: bool = False,
        disable_idle: bool = False,
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
            block_battery_charge: Whether battery charging is blocked for each
                time step. Used for export-only windows where grid charging
                must not occur even when arbitrage appears profitable.
            allow_grid_charge: Whether the optimizer may charge the battery
                from grid import. When false, solar surplus can still charge
                the battery.
            grid_charge_allowed: Optional per-step forced-grid-charge mask.
                False preserves solar surplus charging but blocks grid top-up.
            import_bonus_prices: Optional per-step import credit/top-up values
                for capped free-import settlement windows.
            import_bonus_cap_kwh: Optional kWh cap for import bonuses.
            schedule_timestamps: Optional per-slot timestamps aligned with the
                price forecast.
            priority_export_slots: Optional per-step export-priority mask.
                When omitted and priority_export_enabled is true, uses
                allow_battery_export.
            priority_export_enabled: Mark provider settlement windows where
                modeled export bonuses may make battery export economic.
            disable_idle: Require ordinary non-forced slots to use the battery
                naturally rather than holding SOC for a later opportunity.

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
        export_bonus_prices = self._pad_array(
            export_bonus_prices, n_steps, 0.0
        )
        import_bonus_prices = self._pad_array(
            import_bonus_prices, n_steps, 0.0
        )
        solar_forecast = self._pad_array(solar_forecast, n_steps, 0.0)
        load_forecast = self._pad_array(load_forecast, n_steps, 0.0)
        allow_battery_export = self._normalize_battery_export_flags(
            allow_battery_export, n_steps
        )
        block_battery_charge = self._normalize_battery_charge_blocks(
            block_battery_charge, n_steps
        )
        grid_charge_allowed = self._normalize_grid_charge_allowed(
            grid_charge_allowed, n_steps
        )
        effective_priority_export_prices = [
            export_prices[idx] + export_bonus_prices[idx]
            for idx in range(n_steps)
        ]
        priority_export_slots = self._normalize_priority_export_slots(
            priority_export_slots,
            allow_battery_export,
            n_steps,
            priority_export_enabled,
            import_prices,
            effective_priority_export_prices,
            grid_charge_allowed,
        )
        # Priority/provider windows affect export permissions and settlement
        # value only. They must not manufacture a home-load bridge floor: that
        # hidden floor forced recovery charging even when direct future imports
        # were cheaper. Explicit caller-supplied export floors remain supported
        # as forced-export boundaries.
        previous_export_floor = self.export_reserve_floor
        previous_export_floor_slots = self.export_reserve_floor_slots
        if export_reserve_floor is not None:
            if isinstance(export_reserve_floor, list):
                floors = [
                    max(0.0, min(1.0, float(value or 0.0)))
                    for value in export_reserve_floor[:n_steps]
                ]
                if len(floors) < n_steps:
                    floors.extend([0.0] * (n_steps - len(floors)))
                self.export_reserve_floor = 0.0
                self.export_reserve_floor_slots = floors
            else:
                self.export_reserve_floor = max(
                    0.0,
                    min(1.0, float(export_reserve_floor)),
                )
                self.export_reserve_floor_slots = None

        modeled_backup_reserve = max(0.0, min(1.0, self.backup_reserve))
        modeled_export_reserve_floor = max(
            0.0,
            min(1.0, float(self.export_reserve_floor or 0.0)),
        )
        modeled_export_reserve_floor_slots = (
            list(self.export_reserve_floor_slots)
            if self.export_reserve_floor_slots is not None
            else None
        )

        try:
            if HIGHS_AVAILABLE:
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
                        block_battery_charge,
                        allow_grid_charge,
                        grid_charge_allowed,
                        export_bonus_prices,
                        export_bonus_cap_kwh,
                        import_bonus_prices,
                        import_bonus_cap_kwh,
                        schedule_timestamps,
                        priority_export_slots,
                        disable_idle,
                    )
                    result.solve_time_s = time.monotonic() - start_time
                    result.modeled_backup_reserve = modeled_backup_reserve
                    result.modeled_export_reserve_floor = modeled_export_reserve_floor
                    result.modeled_export_reserve_floor_slots = (
                        modeled_export_reserve_floor_slots
                    )
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
                block_battery_charge,
                allow_grid_charge,
                grid_charge_allowed,
                export_bonus_prices,
                export_bonus_cap_kwh,
                import_bonus_prices,
                import_bonus_cap_kwh,
                schedule_timestamps,
                priority_export_slots,
                disable_idle,
            )
            result.solve_time_s = time.monotonic() - start_time
            result.modeled_backup_reserve = modeled_backup_reserve
            result.modeled_export_reserve_floor = modeled_export_reserve_floor
            result.modeled_export_reserve_floor_slots = (
                modeled_export_reserve_floor_slots
            )
            return result
        finally:
            if export_reserve_floor is not None:
                self.export_reserve_floor = previous_export_floor
                self.export_reserve_floor_slots = previous_export_floor_slots

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
        # Pad with the caller-supplied default
        return arr + [default] * (target_len - len(arr))

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

    def _normalize_battery_charge_blocks(
        self,
        block_battery_charge: bool | list[bool],
        target_len: int,
    ) -> list[bool]:
        """Normalize battery-charge blocking into one flag per time step."""
        if isinstance(block_battery_charge, bool):
            return [block_battery_charge] * target_len

        flags = [bool(v) for v in block_battery_charge[:target_len]]
        if len(flags) < target_len:
            flags.extend([False] * (target_len - len(flags)))
        return flags

    def _normalize_grid_charge_allowed(
        self,
        grid_charge_allowed: bool | list[bool] | None,
        target_len: int,
    ) -> list[bool]:
        """Normalize grid-charge permission into one flag per time step."""
        if grid_charge_allowed is None:
            return [True] * target_len
        if isinstance(grid_charge_allowed, bool):
            return [grid_charge_allowed] * target_len

        flags = [bool(v) for v in grid_charge_allowed[:target_len]]
        if len(flags) < target_len:
            flags.extend([True] * (target_len - len(flags)))
        return flags

    def _normalize_priority_export_slots(
        self,
        priority_export_slots: bool | list[bool] | None,
        allow_battery_export: list[bool],
        target_len: int,
        enabled: bool,
        import_prices: list[float],
        export_prices: list[float],
        grid_charge_allowed: list[bool],
    ) -> list[bool]:
        """Return export-priority slots that should prefer surplus export."""
        if not enabled:
            return [False] * target_len

        if priority_export_slots is None:
            raw = list(allow_battery_export[:target_len])
        elif isinstance(priority_export_slots, bool):
            raw = [priority_export_slots] * target_len
        else:
            raw = [bool(v) for v in priority_export_slots[:target_len]]
            if len(raw) < target_len:
                raw.extend([False] * (target_len - len(raw)))

        flags: list[bool] = []
        round_trip_eff = max(0.0, min(1.0, self.efficiency)) ** 2
        for idx in range(target_len):
            if not raw[idx] or not allow_battery_export[idx]:
                flags.append(False)
                continue
            try:
                export_price = float(export_prices[idx] or 0.0)
                import_price = float(import_prices[idx] or 0.0)
            except (TypeError, ValueError):
                flags.append(False)
                continue
            if export_price <= 0.001:
                flags.append(False)
                continue
            # If this slot is already cheap enough to refill the battery after
            # efficiency losses, leave it available for charging. Priority
            # export is for high-price sell windows where the excess can be
            # replenished later, not for cheap import windows.
            if grid_charge_allowed[idx] and import_price <= export_price * round_trip_eff:
                flags.append(False)
                continue
            flags.append(True)
        return flags

    def _priority_export_reserve_floor_slots(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        priority_export_slots: list[bool],
        block_battery_charge: list[bool],
        allow_grid_charge: bool,
        grid_charge_allowed: list[bool],
        import_bonus_prices: list[float] | None = None,
    ) -> list[float] | None:
        """Build per-slot export floors that bridge home load to next recharge."""
        if not any(priority_export_slots):
            return None
        if self.capacity_kwh <= 0:
            return None

        n = len(priority_export_slots)
        import_bonus_prices = import_bonus_prices or [0.0] * n
        floors = [0.0] * n
        base_floor = max(
            0.0,
            min(1.0, self.backup_reserve),
            min(1.0, self.hardware_reserve),
        )
        round_trip_eff = max(0.0, min(1.0, self.efficiency)) ** 2
        threshold_kw = ACTION_THRESHOLD_W / 1000.0
        idx = 0

        def _forecast_kw(values: list[float], pos: int) -> float:
            if pos >= len(values):
                return 0.0
            try:
                return max(0.0, float(values[pos] or 0.0))
            except (TypeError, ValueError):
                return 0.0

        while idx < n:
            if not priority_export_slots[idx]:
                idx += 1
                continue

            start = idx
            while idx < n and priority_export_slots[idx]:
                idx += 1
            end = idx
            reference_export = max(
                max(0.0, float(price or 0.0))
                for price in export_prices[start:end]
            )
            cheap_recharge_price = reference_export * round_trip_eff
            bridge_kwh = 0.0

            for scan_idx in range(end, n):
                solar_kw = _forecast_kw(solar, scan_idx)
                load_kw = _forecast_kw(load, scan_idx)
                if solar_kw - load_kw > threshold_kw:
                    break
                effective_import_price = import_prices[scan_idx] - (
                    import_bonus_prices[scan_idx]
                    if scan_idx < len(import_bonus_prices)
                    else 0.0
                )
                if (
                    allow_grid_charge
                    and scan_idx < len(grid_charge_allowed)
                    and grid_charge_allowed[scan_idx]
                    and not block_battery_charge[scan_idx]
                    and effective_import_price <= cheap_recharge_price
                    and self._charge_limit_kw(load_kw, solar_kw, True) > threshold_kw
                ):
                    break
                bridge_kwh += max(0.0, load_kw - solar_kw) * self.dt_hours

            bridge_soc = bridge_kwh / max(self.capacity_kwh * self.efficiency, 0.001)
            floor = max(base_floor, min(1.0, base_floor + bridge_soc))
            for floor_idx in range(start, end):
                floors[floor_idx] = floor

        return floors if any(value > 0 for value in floors) else None

    @staticmethod
    def _merge_export_reserve_floor(
        explicit_floor: float | list[float] | None,
        priority_floor: list[float] | None,
        target_len: int,
    ) -> float | list[float] | None:
        """Merge user/auto export floors with priority-export bridge floors."""
        if priority_floor is None:
            return explicit_floor
        if explicit_floor is None:
            return priority_floor

        if isinstance(explicit_floor, list):
            explicit = [
                max(0.0, min(1.0, float(value or 0.0)))
                for value in explicit_floor[:target_len]
            ]
            if len(explicit) < target_len:
                explicit.extend([0.0] * (target_len - len(explicit)))
        else:
            explicit_value = max(0.0, min(1.0, float(explicit_floor or 0.0)))
            explicit = [explicit_value] * target_len
        return [
            max(explicit[idx], priority_floor[idx] if idx < len(priority_floor) else 0.0)
            for idx in range(target_len)
        ]

    def _has_future_self_consumption_value(
        self,
        t: int,
        n: int,
        import_prices: list[float],
        solar: list[float],
        load: list[float],
    ) -> bool:
        """Return True when charging now can avoid later higher-price load."""
        return any(
            import_prices[i] > import_prices[t] + 0.001
            and max(0.0, load[i] - solar[i]) > 0.05
            for i in range(t + 1, n)
        )

    def _future_self_consumption_values(
        self,
        n: int,
        import_prices: list[float],
        solar: list[float],
        load: list[float],
    ) -> list[bool]:
        """Precompute whether each period has later higher-price net load."""
        future_values = [False] * n
        best_future_price = float("-inf")

        for t in range(n - 1, -1, -1):
            future_values[t] = best_future_price > import_prices[t] + 0.001
            if max(0.0, load[t] - solar[t]) > 0.05:
                best_future_price = max(best_future_price, import_prices[t])

        return future_values

    @staticmethod
    def _effective_export_acquisition_costs(
        n: int,
        import_prices: list[float],
        block_battery_charge: list[bool],
        allow_grid_charge: bool,
        acquisition_cost_kwh: float,
        grid_charge_allowed: list[bool] | None = None,
    ) -> list[float]:
        """Return the best known acquisition cost available before each slot."""
        if acquisition_cost_kwh <= 0:
            return [0.0] * n

        costs: list[float] = []
        cheapest_prior_charge: float | None = None
        grid_charge_allowed = grid_charge_allowed or [True] * n
        for t in range(n):
            effective_cost = acquisition_cost_kwh
            if cheapest_prior_charge is not None:
                effective_cost = min(effective_cost, cheapest_prior_charge)
            costs.append(effective_cost)

            if (
                allow_grid_charge
                and not block_battery_charge[t]
                and grid_charge_allowed[t]
            ):
                try:
                    import_price = float(import_prices[t] or 0.0)
                except (TypeError, ValueError):
                    continue
                if cheapest_prior_charge is None:
                    cheapest_prior_charge = import_price
                else:
                    cheapest_prior_charge = min(cheapest_prior_charge, import_price)

        return costs

    @staticmethod
    def _is_export_profitable(
        export_price: float,
        import_price: float,
        acquisition_cost_kwh: float,
        effective_acquisition_cost_kwh: float,
    ) -> bool:
        """Return True when a slot can intentionally export battery energy."""
        if export_price <= 0.001:
            return False

        if export_price > import_price:
            return (
                acquisition_cost_kwh <= 0
                or export_price >= effective_acquisition_cost_kwh
            )

        # Some tariffs fill the battery cheaply before a lower-FIT export
        # window. The current import price is still relevant to self-consumption,
        # but it should not completely block exporting energy that was acquired
        # below the export rate.
        return (
            acquisition_cost_kwh > 0
            and export_price >= effective_acquisition_cost_kwh
        )

    def _build_lp_periods(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        allow_battery_export: list[bool],
        block_battery_charge: list[bool],
        grid_charge_allowed: list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        import_bonus_prices: list[float] | None = None,
        priority_export_slots: list[bool] | None = None,
        mode_slots: list[str | None] | None = None,
        required_self_use_kw: list[float] | None = None,
    ) -> list[_LpPeriod]:
        """Aggregate base 5-minute slots into internal LP periods."""
        near_slots = int(LP_NEAR_HORIZON_HOURS * 60 / self.interval_minutes)
        mid_slots = int(LP_MID_HORIZON_HOURS * 60 / self.interval_minutes)
        mid_width = max(1, int(LP_MID_PERIOD_MINUTES / self.interval_minutes))
        far_width = max(1, int(LP_FAR_PERIOD_MINUTES / self.interval_minutes))
        bonus_prices = export_bonus_prices or [0.0] * n
        import_bonus = import_bonus_prices or [0.0] * n
        grid_charge_allowed = grid_charge_allowed or [True] * n
        priority_export_slots = priority_export_slots or [False] * n
        mode_slots = mode_slots or [None] * n
        required_self_use_kw = required_self_use_kw or [0.0] * n
        periods: list[_LpPeriod] = []
        idx = 0

        while idx < n:
            if idx < near_slots:
                width = 1
            elif idx < mid_slots:
                width = min(mid_width, mid_slots - idx)
            else:
                width = far_width

            end = min(n, idx + width)
            end = self._split_lp_period_end(
                idx,
                end,
                import_prices,
                export_prices,
                solar,
                load,
                allow_battery_export,
                block_battery_charge,
                grid_charge_allowed,
                bonus_prices,
                import_bonus,
                priority_export_slots,
                mode_slots,
                required_self_use_kw,
            )

            # Keep the pre-window SOC deadline on an exact internal boundary.
            if self.pre_window_slot is not None and idx < self.pre_window_slot < end:
                end = self.pre_window_slot

            periods.append(
                _LpPeriod(
                    start=idx,
                    end=end,
                    import_price=sum(import_prices[idx:end]) / (end - idx),
                    export_price=sum(export_prices[idx:end]) / (end - idx),
                    export_bonus_price=sum(bonus_prices[idx:end]) / (end - idx),
                    import_bonus_price=sum(import_bonus[idx:end]) / (end - idx),
                    solar_kw=sum(solar[idx:end]) / (end - idx),
                    load_kw=sum(load[idx:end]) / (end - idx),
                    allow_battery_export=allow_battery_export[idx],
                    block_battery_charge=block_battery_charge[idx],
                    grid_charge_allowed=all(grid_charge_allowed[idx:end]),
                    priority_export=priority_export_slots[idx],
                    mode=mode_slots[idx],
                    required_self_use_kw=(
                        sum(required_self_use_kw[idx:end]) / (end - idx)
                    ),
                )
            )
            idx = end

        return periods

    def _split_lp_period_end(
        self,
        start: int,
        proposed_end: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        allow_battery_export: list[bool],
        block_battery_charge: list[bool],
        grid_charge_allowed: list[bool],
        export_bonus_prices: list[float],
        import_bonus_prices: list[float],
        priority_export_slots: list[bool],
        mode_slots: list[str | None],
        required_self_use_kw: list[float],
    ) -> int:
        """Shorten a coarse period when correctness-sensitive inputs change."""
        if proposed_end <= start + 1:
            return proposed_end

        first_allow = allow_battery_export[start]
        first_block = block_battery_charge[start]
        first_grid_charge_allowed = grid_charge_allowed[start]
        first_priority_export = priority_export_slots[start]
        first_mode = mode_slots[start]
        min_required_self_use = max_required_self_use = required_self_use_kw[start]
        first_import_free = import_prices[start] <= 0.001
        first_export_free = export_prices[start] <= 0.001
        first_bonus_free = export_bonus_prices[start] <= 0.001
        first_import_bonus_free = import_bonus_prices[start] <= 0.001
        min_import = max_import = import_prices[start]
        min_export = max_export = export_prices[start]
        min_bonus = max_bonus = export_bonus_prices[start]
        min_import_bonus = max_import_bonus = import_bonus_prices[start]
        first_net_load = load[start] - solar[start]
        first_surplus = max(0.0, solar[start] - load[start])
        first_net_load_positive = first_net_load > LP_POWER_SPLIT_THRESHOLD_KW
        first_surplus_positive = first_surplus > LP_POWER_SPLIT_THRESHOLD_KW
        min_net_load = max_net_load = first_net_load
        min_surplus = max_surplus = first_surplus

        for idx in range(start + 1, proposed_end):
            min_import = min(min_import, import_prices[idx])
            max_import = max(max_import, import_prices[idx])
            min_export = min(min_export, export_prices[idx])
            max_export = max(max_export, export_prices[idx])
            min_bonus = min(min_bonus, export_bonus_prices[idx])
            max_bonus = max(max_bonus, export_bonus_prices[idx])
            min_import_bonus = min(min_import_bonus, import_bonus_prices[idx])
            max_import_bonus = max(max_import_bonus, import_bonus_prices[idx])
            min_required_self_use = min(
                min_required_self_use,
                required_self_use_kw[idx],
            )
            max_required_self_use = max(
                max_required_self_use,
                required_self_use_kw[idx],
            )
            net_load = load[idx] - solar[idx]
            surplus = max(0.0, solar[idx] - load[idx])
            min_net_load = min(min_net_load, net_load)
            max_net_load = max(max_net_load, net_load)
            min_surplus = min(min_surplus, surplus)
            max_surplus = max(max_surplus, surplus)
            if (
                allow_battery_export[idx] != first_allow
                or block_battery_charge[idx] != first_block
                or grid_charge_allowed[idx] != first_grid_charge_allowed
                or priority_export_slots[idx] != first_priority_export
                or mode_slots[idx] != first_mode
                or (import_prices[idx] <= 0.001) != first_import_free
                or (export_prices[idx] <= 0.001) != first_export_free
                or (export_bonus_prices[idx] <= 0.001) != first_bonus_free
                or (import_bonus_prices[idx] <= 0.001) != first_import_bonus_free
                or max_import - min_import > LP_PRICE_SPLIT_THRESHOLD
                or max_export - min_export > LP_PRICE_SPLIT_THRESHOLD
                or max_bonus - min_bonus > LP_PRICE_SPLIT_THRESHOLD
                or max_import_bonus - min_import_bonus > LP_PRICE_SPLIT_THRESHOLD
                or max_required_self_use - min_required_self_use
                > LP_POWER_SPLIT_THRESHOLD_KW
                or (net_load > LP_POWER_SPLIT_THRESHOLD_KW) != first_net_load_positive
                or (surplus > LP_POWER_SPLIT_THRESHOLD_KW) != first_surplus_positive
                or max_net_load - min_net_load > LP_POWER_SPLIT_THRESHOLD_KW
                or max_surplus - min_surplus > LP_POWER_SPLIT_THRESHOLD_KW
            ):
                return idx

        return proposed_end

    def _period_index_for_base_slot(
        self,
        periods: list[_LpPeriod],
        base_slot: int,
    ) -> int:
        """Return the internal boundary index matching a base slot deadline."""
        for idx, period in enumerate(periods):
            if period.end >= base_slot:
                return idx + 1 if period.end == base_slot else idx
        return len(periods)

    def _pre_window_solar_prefill_ceilings(
        self,
        *,
        pre_window_boundary: int | None,
        target_soc: float | None,
        solar: list[float],
        load: list[float],
        dt_hours: list[float],
        reserve_floor: list[float],
        current_soc: float,
        charge_pinned: list[bool] | None = None,
    ) -> list[float | None]:
        """Return SOC upper bounds that leave room for forecast solar."""
        p_n = len(solar)
        ceilings: list[float | None] = [None] * (p_n + 1)
        if (
            pre_window_boundary is None
            or target_soc is None
            or pre_window_boundary <= 1
            or pre_window_boundary > p_n
            or self.capacity_kwh <= 0
            or self.max_charge_kw <= 0
        ):
            return ceilings

        credit_factor = max(0.0, min(1.0, self.pre_window_solar_credit_factor))
        if credit_factor <= 0:
            return ceilings

        buffer_soc = max(0.0, self.pre_window_solar_buffer_soc)
        remaining_solar_kwh = [0.0] * (p_n + 1)
        for idx in range(pre_window_boundary - 1, -1, -1):
            # Solar surplus can only be stored in periods where charging is
            # actually permitted. Crediting surplus in charge-blocked or
            # export-suppressed periods holds the pre-window SOC ceiling too
            # low to ever meet the deadline floor, making the LP infeasible.
            if charge_pinned is not None and charge_pinned[idx]:
                remaining_solar_kwh[idx] = remaining_solar_kwh[idx + 1]
                continue
            surplus_kw = max(0.0, solar[idx] - load[idx])
            usable_kw = min(self.max_charge_kw, surplus_kw)
            stored_kwh = usable_kw * self.efficiency * dt_hours[idx] * credit_factor
            remaining_solar_kwh[idx] = remaining_solar_kwh[idx + 1] + stored_kwh

        active_count = 0
        min_ceiling = 1.0
        for boundary in range(1, pre_window_boundary):
            remaining_soc = remaining_solar_kwh[boundary] / self.capacity_kwh
            if remaining_soc <= 1e-6:
                continue

            ceiling = target_soc - remaining_soc + buffer_soc
            # Never force a discharge just to make room. This only limits
            # additional prefill above the current SOC.
            ceiling = max(
                current_soc,
                reserve_floor[boundary],
                min(1.0, ceiling),
            )
            ceiling = max(0.0, min(1.0, ceiling))
            if ceiling < 1.0 - 1e-6:
                ceilings[boundary] = ceiling
                active_count += 1
                min_ceiling = min(min_ceiling, ceiling)

        if active_count:
            _LOGGER.debug(
                "Solar-aware pre-window ceiling: %d boundaries, min %.1f%% "
                "(target %.1f%%, credit %.0f%%, buffer %.1f%%)",
                active_count,
                min_ceiling * 100,
                target_soc * 100,
                credit_factor * 100,
                buffer_soc * 100,
            )

        return ceilings

    def _expand_period_values(
        self,
        periods: list[_LpPeriod],
        values: list[float],
        n: int,
    ) -> list[float]:
        """Expand internal period values back to base schedule slots."""
        expanded = [0.0] * n
        for period, value in zip(periods, values):
            for base_idx in range(period.start, period.end):
                expanded[base_idx] = value
        return expanded

    @staticmethod
    def _schedule_mode_constraints(
        schedule: OptimizationSchedule,
        n: int,
    ) -> tuple[list[str], list[float]]:
        """Return command modes and required natural discharge from a schedule."""
        modes = ["idle"] * n
        required_self_use_kw = [0.0] * n
        for idx, action in enumerate((schedule.actions or [])[:n]):
            if action.action == "charge":
                modes[idx] = "charge"
            elif action.action in ("export", "discharge"):
                modes[idx] = "export"
            elif action.action == "idle":
                modes[idx] = "idle"
            else:
                modes[idx] = "self_use"
                required_self_use_kw[idx] = max(
                    0.0,
                    float(action.battery_discharge_w or 0.0) / 1000.0,
                )
        return modes, required_self_use_kw

    @staticmethod
    def _mode_constraints_match(
        left_modes: list[str],
        left_required: list[float],
        right_modes: list[str],
        right_required: list[float],
    ) -> bool:
        """Return True when two physical command projections are equivalent."""
        if left_modes != right_modes:
            return False

        # Coarse LP periods can shift the exact base slot in which a continuous
        # self-use run reaches its floor. Compare the run's total requested
        # natural discharge rather than requiring an identical sub-slot shape;
        # unlike a modes-only comparison, this still proves the next solve was
        # constrained by the same amount of battery energy that will be emitted.
        idx = 0
        while idx < len(left_modes):
            if left_modes[idx] != "self_use":
                idx += 1
                continue
            end = idx + 1
            while end < len(left_modes) and left_modes[end] == "self_use":
                end += 1
            left_total = sum(left_required[idx:end])
            right_total = sum(right_required[idx:end])
            if not math.isclose(
                left_total,
                right_total,
                rel_tol=1e-4,
                abs_tol=0.01,
            ):
                return False
            idx = end
        return True

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
        block_battery_charge: list[bool] | None = None,
        allow_grid_charge: bool = True,
        grid_charge_allowed: list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        schedule_timestamps: list[datetime] | None = None,
        priority_export_slots: list[bool] | None = None,
        disable_idle: bool = False,
    ) -> OptimizerResult:
        """
        Solve the LP formulation using the HiGHS solver (highspy).

        Variables per time step (4 * n total):
            x[0..n-1]   = grid_import[t]  (kW, >= 0)
            x[n..2n-1]  = grid_export[t]  (kW, >= 0)
            x[2n..3n-1] = battery_charge[t] (kW, >= 0)
            x[3n..4n-1] = battery_discharge[t] (kW, >= 0)
        """
        # If SOC is below the optimiser reserve, self-consumption can still use
        # the battery down to the hardware reserve. Keep the optimiser reserve
        # intact for forced export/discharge decisions, but suppress battery
        # export for this solve and lower only the physical SOC floor used by
        # the LP. This avoids force-charging solely to recover the optimiser
        # reserve while still treating it as the forced-discharge boundary.
        _soc_below_reserve = soc_0 < self.backup_reserve
        allow_battery_export = allow_battery_export or [True] * n
        import_bonus_prices = import_bonus_prices or [0.0] * n
        priority_export_slots = priority_export_slots or [False] * n
        terminal_weight_override: float | None = None
        if _soc_below_reserve:
            effective_reserve = self._natural_self_consumption_floor(soc_0)
            log = _LOGGER.info if self.suppress_reserve_warning else _LOGGER.warning
            log(
                "SOC (%.1f%%) below optimiser reserve (%.0f%%) — using "
                "hardware reserve %.1f%% as self-consumption floor",
                soc_0 * 100, self.backup_reserve * 100, effective_reserve * 100,
            )
            # Do not assign artificial end-of-horizon value to recovering the
            # optimiser reserve. Real import/export prices can still justify
            # charging, but ordinary self-use should be allowed to continue.
            # Pass the override as a solve-local parameter instead of mutating
            # self.terminal_weight: the solve runs in a worker thread while
            # config writers (update_config) run on the event loop, so a
            # save/restore around the solve can revert a concurrent write.
            terminal_weight_override = 0.0
            export_floor = max(
                self.backup_reserve,
                self._configured_export_reserve_floor(),
            )
            allow_battery_export = self._export_allowed_after_reserve_recovery(
                allow_battery_export,
                block_battery_charge or [False] * n,
                import_prices,
                export_prices,
                solar,
                load,
                soc_0,
                export_floor,
                allow_grid_charge,
                grid_charge_allowed or [True] * n,
                acquisition_cost_kwh,
                export_bonus_prices or [0.0] * n,
                priority_export_slots,
            )

        try:
            mode_slots: list[str] | None = None
            required_self_use_kw: list[float] | None = None
            last_result: OptimizerResult | None = None
            for iteration in range(MODE_PROJECTION_MAX_ITERATIONS):
                result = self._solve_lp_inner(
                    n, import_prices, export_prices, solar, load, soc_0,
                    cost_function,
                    acquisition_cost_kwh,
                    allow_battery_export,
                    block_battery_charge or [False] * n,
                    allow_grid_charge,
                    grid_charge_allowed or [True] * n,
                    export_bonus_prices or [0.0] * n,
                    export_bonus_cap_kwh,
                    import_bonus_prices,
                    import_bonus_cap_kwh,
                    schedule_timestamps,
                    terminal_weight_override=terminal_weight_override,
                    priority_export_slots=priority_export_slots,
                    disable_idle=disable_idle,
                    mode_slots=mode_slots,
                    required_self_use_kw=required_self_use_kw,
                )
                result.lp_stats["mode_iterations"] = iteration + 1
                if result.solver_used != "highs":
                    if last_result is not None:
                        _LOGGER.warning(
                            "Optimizer command-mode projection became infeasible on "
                            "pass %d; using the previous physically projected HiGHS plan",
                            iteration + 1,
                        )
                        last_result.lp_stats = {
                            **last_result.lp_stats,
                            "mode_iterations": iteration + 1,
                            "mode_converged": False,
                            "fallback_reason": (
                                "mode_projection_infeasible_projected_highs"
                            ),
                        }
                        return last_result
                    return result

                next_modes, next_required = self._schedule_mode_constraints(
                    result.schedule,
                    n,
                )
                if mode_slots is not None and self._mode_constraints_match(
                    mode_slots,
                    required_self_use_kw or [0.0] * n,
                    next_modes,
                    next_required,
                ):
                    result.lp_stats["mode_converged"] = True
                    return result

                mode_slots = next_modes
                required_self_use_kw = next_required
                last_result = result

            # Every pass is rendered through _build_schedule(), which caps the
            # chronological trajectory to the physical and export floors. If
            # the command projection cycles, retain that last feasible projected
            # HiGHS plan instead of replacing a valuable 48-hour plan with the
            # much less capable emergency greedy heuristic.
            _LOGGER.warning(
                "Optimizer command-mode projection did not converge after %d passes; "
                "using the last physically projected HiGHS plan",
                MODE_PROJECTION_MAX_ITERATIONS,
            )
            if last_result is None:
                return self._solve_greedy(
                    n, import_prices, export_prices, solar, load, soc_0,
                    cost_function,
                    acquisition_cost_kwh,
                    allow_battery_export,
                    block_battery_charge or [False] * n,
                    allow_grid_charge,
                    grid_charge_allowed or [True] * n,
                    export_bonus_prices or [0.0] * n,
                    export_bonus_cap_kwh,
                    import_bonus_prices,
                    import_bonus_cap_kwh,
                    schedule_timestamps,
                    priority_export_slots,
                    disable_idle,
                )
            last_result.lp_stats = {
                **last_result.lp_stats,
                "mode_iterations": MODE_PROJECTION_MAX_ITERATIONS,
                "mode_converged": False,
                "fallback_reason": "mode_non_convergence_projected_highs",
            }
            return last_result
        finally:
            if _soc_below_reserve:
                self._below_reserve_recovery_target = None

    def _export_allowed_after_reserve_recovery(
        self,
        allow_battery_export: list[bool],
        block_battery_charge: list[bool],
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        soc_0: float,
        export_floor: float,
        allow_grid_charge: bool,
        grid_charge_allowed: list[bool],
        acquisition_cost_kwh: float,
        export_bonus_prices: list[float],
        priority_export_slots: list[bool],
    ) -> list[bool]:
        """Allow export slots only after charge headroom can recover SOC."""
        if soc_0 >= export_floor:
            return allow_battery_export

        round_trip_eff = max(0.0, min(1.0, self.efficiency)) ** 2
        future_recovery_prices = [0.0] * len(allow_battery_export)
        best_future_export = 0.0
        for idx in range(len(allow_battery_export) - 1, -1, -1):
            effective_export_price = (
                export_prices[idx]
                + (export_bonus_prices[idx] if idx < len(export_bonus_prices) else 0.0)
                if idx < len(export_prices)
                else 0.0
            )
            export_profitable = (
                bool(allow_battery_export[idx])
                and idx < len(import_prices)
                and self._is_export_profitable(
                    effective_export_price,
                    import_prices[idx],
                    acquisition_cost_kwh,
                    acquisition_cost_kwh,
                )
            )
            priority_export = (
                bool(allow_battery_export[idx])
                and idx < len(priority_export_slots)
                and priority_export_slots[idx]
                and effective_export_price > 0.001
            )
            if export_profitable or priority_export:
                best_future_export = max(best_future_export, effective_export_price)
            future_recovery_prices[idx] = best_future_export

        reachable_soc = max(0.0, min(1.0, soc_0))
        recovered: list[bool] = []
        for idx, allowed in enumerate(allow_battery_export):
            effective_export_price = (
                export_prices[idx]
                + (export_bonus_prices[idx] if idx < len(export_bonus_prices) else 0.0)
                if idx < len(export_prices)
                else 0.0
            )
            export_profitable = (
                bool(allowed)
                and idx < len(import_prices)
                and self._is_export_profitable(
                    effective_export_price,
                    import_prices[idx],
                    acquisition_cost_kwh,
                    acquisition_cost_kwh,
                )
            )
            priority_export = (
                bool(allowed)
                and idx < len(priority_export_slots)
                and priority_export_slots[idx]
                and effective_export_price > 0.001
            )
            # Use this slot's own floor, not the horizon-wide maximum: a high
            # floor scoped to a later window (e.g. tomorrow's export bridge)
            # must not block re-enabling export in an earlier window whose
            # real floor is just the optimiser reserve.
            slot_export_floor = max(
                self.backup_reserve,
                self._configured_export_reserve_floor_for_range(idx, idx + 1),
            )
            # Only re-enable export once charge headroom can restore SOC to
            # this slot's floor AND exporting here is actually profitable.
            # Re-allowing unprofitable slots serves no purpose except to raise
            # the LP reserve floor, which force-charges the battery at the
            # current (often peak) price purely to recover the optimiser
            # reserve — the behaviour this below-reserve path exists to avoid.
            recovered.append(
                (export_profitable or priority_export)
                and reachable_soc >= slot_export_floor - 1e-6
            )
            if idx >= len(solar) or idx >= len(load):
                continue
            blocked = idx < len(block_battery_charge) and block_battery_charge[idx]
            if not blocked:
                # During a profitable-export slot the battery exports rather
                # than charges, so it adds no recovery headroom.
                blocked = export_profitable or priority_export
            if blocked:
                continue
            solar_only_charge_kw = self._charge_limit_kw(load[idx], solar[idx], False)
            charge_kw = solar_only_charge_kw
            if (
                allow_grid_charge
                and idx < len(grid_charge_allowed)
                and grid_charge_allowed[idx]
            ):
                future_export_value = (
                    future_recovery_prices[idx + 1]
                    if idx + 1 < len(future_recovery_prices)
                    else 0.0
                )
                try:
                    import_price = float(import_prices[idx])
                except (IndexError, TypeError, ValueError):
                    import_price = None
                if (
                    import_price is not None
                    and future_export_value > 0.001
                    and import_price <= future_export_value * round_trip_eff + 1e-9
                ):
                    charge_kw = self._charge_limit_kw(load[idx], solar[idx], True)
            if charge_kw <= 0:
                continue
            reachable_soc = min(
                1.0,
                reachable_soc
                + charge_kw * self.efficiency * self.dt_hours / self.capacity_kwh,
            )
        return recovered

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
        block_battery_charge: list[bool] | None = None,
        allow_grid_charge: bool = True,
        grid_charge_allowed: list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        schedule_timestamps: list[datetime] | None = None,
        terminal_weight_override: float | None = None,
        priority_export_slots: list[bool] | None = None,
        disable_idle: bool = False,
        mode_slots: list[str | None] | None = None,
        required_self_use_kw: list[float] | None = None,
    ) -> OptimizerResult:
        """Inner LP solver (separated for SOC-below-reserve guard in _solve_lp)."""
        formulation_start = time.monotonic()
        eff = self.efficiency
        cap = self.capacity_kwh
        # Solve-local terminal weight so callers can override without mutating
        # shared instance state (thread-safe against concurrent config writes).
        terminal_weight = (
            self.terminal_weight
            if terminal_weight_override is None
            else terminal_weight_override
        )
        allow_battery_export = allow_battery_export or [True] * n
        block_battery_charge = block_battery_charge or [False] * n
        grid_charge_allowed = grid_charge_allowed or [True] * n
        export_bonus_prices = export_bonus_prices or [0.0] * n
        import_bonus_prices = import_bonus_prices or [0.0] * n
        priority_export_slots = priority_export_slots or [False] * n
        allow_grid_charge = bool(allow_grid_charge)
        periods = self._build_lp_periods(
            n,
            import_prices,
            export_prices,
            solar,
            load,
            allow_battery_export,
            block_battery_charge,
            grid_charge_allowed,
            export_bonus_prices,
            import_bonus_prices,
            priority_export_slots,
            mode_slots,
            required_self_use_kw,
        )
        p_n = len(periods)
        p_import = [period.import_price for period in periods]
        p_export = [period.export_price for period in periods]
        p_export_bonus = [period.export_bonus_price for period in periods]
        p_import_bonus = [period.import_bonus_price for period in periods]
        p_solar = [period.solar_kw for period in periods]
        p_load = [period.load_kw for period in periods]
        p_allow_export = [period.allow_battery_export for period in periods]
        p_block_charge = [period.block_battery_charge for period in periods]
        p_grid_charge_allowed = [period.grid_charge_allowed for period in periods]
        p_priority_export = [period.priority_export for period in periods]
        p_mode = [period.mode for period in periods]
        p_required_self_use = [period.required_self_use_kw for period in periods]
        p_dt = [period.slot_count * self.dt_hours for period in periods]
        p_effective_acquisition = self._effective_export_acquisition_costs(
            p_n,
            p_import,
            p_block_charge,
            allow_grid_charge,
            acquisition_cost_kwh,
            p_grid_charge_allowed,
        )

        def _priority_export_slot(t: int) -> bool:
            export_value = p_export[t] + p_export_bonus[t]
            return (
                p_priority_export[t]
                and p_allow_export[t]
                and export_value > 0.001
            )

        future_priority_recharge_cost = [float("inf")] * p_n
        best_future_recharge_cost = float("inf")
        import_bonus_active = bool(
            import_bonus_cap_kwh is not None
            and import_bonus_cap_kwh > 1e-6
            and any(price > 1e-6 for price in p_import_bonus)
        )
        for idx in range(p_n - 1, -1, -1):
            future_priority_recharge_cost[idx] = best_future_recharge_cost
            if (
                allow_grid_charge
                and p_grid_charge_allowed[idx]
                and not p_block_charge[idx]
            ):
                net_import_price = max(
                    0.0,
                    p_import[idx]
                    - (p_import_bonus[idx] if import_bonus_active else 0.0),
                )
                best_future_recharge_cost = min(
                    best_future_recharge_cost,
                    net_import_price / max(self.efficiency**2, 1e-9),
                )

        def _profitable_export_slot(t: int) -> bool:
            return (
                p_allow_export[t]
                and self._is_export_profitable(
                    p_export[t] + p_export_bonus[t],
                    p_import[t],
                    acquisition_cost_kwh,
                    p_effective_acquisition[t],
                )
            )

        future_self_consumption_values = self._future_self_consumption_values(
            p_n, p_import, p_solar, p_load
        )
        grid_charge_soc_cap = max(
            0.0,
            min(1.0, float(getattr(self, "grid_charge_soc_cap", 1.0) or 0.0)),
        )
        grid_charge_cap_active = allow_grid_charge and grid_charge_soc_cap < 0.999
        grid_charge_cap_headroom_kwh = max(
            0.0,
            (grid_charge_soc_cap - soc_0) * cap,
        )

        # Periods where the LP pins battery_charge to zero (see charge bounds
        # below): explicitly blocked windows, or export-profitable slots with
        # no future self-consumption value. No charge — solar or grid — can
        # enter the battery here, so these periods contribute nothing to the
        # reachable SOC used for feasibility caps and prefill ceilings.
        charge_pinned_periods = []
        for t in range(p_n):
            export_profitable_slot = _profitable_export_slot(t)
            charge_pinned_periods.append(
                p_mode[t] in ("export", "idle")
                or p_block_charge[t]
                or _priority_export_slot(t)
                or (
                    export_profitable_slot
                    and not future_self_consumption_values[t]
                )
            )

        # Best-case reachable SOC at each period boundary starting from soc_0,
        # charging at the permitted limit in every non-pinned period. Used to
        # cap hard SOC floors so they never exceed what is physically reachable
        # (an uncapped floor above reachable SOC makes the whole LP infeasible).
        max_reachable_soc = [min(1.0, soc_0)] * (p_n + 1)
        _reach = min(1.0, soc_0)
        for t in range(p_n):
            if charge_pinned_periods[t]:
                charge_kw = 0.0
            else:
                charge_kw = self._charge_limit_kw(
                    p_load[t],
                    p_solar[t],
                    (
                        False
                        if p_mode[t] == "self_use"
                        else allow_grid_charge and p_grid_charge_allowed[t]
                    ),
                )
            _reach = min(1.0, _reach + charge_kw * eff * p_dt[t] / cap)
            max_reachable_soc[t + 1] = _reach

        optimizer_reserve = max(0.0, min(1.0, self.backup_reserve))
        # The ordinary energy bound is the physical/natural floor. The
        # optimizer reserve is applied separately to slots that actually emit
        # a forced battery-export command; merely permitting export must not
        # make the software reserve a global hold target.
        self_consumption_floor = self._natural_self_consumption_floor(soc_0)
        reserve_floor = [self_consumption_floor] * (p_n + 1)
        recovery_target = self._below_reserve_recovery_target
        if recovery_target is not None and recovery_target > self_consumption_floor:
            max_reachable = soc_0
            reserve_floor[0] = soc_0
            for t in range(p_n):
                reachable_charge_kw = (
                    0.0
                    if p_block_charge[t]
                    else self._charge_limit_kw(
                        p_load[t],
                        p_solar[t],
                        allow_grid_charge and p_grid_charge_allowed[t],
                    )
                )
                max_reachable = min(
                    recovery_target,
                    max_reachable + reachable_charge_kw * eff * p_dt[t] / cap,
                )
                reserve_floor[t + 1] = max(self_consumption_floor, max_reachable)

        # The export floor is an end-of-window boundary condition, not a floor
        # that later periods' self-consumption must respect. Snapshot the base
        # floor (self-consumption + recovery target only) before the export
        # raises so the intra-period discharge rows below do not carry a raised
        # export floor into the period that follows an export window.
        base_reserve_floor = list(reserve_floor)

        # Even when a solve starts below the optimiser reserve and self-use is
        # allowed down to the hardware floor, forced battery export must still
        # respect the user's optimiser reserve once export is allowed again.
        export_reserve_floor = max(
            optimizer_reserve,
            self._configured_export_reserve_floor(),
        )
        if export_reserve_floor > self_consumption_floor:
            for t, allow_export in enumerate(p_allow_export):
                period_export_floor = max(
                    optimizer_reserve,
                    self._configured_export_reserve_floor_for_range(
                        periods[t].start,
                        periods[t].end,
                    ),
                )
                if allow_export and p_mode[t] == "export":
                    # Cap at physically reachable SOC (0.5% buffer) so an
                    # export floor above what charging can reach cannot make
                    # the whole LP infeasible and collapse the horizon to a
                    # self-consumption hold.
                    reachable_cap = max(
                        self_consumption_floor,
                        max_reachable_soc[t + 1] - 0.005,
                    )
                    reserve_floor[t + 1] = max(
                        reserve_floor[t + 1],
                        min(period_export_floor, reachable_cap),
                    )

        # Boundary-energy state model: power variables per period, battery energy
        # variables at period boundaries. This removes the dense cumulative SOC
        # rows that made the 48h/5min model expensive to build and solve.
        bonus_export_active = (
            export_bonus_cap_kwh is not None
            and export_bonus_cap_kwh > 1e-6
            and any(price > 1e-6 for price in p_export_bonus)
        )
        bonus_export_periods = [
            idx for idx, price in enumerate(p_export_bonus) if price > 1e-6
        ]
        bonus_import_active = (
            import_bonus_cap_kwh is not None
            and import_bonus_cap_kwh > 1e-6
            and any(price > 1e-6 for price in p_import_bonus)
        )
        bonus_import_periods = [
            idx for idx, price in enumerate(p_import_bonus) if price > 1e-6
        ]
        grid_charge_offset = 4 * p_n
        curtail_offset = 5 * p_n
        next_offset = 6 * p_n
        bonus_export_offset = next_offset
        if bonus_export_active:
            next_offset += p_n
        bonus_import_offset = next_offset
        if bonus_import_active:
            next_offset += p_n
        energy_offset = next_offset
        num_vars = energy_offset + p_n + 1

        def grid_import_var(t: int) -> int:
            return t

        def grid_export_var(t: int) -> int:
            return p_n + t

        def charge_var(t: int) -> int:
            return 2 * p_n + t

        def discharge_var(t: int) -> int:
            return 3 * p_n + t

        def grid_charge_var(t: int) -> int:
            return grid_charge_offset + t

        def curtail_var(t: int) -> int:
            return curtail_offset + t

        def bonus_export_var(t: int) -> int:
            return bonus_export_offset + t

        def bonus_import_var(t: int) -> int:
            return bonus_import_offset + t

        def energy_var(t: int) -> int:
            return energy_offset + t

        # === Objective function: cost minimization ===
        # minimize SUM(import_price * grid_import - export_price * grid_export) * dt
        c = [0.0] * num_vars
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
        # ties resolve to EARLIER charging. Do the same when the battery is at
        # the reserve floor: waiting until the end of a flat cheap window leaves
        # no margin for inverter latency, BMS taper, or forecast jitter.
        # Solar-SC users with useful SOC and no deadline keep the prefer-later
        # default so grid imports happen after solar has had a chance to fill
        # the battery.
        deadline_mode = (
            allow_grid_charge
            and (
                (
                    self.pre_window_slot is not None
                    and self.pre_window_slot > 0
                    and self.pre_window_soc_target > 0.0
                )
                or soc_0 <= self.backup_reserve + 0.02
            )
        )

        # Pre-compute free charging bonus: use median non-free import price
        # so the LP sees free charging as "saving" that future import cost.
        # See use_per_kwh_terminal field: legacy form divides by `cap` (a unit
        # error; attenuates the bonus to noise on large batteries), corrected
        # form drops the `/cap`. _build_schedule has a hard override that
        # forces max charge during 0c periods regardless of solver output —
        # the corrected coefficient just lets the LP arrive at the same
        # answer through its own economics.
        _nonzero_prices = sorted(p for p in p_import if p > 0.01)
        _terminal_unit_divisor = 1.0 if self.use_per_kwh_terminal else cap
        _free_charge_bonus = (
            _nonzero_prices[len(_nonzero_prices) // 2] * eff / _terminal_unit_divisor
            if _nonzero_prices else 0.0
        )

        for t in range(p_n):
            # Import/charge tie-breaker: prefer EARLIER when a deadline is
            # binding, prefer LATER otherwise (see deadline_mode comment above).
            import_eps = eps * (t if deadline_mode else (p_n - t))
            c[grid_import_var(t)] = (p_import[t] + import_eps) * p_dt[t]
            # Prefer direct grid supply over a price-identical battery cycle.
            # This is only a deterministic tie-breaker (0.001 c/kWh per side),
            # not a degradation-cost model.
            c[charge_var(t)] += 1e-5 * p_dt[t]
            c[discharge_var(t)] += 1e-5 * p_dt[t]
            if p_export[t] > 0:
                c[grid_export_var(t)] = -(
                    p_export[t] + eps * (p_n - t)
                ) * p_dt[t]  # grid_export: prefer earlier
            elif bonus_export_active and p_export_bonus[t] > 0:
                # ZeroHero-style capped bonuses make otherwise-zero exports
                # valuable only through the linked bonus variable below.
                c[grid_export_var(t)] = 0.0
            else:
                # Exporting at 0c costs the same as importing — any energy pushed out
                # at 0c must be bought back at the import rate, so it's never worthwhile
                # to intentionally discharge for 0c export (e.g. Flow Power non-happy-hour).
                c[grid_export_var(t)] = max(0.01, p_import[t]) * p_dt[t]

            # Free electricity: strongly incentivize charging.
            # Without this, the LP may idle during free windows because
            # the near-zero import cost doesn't overcome terminal valuation.
            if p_import[t] <= 0.001 and _free_charge_bonus > 0:
                c[charge_var(t)] -= _free_charge_bonus * p_dt[t]

            if bonus_export_active and p_export_bonus[t] > 0:
                c[bonus_export_var(t)] = -(
                    p_export_bonus[t] + eps * (p_n - t)
                ) * p_dt[t]

            if bonus_import_active and p_import_bonus[t] > 0:
                # ZeroCharge-style capped import credits reduce the settlement
                # cost of grid import without mutating the base tariff price.
                c[bonus_import_var(t)] = -(
                    p_import_bonus[t] + eps * (p_n - t)
                ) * p_dt[t]

            if _profitable_export_slot(t) or _priority_export_slot(t):
                # During an explicit export window, prefer serving concurrent
                # household load from the battery instead of importing for load
                # while exporting only the capped command amount.
                import_penalty = 0.02
                # Close the phantom import->export loop: with solar surplus and
                # an export price above the import price, the export-backing
                # constraint (export - discharge <= surplus) lets the LP charge
                # the surplus into the battery AND export the same surplus,
                # booking simultaneous grid import and export of one kWh. That
                # only pays when export exceeds import, so raise the import
                # penalty to the full spread in exactly those slots to remove
                # the fictitious revenue without distorting other periods.
                surplus_kw = p_solar[t] - p_load[t]
                phantom_spread = (p_export[t] + p_export_bonus[t]) - p_import[t]
                if surplus_kw > 1e-6 and phantom_spread > import_penalty:
                    import_penalty = phantom_spread + 0.001
                c[grid_import_var(t)] += import_penalty * p_dt[t]

            # Real systems can curtail solar when the battery cannot accept
            # charge and the DNSP/export cap is binding. Penalize curtailment
            # at the better of avoided import or export value so the LP only
            # uses it after available charge/export outlets are exhausted.
            c[curtail_var(t)] = max(0.01, p_import[t], p_export[t]) * p_dt[t]
            if grid_charge_cap_active:
                # Keep the grid-charge accounting variable at its minimum
                # feasible value so the cap constrains real grid-to-battery
                # energy, not arbitrary solver slack.
                c[grid_charge_var(t)] = 1e-9 * p_dt[t]

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
        # "Second half of horizon" must be the time midpoint, not the period
        # midpoint: tiered aggregation packs many short periods into the first
        # 6h, so p_n // 2 lands only a few hours in. Map the base-slot time
        # midpoint (n // 2) to its period index instead.
        half_n = self._period_index_for_base_slot(periods, n // 2)
        second_half_prices = p_import[half_n:] if half_n < p_n else p_import
        min_grid_recharge = min(second_half_prices) if second_half_prices else 0.0

        # Check if solar can recharge the battery in the second half of horizon.
        # If so, the marginal recharge cost is the export price (opportunity cost).
        solar_recharge_costs = [
            p_export[t]
            for t in range(half_n, p_n)
            if p_solar[t] > 0.1  # Meaningful solar available
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
            all_nonzero = [p for p in p_import if p > 0.01]
            if all_nonzero:
                median_price = sorted(all_nonzero)[len(all_nonzero) // 2]
                terminal_price = max(terminal_price, median_price * (1 - eff))

        terminal_price *= terminal_weight

        if terminal_price > 0:
            # See use_per_kwh_terminal field for the unit-error history.
            # Correct coefficients are terminal_price * eff * dt (no /cap):
            # terminal_price is $/kWh, so a per-kW objective coefficient over
            # dt hours produces $ — adding /cap would give $·h/kWh², garbage.
            # Solar-equipped users see no behavior change because solar
            # export sets terminal_price low (~5c FiT), keeping the
            # discharge penalty well under avoided-import savings.
            for t in range(p_n):
                # Charging adds SOC → subtract cost (incentivize keeping charge)
                c[charge_var(t)] -= terminal_price * eff * p_dt[t] / _terminal_unit_divisor
                # Discharging removes SOC → add cost (penalize draining)
                c[discharge_var(t)] += terminal_price * p_dt[t] / (eff * _terminal_unit_divisor)

        # === Equality constraints: power balance ===
        # solar[t] + grid_import[t] + battery_discharge[t] =
        # load[t] + grid_export[t] + battery_charge[t] + solar_curtail[t]
        # Rearranged:
        # grid_import[t] - grid_export[t] - battery_charge[t]
        # + battery_discharge[t] - solar_curtail[t] = load[t] - solar[t]
        A_eq = _LpMatrix((2 * p_n, num_vars), dtype=float)
        b_eq = [0.0] * (2 * p_n)

        for t in range(p_n):
            A_eq[t, grid_import_var(t)] = 1.0
            A_eq[t, grid_export_var(t)] = -1.0
            A_eq[t, charge_var(t)] = -1.0
            A_eq[t, discharge_var(t)] = 1.0
            A_eq[t, curtail_var(t)] = -1.0
            b_eq[t] = p_load[t] - p_solar[t]

            # Energy transition: E[t+1] = E[t] + charge*eff*dt - discharge*dt/eff
            row = p_n + t
            A_eq[row, energy_var(t + 1)] = 1.0
            A_eq[row, energy_var(t)] = -1.0
            A_eq[row, charge_var(t)] = -eff * p_dt[t]
            A_eq[row, discharge_var(t)] = p_dt[t] / eff

        # Priority/provider status is permission, not a synthetic subsidy. When
        # export is below the modeled acquisition cost, allow it only when real
        # future charge is both cheap enough and explicitly paired by a linear
        # constraint below. This keeps a one-kWh rebate or charge slot from
        # authorising an entire window of below-cost battery export.
        paired_priority_recharge_periods: dict[int, list[int]] = {}
        for t in range(p_n):
            export_value = p_export[t] + p_export_bonus[t]
            if (
                not _priority_export_slot(t)
                or acquisition_cost_kwh <= 0
                or export_value + 1e-9 >= p_effective_acquisition[t]
            ):
                continue
            eligible_recharge = []
            for future_idx in range(t + 1, p_n):
                if (
                    not allow_grid_charge
                    or not p_grid_charge_allowed[future_idx]
                    or p_block_charge[future_idx]
                    or p_mode[future_idx] not in (None, "charge")
                ):
                    continue
                net_import_price = max(
                    0.0,
                    p_import[future_idx]
                    - (
                        p_import_bonus[future_idx]
                        if bonus_import_active
                        else 0.0
                    ),
                )
                if net_import_price <= export_value * eff * eff + 1e-9:
                    eligible_recharge.append(future_idx)
            if eligible_recharge:
                paired_priority_recharge_periods[t] = eligible_recharge

        pre_window_boundary: int | None = None
        pre_window_effective_target: float | None = None
        A_ub_rows = 2 * p_n
        if bonus_export_active:
            A_ub_rows += 2 * len(bonus_export_periods) + 1
        if bonus_import_active:
            A_ub_rows += len(bonus_import_periods) + 1
        if grid_charge_cap_active:
            A_ub_rows += 3 * p_n + 1
        if paired_priority_recharge_periods:
            A_ub_rows += len(paired_priority_recharge_periods) + 1
        if (
            allow_grid_charge
            and self.pre_window_slot is not None
            and self.pre_window_slot > 0
            and self.pre_window_slot <= n
            and self.pre_window_soc_target > 0.0
            and not getattr(self, "_relaxing", False)
        ):
            pre_window_boundary = self._period_index_for_base_slot(
                periods, self.pre_window_slot
            )
            if pre_window_boundary > 0:
                slots_to_window = pre_window_boundary

                def _deadline_charge_limit_kw(t: int) -> float:
                    export_profitable_slot = _profitable_export_slot(t)
                    if (
                        p_block_charge[t]
                        or _priority_export_slot(t)
                        or (
                            export_profitable_slot
                            and not future_self_consumption_values[t]
                        )
                    ):
                        return 0.0
                    return self._charge_limit_kw(
                        p_load[t],
                        p_solar[t],
                        allow_grid_charge and p_grid_charge_allowed[t],
                    )

                max_stored_kwh = 0.0
                remaining_grid_stored_kwh = grid_charge_cap_headroom_kwh
                for t in range(slots_to_window):
                    charge_limit_kw = _deadline_charge_limit_kw(t)
                    if charge_limit_kw <= 0:
                        continue
                    solar_surplus_kw = max(0.0, p_solar[t] - p_load[t])
                    solar_charge_kw = min(charge_limit_kw, solar_surplus_kw)
                    max_stored_kwh += solar_charge_kw * eff * p_dt[t]
                    grid_charge_kw = max(0.0, charge_limit_kw - solar_charge_kw)
                    if grid_charge_kw <= 0:
                        continue
                    grid_stored_kwh = grid_charge_kw * eff * p_dt[t]
                    if grid_charge_cap_active:
                        grid_stored_kwh = min(
                            grid_stored_kwh,
                            remaining_grid_stored_kwh,
                        )
                        remaining_grid_stored_kwh = max(
                            0.0,
                            remaining_grid_stored_kwh - grid_stored_kwh,
                        )
                    max_stored_kwh += grid_stored_kwh

                max_soc_gain = max_stored_kwh / cap
                max_reachable = min(1.0, soc_0 + max_soc_gain)
                # Keep the established 0.5% feasibility margin while the
                # configured target is reachable. Once execution has fallen
                # behind that target, use only a numerical margin: granting a
                # fresh 0.5% on every rolling solve ratchets the deadline down.
                reachability_margin = (
                    PRE_WINDOW_REACHABLE_TARGET_MARGIN_SOC
                    if self.pre_window_soc_target <= max_reachable + 1e-9
                    else PRE_WINDOW_REACHABILITY_MARGIN_SOC
                )
                pre_window_effective_target = min(
                    self.pre_window_soc_target,
                    max_reachable - reachability_margin,
                )
                A_ub_rows += 1

        A_ub = _LpMatrix((A_ub_rows, num_vars), dtype=float)
        b_ub: list[float] = []

        for t in range(p_n):
            # Prevent current-period charge from funding same-period discharge.
            # Use the base floor (not the export-raised boundary floor) so the
            # period after an export window can still self-consume below that
            # window's transient export floor.
            A_ub[len(b_ub), discharge_var(t)] = p_dt[t] / eff
            A_ub[len(b_ub), energy_var(t)] = -1.0
            b_ub.append(-base_reserve_floor[t] * cap)

            # Export must be backed by physical energy from solar surplus or
            # battery discharge.
            A_ub[len(b_ub), grid_export_var(t)] = 1.0
            A_ub[len(b_ub), discharge_var(t)] = -1.0
            b_ub.append(max(0.0, p_solar[t] - p_load[t]))

        if paired_priority_recharge_periods:
            # Per-period pairing prevents a cheap slot before a later export
            # from being counted as its replacement.
            for export_idx, recharge_indices in (
                paired_priority_recharge_periods.items()
            ):
                A_ub[len(b_ub), discharge_var(export_idx)] = p_dt[export_idx]
                for recharge_idx in recharge_indices:
                    A_ub[len(b_ub), charge_var(recharge_idx)] = (
                        -eff * eff * p_dt[recharge_idx]
                    )
                b_ub.append(
                    max(0.0, p_load[export_idx] - p_solar[export_idx])
                    * p_dt[export_idx]
                )

            # The aggregate row prevents one future charge kWh from backing
            # multiple earlier below-acquisition priority exports.
            paired_recharge_union: set[int] = set()
            paired_home_kwh = 0.0
            for export_idx, recharge_indices in (
                paired_priority_recharge_periods.items()
            ):
                A_ub[len(b_ub), discharge_var(export_idx)] = p_dt[export_idx]
                paired_home_kwh += (
                    max(0.0, p_load[export_idx] - p_solar[export_idx])
                    * p_dt[export_idx]
                )
                paired_recharge_union.update(recharge_indices)
            for recharge_idx in paired_recharge_union:
                A_ub[len(b_ub), charge_var(recharge_idx)] = (
                    -eff * eff * p_dt[recharge_idx]
                )
            b_ub.append(paired_home_kwh)

        if bonus_export_active:
            for t in bonus_export_periods:
                # Only physical exports can consume the capped ZeroHero bucket.
                A_ub[len(b_ub), bonus_export_var(t)] = 1.0
                A_ub[len(b_ub), grid_export_var(t)] = -1.0
                b_ub.append(0.0)

            for t in bonus_export_periods:
                # Intentional battery export must fit inside the bonus bucket.
                # Solar surplus may still export at the base FiT outside it.
                A_ub[len(b_ub), grid_export_var(t)] = 1.0
                A_ub[len(b_ub), bonus_export_var(t)] = -1.0
                b_ub.append(max(0.0, p_solar[t] - p_load[t]))

            for t in bonus_export_periods:
                A_ub[len(b_ub), bonus_export_var(t)] = p_dt[t]
            b_ub.append(max(0.0, float(export_bonus_cap_kwh or 0.0)))

        if bonus_import_active:
            for t in bonus_import_periods:
                # Only physical grid imports can consume the capped
                # ZeroCharge/free-import bucket.
                A_ub[len(b_ub), bonus_import_var(t)] = 1.0
                A_ub[len(b_ub), grid_import_var(t)] = -1.0
                b_ub.append(0.0)

            for t in bonus_import_periods:
                A_ub[len(b_ub), bonus_import_var(t)] = p_dt[t]
            b_ub.append(max(0.0, float(import_bonus_cap_kwh or 0.0)))

        if grid_charge_cap_active:
            for t in range(p_n):
                # grid_charge[t] <= battery_charge[t]
                A_ub[len(b_ub), grid_charge_var(t)] = 1.0
                A_ub[len(b_ub), charge_var(t)] = -1.0
                b_ub.append(0.0)

                # grid_charge[t] <= grid_import[t]
                A_ub[len(b_ub), grid_charge_var(t)] = 1.0
                A_ub[len(b_ub), grid_import_var(t)] = -1.0
                b_ub.append(0.0)

                # battery_charge[t] - grid_charge[t] <= available solar surplus.
                # This lets solar charge above the cap while every kW of charge
                # beyond exogenous surplus is counted as grid-to-battery energy.
                A_ub[len(b_ub), charge_var(t)] = 1.0
                A_ub[len(b_ub), grid_charge_var(t)] = -1.0
                b_ub.append(max(0.0, p_solar[t] - p_load[t]))

            for t in range(p_n):
                A_ub[len(b_ub), grid_charge_var(t)] = eff * p_dt[t]
            b_ub.append(grid_charge_cap_headroom_kwh)

        # === Pre-window SOC floor ===
        # Force soc[pre_window_slot - 1] >= target so the battery is filled
        # before a known high-value export window (e.g. Flow Power Happy Hour).
        # The 48 h rolling horizon otherwise places grid-charge slots at the
        # globally cheapest periods, which often misses today's HH entirely.
        # Cap target at what's physically reachable to keep the LP feasible.
        if pre_window_boundary is not None and pre_window_boundary > 0:
            if (
                pre_window_effective_target is not None
                and pre_window_effective_target > soc_0
            ):
                A_ub[len(b_ub), energy_var(pre_window_boundary)] = -1.0
                b_ub.append(-pre_window_effective_target * cap)
                _LOGGER.debug(
                    "Pre-window SOC floor: target=%.1f%% (capped from %.1f%%) "
                    "at slot %d (%.1f h ahead), current=%.1f%%",
                    pre_window_effective_target * 100,
                    self.pre_window_soc_target * 100,
                    self.pre_window_slot,
                    sum(p_dt[:pre_window_boundary]),
                    soc_0 * 100,
                )
            else:
                # Keep A_ub row count aligned with b_ub when the pre-window
                # request is already satisfied by current SOC.
                b_ub.append(0.0)

        solar_prefill_ceilings = self._pre_window_solar_prefill_ceilings(
            pre_window_boundary=pre_window_boundary,
            target_soc=pre_window_effective_target,
            solar=p_solar,
            load=p_load,
            dt_hours=p_dt,
            reserve_floor=reserve_floor,
            current_soc=soc_0,
            charge_pinned=charge_pinned_periods,
        )

        # === Variable bounds ===
        # Cap grid at 100 kW by default (generous safety limit; prevents
        # unbounded LP if a price accidentally goes negative or zero). Sites
        # with a known DNSP/export limit override the export side so the LP
        # models the same physical cap the runtime controller will enforce.
        max_grid_kw = (
            max(0.0, self.max_grid_import_w / 1000.0)
            if self.max_grid_import_w is not None
            else 100.0
        )
        max_grid_export_kw = 100.0
        if self.max_grid_export_w is not None:
            max_grid_export_kw = max(0.0, self.max_grid_export_w / 1000.0)

        def _export_acquisition_threshold(t: int) -> float:
            threshold = p_effective_acquisition[t]
            if _priority_export_slot(t):
                threshold = min(
                    threshold,
                    future_priority_recharge_cost[t],
                )
            return threshold

        bounds = []
        for t in range(p_n):
            bounds.append((0, max_grid_kw))  # grid_import

        # Grid export is always allowed for solar surplus. When battery export is
        # disabled, cap export to exogenous surplus so the LP cannot invent
        # grid-import -> grid-export or battery -> grid arbitrage.
        for t in range(p_n):
            if p_mode[t] is not None and p_mode[t] != "export":
                solar_surplus_kw = max(0.0, p_solar[t] - p_load[t])
                bounds.append((0, min(max_grid_export_kw, solar_surplus_kw)))
                continue
            export_profitable_slot = _profitable_export_slot(t)
            priority_export_slot = _priority_export_slot(t)
            future_self_consumption_value = future_self_consumption_values[t]
            suppress_generic_battery_export = (
                export_profitable_slot
                and future_self_consumption_value
                and not priority_export_slot
                and not p_block_charge[t]
            )
            if p_allow_export[t] and not suppress_generic_battery_export:
                export_limit_kw = max_grid_export_kw
                if self.max_battery_export_kw is not None:
                    solar_surplus_kw = max(0.0, p_solar[t] - p_load[t])
                    export_limit_kw = min(
                        export_limit_kw,
                        solar_surplus_kw + self.max_battery_export_kw,
                    )
                bounds.append((0, export_limit_kw))  # grid_export
            else:
                solar_surplus_kw = max(0.0, p_solar[t] - p_load[t])
                bounds.append((0, min(max_grid_export_kw, solar_surplus_kw)))

        for t in range(p_n):
            if p_mode[t] in ("export", "idle"):
                bounds.append((0, 0.0))
                continue
            if p_mode[t] == "self_use":
                bounds.append((
                    0,
                    0.0
                    if p_block_charge[t]
                    else self._charge_limit_kw(
                        p_load[t],
                        p_solar[t],
                        False,
                    ),
                ))
                continue
            export_profitable_slot = _profitable_export_slot(t)
            priority_export_slot = _priority_export_slot(t)
            future_self_consumption_value = future_self_consumption_values[t]
            if p_block_charge[t] or priority_export_slot or (
                export_profitable_slot and not future_self_consumption_value
            ):
                # Do not charge during explicitly blocked export windows
                # (for example fixed Flow Power Happy Hour export windows).
                # A generic positive FiT is not enough to block charging:
                # Octopus IOG can have 6.9p import and 12p export across the
                # whole off-peak window. Permit charging there only when it has
                # later self-consumption value, not for grid-import->export
                # passthrough.
                bounds.append((0, 0.0))
            elif not allow_grid_charge:
                bounds.append((
                    0,
                    self._charge_limit_kw(
                        p_load[t], p_solar[t], allow_grid_charge
                    ),
                ))
            else:
                bounds.append((
                    0,
                    self._charge_limit_kw(
                        p_load[t],
                        p_solar[t],
                        p_grid_charge_allowed[t],
                    ),
                ))  # battery_charge

        for t in range(p_n):
            if p_mode[t] in ("charge", "idle"):
                bounds.append((0, 0.0))
                continue
            if p_mode[t] == "self_use":
                net_load_kw = max(0.0, p_load[t] - p_solar[t])
                upper = min(self.max_discharge_kw, net_load_kw)
                lower = min(
                    upper,
                    max(0.0, p_required_self_use[t]),
                )
                bounds.append((lower, upper))
                continue
            export_profitable_slot = _profitable_export_slot(t)
            priority_export_slot = _priority_export_slot(t)
            future_self_consumption_value = future_self_consumption_values[t]
            suppress_generic_battery_export = (
                export_profitable_slot
                and future_self_consumption_value
                and not priority_export_slot
                and not p_block_charge[t]
            )
            restrict_to_self_consumption = (
                suppress_generic_battery_export
                or not p_allow_export[t]
                or (
                    acquisition_cost_kwh > 0
                    and (p_export[t] + p_export_bonus[t])
                    < _export_acquisition_threshold(t)
                )
            )
            if restrict_to_self_consumption:
                # Allow discharge only for self-consumption (serving home load)
                net_load_kw = max(0.0, p_load[t] - p_solar[t])
                max_self_consumption = net_load_kw
                bounds.append((0, min(self.max_discharge_kw, max_self_consumption)))
            elif self.max_battery_export_kw is not None:
                # Target-export batteries receive a grid-export power command.
                # The battery still has to cover local load before any surplus
                # reaches the grid, so do not let the command cap masquerade as
                # a total battery-discharge cap during export windows.
                net_load_kw = max(0.0, p_load[t] - p_solar[t])
                bounds.append((
                    0,
                    min(
                        self.max_discharge_kw,
                        net_load_kw + self.max_battery_export_kw,
                    ),
                ))
            else:
                bounds.append((0, self.max_discharge_kw))  # battery_discharge

        for t in range(p_n):
            if not grid_charge_cap_active:
                bounds.append((0, 0.0))
                continue
            if p_block_charge[t] or not p_grid_charge_allowed[t]:
                bounds.append((0, 0.0))
            else:
                bounds.append((
                    0,
                    self._charge_limit_kw(
                        p_load[t],
                        p_solar[t],
                        p_grid_charge_allowed[t],
                    ),
                ))

        for t in range(p_n):
            bounds.append((0, max(0.0, p_solar[t])))  # solar_curtail

        if bonus_export_active:
            for t in range(p_n):
                bonus_limit_kw = max_grid_export_kw if p_export_bonus[t] > 0 else 0.0
                bounds.append((0, bonus_limit_kw))

        if bonus_import_active:
            for t in range(p_n):
                bonus_limit_kw = max_grid_kw if p_import_bonus[t] > 0 else 0.0
                bounds.append((0, bonus_limit_kw))

        bounds.append((soc_0 * cap, soc_0 * cap))
        for t in range(1, p_n + 1):
            upper_soc = solar_prefill_ceilings[t]
            upper = cap if upper_soc is None else upper_soc * cap
            lower = reserve_floor[t] * cap
            bounds.append((lower, max(lower, upper)))

        A_eq = A_eq.tocsr()
        A_ub = A_ub.tocsr()
        formulation_time_s = time.monotonic() - formulation_start

        # === Solve ===
        _LOGGER.debug(
            "Solving LP: %d base steps, %d periods, %d variables, %d constraints, "
            "%d nonzeros, %.0fs limit",
            n,
            p_n,
            num_vars,
            A_eq.shape[0] + A_ub.shape[0],
            A_eq.nnz + A_ub.nnz,
            LP_SOLVER_TIME_LIMIT_SECONDS,
        )

        solver_start = time.monotonic()
        result = _solve_lp_highs(
            c,
            A_ub,
            b_ub,
            A_eq,
            b_eq,
            bounds,
            time_limit=LP_SOLVER_TIME_LIMIT_SECONDS,
        )
        solver_time_s = time.monotonic() - solver_start
        lp_stats = {
            "backend": "highspy",
            "base_steps": n,
            "period_count": p_n,
            "variables": num_vars,
            "constraints": int(A_eq.shape[0] + A_ub.shape[0]),
            "nonzeros": int(A_eq.nnz + A_ub.nnz),
            "formulation_time_s": round(formulation_time_s, 4),
            "solver_time_s": round(solver_time_s, 4),
            "time_limit_s": LP_SOLVER_TIME_LIMIT_SECONDS,
            "status": getattr(result, "status", None),
            "message": getattr(result, "message", ""),
        }

        if not result.success:
            _LOGGER.warning(f"LP solver status: {result.message}")
            if "infeasible" in result.message.lower():
                # The LP could not be satisfied with the real backup-reserve
                # floor. Rather than relaxing that floor to 5% and re-solving
                # — which authorises the battery to discharge to near-empty
                # purely to make the model feasible (and has drained users'
                # batteries to 5%) — fall back to a self-consumption hold that
                # never exports the battery, never grid-charges, and never
                # drops below the genuine reserve.
                hold = self._solve_self_consumption_hold(
                    n, import_prices, export_prices, solar, load, soc_0, cost_function,
                    acquisition_cost_kwh,
                    allow_battery_export,
                    block_battery_charge,
                    allow_grid_charge,
                    grid_charge_allowed,
                    export_bonus_prices,
                    export_bonus_cap_kwh,
                    import_bonus_prices,
                    import_bonus_cap_kwh,
                    schedule_timestamps,
                    disable_idle=disable_idle,
                )
                hold.lp_stats = {**lp_stats, "fallback_reason": "infeasible_self_consumption_hold"}
                return hold
            # Fall back to greedy
            greedy = self._solve_greedy(
                n, import_prices, export_prices, solar, load, soc_0, cost_function,
                acquisition_cost_kwh,
                allow_battery_export,
                block_battery_charge,
                allow_grid_charge,
                grid_charge_allowed,
                export_bonus_prices,
                export_bonus_cap_kwh,
                import_bonus_prices,
                import_bonus_cap_kwh,
                schedule_timestamps,
                priority_export_slots,
                disable_idle,
            )
            greedy.lp_stats = {**lp_stats, "fallback_reason": "solver_failed"}
            return greedy

        # === Extract solution ===
        x = result.x
        # Clamp tiny negative values to 0
        x = [max(0.0, v) for v in x]

        period_grid_import = [x[grid_import_var(t)] for t in range(p_n)]
        period_grid_export = [x[grid_export_var(t)] for t in range(p_n)]
        period_battery_charge = [x[charge_var(t)] for t in range(p_n)]
        period_battery_discharge = [x[discharge_var(t)] for t in range(p_n)]
        grid_import = self._expand_period_values(periods, period_grid_import, n)
        grid_export = self._expand_period_values(periods, period_grid_export, n)
        battery_charge = self._expand_period_values(periods, period_battery_charge, n)
        battery_discharge = self._expand_period_values(periods, period_battery_discharge, n)
        effective_export_prices = [
            export_prices[t] + export_bonus_prices[t]
            for t in range(n)
        ]

        # Build schedule with action mapping
        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0, import_prices, effective_export_prices,
            block_battery_charge,
            schedule_timestamps,
            allow_grid_charge,
            grid_charge_allowed,
            priority_export_slots,
            disable_idle,
        )

        # _build_schedule re-models "hold" slots (LP imports to serve load while
        # the battery idles) as natural self-consumption discharge, and clamps
        # charge/discharge to physically-available SOC. Recompute the reported
        # grid flows from the schedule the user actually sees so grid_import_w /
        # grid_export_w and predicted_cost describe that schedule — not the raw
        # LP solution, which would double-count imports the schedule covers from
        # the battery.
        grid_import, grid_export = self._grid_flows_from_schedule(
            schedule, n, solar, load
        )
        bonus_export = self._allocate_capped_bonus(
            grid_export, export_bonus_prices, export_bonus_cap_kwh
        )
        bonus_import = self._allocate_capped_bonus(
            grid_import, import_bonus_prices, import_bonus_cap_kwh
        )

        # Calculate costs for first 24 hours only (display as daily cost)
        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * self.dt_hours
            - import_bonus_prices[t] * bonus_import[t] * self.dt_hours
            - export_prices[t] * grid_export[t] * self.dt_hours
            - export_bonus_prices[t] * bonus_export[t] * self.dt_hours
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h,
            import_prices,
            export_prices,
            solar,
            load,
            export_bonus_prices=export_bonus_prices,
            export_bonus_cap_kwh=export_bonus_cap_kwh,
            import_bonus_prices=import_bonus_prices,
            import_bonus_cap_kwh=import_bonus_cap_kwh,
        )
        predicted_savings = baseline_cost - predicted_cost

        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(predicted_savings, 2)
        reserve_recommendation = self._build_reserve_recommendation(
            schedule,
            solar,
            load,
        )

        return OptimizerResult(
            schedule=schedule,
            objective_value=result.fun,
            solver_used="highs",
            feasible=True,
            grid_import_w=[v * 1000 for v in grid_import],
            grid_export_w=[v * 1000 for v in grid_export],
            lp_stats=lp_stats,
            reserve_recommendation=reserve_recommendation,
        )

    def _build_reserve_recommendation(
        self,
        schedule: OptimizationSchedule,
        solar: list[float],
        load: list[float],
    ) -> dict[str, Any]:
        """Suggest the optimizer reserve needed to bridge to the next charge."""
        actions = schedule.actions or []
        if not actions:
            return {}

        threshold_w = ACTION_THRESHOLD_W
        next_charge_idx: int | None = None
        next_charge_reason: str | None = None
        for idx, action in enumerate(actions):
            if action.battery_charge_w > threshold_w:
                next_charge_idx = idx
                next_charge_reason = (
                    "scheduled_grid_charge"
                    if action.action == "charge"
                    else "forecast_solar_surplus"
                )
                break

            if idx < len(solar) and idx < len(load):
                if (solar[idx] - load[idx]) * 1000 > threshold_w:
                    next_charge_idx = idx
                    next_charge_reason = "forecast_solar_surplus"
                    break

        bridge_actions = (
            actions[: next_charge_idx + 1]
            if next_charge_idx is not None
            else actions
        )
        soc_points = [
            (idx, action.soc)
            for idx, action in enumerate(bridge_actions)
            if action.soc is not None
        ]
        if not soc_points:
            return {}

        minimum_idx, minimum_soc_raw = min(soc_points, key=lambda item: item[1])
        minimum_soc = float(minimum_soc_raw)
        configured_percent = max(
            0,
            min(100, int(round(self.backup_reserve * 100))),
        )
        hardware_percent = max(
            0,
            min(100, int(round(self.hardware_reserve * 100))),
        )
        starting_soc = float(soc_points[0][1])
        meaningful_bridge_drop = starting_soc - minimum_soc > 0.02
        if meaningful_bridge_drop:
            suggested_ratio = max(self.hardware_reserve, min(1.0, minimum_soc))
        else:
            suggested_ratio = max(self.hardware_reserve, self.backup_reserve)
        suggested_percent = max(0, min(100, int(round(suggested_ratio * 100))))

        recommendation: dict[str, Any] = {
            "suggested_optimizer_reserve_percent": suggested_percent,
            "configured_optimizer_reserve_percent": configured_percent,
            "hardware_reserve_percent": hardware_percent,
            "minimum_forecast_soc_percent": max(
                0,
                min(100, round(minimum_soc * 100, 1)),
            ),
            "minimum_forecast_soc_time": actions[minimum_idx].timestamp.isoformat(),
            "protects_until": (
                actions[next_charge_idx].timestamp.isoformat()
                if next_charge_idx is not None
                else actions[-1].timestamp.isoformat()
            ),
            "next_charge_reason": next_charge_reason or "no_charge_in_horizon",
            "needs_optimizer_reserve_raise": suggested_percent > configured_percent,
        }
        if not meaningful_bridge_drop:
            recommendation["note"] = "No discharge bridge before next charge"
        if next_charge_idx is None:
            recommendation["note"] = "No charging opportunity in optimizer horizon"
        return recommendation

    def _build_home_load_export_bridge(
        self,
        actions: list[ScheduleAction],
        solar: list[float],
        load: list[float],
    ) -> dict[str, Any]:
        """Return an export-only floor that leaves energy for post-export home load."""
        threshold_w = ACTION_THRESHOLD_W
        best_bridge: dict[str, Any] = {}
        best_floor = 0.0
        idx = 0

        while idx < len(actions):
            if actions[idx].action != "export":
                idx += 1
                continue

            export_start_idx = idx
            while idx < len(actions) and actions[idx].action == "export":
                idx += 1
            bridge_start_idx = idx
            if bridge_start_idx >= len(actions):
                continue

            next_charge_idx: int | None = None
            next_charge_reason: str | None = None
            for scan_idx in range(bridge_start_idx, len(actions)):
                action = actions[scan_idx]
                if action.battery_charge_w > threshold_w:
                    next_charge_idx = scan_idx
                    next_charge_reason = (
                        "scheduled_grid_charge"
                        if action.action == "charge"
                        else "forecast_solar_surplus"
                    )
                    break

                if scan_idx < len(solar) and scan_idx < len(load):
                    if (solar[scan_idx] - load[scan_idx]) * 1000 > threshold_w:
                        next_charge_idx = scan_idx
                        next_charge_reason = "forecast_solar_surplus"
                        break

            bridge_end_exclusive = (
                next_charge_idx
                if next_charge_idx is not None
                else len(actions)
            )
            bridge_kwh = 0.0
            for load_idx in range(bridge_start_idx, bridge_end_exclusive):
                if load_idx >= len(solar) or load_idx >= len(load):
                    break
                bridge_kwh += max(0.0, load[load_idx] - solar[load_idx]) * self.dt_hours

            if bridge_kwh <= 0:
                continue

            bridge_soc = bridge_kwh / max(self.capacity_kwh * self.efficiency, 0.001)
            export_floor = max(
                self.hardware_reserve,
                min(1.0, self.hardware_reserve + bridge_soc),
            )
            if export_floor <= best_floor:
                continue

            best_floor = export_floor
            protects_until_idx = (
                next_charge_idx
                if next_charge_idx is not None
                else len(actions) - 1
            )
            best_bridge = {
                "home_load_export_floor_percent": max(
                    0,
                    min(100, int(round(export_floor * 100))),
                ),
                "home_load_bridge_kwh": round(bridge_kwh, 3),
                "home_load_bridge_start": actions[bridge_start_idx].timestamp.isoformat(),
                "home_load_bridge_until": actions[protects_until_idx].timestamp.isoformat(),
                "home_load_bridge_next_charge_reason": (
                    next_charge_reason or "no_charge_in_horizon"
                ),
                "home_load_bridge_after_export_start": actions[
                    export_start_idx
                ].timestamp.isoformat(),
            }

        return best_bridge

    def _solve_self_consumption_hold(
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
        block_battery_charge: list[bool] | None = None,
        allow_grid_charge: bool = True,
        grid_charge_allowed: list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        schedule_timestamps: list[datetime] | None = None,
        priority_export_slots: list[bool] | None = None,
        disable_idle: bool = False,
    ) -> OptimizerResult:
        """Safe fallback when the LP is infeasible: hold in self-consumption.

        The previous fallback relaxed the backup-reserve floor to 5% and
        re-solved the LP. That made the model feasible by deleting the very
        safety floor it exists to protect — so the "optimal" relaxed plan would
        happily discharge the battery to ~5% just to satisfy the objective,
        draining users' batteries overnight.

        Instead, fall back to native self-consumption — the same do-no-harm
        behaviour the inverter exhibits without optimisation:

        * the battery only discharges to serve home load (never exports to grid),
        * the battery only charges from solar surplus (never from the grid),
        * SOC never drops below the genuine reserve floor (or, when already
          below it, holds at the current SOC down to the hardware floor).

        The result is marked ``feasible=False`` with no reserve recommendation
        so Auto-Apply Optimizer Reserve never ratchets the reserve down off the
        back of an infeasible solve.
        """
        _LOGGER.warning(
            "LP infeasible — holding in self-consumption: battery serves home "
            "load and charges from solar only (no grid export/charge), drawing "
            "down to the hardware reserve floor as the inverter would natively",
        )

        eff = self.efficiency
        cap = self.capacity_kwh
        dt = self.dt_hours
        export_bonus_prices = export_bonus_prices or [0.0] * n
        import_bonus_prices = import_bonus_prices or [0.0] * n
        block_battery_charge = block_battery_charge or [False] * n

        # Use the SAME floor the emitted schedule is rebuilt with
        # (_build_schedule -> _natural_self_consumption_floor). In native
        # self-consumption the inverter serves home load down to its hardware
        # reserve, not the software optimiser reserve, so simulating a hold at
        # the optimiser reserve would make grid_import_w/predicted_cost describe
        # a dispatch that never happens and diverge from the displayed SOC.
        self_consumption_floor = self._natural_self_consumption_floor(soc_0)
        max_grid_export_kw = (
            max(0.0, self.max_grid_export_w / 1000.0)
            if self.max_grid_export_w is not None
            else None
        )

        grid_import = [0.0] * n
        grid_export = [0.0] * n
        battery_charge = [0.0] * n
        battery_discharge = [0.0] * n

        soc = soc_0
        for t in range(n):
            net_load = load[t] - solar[t]
            charge_kw = 0.0
            discharge_kw = 0.0
            if net_load > 0:
                # Home needs power: discharge the battery to serve load only,
                # bounded by the discharge rate and the energy available above
                # the reserve floor.
                discharge_room = max(0.0, soc - self_consumption_floor) * cap * eff / dt
                discharge_kw = min(self.max_discharge_kw, net_load, discharge_room)
            elif net_load < 0 and not block_battery_charge[t]:
                # Solar surplus: charge from solar only (never from the grid).
                surplus = -net_load
                charge_room = max(0.0, 1.0 - soc) * cap / (eff * dt)
                charge_kw = min(self.max_charge_kw, surplus, charge_room)

            battery_charge[t] = charge_kw
            battery_discharge[t] = discharge_kw

            # Power balance: grid_import + solar + discharge = load + export + charge
            net_grid = net_load + charge_kw - discharge_kw
            if net_grid > 0:
                grid_import[t] = net_grid
            else:
                # Only ever solar surplus reaches the grid — the battery is
                # never exported in this fallback.
                export_kw = -net_grid
                if max_grid_export_kw is not None:
                    export_kw = min(export_kw, max_grid_export_kw)
                grid_export[t] = export_kw

            soc += (charge_kw * eff - discharge_kw / eff) * dt / cap
            soc = max(self_consumption_floor, min(1.0, soc))

        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0, import_prices,
            [export_prices[t] + export_bonus_prices[t] for t in range(n)],
            block_battery_charge,
            schedule_timestamps,
            allow_grid_charge,
            grid_charge_allowed,
            disable_idle=disable_idle,
        )

        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        bonus_import = [0.0] * n
        import_bonus_remaining = max(0.0, float(import_bonus_cap_kwh or 0.0))
        if import_bonus_remaining > 0:
            for t in range(n):
                if import_bonus_prices[t] <= 0:
                    continue
                bonus_kw = min(grid_import[t], import_bonus_remaining / dt)
                bonus_import[t] = bonus_kw
                import_bonus_remaining -= bonus_kw * dt
                if import_bonus_remaining <= 1e-6:
                    break
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * dt
            - import_bonus_prices[t] * bonus_import[t] * dt
            - export_prices[t] * grid_export[t] * dt
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h,
            import_prices,
            export_prices,
            solar,
            load,
            export_bonus_prices=export_bonus_prices,
            export_bonus_cap_kwh=export_bonus_cap_kwh,
            import_bonus_prices=import_bonus_prices,
            import_bonus_cap_kwh=import_bonus_cap_kwh,
        )
        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(baseline_cost - predicted_cost, 2)

        return OptimizerResult(
            schedule=schedule,
            solver_used="self_consumption_hold",
            # Fallback solve: never let Auto-Apply ratchet the reserve off this.
            feasible=False,
            grid_import_w=[v * 1000 for v in grid_import],
            grid_export_w=[v * 1000 for v in grid_export],
            reserve_recommendation={},
        )

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
        block_battery_charge: list[bool] | None = None,
        allow_grid_charge: bool = True,
        grid_charge_allowed: list[bool] | None = None,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        schedule_timestamps: list[datetime] | None = None,
        priority_export_slots: list[bool] | None = None,
        disable_idle: bool = False,
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
        block_battery_charge = block_battery_charge or [False] * n
        grid_charge_allowed = grid_charge_allowed or [True] * n
        export_bonus_prices = export_bonus_prices or [0.0] * n
        import_bonus_prices = import_bonus_prices or [0.0] * n
        priority_export_slots = priority_export_slots or [False] * n
        effective_export_prices = [
            export_prices[t] + export_bonus_prices[t]
            for t in range(n)
        ]
        allow_grid_charge = bool(allow_grid_charge)
        effective_acquisition_costs = self._effective_export_acquisition_costs(
            n,
            import_prices,
            block_battery_charge,
            allow_grid_charge,
            acquisition_cost_kwh,
            grid_charge_allowed,
        )

        def _priority_export_slot(t: int) -> bool:
            export_value = effective_export_prices[t]
            return (
                priority_export_slots[t]
                and allow_battery_export[t]
                and export_value > 0.001
            )

        future_priority_recharge_cost = [float("inf")] * n
        best_future_recharge_cost = float("inf")
        import_bonus_active = bool(
            import_bonus_cap_kwh is not None
            and import_bonus_cap_kwh > 1e-6
            and any(price > 1e-6 for price in import_bonus_prices)
        )
        for idx in range(n - 1, -1, -1):
            future_priority_recharge_cost[idx] = best_future_recharge_cost
            if (
                allow_grid_charge
                and grid_charge_allowed[idx]
                and not block_battery_charge[idx]
            ):
                net_import_price = max(
                    0.0,
                    import_prices[idx]
                    - (import_bonus_prices[idx] if import_bonus_active else 0.0),
                )
                best_future_recharge_cost = min(
                    best_future_recharge_cost,
                    net_import_price / max(eff**2, 1e-9),
                )

        def _economic_export_slot(t: int) -> bool:
            return self._is_export_profitable(
                effective_export_prices[t],
                import_prices[t],
                acquisition_cost_kwh,
                effective_acquisition_costs[t],
            )

        max_grid_export_kw = (
            max(0.0, self.max_grid_export_w / 1000.0)
            if self.max_grid_export_w is not None
            else None
        )
        optimizer_reserve = self.backup_reserve
        below_optimizer_reserve = soc_0 < optimizer_reserve
        self_consumption_floor = self._natural_self_consumption_floor(soc_0)
        grid_charge_soc_cap = max(
            0.0,
            min(1.0, float(getattr(self, "grid_charge_soc_cap", 1.0) or 0.0)),
        )
        remaining_grid_charge_stored_kwh = (
            max(0.0, (grid_charge_soc_cap - soc_0) * cap)
            if allow_grid_charge and grid_charge_soc_cap < 0.999
            else float("inf")
        )

        grid_import = [0.0] * n
        grid_export = [0.0] * n
        battery_charge = [0.0] * n
        battery_discharge = [0.0] * n

        # Price-based greedy: sort export opportunities by spread, then charge
        # during the cheapest import slots that are not real export windows.
        spreads = []
        for t in range(n):
            net_load = load[t] - solar[t]
            spread = effective_export_prices[t] - import_prices[t]
            spreads.append((spread, t, net_load))

        # Sort: most profitable export first (highest spread)
        spreads.sort(key=lambda x: -x[0])

        # Two-pass: first assign exports (top spread), then imports (bottom spread)
        soc = soc_0
        actions = {}  # t -> (charge_kw, discharge_kw)

        # Pass 1: assign discharge/export to highest-spread periods.
        # Track the remaining capped bonus-export bucket (e.g. GloBird ZeroHero
        # Super Export). Intentional battery export beyond this bucket earns
        # only the base FiT (often ~0c) and would have to be re-bought at import
        # price — a loss. When the slot is profitable only because of the bonus,
        # cap intentional battery export to what still fits in the bucket, as
        # the LP does.
        bonus_export_remaining_kwh = max(0.0, float(export_bonus_cap_kwh or 0.0))
        soc_tracker = soc_0
        for spread, t, net_load in spreads:
            battery_export_allowed = allow_battery_export[t] and not below_optimizer_reserve
            export_profitable_slot = (
                battery_export_allowed
                and _economic_export_slot(t)
            )
            priority_export_slot = battery_export_allowed and _priority_export_slot(t)
            future_self_consumption_value = self._has_future_self_consumption_value(
                t, n, import_prices, solar, load
            )
            self_consumption_value_slot = (
                not battery_export_allowed
                and import_prices[t] > export_prices[t]
                and (
                    acquisition_cost_kwh <= 0
                    or import_prices[t] >= acquisition_cost_kwh
                )
            )
            if (
                export_profitable_slot
                and (priority_export_slot or not future_self_consumption_value)
                or self_consumption_value_slot
            ):
                forced_export_slot = export_profitable_slot and (
                    priority_export_slot or not future_self_consumption_value
                )
                # Profitable to discharge; cap to home load when battery export
                # is not explicitly permitted or export is below acquisition cost.
                discharge_limit = self.max_discharge_kw
                if forced_export_slot and self.max_battery_export_kw is not None:
                    discharge_limit = min(
                        discharge_limit,
                        max(0.0, net_load) + self.max_battery_export_kw,
                    )
                if max_grid_export_kw is not None:
                    discharge_limit = min(
                        discharge_limit,
                        max(0.0, net_load + max_grid_export_kw),
                    )
                if (
                    not battery_export_allowed
                    or (
                        not priority_export_slot
                        and acquisition_cost_kwh > 0
                        and effective_export_prices[t] < effective_acquisition_costs[t]
                    )
                ):
                    discharge_limit = min(discharge_limit, max(0.0, net_load))
                # Bonus-cap guard: when this slot is profitable to export only
                # because of the capped bonus (base FiT alone would not be), cap
                # intentional battery export (discharge above home load) to the
                # bonus bucket still remaining.
                bonus_only_profitable = (
                    forced_export_slot
                    and export_bonus_prices[t] > 0
                    and not self._is_export_profitable(
                        export_prices[t],
                        import_prices[t],
                        acquisition_cost_kwh,
                        effective_acquisition_costs[t],
                    )
                )
                if bonus_only_profitable:
                    intentional_export_room_kw = bonus_export_remaining_kwh / dt
                    discharge_limit = min(
                        discharge_limit,
                        max(0.0, net_load) + max(0.0, intentional_export_room_kw),
                    )
                discharge_floor = (
                    max(
                        optimizer_reserve,
                        self._configured_export_reserve_floor_for_range(t, t + 1),
                    )
                    if forced_export_slot
                    else self_consumption_floor
                )
                discharge_room = (soc_tracker - discharge_floor) * cap * eff / dt
                discharge_kw = min(discharge_limit, max(0, discharge_room))
                if discharge_kw > 0.01:
                    actions[t] = (0.0, discharge_kw)
                    soc_tracker -= discharge_kw * dt / (eff * cap)
                    if bonus_only_profitable:
                        intentional_export_kw = max(0.0, discharge_kw - max(0.0, net_load))
                        bonus_export_remaining_kwh = max(
                            0.0,
                            bonus_export_remaining_kwh - intentional_export_kw * dt,
                        )

        # Pass 2: buy only the energy that can avoid a strictly more expensive
        # future import. The previous greedy fallback filled every available
        # slot to the battery ceiling, which created mandatory and uneconomic
        # top-ups whenever HiGHS was unavailable.
        remaining_load_kwh = [
            max(
                0.0,
                max(0.0, load[idx] - solar[idx])
                - actions.get(idx, (0.0, 0.0))[1],
            )
            * dt
            for idx in range(n)
        ]
        base_discharge_kw = [
            actions.get(idx, (0.0, 0.0))[1]
            for idx in range(n)
        ]

        # Split future household demand into its rebated and ordinary marginal
        # values. A capped ZeroCharge-style rebate makes only the covered kWh
        # cheap; treating every future kWh at the raw retail tariff can make the
        # fallback buy energy now to avoid a later import that would cost zero.
        remaining_rebated_load_kwh = [0.0] * n
        remaining_standard_load_kwh = list(remaining_load_kwh)
        import_bonus_remaining_kwh = max(
            0.0,
            float(import_bonus_cap_kwh or 0.0),
        )
        for idx in range(n):
            if import_bonus_remaining_kwh <= 1e-9:
                break
            if import_bonus_prices[idx] <= 0:
                continue
            covered_kwh = min(
                remaining_standard_load_kwh[idx],
                import_bonus_remaining_kwh,
            )
            remaining_rebated_load_kwh[idx] = covered_kwh
            remaining_standard_load_kwh[idx] -= covered_kwh
            import_bonus_remaining_kwh -= covered_kwh

        initial_rebated_load_kwh = list(remaining_rebated_load_kwh)
        import_bonus_cap_total_kwh = max(
            0.0,
            float(import_bonus_cap_kwh or 0.0),
        )
        bonus_charge_consumed_kwh = 0.0

        # Describe still-available future battery-to-grid output. Pass 1 can
        # spend energy already present at the start of the horizon; these
        # buckets let pass 2 buy additional energy only when a later export
        # pays for the complete round trip. Bonus output is kept separate so a
        # capped provider credit cannot value more export than it settles.
        remaining_bonus_export_kwh = [0.0] * n
        remaining_base_export_kwh = [0.0] * n
        export_bonus_remaining_kwh = max(
            0.0,
            float(export_bonus_cap_kwh or 0.0),
        )
        for idx in range(n):
            solar_surplus_kw = max(0.0, solar[idx] - load[idx])
            if max_grid_export_kw is not None:
                solar_surplus_kw = min(solar_surplus_kw, max_grid_export_kw)

            existing_discharge_kw = actions.get(idx, (0.0, 0.0))[1]
            net_home_kw = max(0.0, load[idx] - solar[idx])
            existing_battery_export_kw = max(
                0.0,
                existing_discharge_kw - net_home_kw,
            )

            if export_bonus_prices[idx] > 0 and export_bonus_remaining_kwh > 0:
                settled_existing_kwh = min(
                    export_bonus_remaining_kwh,
                    (solar_surplus_kw + existing_battery_export_kw) * dt,
                )
                export_bonus_remaining_kwh -= settled_existing_kwh

            if not allow_battery_export[idx] or below_optimizer_reserve:
                continue

            battery_export_limit_kw = max(0.0, self.max_discharge_kw - net_home_kw)
            if self.max_battery_export_kw is not None:
                battery_export_limit_kw = min(
                    battery_export_limit_kw,
                    self.max_battery_export_kw,
                )
            if max_grid_export_kw is not None:
                battery_export_limit_kw = min(
                    battery_export_limit_kw,
                    max(0.0, max_grid_export_kw - solar_surplus_kw),
                )
            remaining_output_kwh = max(
                0.0,
                (battery_export_limit_kw - existing_battery_export_kw) * dt,
            )
            if remaining_output_kwh <= 1e-9:
                continue

            bonus_output_kwh = 0.0
            if export_bonus_prices[idx] > 0 and export_bonus_remaining_kwh > 0:
                bonus_output_kwh = min(
                    remaining_output_kwh,
                    export_bonus_remaining_kwh,
                )
                export_bonus_remaining_kwh -= bonus_output_kwh
            remaining_bonus_export_kwh[idx] = bonus_output_kwh
            remaining_base_export_kwh[idx] = (
                remaining_output_kwh - bonus_output_kwh
            )

        planned_load_output_kwh = [0.0] * n
        planned_export_output_kwh = [0.0] * n

        def _projected_soc_before(slot: int) -> float:
            """Project assigned actions chronologically up to ``slot``."""
            projected_soc = soc_0
            for idx in range(slot):
                charge_kw, discharge_kw = actions.get(idx, (0.0, 0.0))
                charge_kw = min(
                    max(0.0, charge_kw),
                    max(0.0, (1.0 - projected_soc) * cap / (eff * dt)),
                )
                net_home_kw = max(0.0, load[idx] - solar[idx])
                discharge_floor = self_consumption_floor
                if (
                    discharge_kw > net_home_kw + 0.001
                    and allow_battery_export[idx]
                    and not below_optimizer_reserve
                ):
                    discharge_floor = max(
                        optimizer_reserve,
                        self._configured_export_reserve_floor_for_range(
                            idx, idx + 1
                        ),
                    )
                discharge_kw = min(
                    max(0.0, discharge_kw),
                    max(
                        0.0,
                        (projected_soc - discharge_floor) * cap * eff / dt,
                    ),
                )
                projected_soc += (
                    charge_kw * eff - discharge_kw / eff
                ) * dt / cap
                projected_soc = max(
                    self_consumption_floor,
                    min(1.0, projected_soc),
                )
            return projected_soc

        for _, t, net_load in sorted(
            spreads,
            key=lambda item: (
                0
                if import_bonus_cap_total_kwh > 0
                and import_bonus_prices[item[1]] > 0
                else 1,
                item[1]
                if import_bonus_cap_total_kwh > 0
                and import_bonus_prices[item[1]] > 0
                else import_prices[item[1]],
                item[1],
            ),
        ):
            if t in actions:
                continue
            battery_export_allowed = allow_battery_export[t] and not below_optimizer_reserve
            export_profitable_slot = (
                battery_export_allowed
                and _economic_export_slot(t)
            )
            priority_export_slot = battery_export_allowed and _priority_export_slot(t)
            future_self_consumption_value = self._has_future_self_consumption_value(
                t, n, import_prices, solar, load
            )
            if block_battery_charge[t] or priority_export_slot or (
                export_profitable_slot and not future_self_consumption_value
            ):
                continue
            projected_soc = _projected_soc_before(t)
            charge_room = (1.0 - projected_soc) * cap / (eff * dt)
            charge_limit = self._charge_limit_kw(
                load[t], solar[t], allow_grid_charge and grid_charge_allowed[t]
            )
            charge_limit = min(charge_limit, max(0, charge_room))
            if charge_limit <= 0.01:
                continue

            total_charge_input_capacity_kwh = charge_limit * dt
            solar_charge_input_capacity_kwh = min(
                total_charge_input_capacity_kwh,
                max(0.0, solar[t] - load[t]) * dt,
            )
            grid_charge_input_capacity_kwh = max(
                0.0,
                total_charge_input_capacity_kwh
                - solar_charge_input_capacity_kwh,
            )
            if remaining_grid_charge_stored_kwh != float("inf"):
                grid_charge_input_capacity_kwh = min(
                    grid_charge_input_capacity_kwh,
                    remaining_grid_charge_stored_kwh / max(eff, 1e-9),
                )
            bonus_input_capacity_kwh = 0.0
            bonus_load_before_or_at_t = sum(initial_rebated_load_kwh[: t + 1])
            bonus_available_for_charge_kwh = max(
                0.0,
                import_bonus_cap_total_kwh
                - bonus_load_before_or_at_t
                - bonus_charge_consumed_kwh,
            )
            if (
                import_bonus_prices[t] > 0
                and bonus_available_for_charge_kwh > 1e-9
            ):
                bonus_input_capacity_kwh = min(
                    grid_charge_input_capacity_kwh,
                    bonus_available_for_charge_kwh,
                )
            charge_tiers = []
            if solar_charge_input_capacity_kwh > 1e-9:
                charge_tiers.append((
                    max(0.0, export_prices[t]),
                    solar_charge_input_capacity_kwh,
                    False,
                    False,
                ))
            if bonus_input_capacity_kwh > 1e-9:
                charge_tiers.append((
                    max(0.0, import_prices[t] - import_bonus_prices[t]),
                    bonus_input_capacity_kwh,
                    True,
                    True,
                ))
            base_input_capacity_kwh = (
                grid_charge_input_capacity_kwh - bonus_input_capacity_kwh
            )
            if base_input_capacity_kwh > 1e-9:
                charge_tiers.append((
                    import_prices[t],
                    base_input_capacity_kwh,
                    False,
                    True,
                ))
            charge_tiers.sort(key=lambda tier: tier[0])

            charged_input_kwh = 0.0
            for (
                marginal_import_price,
                tier_input_kwh,
                uses_bonus,
                uses_grid,
            ) in charge_tiers:
                delivered_capacity_kwh = tier_input_kwh * eff * eff
                delivered_kwh = 0.0
                # Allocate the charge to the highest-value future output,
                # whether that output avoids a household import or funds an
                # intentional battery export.
                candidates: list[tuple[float, int, str]] = []
                for future_idx in range(t + 1, n):
                    if remaining_rebated_load_kwh[future_idx] > 1e-9:
                        candidates.append((
                            max(
                                0.0,
                                import_prices[future_idx]
                                - import_bonus_prices[future_idx],
                            ),
                            future_idx,
                            "rebated_load",
                        ))
                    if remaining_standard_load_kwh[future_idx] > 1e-9:
                        candidates.append((
                            import_prices[future_idx],
                            future_idx,
                            "standard_load",
                        ))
                    if remaining_bonus_export_kwh[future_idx] > 1e-9:
                        candidates.append((
                            export_prices[future_idx]
                            + export_bonus_prices[future_idx],
                            future_idx,
                            "bonus_export",
                        ))
                    if remaining_base_export_kwh[future_idx] > 1e-9:
                        candidates.append((
                            export_prices[future_idx],
                            future_idx,
                            "base_export",
                        ))

                candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
                for future_value, future_idx, kind in candidates:
                    if delivered_kwh >= delivered_capacity_kwh - 1e-9:
                        break
                    if (
                        future_value * eff * eff
                        <= marginal_import_price + 0.001
                    ):
                        continue
                    if kind == "rebated_load":
                        remaining_kwh = remaining_rebated_load_kwh[future_idx]
                    elif kind == "standard_load":
                        remaining_kwh = remaining_standard_load_kwh[future_idx]
                    elif kind == "bonus_export":
                        remaining_kwh = remaining_bonus_export_kwh[future_idx]
                    else:
                        remaining_kwh = remaining_base_export_kwh[future_idx]
                    take = min(
                        remaining_kwh,
                        delivered_capacity_kwh - delivered_kwh,
                    )
                    if kind == "rebated_load":
                        remaining_rebated_load_kwh[future_idx] -= take
                    elif kind == "standard_load":
                        remaining_standard_load_kwh[future_idx] -= take
                    elif kind == "bonus_export":
                        remaining_bonus_export_kwh[future_idx] -= take
                    else:
                        remaining_base_export_kwh[future_idx] -= take

                    if kind in ("rebated_load", "standard_load"):
                        planned_load_output_kwh[future_idx] += take
                    else:
                        planned_export_output_kwh[future_idx] += take
                    total_discharge_kw = (
                        base_discharge_kw[future_idx]
                        + planned_load_output_kwh[future_idx] / dt
                        + planned_export_output_kwh[future_idx] / dt
                    )
                    if planned_export_output_kwh[future_idx] > 0:
                        total_discharge_kw = max(
                            total_discharge_kw,
                            max(0.0, load[future_idx] - solar[future_idx])
                            + planned_export_output_kwh[future_idx] / dt,
                        )
                    actions[future_idx] = (0.0, total_discharge_kw)
                    delivered_kwh += take

                used_input_kwh = delivered_kwh / max(1e-9, eff * eff)
                charged_input_kwh += used_input_kwh
                if uses_grid and remaining_grid_charge_stored_kwh != float("inf"):
                    remaining_grid_charge_stored_kwh = max(
                        0.0,
                        remaining_grid_charge_stored_kwh
                        - used_input_kwh * eff,
                    )
                if uses_bonus:
                    bonus_charge_consumed_kwh += used_input_kwh
                    # Canonical settlement applies the cap chronologically.
                    # Earlier battery import therefore displaces an equal
                    # amount of credit previously forecast for later home load.
                    displaced_kwh = used_input_kwh
                    for future_idx in range(t + 1, n):
                        if displaced_kwh <= 1e-9:
                            break
                        move_kwh = min(
                            remaining_rebated_load_kwh[future_idx],
                            displaced_kwh,
                        )
                        if move_kwh <= 0:
                            continue
                        remaining_rebated_load_kwh[future_idx] -= move_kwh
                        remaining_standard_load_kwh[future_idx] += move_kwh
                        displaced_kwh -= move_kwh

            charge_kw = charged_input_kwh / max(1e-9, dt)
            if charge_kw > 0.01:
                actions[t] = (charge_kw, 0.0)

        # Pair below-acquisition priority export with actual future recharge.
        # This is deliberately quantity-aware: a one-kWh cheap/rebated slot can
        # fund one round-trip kWh, not authorize the whole battery to be sold
        # below its modeled acquisition cost.
        paired_bonus_export_remaining_kwh = max(
            0.0,
            float(export_bonus_cap_kwh or 0.0),
        )
        if paired_bonus_export_remaining_kwh > 0:
            for idx in range(n):
                if export_bonus_prices[idx] <= 0:
                    continue
                net_home_kw = max(0.0, load[idx] - solar[idx])
                battery_export_kw = max(
                    0.0,
                    actions.get(idx, (0.0, 0.0))[1] - net_home_kw,
                )
                existing_grid_export_kw = (
                    max(0.0, solar[idx] - load[idx]) + battery_export_kw
                )
                if max_grid_export_kw is not None:
                    existing_grid_export_kw = min(
                        existing_grid_export_kw,
                        max_grid_export_kw,
                    )
                paired_bonus_export_remaining_kwh = max(
                    0.0,
                    paired_bonus_export_remaining_kwh
                    - existing_grid_export_kw * dt,
                )
        for t in range(n):
            export_value = effective_export_prices[t]
            if (
                not _priority_export_slot(t)
                or acquisition_cost_kwh <= 0
                or export_value + 1e-9 >= effective_acquisition_costs[t]
                or future_priority_recharge_cost[t] > export_value + 1e-9
            ):
                continue

            net_home_kw = max(0.0, load[t] - solar[t])
            existing_discharge_kw = actions.get(t, (0.0, 0.0))[1]
            if existing_discharge_kw > net_home_kw + 0.001:
                # Already backed by an earlier charge allocation.
                continue

            projected_soc = _projected_soc_before(t)
            export_floor = max(
                optimizer_reserve,
                self._configured_export_reserve_floor_for_range(t, t + 1),
            )
            available_output_kwh = max(
                0.0,
                (projected_soc - export_floor) * cap * eff
                - net_home_kw * dt,
            )
            battery_export_limit_kw = max(0.0, self.max_discharge_kw - net_home_kw)
            if self.max_battery_export_kw is not None:
                battery_export_limit_kw = min(
                    battery_export_limit_kw,
                    self.max_battery_export_kw,
                )
            if max_grid_export_kw is not None:
                solar_surplus_kw = max(0.0, solar[t] - load[t])
                battery_export_limit_kw = min(
                    battery_export_limit_kw,
                    max(0.0, max_grid_export_kw - solar_surplus_kw),
                )
            max_paired_output_kwh = min(
                available_output_kwh,
                battery_export_limit_kw * dt,
            )

            base_export_can_fund_recharge = (
                export_prices[t] + 1e-9 >= future_priority_recharge_cost[t]
            )
            bonus_only_pair = (
                export_bonus_prices[t] > 0
                and not base_export_can_fund_recharge
            )
            if bonus_only_pair:
                max_paired_output_kwh = min(
                    max_paired_output_kwh,
                    paired_bonus_export_remaining_kwh,
                )
            if max_paired_output_kwh <= 1e-9:
                continue

            remaining_output_kwh = max_paired_output_kwh
            for recharge_idx in range(t + 1, n):
                if remaining_output_kwh <= 1e-9:
                    break
                if (
                    not allow_grid_charge
                    or not grid_charge_allowed[recharge_idx]
                    or block_battery_charge[recharge_idx]
                ):
                    continue
                existing_charge_kw, future_discharge_kw = actions.get(
                    recharge_idx,
                    (0.0, 0.0),
                )
                if future_discharge_kw > 0:
                    continue
                charge_limit_kw = self._charge_limit_kw(
                    load[recharge_idx],
                    solar[recharge_idx],
                    True,
                )
                available_charge_input_kwh = max(
                    0.0,
                    (charge_limit_kw - existing_charge_kw) * dt,
                )
                if available_charge_input_kwh <= 1e-9:
                    continue

                solar_charge_input_kwh = min(
                    available_charge_input_kwh,
                    max(
                        0.0,
                        solar[recharge_idx]
                        - load[recharge_idx]
                        - existing_charge_kw,
                    )
                    * dt,
                )
                grid_charge_input_kwh = max(
                    0.0,
                    available_charge_input_kwh - solar_charge_input_kwh,
                )
                if remaining_grid_charge_stored_kwh != float("inf"):
                    grid_charge_input_kwh = min(
                        grid_charge_input_kwh,
                        remaining_grid_charge_stored_kwh / max(eff, 1e-9),
                    )

                bonus_load_before_or_at_slot = sum(
                    initial_rebated_load_kwh[: recharge_idx + 1]
                )
                bonus_available_kwh = max(
                    0.0,
                    import_bonus_cap_total_kwh
                    - bonus_load_before_or_at_slot
                    - bonus_charge_consumed_kwh,
                )
                bonus_capacity_kwh = 0.0
                if import_bonus_prices[recharge_idx] > 0:
                    bonus_capacity_kwh = min(
                        grid_charge_input_kwh,
                        bonus_available_kwh,
                    )
                recharge_tiers = []
                if solar_charge_input_kwh > 1e-9:
                    recharge_tiers.append((
                        max(0.0, export_prices[recharge_idx]),
                        solar_charge_input_kwh,
                        False,
                        False,
                    ))
                if bonus_capacity_kwh > 1e-9:
                    recharge_tiers.append((
                        max(
                            0.0,
                            import_prices[recharge_idx]
                            - import_bonus_prices[recharge_idx],
                        ),
                        bonus_capacity_kwh,
                        True,
                        True,
                    ))
                raw_capacity_kwh = grid_charge_input_kwh - bonus_capacity_kwh
                if raw_capacity_kwh > 1e-9:
                    recharge_tiers.append((
                        import_prices[recharge_idx],
                        raw_capacity_kwh,
                        False,
                        True,
                    ))
                recharge_tiers.sort(key=lambda tier: tier[0])

                added_input_kwh = 0.0
                for (
                    marginal_price,
                    tier_input_kwh,
                    uses_bonus,
                    uses_grid,
                ) in recharge_tiers:
                    if marginal_price >= export_value * eff * eff - 0.001:
                        continue
                    take_input_kwh = min(
                        tier_input_kwh,
                        remaining_output_kwh / max(eff * eff, 1e-9),
                    )
                    if take_input_kwh <= 1e-9:
                        continue
                    added_input_kwh += take_input_kwh
                    remaining_output_kwh -= take_input_kwh * eff * eff
                    if (
                        uses_grid
                        and remaining_grid_charge_stored_kwh != float("inf")
                    ):
                        remaining_grid_charge_stored_kwh = max(
                            0.0,
                            remaining_grid_charge_stored_kwh
                            - take_input_kwh * eff,
                        )
                    if uses_bonus:
                        bonus_charge_consumed_kwh += take_input_kwh
                        displaced_kwh = take_input_kwh
                        for future_idx in range(recharge_idx + 1, n):
                            if displaced_kwh <= 1e-9:
                                break
                            move_kwh = min(
                                remaining_rebated_load_kwh[future_idx],
                                displaced_kwh,
                            )
                            remaining_rebated_load_kwh[future_idx] -= move_kwh
                            remaining_standard_load_kwh[future_idx] += move_kwh
                            displaced_kwh -= move_kwh
                if added_input_kwh > 1e-9:
                    actions[recharge_idx] = (
                        existing_charge_kw + added_input_kwh / dt,
                        0.0,
                    )

            paired_output_kwh = max_paired_output_kwh - remaining_output_kwh
            if paired_output_kwh <= 0.001:
                continue
            actions[t] = (
                0.0,
                net_home_kw + paired_output_kwh / dt,
            )
            if bonus_only_pair:
                paired_bonus_export_remaining_kwh = max(
                    0.0,
                    paired_bonus_export_remaining_kwh - paired_output_kwh,
                )

        # Now compute grid flows in time order. The two price-priority passes
        # above track SOC in assignment order, not chronological order, so an
        # assigned action can exceed the room actually available at its real
        # time (e.g. a charge scheduled before a full battery drains, or a
        # discharge of energy not yet charged). Clamp each action to the SOC
        # physically available now so the emitted schedule — and the grid flows
        # / predicted cost derived from it — cannot overcharge a full battery
        # or discharge energy that is not there.
        soc = soc_0
        for t in range(n):
            net_load = load[t] - solar[t]
            charge_kw, discharge_kw = actions.get(t, (0.0, 0.0))

            max_charge_room_kw = max(0.0, (1.0 - soc) * cap / (eff * dt))
            charge_kw = min(charge_kw, max_charge_room_kw)
            discharge_floor = self_consumption_floor
            if discharge_kw > 0:
                battery_export_allowed = (
                    allow_battery_export[t] and not below_optimizer_reserve
                )
                export_profitable_slot = (
                    battery_export_allowed
                    and _economic_export_slot(t)
                )
                priority_export_slot = battery_export_allowed and _priority_export_slot(t)
                future_self_consumption_value = self._has_future_self_consumption_value(
                    t, n, import_prices, solar, load
                )
                if priority_export_slot or (
                    export_profitable_slot and not future_self_consumption_value
                ):
                    discharge_floor = max(
                        optimizer_reserve,
                        self._configured_export_reserve_floor_for_range(t, t + 1),
                    )
            max_discharge_room_kw = max(
                0.0, (soc - discharge_floor) * cap * eff / dt
            )
            discharge_kw = min(discharge_kw, max_discharge_room_kw)

            battery_charge[t] = charge_kw
            battery_discharge[t] = discharge_kw

            # Power balance: grid_import + solar + discharge = load + grid_export + charge
            net_grid = net_load + charge_kw - discharge_kw
            if net_grid > 0:
                grid_import[t] = net_grid
            else:
                export_kw = -net_grid
                if self.max_grid_export_w is not None:
                    export_kw = min(export_kw, max(0.0, self.max_grid_export_w / 1000.0))
                if self.max_battery_export_kw is not None:
                    solar_surplus_kw = max(0.0, solar[t] - load[t])
                    export_kw = min(export_kw, solar_surplus_kw + self.max_battery_export_kw)
                grid_export[t] = export_kw

            soc += (charge_kw * eff - discharge_kw / eff) * dt / cap
            soc = max(self_consumption_floor, min(1.0, soc))

        # Build schedule
        schedule = self._build_schedule(
            n, grid_import, grid_export, battery_charge, battery_discharge,
            solar, load, soc_0, import_prices, effective_export_prices,
            block_battery_charge,
            schedule_timestamps,
            allow_grid_charge,
            grid_charge_allowed,
            priority_export_slots,
            disable_idle,
        )

        grid_import, grid_export = self._grid_flows_from_schedule(
            schedule,
            n,
            solar,
            load,
        )

        # Calculate costs for first 24 hours only (display as daily cost)
        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        bonus_import = [0.0] * n
        import_bonus_remaining = max(0.0, float(import_bonus_cap_kwh or 0.0))
        if import_bonus_remaining > 0:
            for t in range(n):
                if import_bonus_prices[t] <= 0:
                    continue
                bonus_kw = min(grid_import[t], import_bonus_remaining / dt)
                bonus_import[t] = bonus_kw
                import_bonus_remaining -= bonus_kw * dt
                if import_bonus_remaining <= 1e-6:
                    break
        bonus_export = [0.0] * n
        bonus_remaining = max(0.0, float(export_bonus_cap_kwh or 0.0))
        if bonus_remaining > 0:
            for t in range(n):
                if export_bonus_prices[t] <= 0:
                    continue
                bonus_kw = min(grid_export[t], bonus_remaining / dt)
                bonus_export[t] = bonus_kw
                bonus_remaining -= bonus_kw * dt
                if bonus_remaining <= 1e-6:
                    break
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * dt
            - import_bonus_prices[t] * bonus_import[t] * dt
            - export_prices[t] * grid_export[t] * dt
            - export_bonus_prices[t] * bonus_export[t] * dt
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h,
            import_prices,
            export_prices,
            solar,
            load,
            export_bonus_prices=export_bonus_prices,
            export_bonus_cap_kwh=export_bonus_cap_kwh,
            import_bonus_prices=import_bonus_prices,
            import_bonus_cap_kwh=import_bonus_cap_kwh,
        )

        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(baseline_cost - predicted_cost, 2)
        reserve_recommendation = self._build_reserve_recommendation(
            schedule,
            solar,
            load,
        )

        return OptimizerResult(
            schedule=schedule,
            solver_used="greedy",
            feasible=True,
            grid_import_w=[v * 1000 for v in grid_import],
            grid_export_w=[v * 1000 for v in grid_export],
            reserve_recommendation=reserve_recommendation,
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
        block_battery_charge: list[bool] | None = None,
        schedule_timestamps: list[datetime] | None = None,
        allow_grid_charge: bool = True,
        grid_charge_allowed: list[bool] | None = None,
        priority_export_slots: list[bool] | None = None,
        disable_idle: bool = False,
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
        # Prefer caller-supplied forecast timestamps so displayed actions stay
        # aligned with the price slots that produced them, even if solving
        # crosses a 5-minute boundary in the executor thread.
        if schedule_timestamps:
            now = schedule_timestamps[0]
        else:
            raw_now = dt_util.now()
            now = raw_now.replace(
                minute=(raw_now.minute // self.interval_minutes) * self.interval_minutes,
                second=0, microsecond=0,
            )
        threshold_kw = ACTION_THRESHOLD_W / 1000.0

        block_battery_charge = block_battery_charge or [False] * n
        grid_charge_allowed = grid_charge_allowed or [True] * n
        priority_export_slots = priority_export_slots or [False] * n
        actions = []
        soc = soc_0
        optimizer_reserve = max(0.0, min(1.0, self.backup_reserve))
        self_consumption_floor = self._natural_self_consumption_floor(soc_0)
        grid_charge_soc_cap = max(
            0.0,
            min(1.0, float(getattr(self, "grid_charge_soc_cap", 1.0) or 0.0)),
        )
        remaining_grid_charge_stored_kwh = (
            max(0.0, (grid_charge_soc_cap - soc_0) * cap)
            if allow_grid_charge and grid_charge_soc_cap < 0.999
            else float("inf")
        )

        def _future_grid_charge_planned(start_idx: int) -> bool:
            for future_idx in range(start_idx + 1, n):
                if future_idx >= len(battery_charge) or future_idx >= len(grid_import):
                    break
                future_charge_kw = battery_charge[future_idx]
                future_import_kw = grid_import[future_idx]
                if future_charge_kw <= threshold_kw:
                    continue
                if (
                    import_prices is not None
                    and future_idx < len(import_prices)
                    and import_prices[future_idx] <= 0.001
                ):
                    return True
                net_load_kw = max(0.0, load[future_idx] - solar[future_idx])
                if future_import_kw > net_load_kw + threshold_kw:
                    return True
            return False

        def _charge_by_time_hold_required(start_idx: int, start_soc: float) -> bool:
            """Return whether natural use now would make the deadline unreachable."""
            if (
                self.pre_window_slot is None
                or start_idx >= self.pre_window_slot
                or self.pre_window_soc_target <= 0.0
                or cap <= 0
                or dt <= 0
            ):
                return False

            deadline = min(n, self.pre_window_slot)
            projected_soc = start_soc
            for future_idx in range(start_idx, deadline):
                if future_idx == start_idx:
                    net_home_kw = load[future_idx] - solar[future_idx]
                    if net_home_kw > threshold_kw:
                        available_kw = (
                            max(0.0, projected_soc - self_consumption_floor)
                            * cap
                            * eff
                            / dt
                        )
                        charge_future_kw = 0.0
                        discharge_future_kw = min(
                            self.max_discharge_kw,
                            net_home_kw,
                            available_kw,
                        )
                    elif net_home_kw < -threshold_kw:
                        available_kw = (
                            max(0.0, 1.0 - projected_soc) * cap / (eff * dt)
                        )
                        charge_future_kw = min(
                            self.max_charge_kw,
                            -net_home_kw,
                            available_kw,
                        )
                        discharge_future_kw = 0.0
                    else:
                        charge_future_kw = 0.0
                        discharge_future_kw = 0.0
                else:
                    charge_future_kw = max(0.0, battery_charge[future_idx])
                    discharge_future_kw = max(0.0, battery_discharge[future_idx])
                projected_soc += (
                    charge_future_kw * eff - discharge_future_kw / eff
                ) * dt / cap
                projected_soc = max(
                    self_consumption_floor,
                    min(1.0, projected_soc),
                )

            return projected_soc < self.pre_window_soc_target - 0.0001

        for t in range(n):
            ts = (
                schedule_timestamps[t]
                if schedule_timestamps and t < len(schedule_timestamps)
                else now + timedelta(minutes=t * self.interval_minutes)
            )
            configured_export_floor = self._configured_export_reserve_floor_for_range(
                t, t + 1
            )
            export_floor = max(optimizer_reserve, configured_export_floor)
            natural_floor = self_consumption_floor

            charge_kw = battery_charge[t]
            discharge_kw = battery_discharge[t]
            import_kw = grid_import[t]
            export_kw = grid_export[t]
            charge_blocked = block_battery_charge[t]
            priority_export_slot = (
                t < len(priority_export_slots)
                and priority_export_slots[t]
                and export_prices is not None
                and t < len(export_prices)
                and export_prices[t] > 0.001
            )
            free_import_slot = (
                import_prices is not None
                and import_prices[t] <= 0.001
                and not charge_blocked
                and allow_grid_charge
                and grid_charge_allowed[t]
            )

            # Determine action
            if free_import_slot:
                # Free electricity — always request force charge for the full
                # feasible slot so the action plan does not oscillate with the LP.
                action = "charge"
                full_slot_w = (
                    self._charge_limit_kw(load[t], solar[t], True) * 1000
                    if self.max_grid_import_w is not None
                    else self.max_charge_w
                )
                power_w = max(charge_kw * 1000, full_slot_w)
            elif charge_kw > threshold_kw and import_kw > (
                max(0.0, load[t] - solar[t]) + threshold_kw
            ):
                # Grid draw exceeds the net home load (load minus solar), so
                # the surplus grid power is charging the battery. Comparing
                # against net load — not total load — is essential: with
                # concurrent solar, charge power can be below total solar yet
                # still grid-sourced (load > solar), and that must still count
                # as grid charging rather than self-consumption.
                action = "charge"
                power_w = charge_kw * 1000
            elif export_kw > threshold_kw and discharge_kw > threshold_kw:
                # Battery discharging AND power going to grid → exporting
                action = "export"
                power_w = export_kw * 1000
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
                preserve_charge_by_time_hold = (
                    _charge_by_time_hold_required(t, soc)
                )
                preserve_recovery_hold = (
                    not disable_idle
                    and soc <= optimizer_reserve
                    and soc
                    <= self_consumption_floor
                    + BELOW_RESERVE_RECOVERY_HOLD_MARGIN_SOC
                    and _future_grid_charge_planned(t)
                )
                if preserve_charge_by_time_hold or preserve_recovery_hold:
                    action = "idle"
                elif disable_idle:
                    action = "self_consumption"
                elif meaningful_hold and export_prices is not None and import_prices is not None:
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
                    # At or below the optimizer reserve, stay in
                    # self_consumption. IDLE is a separate hold strategy for
                    # preserving useful SOC above that floor for a future
                    # export/avoidance window.
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

            reported_charge_w = charge_kw * 1000
            reported_discharge_w = discharge_kw * 1000
            if free_import_slot and action == "charge":
                reported_charge_w = power_w
                reported_discharge_w = 0.0
            elif action in ("discharge", "export"):
                export_room_kw = (
                    max(0.0, soc - export_floor) * cap * eff / dt
                    if cap > 0 and dt > 0
                    else 0.0
                )
                if export_room_kw <= threshold_kw:
                    net_home_kw = max(0.0, load[t] - solar[t])
                    natural_room_kw = (
                        max(0.0, soc - natural_floor) * cap * eff / dt
                        if cap > 0 and dt > 0
                        else 0.0
                    )
                    natural_discharge_kw = min(
                        self.max_discharge_kw,
                        net_home_kw,
                        max(0.0, natural_room_kw),
                    )
                    action = "self_consumption"
                    power_w = natural_discharge_kw * 1000
                    reported_charge_w = 0.0
                    reported_discharge_w = natural_discharge_kw * 1000
                elif discharge_kw > export_room_kw:
                    capped_discharge_w = export_room_kw * 1000
                    reported_charge_w = 0.0
                    reported_discharge_w = capped_discharge_w
                    power_w = min(power_w, capped_discharge_w)
            elif action == "self_consumption" and discharge_kw >= threshold_kw:
                net_home_kw = max(0.0, load[t] - solar[t])
                natural_room_kw = (
                    max(0.0, soc - natural_floor) * cap * eff / dt
                    if cap > 0 and dt > 0
                    else 0.0
                )
                natural_discharge_kw = min(
                    self.max_discharge_kw,
                    net_home_kw,
                    discharge_kw,
                    max(0.0, natural_room_kw),
                )
                reported_charge_w = 0.0
                reported_discharge_w = natural_discharge_kw * 1000
                power_w = natural_discharge_kw * 1000
            elif (
                action == "self_consumption"
                and charge_kw < threshold_kw
                and discharge_kw < threshold_kw
            ):
                net_home_kw = load[t] - solar[t]
                if net_home_kw > threshold_kw:
                    available_kw = (
                        max(0.0, soc - natural_floor) * cap * eff / dt
                    )
                    natural_discharge_kw = min(
                        self.max_discharge_kw,
                        net_home_kw,
                        max(0.0, available_kw),
                    )
                    reported_discharge_w = natural_discharge_kw * 1000
                    reported_charge_w = 0.0
                    power_w = natural_discharge_kw * 1000
                elif net_home_kw < -threshold_kw and not charge_blocked:
                    available_kw = (1.0 - soc) * cap / (eff * dt)
                    natural_charge_kw = min(
                        self.max_charge_kw,
                        -net_home_kw,
                        max(0.0, available_kw),
                    )
                    reported_charge_w = natural_charge_kw * 1000
                    reported_discharge_w = 0.0
                    power_w = natural_charge_kw * 1000

            if action == "charge" and reported_charge_w > 0:
                solar_surplus_w = max(0.0, solar[t] - load[t]) * 1000.0
                solar_charge_w = min(reported_charge_w, solar_surplus_w)
                grid_charge_w = max(0.0, reported_charge_w - solar_charge_w)
                if remaining_grid_charge_stored_kwh != float("inf"):
                    allowed_grid_charge_w = (
                        remaining_grid_charge_stored_kwh
                        * 1000.0
                        / max(eff * dt, 1e-9)
                    )
                    grid_charge_w = min(grid_charge_w, allowed_grid_charge_w)
                reported_charge_w = solar_charge_w + grid_charge_w
                if remaining_grid_charge_stored_kwh != float("inf"):
                    remaining_grid_charge_stored_kwh = max(
                        0.0,
                        remaining_grid_charge_stored_kwh
                        - grid_charge_w / 1000.0 * eff * dt,
                    )
                power_w = min(power_w, reported_charge_w)
                if grid_charge_w <= ACTION_THRESHOLD_W:
                    action = "self_consumption"
                    power_w = reported_charge_w

            effective_charge_kw = reported_charge_w / 1000
            effective_discharge_kw = reported_discharge_w / 1000
            soc += (effective_charge_kw * eff - effective_discharge_kw / eff) * dt / cap
            # Floor the *reported* SOC at the real reserve only. The export floor
            # already gates discharge and export through the room calculations
            # above; using it here as a lower clamp would inflate a genuinely-low
            # SOC up to the export floor — e.g. plotting the battery at the 45%
            # export floor while it is really at 23%, and reporting that inflated
            # value as minimum_forecast_soc.
            soc = max(self_consumption_floor, min(1.0, soc))

            actions.append(ScheduleAction(
                timestamp=ts,
                action=action,
                power_w=round(power_w, 1),
                soc=round(soc, 4),
                battery_charge_w=round(reported_charge_w, 1),
                battery_discharge_w=round(reported_discharge_w, 1),
            ))

        return OptimizationSchedule(
            actions=actions,
            predicted_cost=0.0,
            predicted_savings=0.0,
            last_updated=now,
        )

    def _grid_flows_from_schedule(
        self,
        schedule: OptimizationSchedule,
        n: int,
        solar: list[float],
        load: list[float],
    ) -> tuple[list[float], list[float]]:
        """Recompute per-slot grid import/export (kW) from the emitted schedule.

        Applies the power balance grid_import - grid_export = load - solar +
        charge - discharge to the schedule's reported battery charge/discharge,
        so the reported flows describe the displayed actions rather than the raw
        LP solution (which models "hold" slots as grid import while the schedule
        serves that load from the battery).
        """
        grid_import = [0.0] * n
        grid_export = [0.0] * n
        max_grid_export_kw = (
            max(0.0, self.max_grid_export_w / 1000.0)
            if self.max_grid_export_w is not None
            else None
        )
        actions = schedule.actions or []
        for t in range(n):
            if t >= len(actions):
                break
            action = actions[t]
            if action.action == "off_grid":
                # OFF_GRID represents an islanded/curtailed site. Any forecast
                # imbalance is absorbed by local control or curtailment, not by
                # a billable grid flow.
                continue
            charge_kw = action.battery_charge_w / 1000.0
            discharge_kw = action.battery_discharge_w / 1000.0
            solar_kw = solar[t] if t < len(solar) else 0.0
            load_kw = load[t] if t < len(load) else 0.0
            net_grid = (load_kw - solar_kw) + charge_kw - discharge_kw
            if net_grid > 0:
                grid_import[t] = net_grid
            else:
                export_kw = -net_grid
                if max_grid_export_kw is not None:
                    export_kw = min(export_kw, max_grid_export_kw)
                grid_export[t] = export_kw
        return grid_import, grid_export

    def reconcile_result_with_schedule(
        self,
        result: OptimizerResult,
        schedule: OptimizationSchedule,
        *,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
        initial_soc: float | None = None,
        optimizer_reserve: float | None = None,
    ) -> OptimizerResult:
        """Make result flows and economics describe the final emitted schedule."""
        actions = list(schedule.actions or [])
        if initial_soc is None and actions and actions[0].soc is not None:
            first = actions[0]
            first_delta = (
                max(0.0, first.battery_charge_w) / 1000.0 * self.efficiency
                - max(0.0, first.battery_discharge_w)
                / 1000.0
                / self.efficiency
            ) * self.dt_hours / self.capacity_kwh
            initial_soc = float(first.soc) - first_delta

        if initial_soc is not None and self.capacity_kwh > 0:
            soc_cursor = max(0.0, min(1.0, float(initial_soc)))
            modeled_backup_reserve = optimizer_reserve
            if modeled_backup_reserve is None:
                modeled_backup_reserve = (
                    result.modeled_backup_reserve
                    if result.modeled_backup_reserve is not None
                    else self.backup_reserve
                )
            modeled_backup_reserve = max(
                0.0,
                min(1.0, float(modeled_backup_reserve)),
            )
            if getattr(self, "hardware_reserve_known", False):
                physical_floor = min(
                    soc_cursor,
                    max(0.0, min(1.0, self.hardware_reserve)),
                )
            else:
                physical_floor = min(soc_cursor, modeled_backup_reserve)
            grid_charge_soc_cap = max(
                0.0,
                min(
                    1.0,
                    float(getattr(self, "grid_charge_soc_cap", 1.0) or 0.0),
                ),
            )
            remaining_grid_charge_stored_wh = (
                max(0.0, grid_charge_soc_cap - soc_cursor)
                * self.capacity_kwh
                * 1000.0
                if grid_charge_soc_cap < 0.999
                else float("inf")
            )

            def _modeled_export_floor(idx: int) -> float:
                floor = result.modeled_export_reserve_floor
                if floor is None:
                    floor = self._configured_export_reserve_floor_for_range(
                        idx, idx + 1
                    )
                slot_floors = result.modeled_export_reserve_floor_slots
                if slot_floors is not None and idx < len(slot_floors):
                    floor = max(float(floor or 0.0), float(slot_floors[idx] or 0.0))
                return max(0.0, min(1.0, float(floor or 0.0)))

            restamped: list[ScheduleAction] = []
            for idx, action in enumerate(actions):
                emitted_action = action.action
                effective_grid_charge_w = 0.0
                solar_w = (
                    max(0.0, float(solar[idx] or 0.0)) * 1000.0
                    if idx < len(solar)
                    else 0.0
                )
                load_w = (
                    max(0.0, float(load[idx] or 0.0)) * 1000.0
                    if idx < len(load)
                    else 0.0
                )
                net_home_w = max(0.0, load_w - solar_w)
                solar_surplus_w = max(0.0, solar_w - load_w)
                charge_w = max(0.0, float(action.battery_charge_w or 0.0))
                discharge_w = max(0.0, float(action.battery_discharge_w or 0.0))
                if action.action == "idle":
                    charge_w = 0.0
                    discharge_w = 0.0
                elif action.action == "charge":
                    discharge_w = 0.0
                elif action.action in ("export", "discharge"):
                    charge_w = 0.0
                elif action.action in ("self_consumption", "consume"):
                    # Natural operation can serve local imbalance only; it must
                    # not hide an unlabelled battery export or grid top-up.
                    charge_w = min(charge_w, solar_surplus_w)
                    discharge_w = min(discharge_w, net_home_w)
                elif action.action == "off_grid":
                    # Islanded operation must locally serve load or absorb solar
                    # before curtailing any remainder.
                    charge_w = solar_surplus_w
                    discharge_w = net_home_w

                charge_room_w = (
                    max(0.0, 1.0 - soc_cursor)
                    * self.capacity_kwh
                    * 1000.0
                    / max(self.efficiency * self.dt_hours, 1e-9)
                )
                charge_w = min(charge_w, self.max_charge_w, charge_room_w)
                if action.action == "charge" and charge_w > 0:
                    solar_charge_w = min(charge_w, solar_surplus_w)
                    grid_charge_w = max(0.0, charge_w - solar_charge_w)
                    if remaining_grid_charge_stored_wh != float("inf"):
                        allowed_grid_charge_w = (
                            remaining_grid_charge_stored_wh
                            / max(self.efficiency * self.dt_hours, 1e-9)
                        )
                        grid_charge_w = min(grid_charge_w, allowed_grid_charge_w)
                    charge_w = solar_charge_w + grid_charge_w
                    if remaining_grid_charge_stored_wh != float("inf"):
                        remaining_grid_charge_stored_wh = max(
                            0.0,
                            remaining_grid_charge_stored_wh
                            - grid_charge_w * self.efficiency * self.dt_hours,
                        )
                    effective_grid_charge_w = grid_charge_w

                discharge_floor = physical_floor
                if action.action in ("export", "discharge"):
                    discharge_floor = max(
                        discharge_floor,
                        modeled_backup_reserve,
                        _modeled_export_floor(idx),
                    )
                discharge_room_w = (
                    max(0.0, soc_cursor - discharge_floor)
                    * self.capacity_kwh
                    * 1000.0
                    * self.efficiency
                    / max(self.dt_hours, 1e-9)
                )
                discharge_w = min(
                    discharge_w,
                    self.max_discharge_w,
                    discharge_room_w,
                )

                if action.action in ("export", "discharge"):
                    # Preserve local load first, then cap the intentional battery
                    # export to the remaining site and battery export headroom.
                    home_discharge_w = min(discharge_w, net_home_w)
                    battery_export_w = max(0.0, discharge_w - home_discharge_w)
                    if self.max_battery_export_w is not None:
                        battery_export_w = min(
                            battery_export_w,
                            self.max_battery_export_w,
                        )
                    if self.max_grid_export_w is not None:
                        battery_export_w = min(
                            battery_export_w,
                            max(0.0, self.max_grid_export_w - solar_surplus_w),
                        )
                    discharge_w = home_discharge_w + battery_export_w

                soc_cursor += (
                    charge_w / 1000.0 * self.efficiency
                    - discharge_w / 1000.0 / self.efficiency
                ) * self.dt_hours / self.capacity_kwh
                soc_cursor = max(physical_floor, min(1.0, soc_cursor))

                power_w = float(action.power_w or 0.0)
                if action.action == "charge":
                    if effective_grid_charge_w <= ACTION_THRESHOLD_W:
                        emitted_action = "self_consumption"
                        power_w = charge_w
                    else:
                        power_w = min(power_w, charge_w)
                elif action.action in ("export", "discharge"):
                    home_discharge_w = min(
                        discharge_w,
                        net_home_w,
                    )
                    battery_export_w = max(0.0, discharge_w - home_discharge_w)
                    if battery_export_w <= ACTION_THRESHOLD_W:
                        emitted_action = "self_consumption"
                        power_w = discharge_w
                    else:
                        power_w = battery_export_w
                elif action.action in ("self_consumption", "consume", "off_grid"):
                    power_w = discharge_w if discharge_w > 0 else charge_w
                else:
                    power_w = 0.0
                restamped.append(
                    ScheduleAction(
                        timestamp=action.timestamp,
                        action=emitted_action,
                        power_w=round(power_w, 1),
                        soc=round(soc_cursor, 4),
                        battery_charge_w=round(charge_w, 1),
                        battery_discharge_w=round(discharge_w, 1),
                    )
                )
            schedule = OptimizationSchedule(
                actions=restamped,
                predicted_cost=schedule.predicted_cost,
                predicted_savings=schedule.predicted_savings,
                last_updated=schedule.last_updated,
            )

        n = min(
            len(schedule.actions or []),
            len(import_prices),
            len(export_prices),
            len(solar),
            len(load),
        )
        export_bonus_prices = self._pad_array(export_bonus_prices, n, 0.0)
        import_bonus_prices = self._pad_array(import_bonus_prices, n, 0.0)
        grid_import, grid_export = self._grid_flows_from_schedule(
            schedule,
            n,
            solar,
            load,
        )
        bonus_export = self._allocate_capped_bonus(
            grid_export,
            export_bonus_prices,
            export_bonus_cap_kwh,
        )
        bonus_import = self._allocate_capped_bonus(
            grid_import,
            import_bonus_prices,
            import_bonus_cap_kwh,
        )
        n_24h = min(n, int(24 * 60 / self.interval_minutes))
        predicted_cost = sum(
            import_prices[t] * grid_import[t] * self.dt_hours
            - import_bonus_prices[t] * bonus_import[t] * self.dt_hours
            - export_prices[t] * grid_export[t] * self.dt_hours
            - export_bonus_prices[t] * bonus_export[t] * self.dt_hours
            for t in range(n_24h)
        )
        baseline_cost = self._calculate_baseline_cost(
            n_24h,
            import_prices,
            export_prices,
            solar,
            load,
            export_bonus_prices=export_bonus_prices,
            export_bonus_cap_kwh=export_bonus_cap_kwh,
            import_bonus_prices=import_bonus_prices,
            import_bonus_cap_kwh=import_bonus_cap_kwh,
        )
        schedule.predicted_cost = round(predicted_cost, 2)
        schedule.predicted_savings = round(baseline_cost - predicted_cost, 2)
        result.schedule = schedule
        result.grid_import_w = [value * 1000 for value in grid_import]
        result.grid_export_w = [value * 1000 for value in grid_export]
        if result.feasible:
            result.reserve_recommendation = self._build_reserve_recommendation(
                schedule,
                solar,
                load,
            )
        return result

    def _allocate_capped_bonus(
        self,
        flows: list[float],
        bonus_prices: list[float],
        cap_kwh: float | None,
    ) -> list[float]:
        """Assign the first ``cap_kwh`` of bonus-priced flow to the bonus bucket."""
        bonus = [0.0] * len(flows)
        remaining = max(0.0, float(cap_kwh or 0.0))
        if remaining <= 0:
            return bonus
        dt = self.dt_hours
        for t in range(len(flows)):
            if t >= len(bonus_prices) or bonus_prices[t] <= 0:
                continue
            take_kw = min(flows[t], remaining / dt)
            if take_kw <= 0:
                continue
            bonus[t] = take_kw
            remaining -= take_kw * dt
            if remaining <= 1e-6:
                break
        return bonus

    def _calculate_baseline_cost(
        self,
        n: int,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        *,
        export_bonus_prices: list[float] | None = None,
        export_bonus_cap_kwh: float | None = None,
        import_bonus_prices: list[float] | None = None,
        import_bonus_cap_kwh: float | None = None,
    ) -> float:
        """
        Calculate baseline cost without battery.

        All load from grid, all excess solar exported.
        """
        dt = self.dt_hours
        cost = 0.0
        bonus_prices = export_bonus_prices or [0.0] * n
        bonus_remaining = max(0.0, float(export_bonus_cap_kwh or 0.0))
        import_bonus = import_bonus_prices or [0.0] * n
        import_bonus_remaining = max(0.0, float(import_bonus_cap_kwh or 0.0))

        for t in range(n):
            net = load[t] - solar[t]
            if net > 0:
                cost += import_prices[t] * net * dt
                if (
                    import_bonus_remaining > 0
                    and t < len(import_bonus)
                    and import_bonus[t] > 0
                ):
                    bonus_kw = min(net, import_bonus_remaining / dt)
                    cost -= import_bonus[t] * bonus_kw * dt
                    import_bonus_remaining -= bonus_kw * dt
            else:
                export_kw = -net
                cost -= export_prices[t] * export_kw * dt
                if bonus_remaining > 0 and t < len(bonus_prices) and bonus_prices[t] > 0:
                    bonus_kw = min(export_kw, bonus_remaining / dt)
                    cost -= bonus_prices[t] * bonus_kw * dt
                    bonus_remaining -= bonus_kw * dt

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
