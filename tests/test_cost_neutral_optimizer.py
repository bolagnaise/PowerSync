"""Regression tests for the Cost Neutral battery-export earnings cap."""

from __future__ import annotations

import importlib
import inspect
import json
import math
import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()
_STUBS = (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.schedule_reader",
)


@pytest.fixture()
def optimizer_module():
    saved = {name: sys.modules.get(name, _SENTINEL) for name in _STUBS}
    for name in _STUBS:
        sys.modules.pop(name, None)
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
    dt.utcnow = dt.now
    dt.UTC = timezone.utc
    util.dt = dt
    ha.util = util
    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
    })
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync"] = package
    sys.modules["power_sync.optimization"] = optimization
    module = importlib.import_module("power_sync.optimization.battery_optimizer")
    try:
        yield module
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _optimizer(module):
    return module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=60,
        horizon_hours=4,
        terminal_weight=0.0,
    )


def _timestamps(n: int) -> list[datetime]:
    start = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
    return [start + timedelta(hours=idx) for idx in range(n)]


@pytest.mark.parametrize("solar_kw", [0.0, 3.0])
@pytest.mark.parametrize("use_highs", [True, False])
def test_cost_neutral_replans_tiny_exports_as_native_operation(
    optimizer_module, monkeypatch, solar_kw, use_highs,
):
    """Small export-mode witnesses must not become repeated force commands."""
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", use_highs)
    optimizer = module.BatteryOptimizer(
        capacity_wh=80_000, max_charge_w=27_000, max_discharge_w=27_000,
        efficiency=0.95, backup_reserve=0.1, hardware_reserve=0.05,
        interval_minutes=60, horizon_hours=24, terminal_weight=0.5,
    )
    solar = [solar_kw if idx >= 18 else 0.0 for idx in range(24)]
    load = [0.8] * 24
    imports = [0.21 if idx < 2 else 0.54 if idx < 6 else 0.31 for idx in range(24)]
    exports = [0.28 if 2 <= idx < 6 else 0.03 for idx in range(24)]
    result = optimizer.optimize(
        import_prices=imports, export_prices=exports,
        solar_forecast=solar, load_forecast=load, current_soc=0.9,
        allow_battery_export=True, schedule_timestamps=_timestamps(24),
        cost_neutral_earnings_cap=1.5, cost_neutral_slots=[True] * 24,
        acquisition_cost_kwh=0.21,
    )
    assert result.feasible
    assert result.solver_used == ("highs" if use_highs else "greedy")
    assert any(action.action == "export" for action in result.schedule.actions)
    assert all(
        power == 0.0 or power >= 500.0
        for power in result.schedule.battery_export_w
    )
    if solar_kw and use_highs:
        assert all(
            action.action == "self_consumption"
            and action.battery_charge_w == pytest.approx(2_200.0)
            for action in result.schedule.actions[18:]
        )
    soc = 0.9
    for idx, action in enumerate(result.schedule.actions):
        soc += (action.battery_charge_w * 0.95 - action.battery_discharge_w / 0.95) / 80_000
        assert action.soc == pytest.approx(soc, abs=0.0002)
        net_grid_w = load[idx] * 1000 - solar[idx] * 1000 + action.battery_charge_w - action.battery_discharge_w
        assert result.grid_import_w[idx] == pytest.approx(max(0, net_grid_w), abs=0.2)
        assert result.grid_export_w[idx] == pytest.approx(max(0, -net_grid_w), abs=0.2)


@pytest.mark.parametrize("use_highs", [True, False])
def test_cost_neutral_small_settlement_remainder_does_not_force_export(
    optimizer_module, monkeypatch, use_highs,
):
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", use_highs)
    result = _optimizer(module).optimize(
        import_prices=[0.2], export_prices=[1.0],
        solar_forecast=[0.0], load_forecast=[0.0], current_soc=0.8,
        allow_grid_charge=False, allow_battery_export=True,
        schedule_timestamps=_timestamps(1), cost_neutral_earnings_cap=0.3,
        cost_neutral_slots=[True],
    )
    assert result.schedule.battery_export_w == [0.0]
    assert result.grid_export_w == [0.0]
    assert result.schedule.actions[0].soc == pytest.approx(0.8)
    assert result.lp_stats["cost_neutral_planned_earnings"] == 0.0


@pytest.mark.parametrize("cost_neutral", [True, False])
def test_micro_export_policy_preserves_manual_control_and_other_modes(
    optimizer_module, cost_neutral,
):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.2], export_prices=[1.0],
        solar_forecast=[0.0], load_forecast=[0.0], current_soc=0.8,
        allow_grid_charge=False, allow_battery_export=True,
        schedule_timestamps=_timestamps(1),
        cost_neutral_earnings_cap=0.3 if cost_neutral else None,
        cost_neutral_slots=[cost_neutral],
        manual_control={
            "mode_slots": ["export"], "required_discharge_kw": [0.1001],
        },
    )
    action = result.schedule.actions[0]
    assert action.control_source == "manual"
    assert action.action == "export"
    assert action.battery_discharge_w == pytest.approx(100.1)


@pytest.mark.parametrize("charge_blocked", [False, True])
def test_final_overlay_removal_restores_native_solar_with_ev_and_capacity_limit(
    optimizer_module, charge_blocked,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start, action="export", power_w=1_000,
            battery_discharge_w=1_000, ev_charge_w=2_000, soc=0.89,
        )],
        predicted_cost=0.0, predicted_savings=0.0, last_updated=start,
    )
    result = module.OptimizerResult(
        schedule=schedule,
        ev_charge_by_vehicle_w={"car": [2_000.0]},
        ev_source_by_vehicle_w={"car": {
            "grid": [0.0], "solar": [2_000.0], "battery": [0.0],
        }},
    )
    result = optimizer.reconcile_result_with_schedule(
        result, schedule, import_prices=[0.2], export_prices=[0.03],
        solar=[6.0], load=[1.0], initial_soc=0.99,
        cost_neutral_earnings_cap=0.003, cost_neutral_slots=[True],
        block_battery_charge=[charge_blocked],
    )
    action = result.schedule.actions[0]
    assert action.action == "self_consumption"
    assert action.battery_discharge_w == 0.0
    assert action.battery_charge_w == pytest.approx(0.0 if charge_blocked else 100.0)
    assert action.soc == pytest.approx(0.99 if charge_blocked else 1.0)
    assert action.ev_charge_w == 2_000.0
    assert result.grid_import_w == [0.0]
    assert result.grid_export_w == pytest.approx([3_000.0 if charge_blocked else 2_900.0])
    assert result.battery_to_grid_w == [0.0]
    assert result.lp_stats["cost_neutral_planned_earnings"] == 0.0
    assert result.ev_source_by_vehicle_w["car"]["solar"] == [2_000.0]


@pytest.mark.parametrize("cost_neutral", [False, True])
def test_small_export_policy_keeps_other_goals_and_useful_priority_export(
    optimizer_module, cost_neutral,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.update_config(max_discharge_w=500 if cost_neutral else 100.1)
    result = optimizer.optimize(
        import_prices=[0.2], export_prices=[1.0],
        solar_forecast=[0.0], load_forecast=[0.0], current_soc=0.8,
        allow_grid_charge=False, allow_battery_export=True,
        priority_export_enabled=True, priority_export_slots=[True],
        schedule_timestamps=_timestamps(1),
        cost_neutral_earnings_cap=0.5 if cost_neutral else None,
        cost_neutral_slots=[cost_neutral],
    )
    assert result.schedule.battery_export_w == pytest.approx([
        500.0 if cost_neutral else 100.1,
    ])


def test_cost_neutral_ev_uses_distinct_surplus_and_selector_columns(
    optimizer_module, monkeypatch,
):
    """Continuous PV surplus must not alias any of its four binary witnesses."""
    module = optimizer_module
    original = module._solve_lp_highs
    captured = []

    def capture(*args, **kwargs):
        scope = inspect.currentframe().f_back.f_locals
        names = (
            "cost_neutral_surplus_offset", "cost_neutral_surplus_sign_offset",
            "cost_neutral_min_surplus_offset", "cost_neutral_min_power_offset",
            "cost_neutral_min_capacity_offset",
        )
        captured.append((
            {name: scope[name] for name in names}, scope["p_n"],
            args[5], list(kwargs.get("integer_indices", ())),
            args[1], scope["cost_neutral_natural_periods"],
        ))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_solve_lp_highs", capture)
    optimizer = _optimizer(module)
    ev = module.EVChargePlan(
        vehicle_id="car", max_power_kw=(1.0,) * 2,
        energy_needed_kwh=1.0, charge_efficiency=1.0, min_power_kw=0.5,
    )
    optimizer.optimize(
        import_prices=[0.25] * 2, export_prices=[0.08] * 2,
        solar_forecast=[4.25, 0.0], load_forecast=[0.7] * 2,
        current_soc=0.5, allow_battery_export=True,
        schedule_timestamps=_timestamps(2), cost_neutral_earnings_cap=1.0,
        cost_neutral_slots=[True, False], ev_plan=ev,
    )
    assert captured
    for offsets, n, bounds, integers, matrix, natural in captured:
        blocks = [set(range(offset, offset + n)) for offset in offsets.values()]
        assert len(set.union(*blocks)) == 5 * n
        assert len(integers) == len(set(integers))
        surplus, *selectors = offsets.values()
        assert all(surplus + t not in integers for t in range(n))
        for selector in selectors:
            assert all(selector + t in integers for t in range(n))
            assert all(bounds[selector + t] in ((0.0, 0.0), (0.0, 1.0))
                       for t in range(n))
        for t in natural:
            assert any(
                row.get(surplus + t) == 1.0
                and selectors[0] + t in row
                for row in matrix._rows
            )
            assert any(
                all(row.get(selector + t) == 1.0 for selector in selectors[1:])
                for row in matrix._rows
            )


@pytest.mark.parametrize("solar,soc,expected_charge", [
    (1.95, 0.2, 0.75),  # PV surplus binds, including a fractional kW.
    (4.75, 0.2, 2.0),   # Native charging power binds.
    (4.75, 0.95, 0.5),  # Remaining battery capacity binds.
    (1.2, 0.2, 0.0),    # EV demand consumes all available surplus.
])
def test_cost_neutral_ev_native_absorption_matches_all_minimum_branches(
    optimizer_module, solar, soc, expected_charge,
):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=2_000, max_discharge_w=2_000,
        efficiency=1.0, backup_reserve=0.0, hardware_reserve=0.0,
        interval_minutes=60, horizon_hours=1, terminal_weight=0.0,
    )
    ev = module.EVChargePlan(
        vehicle_id="car", max_power_kw=(0.5,), energy_needed_kwh=0.5,
        charge_efficiency=1.0, min_power_kw=0.5,
        allow_grid=(True,), allow_solar=(True,), allow_battery=(False,),
    )
    result = optimizer.optimize(
        import_prices=[0.25], export_prices=[0.08], solar_forecast=[solar],
        load_forecast=[0.7], current_soc=soc, allow_battery_export=False,
        allow_grid_charge=False, schedule_timestamps=_timestamps(1), ev_plan=ev,
        cost_neutral_earnings_cap=0.0, cost_neutral_slots=[True],
    )
    assert result.solver_used == "highs"
    assert result.feasible
    action = result.schedule.actions[0]
    assert action.ev_charge_w == pytest.approx(500.0, abs=0.1)
    assert action.battery_charge_w == pytest.approx(expected_charge * 1000, abs=0.1)
    assert result.grid_export_w[0] == pytest.approx(
        max(0.0, solar - 0.7 - 0.5 - expected_charge) * 1000, abs=0.1,
    )


def test_cycle_diagnostics_keep_discarded_projection_and_reset_between_calls(
    optimizer_module, monkeypatch,
):
    module = optimizer_module
    real_solve = module._solve_lp_highs
    calls = 0

    def later_timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return module._HighsResult(None, False, "Time limit reached", 13, None)
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(module, "_solve_lp_highs", later_timeout)
    inputs = dict(import_prices=[0.25]*2, export_prices=[0.08]*2,
                  solar_forecast=[0.0]*2, load_forecast=[0.7]*2,
                  current_soc=0.5, schedule_timestamps=_timestamps(2))
    result = _optimizer(module).optimize(**inputs)
    cycle = result.lp_stats["cycle"]
    assert result.solver_used == "highs"
    assert cycle["selected_pass"] == 0
    assert cycle["pass_count"] == 2
    assert cycle["passes"][1]["status"] == 13
    assert cycle["native_time_s"] > 0
    assert cycle["stages"]["schedule_build_time_s"] > 0
    assert module._SOLVER_CYCLE.get() is None
    json.dumps(cycle, allow_nan=False)

    monkeypatch.setattr(module, "_solve_lp_highs", real_solve)
    second = _optimizer(module).optimize(**inputs).lp_stats["cycle"]
    assert second is not cycle
    assert second["passes"][0]["id"] == 0
    assert second["passes"] is not cycle["passes"]
    assert len(cycle["passes"]) == 2


def test_benchmark_corpus_keeps_actual_capture_length_and_full_horizons(
    optimizer_module,
):
    import runpy

    benchmark = runpy.run_path(str(ROOT / "scripts/benchmark_lp_optimizer.py"))
    cases = benchmark["_cases"](optimizer_module)
    assert len(cases["sunny-cost-neutral"].inputs["import_prices"]) == 250
    for name in ("cost-neutral-48h", "cost-neutral-ev-48h", "cost-neutral-ledger-48h"):
        case = cases[name]
        assert len(case.inputs["import_prices"]) == 576
        assert len(case.inputs["cost_neutral_plan"].day_ids) == 576
    assert cases["cost-neutral-ev-48h"].inputs["ev_plan"].energy_needed_kwh > 0
    assert cases["cost-neutral-ledger-48h"].inputs[
        "cost_neutral_plan"
    ].forecast_natural_export_earnings_by_day
    plans = cases["multi-ev-deadlines-48h"].inputs["ev_plan"]
    assert len(plans) == 2
    assert plans[0].deadline_index < plans[1].deadline_index
    assert not any(cases["ev-permissions-floor-48h"].inputs["ev_plan"].allow_battery)


@pytest.mark.parametrize("empty_rhs", [1.0, -1.0])
def test_bulk_solver_preserves_integrality_infinite_bounds_and_empty_rows(
    optimizer_module, empty_rhs,
):
    module = optimizer_module
    equality = module._LpMatrix((1, 2))
    equality[0, 0] = equality[0, 1] = 1.0
    inequality = module._LpMatrix((2, 2))
    inequality[0, 1] = -1.0
    result = module._solve_lp_highs(
        [-1.0, 0.0], inequality, [0.0, empty_rhs], equality, [3.5],
        [(0.0, None), (None, 4.0)], 30.0, integer_indices=[0],
    )
    if empty_rhs < 0:
        assert not result.success
        assert "infeasible" in result.message.lower()
    else:
        assert result.success
        assert result.x == pytest.approx([3.0, 0.5])
        assert result.fun == pytest.approx(-3.0)
        assert result.stats["packing_time_s"] >= 0
        assert result.stats["mip_dual_bound"] == pytest.approx(-3.0)


def test_bulk_loading_error_cannot_return_a_previous_success(
    optimizer_module, monkeypatch,
):
    module = optimizer_module
    h = module.highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.addCol(1.0, 1.0, 2.0, 0, [], [])
    h.run()

    class FailedLoad:
        def __getattr__(self, name):
            return getattr(h, name)

        def passModel(self, model):
            return module.highspy.HighsStatus.kError

    monkeypatch.setattr(module.highspy, "Highs", FailedLoad)
    with pytest.raises(RuntimeError, match="rejected"):
        module._solve_lp_highs([1.0], module._LpMatrix((0, 1)), [],
                               module._LpMatrix((0, 1)), [], [(0.0, 2.0)], 30.0)


@pytest.mark.parametrize("start,expected", [
    ([1.0], "submitted"), ([0.25], "invalid"),
    ([float("nan")], "invalid"), ([1.0, 2.0], "invalid"),
])
def test_optional_full_start_is_validated_and_cold_solve_remains_available(
    optimizer_module, start, expected,
):
    module = optimizer_module
    equality = module._LpMatrix((1, 1))
    equality[0, 0] = 1.0
    result = module._solve_lp_highs(
        [1.0], module._LpMatrix((0, 1)), [], equality, [1.0],
        [(0.0, 2.0)], 30.0, integer_indices=[0], initial_solution=start,
    )
    assert result.success
    assert result.x == pytest.approx([1.0])
    assert result.stats["start"] == expected


def test_rejected_start_uses_a_fresh_cold_solver(optimizer_module, monkeypatch):
    module = optimizer_module
    constructor = module.highspy.Highs
    instances = []

    class RejectStart:
        def __init__(self):
            self.native = constructor()
            instances.append(self)

        def __getattr__(self, name):
            return getattr(self.native, name)

        def setSolution(self, solution):
            raise RuntimeError("unsupported start overload")

    monkeypatch.setattr(module.highspy, "Highs", RejectStart)
    result = module._solve_lp_highs(
        [1.0], module._LpMatrix((0, 1)), [], module._LpMatrix((0, 1)), [],
        [(1.0, 2.0)], 30.0, initial_solution=[1.0],
    )
    assert result.success
    assert result.x == pytest.approx([1.0])
    assert len(instances) == 2
    assert result.stats["start"] == "not_requested"


def test_rejected_preference_start_keeps_its_original_deadline(optimizer_module, monkeypatch):
    module = optimizer_module
    constructor = module.highspy.Highs
    clock = 0.0
    native_allowances = []

    class RejectStart:
        def __init__(self):
            self.native = constructor()

        def __getattr__(self, name):
            return getattr(self.native, name)

        def setSolution(self, solution):
            nonlocal clock
            clock += 2.75
            raise RuntimeError("unsupported start")

        def run(self):
            native_allowances.append(self.native.getOptionValue("time_limit")[1])
            return self.native.run()

    fake_time = types.SimpleNamespace(monotonic=lambda: clock)
    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(sys.modules[module.OptimizationRun.__module__], "time", fake_time)
    monkeypatch.setattr(module.highspy, "Highs", RejectStart)
    work = module.OptimizationRun()
    with work.free_preference(30.0) as limit:
        result = module._solve_lp_highs(
            [1.0], module._LpMatrix((0, 1)), [], module._LpMatrix((0, 1)), [],
            [(1.0, 2.0)], limit, initial_solution=[1.0], solve_role="tiebreak",
        )
    assert result.success
    assert native_allowances == [pytest.approx(0.25)]
    assert work.preference_elapsed_s == pytest.approx(2.75)


def test_free_preference_budget_is_shared_across_full_solves(optimizer_module, monkeypatch):
    module = optimizer_module
    solve = module._solve_lp_highs
    clock = 0.0
    preference_limits = []
    primary_limits = []

    def observe(*args, **kwargs):
        nonlocal clock
        if kwargs.get("solve_role") == "tiebreak":
            limit = kwargs["time_limit"]
            preference_limits.append(limit)
            clock += limit + 0.25  # HiGHS can overrun its requested limit.
            return module._HighsResult(
                list(kwargs["initial_solution"]), True, "Time limit reached",
                int(module.highspy.HighsModelStatus.kTimeLimit), 0.0,
            )
        primary_limits.append(kwargs["time_limit"])
        return solve(*args, **kwargs)

    fake_time = types.SimpleNamespace(monotonic=lambda: clock)
    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(sys.modules[module.OptimizationRun.__module__], "time", fake_time)
    monkeypatch.setattr(module, "_solve_lp_highs", observe)
    work = module.OptimizationRun()
    for _ in range(5):
        result = work.solve(lambda: _optimizer(module).optimize(
            import_prices=[0.31, 0.31, 0.0, 0.0], export_prices=[0.0]*4,
            solar_forecast=[0.0]*4, load_forecast=[0.5]*4,
            current_soc=0.2, allow_battery_export=False, disable_idle=True,
            schedule_timestamps=_timestamps(4), cost_neutral_earnings_cap=0.2,
            cost_neutral_slots=[True]*4,
        ))
        assert result.solver_used == "highs" and result.feasible
        assert all(action.action != "idle" for action in result.schedule.actions)
    assert preference_limits == pytest.approx([3.0, 3.0, 3.0, 2.25])
    assert all(limit == 30.0 for limit in primary_limits)
    stats = result.lp_stats["cycle"]["cost_neutral_free_preference"]
    assert stats["attempts"] == 4
    assert stats["elapsed_s"] == pytest.approx(12.25)
    assert stats["skips"] > 0


def test_export_preference_shares_the_cycle_preference_budget(optimizer_module, monkeypatch):
    """Ticket 410: the fewer-exports preference must not take each pass's rest."""
    module = optimizer_module
    solve = module._solve_lp_highs
    clock = 0.0
    preference_limits = []

    def observe(*args, **kwargs):
        nonlocal clock
        if kwargs.get("solve_role") == "tiebreak":
            limit = kwargs["time_limit"]
            preference_limits.append(limit)
            clock += limit
            return module._HighsResult(
                list(kwargs["initial_solution"]), True, "Time limit reached",
                int(module.highspy.HighsModelStatus.kTimeLimit), 0.0,
            )
        return solve(*args, **kwargs)

    fake_time = types.SimpleNamespace(monotonic=lambda: clock)
    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(sys.modules[module.OptimizationRun.__module__], "time", fake_time)
    monkeypatch.setattr(module, "_solve_lp_highs", observe)
    work = module.OptimizationRun()
    for _ in range(5):
        result = work.solve(lambda: _optimizer(module).optimize(
            import_prices=[0.31]*4, export_prices=[0.25, 0.25, 0.05, 0.05],
            solar_forecast=[0.0]*4, load_forecast=[0.5]*4,
            current_soc=0.8, allow_battery_export=True,
            schedule_timestamps=_timestamps(4), cost_neutral_earnings_cap=0.2,
            cost_neutral_slots=[True]*4,
        ))
        assert result.solver_used == "highs" and result.feasible
    assert preference_limits
    assert all(limit <= module_attempt_limit(module) for limit in preference_limits)
    assert sum(preference_limits) == pytest.approx(12.0)
    stats = result.lp_stats["cycle"]["cost_neutral_free_preference"]
    assert stats["skips"] > 0


def module_attempt_limit(module):
    return sys.modules[module.OptimizationRun.__module__].COST_NEUTRAL_FREE_PREFERENCE_ATTEMPT_SECONDS


def test_primary_mixed_integer_solve_stops_within_half_a_cent(optimizer_module, monkeypatch):
    """Ticket 410: proving the last fraction of a cent must not cost the plan."""
    module = optimizer_module
    options = []
    real_highs = module.highspy.Highs

    class RecordingHighs(real_highs):
        def setOptionValue(self, name, value):
            options.append((name, value))
            return super().setOptionValue(name, value)

    monkeypatch.setattr(module.highspy, "Highs", RecordingHighs)
    bounds = [(0.0, 1.0), (0.0, 1.0)]
    args = ([1.0, 1.0], module._LpMatrix((0, 2)), [], module._LpMatrix((0, 2)), [], bounds)
    module._solve_lp_highs(*args, time_limit=5.0, integer_indices=[0])
    assert ("mip_abs_gap", module.PRIMARY_MIP_ABS_GAP_DOLLARS) in options
    assert module.PRIMARY_MIP_ABS_GAP_DOLLARS == pytest.approx(0.005)
    options.clear()
    module._solve_lp_highs(*args, time_limit=5.0, integer_indices=[0], solve_role="tiebreak")
    module._solve_lp_highs(*args, time_limit=5.0)
    assert not any(name == "mip_abs_gap" for name, _ in options)


def test_cost_neutral_later_days_use_tiered_periods(optimizer_module):
    """Ticket 410: only today's Cost Neutral settlement stays at 5 minutes."""
    module = optimizer_module
    optimizer = module.BatteryOptimizer(interval_minutes=5, horizon_hours=48)
    n = 576
    today = 156  # 13 h left today, as in the ticket's 10:56 capture
    day_ids = (["2026-11-25"] * today + ["2026-11-26"] * 288
               + ["2026-11-27"] * (n - today - 288))
    common = dict(
        import_prices=[0.25] * n, export_prices=[0.08] * n,
        solar=[0.0] * n, load=[0.7] * n,
        allow_battery_export=[True] * n, block_battery_charge=[False] * n,
        cost_neutral_slots=[True] * n,
    )
    periods = optimizer._build_lp_periods(n, cost_neutral_day_ids=day_ids, **common)

    assert all(p.slot_count == 1 for p in periods if p.start < today)
    assert max(p.slot_count for p in periods if p.start >= today) > 1
    assert len(periods) < 250
    for period in periods:
        assert len(set(day_ids[period.start:period.end])) == 1
    assert {period.end for period in periods} >= {today, today + 288}

    # Without day ids the existing all-base-resolution behaviour is kept.
    legacy = optimizer._build_lp_periods(n, **common)
    assert len(legacy) == n


def test_superseding_a_primary_solve_stops_before_the_next_pass(optimizer_module, monkeypatch):
    module = optimizer_module
    solve = module._solve_lp_highs
    work = module.OptimizationRun()
    calls = []

    def supersede(*args, **kwargs):
        calls.append(kwargs.get("solve_role", "primary"))
        result = solve(*args, **kwargs)
        work.superseded.set()
        return result

    monkeypatch.setattr(module, "_solve_lp_highs", supersede)
    with pytest.raises(module.OptimizerPlanSuperseded):
        work.solve(lambda: _optimizer(module).optimize(
            import_prices=[0.31, 0.31, 0.0, 0.0], export_prices=[0.0]*4,
            solar_forecast=[0.0]*4, load_forecast=[0.5]*4,
            current_soc=0.2, allow_battery_export=False,
            schedule_timestamps=_timestamps(4), cost_neutral_earnings_cap=0.2,
            cost_neutral_slots=[True]*4,
        ))
    assert calls == ["primary"]


@pytest.mark.parametrize("failure", ["exception", "invalid_point"])
def test_optional_tiebreak_failure_keeps_primary_plan(optimizer_module, monkeypatch, failure):
    module = optimizer_module
    solve = module._solve_lp_highs
    attempts = []

    def fail_preference(*args, **kwargs):
        if kwargs.get("solve_role") == "tiebreak":
            attempts.append(failure)
            if failure == "exception":
                raise RuntimeError("secondary API failed")
            point = list(kwargs["initial_solution"])
            point[0] = float("nan")
            return module._HighsResult(point, True, "Optimal", 7, 0.0)
        return solve(*args, **kwargs)

    monkeypatch.setattr(module, "_solve_lp_highs", fail_preference)
    result = _optimizer(module).optimize(
        import_prices=[0.31, 0.31, 0.0, 0.0], export_prices=[0.0]*4,
        solar_forecast=[0.0]*4, load_forecast=[0.5]*4,
        current_soc=0.2, allow_battery_export=False,
        schedule_timestamps=_timestamps(4), cost_neutral_earnings_cap=0.2,
        cost_neutral_slots=[True]*4,
    )
    assert attempts
    assert result.solver_used == "highs"
    assert result.feasible
    assert result.lp_stats["cost_neutral_charging_tiebreak_used"] is False
    assert all(math.isfinite(a.soc) for a in result.schedule.actions)
    if failure == "exception":
        assert result.lp_stats["charging_tiebreak_solver"]["error"] == "RuntimeError"


def test_economic_tiebreak_row_is_independent_of_primary_model(optimizer_module):
    module = optimizer_module
    primary = module._LpMatrix((1, 2))
    primary[0, 0] = 2.0
    secondary = primary.with_row([1.0, 0.0])
    secondary[0, 0] = 3.0
    assert primary.shape == (1, 2)
    assert primary[0, 0] == 2.0
    assert secondary[1, 0] == 1.0
    assert secondary[1, 1] == 0.0



@pytest.mark.parametrize("disable_idle", [False, True])
def test_cost_neutral_waits_for_free_window_without_paid_topups(
    optimizer_module, disable_idle,
):
    """A protected export must not invent a later need for paid recharge."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    inputs = json.loads(
        (ROOT / "tests/fixtures/cost_neutral_free_window_paid_topups.json").read_text()
    )
    start = datetime.fromisoformat(inputs["start"])
    timestamps = [
        start + timedelta(minutes=inputs["interval_minutes"] * idx)
        for idx in range(len(inputs["import_prices"]))
    ]
    allowances = inputs["cost_neutral_fixed_allowances"]
    plan = optimizer_module.CostNeutralPlan(
        day_ids=[timestamp.date().isoformat() for timestamp in timestamps],
        earnings_caps_by_day=allowances,
        forecast_import_costs_by_day={day: 0.0 for day in allowances},
        fixed_cost_allowances_by_day=allowances,
        current_day=start.date().isoformat(),
        timezone="Australia/Brisbane",
        settlement_import_prices=inputs["import_prices"],
        settlement_export_prices=inputs["export_prices"],
    )
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=54_000,
        max_charge_w=14_700,
        max_discharge_w=21_000,
        max_grid_import_w=15_000,
        max_grid_export_w=10_000,
        efficiency=0.92,
        backup_reserve=0.38,
        hardware_reserve=0.0,
        interval_minutes=5,
        horizon_hours=48,
    )
    result = optimizer.optimize(
        import_prices=inputs["import_prices"],
        export_prices=inputs["export_prices"],
        solar_forecast=inputs["solar_forecast"],
        load_forecast=inputs["load_forecast"],
        current_soc=inputs["initial_soc"],
        allow_battery_export=[price > 0 for price in inputs["export_prices"]],
        schedule_timestamps=timestamps,
        disable_idle=disable_idle,
        cost_neutral_plan=plan,
    )

    assert result.solver_used == "highs"
    assert result.feasible
    assert result.lp_stats["mode_converged"] is True
    assert not [
        action.timestamp
        for idx, action in enumerate(result.schedule.actions)
        if action.action == "charge" and inputs["import_prices"][idx] > 0.001
    ]
    assert any(action.action == "export" for action in result.schedule.actions)
    assert any(
        action.action == "charge" and inputs["import_prices"][idx] == 0.0
        for idx, action in enumerate(result.schedule.actions)
    )
    assert all(
        action.soc >= 0.38 - 0.0001
        for action in result.schedule.actions
        if action.action == "export"
    )
    assert all(
        power == 0.0 or power >= 500.0
        for power in result.schedule.battery_export_w
    )


@pytest.mark.parametrize("secondary_timeout", [False, True])
def test_cost_neutral_keeps_paid_charging_for_deadline_before_free_window(
    optimizer_module, monkeypatch, secondary_timeout,
):
    """Free charging preferences must not prevent a necessary earlier top-up."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    optimizer = _optimizer(optimizer_module)
    optimizer.pre_window_slot = 2
    optimizer.pre_window_soc_target = 1.0
    if secondary_timeout:
        real_solve = optimizer_module._solve_lp_highs
        calls = 0

        def timeout_preference(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                return optimizer_module._HighsResult(
                    None, False, "Time limit reached", 13, None,
                )
            return real_solve(*args, **kwargs)

        monkeypatch.setattr(optimizer_module, "_solve_lp_highs", timeout_preference)
    result = optimizer.optimize(
        import_prices=[0.31, 0.31, 0.0, 0.0],
        export_prices=[0.0] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0, 0.0, 0.5, 0.5],
        current_soc=0.2,
        allow_battery_export=False,
        schedule_timestamps=_timestamps(4),
        cost_neutral_earnings_cap=0.2,
        cost_neutral_slots=[True] * 4,
    )

    assert result.feasible
    assert result.solver_used == "highs"
    assert result.lp_stats["cost_neutral_charging_tiebreak_used"] is True
    assert result.schedule.actions[1].soc >= 0.995
    assert sum(result.schedule.charge_w[:2]) / 1000.0 == pytest.approx(7.95, abs=0.01)
    assert any(action.action == "charge" for action in result.schedule.actions[:2])


@pytest.mark.parametrize("free_price", [0.0, -0.05])
def test_cost_neutral_keeps_paid_charge_that_avoids_expensive_home_import(
    optimizer_module, free_price,
):
    """A later free window does not prohibit useful earlier price arbitrage."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.10, 0.50, free_price, 0.30],
        export_prices=[0.0] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0, 5.0, 0.0, 0.0],
        current_soc=0.0,
        allow_battery_export=False,
        schedule_timestamps=_timestamps(4),
        cost_neutral_earnings_cap=0.2,
        cost_neutral_slots=[True] * 4,
    )

    assert result.feasible
    assert result.solver_used == "highs"
    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.charge_w[0] == pytest.approx(5_000.0, abs=0.01)
    assert result.grid_import_w[1] <= 0.01
    assert result.schedule.actions[2].action == "charge"


def _charge_by_time_cost_neutral_optimizer(module):
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=60,
        horizon_hours=1,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 1
    optimizer.pre_window_soc_target = 1.0
    return optimizer


def test_cost_neutral_deadline_can_select_initial_grid_charge(
    optimizer_module,
):
    """Cost Neutral must not make a reachable deadline LP infeasible."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    result = _charge_by_time_cost_neutral_optimizer(optimizer_module).optimize(
        import_prices=[0.1291225],
        export_prices=[0.0873771],
        solar_forecast=[0.0],
        load_forecast=[0.0],
        current_soc=0.508,
        allow_battery_export=[True],
        allow_grid_charge=True,
        cost_neutral_earnings_cap=3.7987,
        cost_neutral_slots=[True],
        schedule_timestamps=_timestamps(1),
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.charge_w[0] > 4_000
    assert result.schedule.actions[0].soc >= 0.995


def test_cost_neutral_five_minute_deadline_can_top_up_before_target(
    optimizer_module,
):
    """The reported 14-slot geometry must retain its reachable target."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=24_180,
        max_charge_w=12_600,
        max_discharge_w=14_400,
        efficiency=0.92,
        backup_reserve=0.05,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=14 / 12,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 14
    optimizer.pre_window_soc_target = 1.0

    result = optimizer.optimize(
        import_prices=[0.1291225] * 14,
        export_prices=[0.0873771] * 14,
        solar_forecast=[0.0] * 14,
        load_forecast=[1.5] * 14,
        current_soc=0.456,
        allow_battery_export=[True] * 14,
        allow_grid_charge=True,
        cost_neutral_earnings_cap=3.7987,
        cost_neutral_slots=[True] * 14,
        schedule_timestamps=_timestamps(14),
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert sum(result.schedule.charge_w) > 10_000
    assert result.schedule.actions[-1].soc >= 0.995


@pytest.mark.parametrize("cost_neutral_enabled", [True, False])
@pytest.mark.parametrize("projection_limit", [None, 13, 14])
def test_subthreshold_deadline_grid_top_up_is_not_reported_as_idle_charge(
    optimizer_module,
    cost_neutral_enabled,
    projection_limit,
    monkeypatch,
):
    """An executable idle fallback must not retain suppressed grid energy."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    n = 12
    soc = 0.995 - 0.0946 * 0.92 * (n / 12) / 24.18
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=24_180,
        max_charge_w=12_600,
        max_discharge_w=14_400,
        max_grid_import_w=1_595,
        max_grid_export_w=5_000,
        efficiency=0.92,
        backup_reserve=0.05,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=n / 12,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = n
    optimizer.pre_window_soc_target = 1.0
    if projection_limit is not None:
        real_solve = optimizer_module._solve_lp_highs
        solve_count = 0

        def limited_projection(*args, **kwargs):
            nonlocal solve_count
            solve_count += 1
            if solve_count > 1:
                message = (
                    "Time limit reached" if projection_limit == 13
                    else "Iteration limit reached"
                )
                return optimizer_module._HighsResult(
                    None, False, message, projection_limit, None
                )
            return real_solve(*args, **kwargs)

        monkeypatch.setattr(optimizer_module, "_solve_lp_highs", limited_projection)
    start = datetime(2026, 10, 13, 15, 0, tzinfo=timezone(timedelta(hours=10)))
    result = optimizer.optimize(
        import_prices=[0.1291225] * n,
        export_prices=[0.0873771] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[1.5] * n,
        current_soc=soc,
        allow_battery_export=[True] * n,
        allow_grid_charge=True,
        cost_neutral_earnings_cap=(3.7987 if cost_neutral_enabled else None),
        cost_neutral_slots=[cost_neutral_enabled] * n,
        schedule_timestamps=[
            start + timedelta(minutes=5 * index) for index in range(n)
        ],
    )

    assert result.solver_used == "highs"
    assert result.feasible is False
    assert (
        result.lp_stats["fallback_reason"]
        == "mode_projection_" + (
            "infeasible" if projection_limit is None
            else "timeout" if projection_limit == 13 else "iteration_limit"
        ) + "_executable_deadline"
    )
    assert result.lp_stats["deadline_unmet_after_mode_projection"] is True
    assert result.reserve_recommendation == {}
    assert all(action.action == "idle" for action in result.schedule.actions)
    assert all(action.battery_charge_w == 0.0 for action in result.schedule.actions)
    assert all(action.battery_discharge_w == 0.0 for action in result.schedule.actions)
    assert all(value == 1_500.0 for value in result.grid_import_w)
    assert result.schedule.actions[-1].soc == pytest.approx(soc, abs=0.0001)


@pytest.mark.parametrize(
    ("charge_w", "expected_action", "expected_reported_charge_w"),
    [
        (99.9, "self_consumption", 0.0),
        (100.0, "self_consumption", 0.0),
        (100.1, "charge", 100.1),
    ],
)
def test_action_threshold_keeps_emitted_grid_flow_executable(
    optimizer_module,
    charge_w,
    expected_action,
    expected_reported_charge_w,
):
    """The 100 W action threshold must not create hidden grid charging."""
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=0.92,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=5,
        horizon_hours=1 / 12,
        terminal_weight=0.0,
    )
    timestamp = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
    schedule = optimizer._build_schedule(
        1,
        [charge_w / 1000],
        [0.0],
        [charge_w / 1000],
        [0.0],
        [0.0],
        [0.0],
        0.5,
        [0.20],
        [0.10],
        schedule_timestamps=[timestamp],
    )

    action = schedule.actions[0]
    assert action.action == expected_action
    assert action.battery_charge_w == pytest.approx(expected_reported_charge_w)
    assert action.battery_discharge_w == 0.0


def test_subthreshold_self_consumption_charge_keeps_native_solar_only(
    optimizer_module,
):
    """Non-charge actions may retain native PV, never the suppressed grid part."""
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=0.92,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=5,
        horizon_hours=1 / 12,
        terminal_weight=0.0,
    )
    timestamp = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
    schedule = optimizer._build_schedule(
        1,
        [0.0],
        [0.0],
        [0.095],
        [0.0],
        [0.5],
        [0.0],
        0.5,
        [0.20],
        [0.10],
        schedule_timestamps=[timestamp],
    )

    action = schedule.actions[0]
    assert action.action == "self_consumption"
    assert action.battery_charge_w == pytest.approx(500.0)
    assert action.soc > 0.5


def test_cost_neutral_deadline_preserves_native_solar_before_ceiling(
    optimizer_module,
):
    """Solar headroom must not make unavoidable native charging infeasible."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=0.92,
        backup_reserve=0.05,
        hardware_reserve=0.05,
        interval_minutes=60,
        horizon_hours=2,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 2
    optimizer.pre_window_soc_target = 1.0

    result = optimizer.optimize(
        import_prices=[0.13, 0.13],
        export_prices=[0.08, 0.08],
        solar_forecast=[5.0, 5.0],
        load_forecast=[0.0, 0.0],
        current_soc=0.50,
        allow_battery_export=[True, True],
        allow_grid_charge=True,
        cost_neutral_earnings_cap=3.8,
        cost_neutral_slots=[True, True],
        schedule_timestamps=_timestamps(2),
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.schedule.charge_w[0] == pytest.approx(5_000.0)
    assert result.grid_import_w[0] == pytest.approx(0.0)
    assert result.schedule.actions[0].soc == pytest.approx(0.96)


def test_cost_neutral_values_executable_morning_solar_charge(
    optimizer_module, monkeypatch,
):
    """A near-empty sunrise solve must not sell PV the inverter will store."""
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=5_000, max_discharge_w=5_000,
        efficiency=0.92, backup_reserve=0.05, hardware_reserve=0.05,
        interval_minutes=60, horizon_hours=8,
    )
    solar = [0.0, 2.0, 5.0, 8.0, 10.0, 12.0, 12.0, 10.0]
    builds = []
    original = optimizer._build_schedule

    def capture(*args, **kwargs):
        schedule = original(*args, **kwargs)
        builds.append((list(args[3]), list(args[4]), schedule))
        return schedule

    monkeypatch.setattr(optimizer, "_build_schedule", capture)
    result = optimizer.optimize(
        import_prices=[0.17] * 5 + [0.03, 0.02, 0.02],
        export_prices=[0.07, 0.04, 0.03, 0.03, 0.03, 0.0, -0.01, -0.01],
        solar_forecast=solar, load_forecast=[1.0] * 8, current_soc=0.05,
        allow_grid_charge=False, allow_battery_export=[True] * 8,
        schedule_timestamps=_timestamps(8), cost_neutral_earnings_cap=1.87,
        cost_neutral_slots=[True] * 8,
    )
    assert result.solver_used == "highs"
    assert result.lp_stats["mode_converged"]
    raw_charge, raw_discharge, schedule = builds[-1]
    for idx, action in enumerate(schedule.actions):
        assert min(raw_charge[idx], raw_discharge[idx]) < 1e-6
        assert raw_charge[idx] == pytest.approx(
            action.battery_charge_w / 1000.0, abs=2e-5,
        )
        assert action.action != "solar_export"
        assert action.soc >= 0.05 - 1e-6
    assert sum(result.schedule.battery_export_w[:5]) > 100
    assert not any(result.schedule.battery_export_w[5:])
    assert 0 < result.lp_stats["cost_neutral_planned_earnings"] <= 2.04


def test_cost_neutral_clipped_export_keeps_native_solar_charge(
    optimizer_module,
):
    """A clipped export witness must not discard available native PV charge."""
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=24_180,
        max_charge_w=12_600,
        max_discharge_w=14_400,
        max_grid_import_w=20_000,
        max_grid_export_w=5_000,
        efficiency=0.92,
        backup_reserve=0.05,
        hardware_reserve=0.02,
        interval_minutes=5,
        horizon_hours=1 / 12,
        terminal_weight=0.0,
    )
    plan = optimizer_module.CostNeutralPlan(
        day_ids=["2026-10-13"],
        earnings_caps_by_day={"2026-10-13": 1.87},
        forecast_import_costs_by_day={"2026-10-13": 0.0},
        fixed_cost_allowances_by_day={"2026-10-13": 1.87},
        settlement_import_prices=[0.28],
        settlement_export_prices=[0.2247],
        current_day="2026-10-13",
    )

    result = optimizer.optimize(
        import_prices=[0.28],
        export_prices=[0.2247],
        solar_forecast=[3.0],
        load_forecast=[2.0],
        current_soc=0.05,
        acquisition_cost_kwh=0.08833937,
        allow_battery_export=[True],
        allow_grid_charge=True,
        cost_neutral_plan=plan,
        schedule_timestamps=[
            datetime(2026, 10, 13, 11, tzinfo=timezone(timedelta(hours=10)))
        ],
    )

    action = result.schedule.actions[0]
    assert result.feasible is True
    assert result.lp_stats["mode_converged"] is True
    assert action.action == "self_consumption"
    assert action.battery_charge_w == pytest.approx(1_000.0)
    assert action.battery_discharge_w == pytest.approx(0.0)
    assert action.soc == pytest.approx(0.0532, abs=0.0001)
    assert result.grid_import_w == pytest.approx([0.0])
    assert result.grid_export_w == pytest.approx([0.0])
    assert result.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(0.0)


def test_cost_neutral_ev_sources_match_settled_battery_export(
    optimizer_module,
):
    """EV source metadata must follow the final export-capped dispatch."""
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=5_000, max_discharge_w=5_000,
        efficiency=1.0, backup_reserve=0.05, hardware_reserve=0.05,
        max_grid_import_w=10_000, interval_minutes=60, horizon_hours=2,
        terminal_weight=0.0,
    )
    ev = optimizer_module.EVChargePlan(
        vehicle_id="car", max_power_kw=(5.0, 0.0), energy_needed_kwh=5.0,
        charge_efficiency=1.0, min_power_kw=1.0,
        allow_grid=(True, True), allow_solar=(True, True),
        allow_battery=(True, True), initially_charging=True,
    )
    result = optimizer.optimize(
        import_prices=[0.2, 0.2], export_prices=[1.0, 0.0],
        solar_forecast=[5.0, 0.0], load_forecast=[1.0, 0.0], current_soc=1.0,
        allow_battery_export=[True, True], allow_grid_charge=False,
        schedule_timestamps=_timestamps(2), ev_plan=ev,
        cost_neutral_earnings_cap=0.6, cost_neutral_slots=[True, True],
    )

    action = result.schedule.actions[0]
    sources = result.ev_source_by_vehicle_w["car"]
    assert action.battery_discharge_w == pytest.approx(1_600.0)
    assert result.grid_export_w[0] == pytest.approx(600.0)
    assert sources["grid"][0] == pytest.approx(0.0)
    assert sources["solar"][0] == pytest.approx(4_000.0)
    assert sources["battery"][0] == pytest.approx(1_000.0)
    assert sum(sources[source][0] for source in sources) == pytest.approx(
        action.ev_charge_w,
    )
    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is True


def test_cost_neutral_two_evs_drop_inactive_solver_residual(optimizer_module):
    """Trace power on an inactive vehicle must not invalidate the LP schedule."""
    plans = [
        optimizer_module.EVChargePlan(
            vehicle_id=vehicle_id,
            max_power_kw=(4.0, 4.0, 0.0, 0.0),
            energy_needed_kwh=4.0,
            charge_efficiency=1.0,
            min_power_kw=1.0,
            allow_grid=(allow_non_solar,) * 4,
            allow_solar=(True,) * 4,
            allow_battery=(allow_non_solar,) * 4,
            initially_charging=True,
        )
        for vehicle_id, allow_non_solar in (("flexible", True), ("solar_only", False))
    ]
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.60, 0.31, 0.20, 0.0],
        export_prices=[0.50, 0.03, 0.50, 0.50],
        solar_forecast=[5.0, 3.0, 3.0, 3.0],
        load_forecast=[1.0, 5.0, 1.0, 5.0],
        current_soc=0.30,
        allow_battery_export=True,
        allow_grid_charge=False,
        schedule_timestamps=_timestamps(4),
        cost_neutral_earnings_cap=0.50,
        cost_neutral_slots=[True] * 4,
        ev_plan=plans,
    )

    assert result.solver_used == "highs"
    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is True
    flexible = result.ev_charge_by_vehicle_w["flexible"]
    solar_only = result.ev_charge_by_vehicle_w["solar_only"]
    assert flexible == pytest.approx([0.0, 4000.0, 0.0, 0.0], abs=0.01)
    assert solar_only == pytest.approx([4000.0, 0.0, 0.0, 0.0])
    assert flexible[0] == 0.0
    assert result.schedule.actions[0].ev_charge_w == pytest.approx(4000.0)
    sources = result.ev_source_by_vehicle_w
    assert sources["solar_only"]["grid"] == [0.0] * 4
    assert sources["solar_only"]["battery"] == [0.0] * 4
    assert sources["solar_only"]["solar"] == pytest.approx(solar_only)
    for vehicle_id, draw in result.ev_charge_by_vehicle_w.items():
        assert [sum(parts) for parts in zip(*sources[vehicle_id].values())] == pytest.approx(draw)


@pytest.mark.parametrize("residual_w,accepted", [(0.05, True), (0.20, False)])
def test_cost_neutral_ev_attribution_bounds_schedule_rounding(
    optimizer_module, residual_w, accepted,
):
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action="self_consumption", power_w=0.0,
            soc=0.5, battery_discharge_w=3500.0 - residual_w,
            ev_charge_w=4000.0,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
    )
    sources = module._reconcile_ev_sources_to_schedule(
        {"car": [4.0]}, {"car": {"solar": [0.5], "battery": [3.5]}},
        schedule, [1.5], [1.0], [residual_w / 1000.0],
    )
    if accepted:
        assert sources is not None
        assert sources["car"]["solar"] == pytest.approx([0.5])
        assert sources["car"]["battery"] == pytest.approx([3.5 - residual_w / 1000.0])
        assert sources["car"]["grid"] == [0.0]
    else:
        assert sources is None


@pytest.mark.parametrize(
    "allow_grid,import_limit,accepted",
    [(True, 14.0, True), (False, 14.0, False), (True, 12.0, False)],
    ids=["allowed-within-limit", "solar-only", "over-import-limit"],
)
def test_cost_neutral_ev_reassigns_existing_grid_import_after_projection(
    optimizer_module, allow_grid, import_limit, accepted,
):
    """Charging the home battery can use the solar first assigned to the car."""
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action="charge", power_w=12_000.0,
            soc=0.5, battery_charge_w=12_000.0, ev_charge_w=6_000.0,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
    )
    sources = module._reconcile_ev_sources_to_schedule(
        {"car": [6.0]}, {"car": {"solar": [6.0]}},
        schedule, [8.0], [3.0], [13.0],
        grid_reallocation_allowed={"car": [allow_grid]},
        max_grid_import_kw=import_limit,
    )
    if accepted:
        assert sources == {"car": {"grid": [6.0], "solar": [0.0], "battery": [0.0]}}
    else:
        assert sources is None


def test_cost_neutral_keeps_grid_enabled_ev_plan_during_free_battery_charge(
    optimizer_module,
):
    """The emitted battery charge and EV draw share the same import budget."""
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=7_000, max_discharge_w=5_000,
        max_grid_import_w=9_000, efficiency=0.92, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=30, horizon_hours=4,
        allow_greedy_fallback=False,
    )
    start = _timestamps(1)[0]
    result = optimizer.optimize(
        import_prices=[0.0, 0.30], export_prices=[0.05, 0.03],
        solar_forecast=[6.0, 0.0], load_forecast=[1.0, 1.0],
        current_soc=0.5, allow_battery_export=True, allow_grid_charge=True,
        cost_neutral_earnings_cap=0.60, cost_neutral_slots=[True, True],
        schedule_timestamps=[start, start + timedelta(minutes=30)],
        disable_idle=True,
        ev_plan=module.EVChargePlan(
            vehicle_id="car", max_power_kw=(3.6, 0.0),
            energy_needed_kwh=3.0, charge_efficiency=1.0, min_power_kw=1.0,
            allow_grid=(True, True), allow_battery=(True, True),
        ),
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is True
    assert result.schedule.actions[0].battery_charge_w == pytest.approx(7_000)
    assert result.ev_charge_by_vehicle_w["car"] == pytest.approx([3_600, 0])
    assert result.ev_source_by_vehicle_w["car"]["grid"] == pytest.approx([3_600, 0])
    assert result.grid_import_w[0] == pytest.approx(5_600)


@pytest.mark.parametrize("solar_only_first", [False, True])
def test_cost_neutral_free_charge_shares_solar_with_a_solar_only_car(
    optimizer_module, solar_only_first,
):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=2_000, max_discharge_w=5_000,
        max_grid_import_w=15_000, efficiency=0.92, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=30, horizon_hours=4,
        allow_greedy_fallback=False,
    )
    plans = [
        module.EVChargePlan(
            vehicle_id=vehicle_id, max_power_kw=(power, 0.0),
            energy_needed_kwh=power / 2, charge_efficiency=1.0,
            min_power_kw=1.0, allow_grid=(grid, grid),
            allow_solar=(True, True), allow_battery=(False, False),
        )
        for vehicle_id, power, grid in (
            ("flexible", 2.0, True), ("solar_only", 1.4, False)
        )
    ]
    start = _timestamps(1)[0]
    result = optimizer.optimize(
        import_prices=[0.0, 0.30], export_prices=[0.05, 0.03],
        solar_forecast=[5.0, 0.0], load_forecast=[1.0, 1.0],
        current_soc=0.5, allow_battery_export=True, allow_grid_charge=True,
        cost_neutral_earnings_cap=0.60, cost_neutral_slots=[True, True],
        schedule_timestamps=[start, start + timedelta(minutes=30)],
        disable_idle=True, ev_plan=plans[::-1] if solar_only_first else plans,
    )

    assert result.feasible and result.solver_used == "highs"
    assert result.schedule.actions[0].battery_charge_w == pytest.approx(2000)
    assert result.grid_import_w[0] == pytest.approx(1400)
    assert result.ev_charge_by_vehicle_w["flexible"][0] == pytest.approx(2000)
    assert result.ev_charge_by_vehicle_w["solar_only"][0] == pytest.approx(1400)
    sources = result.ev_source_by_vehicle_w
    assert sources["flexible"]["grid"][0] == pytest.approx(1400)
    assert sources["flexible"]["solar"][0] == pytest.approx(600)
    assert sources["solar_only"]["solar"][0] == pytest.approx(1400)
    assert sources["solar_only"]["grid"][0] == 0


@pytest.mark.parametrize(
    "grid_reallocation,site_cap,feasible",
    [(True, 1.4, True), (False, 1.4, False), (True, 1.3, False)],
)
def test_cost_neutral_source_swap_respects_grid_permissions_and_limit(
    optimizer_module, grid_reallocation, site_cap, feasible,
):
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action="charge", power_w=2000,
            soc=0.5, battery_charge_w=2000, ev_charge_w=3400,
        )], predicted_cost=0, predicted_savings=0,
    )
    sources = module._reconcile_ev_sources_to_schedule(
        {"flexible": [2.0], "solar_only": [1.4]},
        {"flexible": {"solar": [1.6], "grid": [0.4]},
         "solar_only": {"solar": [1.4]}},
        schedule, [5.0], [1.0], [1.4],
        grid_reallocation_allowed={"flexible": [grid_reallocation]},
        solar_reallocation_allowed={"flexible": [True], "solar_only": [True]},
        max_grid_import_kw=site_cap,
    )
    assert (sources is not None) is feasible


@pytest.mark.parametrize("battery_permission", [True, False])
def test_cost_neutral_can_reassign_through_two_other_cars(
    optimizer_module, battery_permission,
):
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action="self_consumption", power_w=0,
            soc=0.5, battery_discharge_w=1000, ev_charge_w=3000,
        )], predicted_cost=0, predicted_savings=0,
    )
    # One kW each of solar, grid and battery. The solar-only car needs the
    # first car to switch to grid, which requires the second to use battery.
    sources = module._reconcile_ev_sources_to_schedule(
        {"a": [1.0], "b": [1.0], "solar_only": [1.0]},
        {"a": {"solar": [1.0]},
         "b": {"grid": [0.9], "battery": [0.1 if battery_permission else 0.0]},
         "solar_only": {"solar": [1.0]}},
        schedule, [2.0], [1.0], [1.0],
        grid_reallocation_allowed={"a": [True]}, max_grid_import_kw=1.0,
    )
    if battery_permission:
        assert sources["solar_only"]["solar"] == pytest.approx([1.0])
        assert sources["a"]["grid"] == pytest.approx([1.0])
        assert sources["b"]["battery"] == pytest.approx([1.0])
    else:
        assert sources is None


@pytest.mark.parametrize("allow_solar", [True, False], ids=["solar-allowed", "solar-blocked"])
def test_cost_neutral_reserves_remaining_solar_for_the_grid_blocked_ev(
    optimizer_module, allow_solar,
):
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action="charge", power_w=1000.0,
            soc=0.5, battery_charge_w=1000.0, ev_charge_w=6000.0,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
    )
    sources = module._reconcile_ev_sources_to_schedule(
        {"flexible": [4.0], "grid_blocked": [2.0]},
        {"flexible": {"battery": [4.0]}, "grid_blocked": {"battery": [2.0]}},
        schedule, [4.0], [1.0], [4.0],
        grid_reallocation_allowed={"flexible": [True], "grid_blocked": [False]},
        solar_reallocation_allowed={"flexible": [True], "grid_blocked": [allow_solar]},
        max_grid_import_kw=4.0,
    )
    if allow_solar:
        assert sources == {
            "flexible": {"grid": [4.0], "solar": [0.0], "battery": [0.0]},
            "grid_blocked": {"grid": [0.0], "solar": [2.0], "battery": [0.0]},
        }
    else:
        assert sources is None


@pytest.mark.parametrize("use_highs", [True, False], ids=["lp-then-greedy", "greedy"])
def test_unreconciled_cost_neutral_ev_plan_cannot_force_grid_charge(
    optimizer_module, monkeypatch, use_highs,
):
    """An invalid joint plan must not authorize a paid battery dispatch."""
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", use_highs)
    optimizer = module.BatteryOptimizer(
        capacity_wh=27_000, max_charge_w=10_000, max_discharge_w=10_000,
        max_grid_import_w=18_000, efficiency=0.95, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=5, horizon_hours=1,
        terminal_weight=0.0,
    )
    ev = module.EVChargePlan(
        vehicle_id="car",
        max_power_kw=(3.45, 1.38, 1.38, 1.38, 0.0, 7.2, 7.2, 0.0),
        energy_needed_kwh=2.2, charge_efficiency=0.95, min_power_kw=1.0,
        allow_grid=(True, False, True, True, True, False, False, True),
        allow_solar=(True,) * 8,
        allow_battery=(True, True, True, False, True, False, True, True),
        initially_charging=True,
    )
    import_prices = [0.31, 0.65, 1.02, 0.8, 0.0, 0.45, 0.31, 0.8]
    export_prices = [0.0, 0.01, 1.0, 1.0, 0.01, 0.0, 0.01, 0.8]
    solar = [0.0, 0.0, 4.5, 4.5, 4.5, 0.0, 8.2, 8.2]
    load = [1.1, 2.9, 4.8, 4.8, 4.8, 0.4, 0.4, 8.1]
    result = optimizer.optimize(
        import_prices=import_prices, export_prices=export_prices,
        solar_forecast=solar, load_forecast=load, current_soc=0.98,
        allow_battery_export=True, allow_grid_charge=True,
        cost_neutral_earnings_cap=1.81, cost_neutral_slots=[True] * 8,
        ev_plan=ev, schedule_timestamps=[
            _timestamps(1)[0] + timedelta(minutes=5 * slot) for slot in range(8)
        ],
    )

    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is False
    if use_highs:
        assert result.lp_stats["message"] == (
            "final schedule cannot physically source the planned EV draw"
        )
    assert result.schedule.actions[0].action == "self_consumption"
    assert result.solver_used == "self_consumption_hold"
    assert result.feasible is False
    assert result.reserve_recommendation == {}
    assert result.ev_charge_by_vehicle_w == {}
    assert result.ev_source_by_vehicle_w == {}
    assert all(action.action == "self_consumption" for action in result.schedule.actions)
    assert result.grid_import_w[0] == pytest.approx(0.0)
    assert result.schedule.actions[0].battery_discharge_w == pytest.approx(1_100)

    # The final coordinator projection repeats settlement before publication.
    # A later projection must not resurrect dispatch or mark this plan valid.
    overlay = replace(result.schedule, actions=[
        replace(result.schedule.actions[0], action="charge", battery_charge_w=5_000),
        *result.schedule.actions[1:],
    ])
    projected = optimizer.reconcile_result_with_schedule(
        result, overlay, import_prices=import_prices,
        export_prices=export_prices, solar=solar, load=load, initial_soc=0.98,
        cost_neutral_earnings_cap=1.81, cost_neutral_slots=[True] * 8,
    )
    assert projected.lp_stats["ev_sources_reconciled_to_schedule"] is False
    assert projected.feasible is False
    assert all(action.action == "self_consumption" for action in projected.schedule.actions)


def test_cost_neutral_greedy_checks_ev_sources_without_small_exports(
    optimizer_module, monkeypatch,
):
    """The EV consistency check must run even with no export post-processing."""
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", False)
    optimizer = module.BatteryOptimizer(
        capacity_wh=27_000, max_charge_w=10_000, max_discharge_w=10_000,
        max_grid_import_w=18_000, efficiency=0.95, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=5, horizon_hours=1,
        terminal_weight=0.0,
    )
    ev = module.EVChargePlan(
        vehicle_id="car", max_power_kw=(7.2,) * 3, energy_needed_kwh=1.05,
        charge_efficiency=0.95, min_power_kw=1.0,
        allow_grid=(False, True, True), allow_solar=(True,) * 3,
        allow_battery=(True,) * 3, initially_charging=True,
    )
    result = optimizer.optimize(
        import_prices=[0.31, 0.45, 0.0], export_prices=[0.0] * 3,
        solar_forecast=[3.1, 0.0, 3.1], load_forecast=[8.1, 8.1, 4.8],
        current_soc=0.99, allow_battery_export=True, allow_grid_charge=True,
        cost_neutral_earnings_cap=0.5, cost_neutral_slots=[True] * 3,
        ev_plan=ev,
    )

    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is False
    assert not result.feasible
    assert all(action.action == "self_consumption" for action in result.schedule.actions)


def test_cost_neutral_caps_discretionary_battery_export_at_exact_earnings(
    optimizer_module,
):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.20] * 4,
        export_prices=[1.00] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
        current_soc=1.0,
        allow_battery_export=[True] * 4,
        priority_export_slots=[True] * 4,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(4),
        cost_neutral_earnings_cap=2.0,
        cost_neutral_slots=[True] * 4,
    )

    planned = sum(result.schedule.battery_export_w) / 1000.0
    assert planned == pytest.approx(2.0, abs=1e-4)
    assert result.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(2.0)
    assert max(result.schedule.battery_export_w) < 5_000


def test_cost_neutral_export_does_not_collapse_when_later_load_has_value(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)
    optimizer.update_config(backup_reserve=0.47)
    result = optimizer.optimize(
        import_prices=[0.51] * 4,
        export_prices=[0.10] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0, 0.0, 1.0, 1.0],
        current_soc=0.88,
        acquisition_cost_kwh=0.16,
        allow_battery_export=[True] * 4,
        priority_export_slots=[False] * 4,
        schedule_timestamps=_timestamps(4),
        cost_neutral_earnings_cap=0.20,
        cost_neutral_slots=[True, True, False, False],
    )

    assert sum(result.schedule.battery_export_w[:2]) / 1000.0 == pytest.approx(
        2.0,
        abs=1e-4,
    )
    assert result.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(
        0.20,
    )
    assert min(action.soc for action in result.schedule.actions) >= 0.47


def test_cost_neutral_prices_export_induced_later_import_in_same_constraint(
    optimizer_module,
):
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=1_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=60,
        horizon_hours=2,
        terminal_weight=0.0,
    )
    result = optimizer.optimize(
        import_prices=[0.20, 0.20],
        export_prices=[1.00, 0.00],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 1.0],
        current_soc=1.0,
        allow_battery_export=[True, False],
        priority_export_slots=[True, False],
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=1.0,
        cost_neutral_slots=[True, True],
        cost_neutral_forecast_import_cost=0.0,
    )

    exported_kwh = result.battery_to_grid_w[0] / 1000.0
    induced_import_kwh = result.grid_import_w[1] / 1000.0
    assert exported_kwh == pytest.approx(1.0, abs=1e-4)
    assert induced_import_kwh == pytest.approx(1.0, abs=1e-4)
    assert exported_kwh - 0.20 * induced_import_kwh == pytest.approx(0.8)
    assert result.lp_stats["cost_neutral_earnings_cap"] == pytest.approx(1.2)
    assert result.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(1.0)


def test_zero_baseline_cap_is_reopened_by_final_plan_import_cost(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)

    effective_cap = optimizer._cost_neutral_effective_earnings_cap(
        grid_import_kw=[2.0, 0.0],
        import_prices=[0.20, 0.20],
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        earnings_cap=0.0,
        forecast_import_cost=0.0,
        cost_neutral_slots=[True, True],
    )

    assert effective_cap == pytest.approx(0.40)


def test_prior_export_credit_is_consumed_before_final_plan_import_reopens_cap(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)

    effective_cap = optimizer._cost_neutral_effective_earnings_cap(
        grid_import_kw=[2.0, 0.0],
        import_prices=[0.20, 0.20],
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        earnings_cap=0.0,
        forecast_import_cost=0.0,
        cost_neutral_slots=[True, True],
        fixed_cost_allowance=-0.20,
    )

    assert effective_cap == pytest.approx(0.20)


def test_zero_cap_does_not_bootstrap_grid_import_to_fund_export(
    optimizer_module,
):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.20] * 2,
        export_prices=[1.00] * 2,
        solar_forecast=[0.0] * 2,
        load_forecast=[0.0] * 2,
        current_soc=1.0,
        allow_battery_export=[True] * 2,
        priority_export_slots=[True] * 2,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True] * 2,
        cost_neutral_forecast_import_cost=0.0,
        cost_neutral_fixed_cost_allowance=-0.20,
    )

    assert result.grid_import_w == pytest.approx([0.0, 0.0])
    assert result.schedule.battery_export_w == pytest.approx([0.0, 0.0])
    assert result.lp_stats["cost_neutral_earnings_cap"] == pytest.approx(0.0)


def test_positive_clipped_cap_with_prior_credit_stays_feasible_without_imports(
    optimizer_module,
):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.20] * 2,
        export_prices=[1.00] * 2,
        solar_forecast=[0.0] * 2,
        load_forecast=[0.0] * 2,
        current_soc=1.0,
        allow_battery_export=[True] * 2,
        priority_export_slots=[True] * 2,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=0.80,
        cost_neutral_slots=[True] * 2,
        cost_neutral_forecast_import_cost=1.00,
        cost_neutral_fixed_cost_allowance=-0.20,
    )

    assert result.feasible is True
    assert result.grid_import_w == pytest.approx([0.0, 0.0])
    assert result.schedule.battery_export_w == pytest.approx([0.0, 0.0])
    assert result.lp_stats["cost_neutral_earnings_cap"] == pytest.approx(0.0)


def test_zero_cap_reoptimizes_from_required_plan_import_with_static_cap(
    optimizer_module,
    monkeypatch,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(2)[0]
    solve_signature = inspect.signature(optimizer._solve_lp)
    calls: list[tuple[float | None, bool]] = []

    def _result(*, export_w: float, effective_cap: float):
        schedule = module.OptimizationSchedule(
            actions=[
                module.ScheduleAction(
                    timestamp=start,
                    action="charge",
                    power_w=4_000,
                    battery_charge_w=4_000,
                ),
                module.ScheduleAction(
                    timestamp=start + timedelta(hours=1),
                    action="export" if export_w else "self_consumption",
                    power_w=export_w,
                    battery_discharge_w=export_w,
                ),
            ],
            predicted_cost=0.0,
            predicted_savings=0.0,
            last_updated=start,
        )
        return module.OptimizerResult(
            schedule=schedule,
            solver_used="highs",
            grid_import_w=[4_000.0, 0.0],
            grid_export_w=[0.0, export_w],
            battery_to_grid_w=[0.0, export_w],
            lp_stats={
                "cost_neutral_earnings_cap": effective_cap,
                "cost_neutral_planned_earnings": export_w / 1000.0,
            },
        )

    def fake_solve(*args, **kwargs):
        bound = solve_signature.bind(*args, **kwargs)
        cap = bound.arguments["cost_neutral_earnings_cap"]
        static = not bound.arguments[
            "cost_neutral_account_for_planned_imports"
        ]
        calls.append((cap, static))
        return _result(
            export_w=0.0 if len(calls) == 1 else 600.0,
            effective_cap=0.60,
        )

    monkeypatch.setattr(optimizer, "_solve_lp", fake_solve)
    result = optimizer.optimize(
        import_prices=[0.20] * 2,
        export_prices=[0.0, 1.0],
        solar_forecast=[0.0] * 2,
        load_forecast=[0.0] * 2,
        current_soc=0.0,
        allow_battery_export=[False, True],
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True] * 2,
        cost_neutral_fixed_cost_allowance=-0.20,
    )

    assert calls == [(0.0, True), (0.60, True)]
    assert result.schedule.battery_export_w == pytest.approx([0.0, 600.0])
    assert result.lp_stats["cost_neutral_baseline_earnings_cap"] == 0.0
    assert result.lp_stats["cost_neutral_reconciliation_iterations"] == 1


def test_cost_neutral_keeps_executable_charge_when_budget_reconciliation_times_out(
    optimizer_module, monkeypatch,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.allow_greedy_fallback = False
    optimizer.update_config(backup_reserve=0.2)
    optimizer.pre_window_slot = 1
    optimizer.pre_window_soc_target = 0.4
    solve = optimizer._solve_lp
    solve_highs = module._solve_lp_highs
    solve_signature = inspect.signature(solve)
    passes = []

    def solve_budget(*args, **kwargs):
        passes.append(solve_signature.bind(*args, **kwargs).arguments[
            "cost_neutral_earnings_cap"
        ])
        return solve(*args, **kwargs)

    def time_out_on_reconciliation(*args, **kwargs):
        if len(passes) > 1:
            status = int(module.highspy.HighsModelStatus.kTimeLimit)
            return module._HighsResult(None, False, "Time limit reached", status, None)
        return solve_highs(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_solve_lp", solve_budget)
    monkeypatch.setattr(module, "_solve_lp_highs", time_out_on_reconciliation)
    result = optimizer.optimize(
        import_prices=[0.12, 0.65, 0.65],
        export_prices=[0.0, 0.0, 0.30],
        solar_forecast=[0.0] * 3,
        load_forecast=[1.0, 2.0, 0.0],
        current_soc=0.2,
        allow_grid_charge=True,
        grid_charge_allowed=[True, False, False],
        allow_battery_export=[False, False, True],
        schedule_timestamps=_timestamps(3),
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True] * 3,
        cost_neutral_fixed_cost_allowance=-0.02,
    )

    assert len(passes) == 2
    assert passes[0] == 0.0
    assert passes[1] > 0.0
    assert result.solver_used == "highs"
    assert result.feasible
    assert not result.degraded
    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.actions[0].battery_charge_w == pytest.approx(2_000.0, abs=1)
    assert result.schedule.battery_export_w == pytest.approx([0.0] * 3)
    assert result.schedule.actions[-1].soc == pytest.approx(0.2, abs=1e-4)
    assert result.lp_stats["cost_neutral_reconciliation_failure_status"] == int(
        module.highspy.HighsModelStatus.kTimeLimit
    )
    assert result.lp_stats["status"] == int(module.highspy.HighsModelStatus.kOptimal)


def test_final_plan_import_reopens_only_uncovered_prior_credit_balance(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(2)[0]
    schedule = module.OptimizationSchedule(
        actions=[
            module.ScheduleAction(
                timestamp=start,
                action="charge",
                power_w=4_000,
                battery_charge_w=4_000,
            ),
            module.ScheduleAction(
                timestamp=start + timedelta(hours=1),
                action="export",
                power_w=1_000,
                battery_discharge_w=1_000,
            ),
        ],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.20, 0.20],
        export_prices=[0.0, 1.0],
        solar=[0.0, 0.0],
        load=[0.0, 0.0],
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True, True],
        cost_neutral_forecast_import_cost=0.0,
        cost_neutral_fixed_cost_allowance=-0.20,
    )

    assert reconciled.grid_import_w == pytest.approx([4_000.0, 0.0])
    assert reconciled.schedule.battery_export_w == pytest.approx([0.0, 600.0])
    assert reconciled.lp_stats["cost_neutral_earnings_cap"] == pytest.approx(0.60)
    assert reconciled.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(
        0.60
    )


def test_zero_cap_preserves_natural_solar_export(optimizer_module):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.20] * 2,
        export_prices=[0.50] * 2,
        solar_forecast=[5.0] * 2,
        load_forecast=[0.0] * 2,
        current_soc=1.0,
        allow_battery_export=[True] * 2,
        priority_export_slots=[True] * 2,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True] * 2,
    )

    assert result.schedule.battery_export_w == [0.0, 0.0]
    assert result.grid_export_w == pytest.approx([5_000.0, 5_000.0])


def test_today_cap_does_not_apply_after_local_midnight(optimizer_module):
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.20] * 2,
        export_prices=[1.00] * 2,
        solar_forecast=[0.0] * 2,
        load_forecast=[0.0] * 2,
        current_soc=1.0,
        allow_battery_export=[True] * 2,
        priority_export_slots=[True] * 2,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_earnings_cap=0.0,
        cost_neutral_slots=[True, False],
    )

    assert result.schedule.battery_export_w[0] == 0.0
    assert result.schedule.battery_export_w[1] > 0.0


def test_final_schedule_guard_trims_overlay_without_touching_home_discharge(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=4_000,
            soc=0.5,
            battery_discharge_w=5_000,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )

    trimmed, earnings = optimizer.enforce_cost_neutral_schedule(
        schedule,
        export_prices=[1.0],
        solar=[0.0],
        load=[1.0],
        earnings_cap=0.5,
        cost_neutral_slots=[True],
    )

    assert earnings == pytest.approx(0.5)
    assert trimmed.actions[0].battery_discharge_w == pytest.approx(1_500.0)
    assert trimmed.actions[0].power_w == pytest.approx(500.0)


@pytest.mark.parametrize(
    ("battery_export_w", "expected_action"),
    [
        (99.9, "self_consumption"),
        (100.0, "self_consumption"),
        (100.01, "self_consumption"),
        (100.1, "export"),
    ],
)
def test_reconcile_uses_emitted_export_threshold(
    optimizer_module,
    battery_export_w,
    expected_action,
):
    """Rounded sub-threshold export must not survive as an export action."""
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.interval_minutes = 5
    optimizer.dt_hours = 5 / 60
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=battery_export_w,
            soc=0.5,
            battery_discharge_w=2_000.0 + battery_export_w,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.20],
        export_prices=[0.10],
        solar=[0.0],
        load=[2.0],
        initial_soc=0.5,
    )

    action = reconciled.schedule.actions[0]
    assert action.action == expected_action
    if expected_action == "self_consumption":
        assert action.battery_discharge_w == pytest.approx(2_000.0)
        assert reconciled.grid_export_w == pytest.approx([0.0])
        assert reconciled.battery_to_grid_w == pytest.approx([0.0])
    else:
        assert action.battery_discharge_w == pytest.approx(
            2_000.0 + battery_export_w
        )
        assert reconciled.grid_export_w == pytest.approx([battery_export_w])
        assert reconciled.battery_to_grid_w == pytest.approx([battery_export_w])


def test_cost_neutral_subthreshold_export_does_not_consume_earnings_cap(
    optimizer_module,
):
    """A converted export is not physical export and earns no settlement credit."""
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.interval_minutes = 5
    optimizer.dt_hours = 5 / 60
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=100.0,
            soc=0.5,
            battery_discharge_w=2_100.0,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.20],
        export_prices=[0.10],
        solar=[0.0],
        load=[2.0],
        initial_soc=0.5,
        cost_neutral_earnings_cap=1.0,
        cost_neutral_slots=[True],
    )

    action = reconciled.schedule.actions[0]
    assert action.action == "self_consumption"
    assert action.battery_discharge_w == pytest.approx(2_000.0)
    assert reconciled.grid_export_w == pytest.approx([0.0])
    assert reconciled.battery_to_grid_w == pytest.approx([0.0])
    assert reconciled.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(0.0)


def test_cost_neutral_dated_subthreshold_export_does_not_consume_day_credit(
    optimizer_module,
):
    """Dated settlement accounting must match the final zero-export action."""
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.interval_minutes = 5
    optimizer.dt_hours = 5 / 60
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=100.0,
            soc=0.5,
            battery_discharge_w=2_100.0,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01"],
        earnings_caps_by_day={"2026-08-01": 1.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 1.0},
        current_day="2026-08-01",
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.20],
        export_prices=[0.10],
        solar=[0.0],
        load=[2.0],
        initial_soc=0.5,
        cost_neutral_plan=plan,
    )

    assert reconciled.grid_export_w == pytest.approx([0.0])
    assert reconciled.lp_stats["cost_neutral_planned_earnings_by_day"] == {
        "2026-08-01": pytest.approx(0.0),
    }


def test_final_schedule_guard_values_only_remaining_capped_export_bonus(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=1_000,
            soc=0.5,
            battery_discharge_w=1_000,
        )],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )

    trimmed, earnings = optimizer.enforce_cost_neutral_schedule(
        schedule,
        export_prices=[0.10],
        solar=[0.0],
        load=[0.0],
        earnings_cap=1.0,
        cost_neutral_slots=[True],
        export_bonus_prices=[0.90],
        export_bonus_cap_kwh=0.10,
    )

    assert trimmed.actions[0].battery_discharge_w == pytest.approx(1_000.0)
    assert earnings == pytest.approx(0.19)


def test_multiday_plan_keeps_covered_today_separate_from_tomorrow(
    optimizer_module,
):
    module = optimizer_module
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-01", "2026-08-02", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        current_day="2026-08-01",
        timezone="UTC",
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20] * 4,
        export_prices=[1.00] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
        current_soc=1.0,
        allow_battery_export=[True] * 4,
        priority_export_slots=[True] * 4,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(4),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w[:2] == pytest.approx([0.0, 0.0])
    assert sum(result.schedule.battery_export_w[2:]) / 1000.0 == pytest.approx(
        1.0,
        abs=1e-4,
    )
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"] == {
        "2026-08-01": pytest.approx(0.0),
        "2026-08-02": pytest.approx(1.0),
    }


def test_multiday_import_accounting_is_selected_per_settlement_day(
    optimizer_module,
    monkeypatch,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    captured: list[bool | dict[str, bool]] = []
    solve_signature = inspect.signature(optimizer._solve_lp)
    start = _timestamps(2)[0]

    def fake_solve(*args, **kwargs):
        bound = solve_signature.bind(*args, **kwargs)
        captured.append(
            bound.arguments["cost_neutral_account_for_planned_imports"]
        )
        return module.OptimizerResult(
            schedule=module.OptimizationSchedule(
                actions=[
                    module.ScheduleAction(
                        timestamp=start + timedelta(hours=idx),
                        action="self_consumption",
                        power_w=0.0,
                    )
                    for idx in range(2)
                ],
                predicted_cost=0.0,
                predicted_savings=0.0,
                last_updated=start,
            ),
            solver_used="highs",
            grid_import_w=[0.0, 0.0],
            grid_export_w=[0.0, 0.0],
            battery_to_grid_w=[0.0, 0.0],
            lp_stats={
                "cost_neutral_earnings_caps_by_day": {
                    "2026-08-01": 0.0,
                    "2026-08-02": 1.0,
                }
            },
        )

    monkeypatch.setattr(optimizer, "_solve_lp", fake_solve)
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": -0.1, "2026-08-02": 1.0},
        current_day="2026-08-01",
    )

    optimizer.optimize(
        import_prices=[0.20, 0.20],
        export_prices=[0.10, 0.10],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=0.5,
        allow_battery_export=[False, False],
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert captured == [
        {"2026-08-01": False, "2026-08-02": True}
    ]


def test_multiday_final_guard_spends_each_local_day_cap_independently(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(4)[0]
    schedule = module.OptimizationSchedule(
        actions=[
            module.ScheduleAction(
                timestamp=start + timedelta(hours=idx),
                action="export",
                power_w=1_000,
                battery_discharge_w=1_000,
            )
            for idx in range(4)
        ],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=start,
    )
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-01", "2026-08-02", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.5, "2026-08-02": 1.5},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.5, "2026-08-02": 1.5},
        current_day="2026-08-01",
    )
    planned: dict[str, float] = {}

    trimmed, earnings = optimizer.enforce_cost_neutral_schedule(
        schedule,
        export_prices=[1.0] * 4,
        solar=[0.0] * 4,
        load=[0.0] * 4,
        earnings_cap=None,
        cost_neutral_slots=None,
        cost_neutral_plan=plan,
        planned_earnings_by_day=planned,
    )

    assert trimmed.battery_export_w == pytest.approx(
        [500.0, 0.0, 1_000.0, 500.0]
    )
    assert planned == {
        "2026-08-01": pytest.approx(0.5),
        "2026-08-02": pytest.approx(1.5),
    }
    assert earnings == pytest.approx(2.0)


def test_multiday_effective_caps_use_only_same_day_final_imports(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": -0.2, "2026-08-02": 0.0},
        current_day="2026-08-01",
    )

    caps = optimizer._cost_neutral_effective_earnings_caps_by_day(
        grid_import_kw=[2.0, 3.0],
        import_prices=[0.20, 0.20],
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        plan=plan,
    )

    assert caps == {
        "2026-08-01": pytest.approx(0.2),
        "2026-08-02": pytest.approx(0.6),
    }


def test_multiday_greedy_fallback_uses_the_same_daily_guards(
    optimizer_module,
    monkeypatch,
):
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", False)
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.5},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 0.5},
        current_day="2026-08-01",
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20, 0.20],
        export_prices=[1.00, 1.00],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        allow_battery_export=[True, True],
        priority_export_slots=[True, True],
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert result.solver_used == "greedy"
    assert result.schedule.battery_export_w == pytest.approx([0.0, 500.0])
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"] == {
        "2026-08-01": pytest.approx(0.0),
        "2026-08-02": pytest.approx(0.5),
    }


@pytest.mark.parametrize("use_greedy", [False, True])
@pytest.mark.parametrize("interval_minutes", [5, 60])
def test_cost_neutral_preserves_energy_for_later_higher_value_export(
    optimizer_module, monkeypatch, use_greedy, interval_minutes,
):
    """Surplus energy must not make an early 2c slot consume the daily cap."""
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", not use_greedy)
    optimizer = module.BatteryOptimizer(
        capacity_wh=40_000, max_charge_w=5_000, max_discharge_w=5_000,
        efficiency=1.0, backup_reserve=0.3, hardware_reserve=0.0,
        interval_minutes=interval_minutes, horizon_hours=4, terminal_weight=0.0,
    )
    cap = 0.27 * interval_minutes / 60.0
    start = _timestamps(1)[0]
    result = optimizer.optimize(
        import_prices=[0.45] * 4, export_prices=[0.02, 0.02, 0.27, 0.27],
        solar_forecast=[0.0] * 4, load_forecast=[0.0] * 4,
        current_soc=0.6, allow_grid_charge=False, allow_battery_export=True,
        schedule_timestamps=[start + timedelta(minutes=interval_minutes * i) for i in range(4)],
        cost_neutral_earnings_cap=cap,
        cost_neutral_slots=[True] * 4,
    )
    assert result.solver_used == ("greedy" if use_greedy else "highs")
    assert result.feasible
    assert result.schedule.battery_export_w[:2] == pytest.approx([0.0, 0.0])
    assert sum(result.schedule.battery_export_w[2:]) == pytest.approx(1_000.0)
    assert result.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(cap, abs=1e-6)


@pytest.mark.parametrize("use_greedy", [False, True])
@pytest.mark.parametrize("horizon_hours", [24, 48])
def test_cost_neutral_long_horizon_preserves_higher_value_export(
    optimizer_module, monkeypatch, use_greedy, horizon_hours,
):
    """Earlier-export tie breaking must not outweigh using less stored energy."""
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", not use_greedy)
    n = horizon_hours * 12
    start = datetime(2026, 11, 9, 11, tzinfo=timezone(timedelta(hours=10)))
    timestamps = [start + timedelta(minutes=5 * i) for i in range(n)]
    day_ids = [stamp.date().isoformat() for stamp in timestamps]
    days = set(day_ids)
    caps = {day: 0.27 if day == day_ids[0] else 0.0 for day in days}
    plan = module.CostNeutralPlan(
        day_ids=day_ids, earnings_caps_by_day=caps,
        forecast_import_costs_by_day={day: 0.0 for day in days},
        fixed_cost_allowances_by_day=caps, current_day=day_ids[0],
    )
    optimizer = module.BatteryOptimizer(
        capacity_wh=40_000, max_charge_w=5_000, max_discharge_w=5_000,
        efficiency=0.95, backup_reserve=0.32, hardware_reserve=0.0,
        interval_minutes=5, horizon_hours=horizon_hours, terminal_weight=0.0,
    )
    prices = [0.27 if 17 <= stamp.hour < 21 else 0.02 for stamp in timestamps]
    result = optimizer.optimize(
        import_prices=[0.45] * n, export_prices=prices,
        solar_forecast=[0.0] * n, load_forecast=[0.0] * n,
        current_soc=1.0, allow_grid_charge=False, allow_battery_export=True,
        schedule_timestamps=timestamps, cost_neutral_plan=plan,
    )
    assert result.solver_used == ("greedy" if use_greedy else "highs")
    assert result.feasible
    assert sum(
        power for power, price in zip(result.battery_to_grid_w, prices)
        if price == 0.02
    ) == pytest.approx(0.0, abs=0.1)
    assert sum(result.battery_to_grid_w) / 12_000 == pytest.approx(1.0, abs=1e-5)
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"] == {
        day: pytest.approx(cap, abs=1e-6) for day, cap in caps.items()
    }


@pytest.mark.parametrize("boundary", ["days", "manual", "bonus"])
def test_greedy_price_preference_preserves_settlement_boundaries(
    optimizer_module, boundary,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    start = _timestamps(1)[0]
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=start + timedelta(hours=i), action="export", power_w=1_000,
            soc=0.5, battery_discharge_w=1_000,
            control_source="manual" if boundary == "manual" and i == 1 else "optimizer",
        ) for i in range(2)],
        predicted_cost=0.0, predicted_savings=0.0, last_updated=start,
    )
    kwargs = dict(export_prices=[0.02, 0.27], solar=[0.0]*2, load=[0.0]*2,
                  earnings_cap=0.02, cost_neutral_slots=[True]*2)
    if boundary == "days":
        kwargs['cost_neutral_plan'] = module.CostNeutralPlan(
            day_ids=['today', 'tomorrow'],
            earnings_caps_by_day={'today': 0.02, 'tomorrow': 0.27},
            forecast_import_costs_by_day={}, fixed_cost_allowances_by_day={},
        )
    elif boundary == "bonus":
        kwargs.update(export_bonus_prices=[0.5, 0.5], export_bonus_cap_kwh=0.5)
    chronological, earned = optimizer.enforce_cost_neutral_schedule(schedule, **kwargs)
    preferred, preferred_earned = optimizer.enforce_cost_neutral_schedule(
        schedule, **kwargs, prefer_higher_prices=True,
    )
    assert preferred.actions == chronological.actions
    assert preferred_earned == pytest.approx(earned)
    assert [a.timestamp for a in preferred.actions] == [a.timestamp for a in schedule.actions]


def test_multiday_greedy_cost_neutral_can_use_low_value_export_slot(
    optimizer_module,
    monkeypatch,
):
    module = optimizer_module
    monkeypatch.setattr(module, "HIGHS_AVAILABLE", False)
    optimizer = _optimizer(module)
    optimizer.update_config(backup_reserve=0.5)
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        current_day="2026-08-01",
    )

    result = optimizer.optimize(
        import_prices=[0.50] * 3,
        export_prices=[0.10] * 3,
        solar_forecast=[0.0] * 3,
        load_forecast=[0.0, 0.0, 1.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.30,
        allow_battery_export=[False, True, False],
        schedule_timestamps=_timestamps(3),
        cost_neutral_plan=plan,
    )

    assert result.solver_used == "greedy"
    assert result.schedule.battery_export_w[1] == pytest.approx(2_000.0)
    assert sum(
        action.battery_discharge_w
        for action in result.schedule.actions
    ) <= 5_000.0
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"][
        "2026-08-02"
    ] == pytest.approx(0.2)


def test_four4free_can_charge_then_cover_tomorrows_cost_neutral_budget(
    optimizer_module,
):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.0,
        hardware_reserve=0.0,
        interval_minutes=60,
        horizon_hours=6,
        terminal_weight=0.0,
    )
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01"] * 2 + ["2026-08-02"] * 4,
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        current_day="2026-08-01",
        timezone="Australia/Sydney",
    )

    result = optimizer.optimize(
        import_prices=[0.30, 0.30, 0.0, 0.0, 0.30, 0.30],
        export_prices=[0.10] * 6,
        solar_forecast=[0.0] * 6,
        load_forecast=[0.0] * 6,
        current_soc=0.0,
        allow_battery_export=[False, False, False, False, True, True],
        schedule_timestamps=_timestamps(6),
        cost_neutral_plan=plan,
    )

    assert sum(
        action.battery_charge_w
        for action in result.schedule.actions[2:4]
    ) > 0.0
    assert result.schedule.battery_export_w[:2] == pytest.approx([0.0, 0.0])
    assert sum(result.schedule.battery_export_w[4:]) / 1000.0 == pytest.approx(
        2.0,
        abs=1e-4,
    )
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"][
        "2026-08-02"
    ] == pytest.approx(0.2)


def test_zerohero_daily_bonus_quota_and_cost_neutral_days_coexist(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.set_quota_bonus_groups(
        import_group_ids=None,
        import_caps_by_group=None,
        export_group_ids=["2026-08-01"] * 2 + ["2026-08-02"] * 2,
        export_caps_by_group={"2026-08-01": 0.0, "2026-08-02": 1.0},
    )
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01"] * 2 + ["2026-08-02"] * 2,
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        current_day="2026-08-01",
        timezone="Australia/Sydney",
    )

    result = optimizer.optimize(
        import_prices=[0.30] * 4,
        export_prices=[0.10] * 4,
        export_bonus_prices=[0.90] * 4,
        export_bonus_cap_kwh=1.0,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
        current_soc=1.0,
        allow_battery_export=[True] * 4,
        priority_export_slots=[True] * 4,
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(4),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w[:2] == pytest.approx([0.0, 0.0])
    assert sum(result.schedule.battery_export_w[2:]) / 1000.0 == pytest.approx(
        1.0,
        abs=1e-4,
    )
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"][
        "2026-08-02"
    ] == pytest.approx(1.0)


def test_multiday_plan_preserves_reserve_floor_and_site_export_limit(
    optimizer_module,
):
    module = optimizer_module
    optimizer = _optimizer(module)
    optimizer.update_config(backup_reserve=0.5)
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02", "2026-08-02", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 5.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 5.0},
        current_day="2026-08-01",
    )

    result = optimizer.optimize(
        import_prices=[0.20] * 4,
        export_prices=[1.00] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
        current_soc=1.0,
        allow_battery_export=[True] * 4,
        priority_export_slots=[True] * 4,
        priority_export_enabled=True,
        grid_export_limits_w=[0.0, 2_000.0, 2_000.0, 2_000.0],
        schedule_timestamps=_timestamps(4),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w[0] == 0.0
    assert max(result.schedule.battery_export_w[1:]) <= 2_000.0
    assert sum(result.schedule.battery_export_w[1:]) / 1000.0 == pytest.approx(
        5.0,
        abs=1e-4,
    )
    assert min(action.soc for action in result.schedule.actions) >= 0.5


@pytest.mark.parametrize("fixed_allowance", [-0.2, 0.0])
def test_multiday_zero_caps_do_not_bootstrap_imports_or_cross_dates(
    optimizer_module,
    fixed_allowance,
):
    module = optimizer_module
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={
            "2026-08-01": fixed_allowance,
            "2026-08-02": fixed_allowance,
        },
        current_day="2026-08-01",
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20, 0.20],
        export_prices=[1.00, 1.00],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        allow_battery_export=[True, True],
        priority_export_slots=[True, True],
        priority_export_enabled=True,
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert result.grid_import_w == pytest.approx([0.0, 0.0])
    assert result.schedule.battery_export_w == pytest.approx([0.0, 0.0])
    assert result.lp_stats["cost_neutral_earnings_caps_by_day"] == {
        "2026-08-01": pytest.approx(0.0),
        "2026-08-02": pytest.approx(0.0),
    }


def test_multiday_zero_cap_preserves_natural_solar_export_on_each_date(
    optimizer_module,
):
    module = optimizer_module
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        current_day="2026-08-01",
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20, 0.20],
        export_prices=[0.50, 0.50],
        solar_forecast=[2.0, 2.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        allow_battery_export=[True, True],
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w == pytest.approx([0.0, 0.0])
    assert result.grid_export_w == pytest.approx([2_000.0, 2_000.0])


def test_multiday_plan_cannot_export_when_permission_mask_is_false(
    optimizer_module,
):
    module = optimizer_module
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 1.0},
        current_day="2026-08-01",
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20, 0.20],
        export_prices=[1.00, 1.00],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        allow_battery_export=[False, False],
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w == pytest.approx([0.0, 0.0])


def test_multiday_constraints_use_billable_rates_not_lp_export_boost(
    optimizer_module,
):
    module = optimizer_module
    plan = module.CostNeutralPlan(
        day_ids=["2026-08-01", "2026-08-02"],
        earnings_caps_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        forecast_import_costs_by_day={"2026-08-01": 0.0, "2026-08-02": 0.0},
        fixed_cost_allowances_by_day={"2026-08-01": 0.0, "2026-08-02": 0.2},
        current_day="2026-08-01",
        settlement_import_prices=[0.20, 0.20],
        settlement_export_prices=[0.10, 0.10],
    )

    result = _optimizer(module).optimize(
        import_prices=[0.20, 0.20],
        export_prices=[1.00, 1.00],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        allow_battery_export=[False, True],
        schedule_timestamps=_timestamps(2),
        cost_neutral_plan=plan,
    )

    assert result.schedule.battery_export_w == pytest.approx([0.0, 2_000.0])
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"][
        "2026-08-02"
    ] == pytest.approx(0.2)


def test_amber_confidence_changes_decision_value_but_cost_neutral_uses_raw_earnings(optimizer_module):
    policy = importlib.import_module('power_sync.optimization.price_forecast_confidence')
    timestamps = _timestamps(5)
    raw = [10.0, 0.05, 24.0, 0.05, 0.05]
    intervals = [policy.AmberPriceInterval(t, t + 1, ts, 'CurrentInterval' if t == 0 else 'ForecastInterval') for t, ts in enumerate(timestamps)]
    weighted, _ = policy.weight_amber_export_forecast(raw, intervals, timestamps[0])
    optimizer = optimizer_module.BatteryOptimizer(capacity_wh=10000, max_charge_w=5000, max_discharge_w=5000,
        backup_reserve=0.2, hardware_reserve=0.2, efficiency=1.0,
        interval_minutes=60, horizon_hours=5, terminal_weight=0)
    day = '2026-08-01'
    plan = optimizer_module.CostNeutralPlan(day_ids=[day]*5, earnings_caps_by_day={day: 20},
        forecast_import_costs_by_day={day: 0}, fixed_cost_allowances_by_day={day: 20}, current_day=day,
        settlement_import_prices=[0.3]*5, settlement_export_prices=raw)
    result = optimizer.optimize(import_prices=[0.3]*5, export_prices=weighted,
        solar_forecast=[0]*5, load_forecast=[0]*5, current_soc=0.7,
        allow_grid_charge=False, allow_battery_export=True,
        cost_neutral_plan=plan, schedule_timestamps=timestamps)
    assert result.feasible
    actual_earnings = sum(w/1000*p for w, p in zip(result.battery_to_grid_w, raw))
    assert actual_earnings == pytest.approx(20, abs=0.05)
    assert result.battery_to_grid_w[0] == pytest.approx(2000, abs=5)
    assert max(result.battery_to_grid_w[1:]) < 5
    assert plan.settlement_export_prices == raw


@pytest.mark.parametrize("interval_minutes,n,allowance", [(60, 4, 0.15), (5, 12, 0.03)])
@pytest.mark.parametrize("disable_idle", [False, True])
@pytest.mark.parametrize("initial_soc", [0.5, 0.99])
def test_cost_neutral_counts_final_solar_credit_before_battery_export(
    optimizer_module, interval_minutes, n, allowance, disable_idle, initial_soc,
):
    """Export mode must not earn a full battery allowance on top of new solar credit."""
    module = optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    optimizer = _optimizer(module)
    optimizer.interval_minutes = interval_minutes
    optimizer.dt_hours = interval_minutes / 60
    optimizer.horizon_hours = n * optimizer.dt_hours
    start = _timestamps(1)[0]
    timestamps = [start + timedelta(minutes=interval_minutes * idx) for idx in range(n)]
    day = start.date().isoformat()
    plan = module.CostNeutralPlan(
        day_ids=[day] * n,
        earnings_caps_by_day={day: allowance},
        forecast_import_costs_by_day={day: 0.0},
        fixed_cost_allowances_by_day={day: allowance},
        forecast_natural_export_earnings_by_day={day: 0.0},
        settlement_import_prices=[0.10] * n,
        settlement_export_prices=[0.08] * n,
    )
    result = optimizer.optimize(
        import_prices=[0.10] * n,
        export_prices=[0.08] * n,
        solar_forecast=[1.5] * n,
        load_forecast=[1.0] * n,
        current_soc=initial_soc,
        allow_battery_export=[True] * n,
        schedule_timestamps=timestamps,
        disable_idle=disable_idle,
        cost_neutral_plan=plan,
    )

    assert result.solver_used == "highs"
    assert result.feasible
    assert result.lp_stats["mode_converged"] is True
    credit = sum(result.grid_export_w) / 1000 * optimizer.dt_hours * 0.08
    imports = sum(result.grid_import_w) / 1000 * optimizer.dt_hours * 0.10
    battery_credit = sum(result.battery_to_grid_w) / 1000 * optimizer.dt_hours * 0.08
    assert imports == pytest.approx(0.0, abs=1e-5)
    if initial_soc == 0.99:
        # Capacity-limited solar export can exceed the target, freely. There
        # must be no discretionary battery discharge alongside that surplus.
        assert credit > allowance
        assert battery_credit == pytest.approx(0.0)
    else:
        assert 0.0 < battery_credit < allowance
        assert credit == pytest.approx(allowance, abs=1e-5)
        assert any(action.battery_charge_w > 100 for action in result.schedule.actions)
        if interval_minutes == 60:
            assert sum(action.action == "export" for action in result.schedule.actions) == 1
            assert result.schedule.actions[-1].soc == pytest.approx(0.5125, abs=1e-4)
    assert result.lp_stats["cost_neutral_planned_earnings_by_day"][day] == pytest.approx(
        battery_credit, abs=1e-5,
    )
    assert result.lp_stats["cost_neutral_earnings_caps_by_day"][day] == pytest.approx(
        max(0.0, allowance + imports - (credit - battery_credit)), abs=1e-5,
    )


@pytest.mark.parametrize("force_greedy", [False, True])
def test_final_solar_credit_guard_handles_dated_capped_bonuses(
    optimizer_module, monkeypatch, force_greedy,
):
    """Future-day solar credit and chronological bonus quota cannot be spent twice."""
    module = optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    if force_greedy:
        monkeypatch.setattr(module, "HIGHS_AVAILABLE", False)
    optimizer = _optimizer(module)
    optimizer.horizon_hours = 6
    start = datetime(2026, 8, 1, 21, tzinfo=timezone.utc)
    timestamps = [start + timedelta(hours=i) for i in range(6)]
    day_ids = [x.date().isoformat() for x in timestamps]
    days = sorted(set(day_ids))
    plan = module.CostNeutralPlan(
        day_ids=day_ids,
        earnings_caps_by_day={day: 0.10 for day in days},
        forecast_import_costs_by_day={day: 0.0 for day in days},
        fixed_cost_allowances_by_day={day: 0.10 for day in days},
        forecast_natural_export_earnings_by_day={day: 0.0 for day in days},
        settlement_import_prices=[0.20] * 6,
        settlement_export_prices=[0.08] * 6,
    )
    result = optimizer.optimize(
        import_prices=[0.20] * 6, export_prices=[0.08] * 6,
        solar_forecast=[2.0] * 6, load_forecast=[1.0] * 6,
        current_soc=0.5, allow_battery_export=[True] * 6,
        schedule_timestamps=timestamps, disable_idle=True,
        cost_neutral_plan=plan,
        export_bonus_prices=[0.10] * 6, export_bonus_cap_kwh=0.5,
    )
    assert result.feasible
    bonus_remaining = 0.5
    for day in days:
        total_credit = 0.0
        battery_credit = 0.0
        for i, action in enumerate(result.schedule.actions):
            if day_ids[i] != day:
                continue
            export_kwh = result.grid_export_w[i] / 1000
            natural_kwh = max(0.0, export_kwh - result.battery_to_grid_w[i] / 1000)
            total_bonus_kwh = min(export_kwh, bonus_remaining)
            natural_bonus_kwh = min(natural_kwh, total_bonus_kwh)
            total_credit += export_kwh * 0.08 + total_bonus_kwh * 0.10
            battery_credit += (
                result.battery_to_grid_w[i] / 1000 * 0.08
                + (total_bonus_kwh - natural_bonus_kwh) * 0.10
            )
            bonus_remaining -= total_bonus_kwh
        imports = sum(
            result.grid_import_w[i] / 1000 * 0.20
            for i in range(6) if day_ids[i] == day
        )
        assert battery_credit <= max(0.0, 0.10 + imports - (total_credit - battery_credit)) + 1e-5
        assert result.lp_stats["cost_neutral_planned_earnings_by_day"][day] == pytest.approx(
            battery_credit, abs=1e-5,
        )


@pytest.mark.parametrize("solar_kw", [0.0, 2.0])
def test_final_solar_budget_preserves_manual_export_and_blocks_extra_export(
    optimizer_module, solar_kw,
):
    module = optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")
    optimizer = _optimizer(module)
    timestamps = _timestamps(4)
    day = timestamps[0].date().isoformat()
    plan = module.CostNeutralPlan(
        day_ids=[day] * 4, earnings_caps_by_day={day: 0.10},
        forecast_import_costs_by_day={day: 0.0},
        fixed_cost_allowances_by_day={day: 0.10},
        forecast_natural_export_earnings_by_day={day: 0.0},
        settlement_import_prices=[0.20] * 4,
        settlement_export_prices=[0.10] * 4,
    )
    result = optimizer.optimize(
        import_prices=[0.20] * 4, export_prices=[0.10] * 4,
        solar_forecast=[solar_kw] * 4, load_forecast=[0.0] * 4,
        current_soc=0.8, allow_battery_export=[True] * 4,
        schedule_timestamps=timestamps, cost_neutral_plan=plan,
        manual_control={
            "control_type": "force_discharge",
            "mode_slots": ["export", None, None, None],
            "required_charge_kw": [0.0] * 4,
            "required_discharge_kw": [2.0, 0.0, 0.0, 0.0],
        },
    )
    assert result.feasible
    assert result.schedule.actions[0].control_source == "manual"
    assert result.schedule.actions[0].battery_discharge_w == pytest.approx(2000)
    assert result.battery_to_grid_w[0] == pytest.approx(2000)
    assert result.battery_to_grid_w[1:] == pytest.approx([0.0] * 3)


@pytest.mark.parametrize(
    ("grid_charge_allowed", "action_name", "solar", "grid", "site_limit", "feasible"),
    [
        (True, "charge", 7.0, 10.0, 10.0, True),
        (False, "charge", 7.0, 10.0, 10.0, False),
        (True, "self_consumption", 7.0, 10.0, 10.0, False),
        (True, "charge", 6.0, 11.0, 12.0, False),
        (True, "charge", 7.0, 10.0, 9.0, False),
        (True, "charge", 7.0, 9.0, 10.0, False),
    ],
)
def test_grid_charging_home_battery_can_share_solar_with_solar_only_ev(
    optimizer_module, grid_charge_allowed, action_name, solar, grid, site_limit, feasible,
):
    module = optimizer_module
    schedule = module.OptimizationSchedule(
        actions=[module.ScheduleAction(
            timestamp=_timestamps(1)[0], action=action_name, power_w=10_000.0,
            soc=0.5, battery_charge_w=10_000.0, ev_charge_w=4_000.0,
        )],
        predicted_cost=0.0, predicted_savings=0.0,
    )
    sources = module._reconcile_ev_sources_to_schedule(
        {"solar_car": [4.0]}, {"solar_car": {"solar": [4.0]}},
        schedule, [solar], [3.0], [grid],
        grid_reallocation_allowed={"solar_car": [False]},
        solar_reallocation_allowed={"solar_car": [True]},
        max_grid_import_kw=site_limit, battery_grid_charge_allowed=[grid_charge_allowed],
    )
    if feasible:
        assert sources == {"solar_car": {"grid": [0.0], "solar": [4.0], "battery": [0.0]}}
    else:
        assert sources is None


def test_free_grid_charge_keeps_joint_plan_for_solar_only_ev(optimizer_module):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=16_000, max_charge_w=10_000, max_discharge_w=9_000,
        max_grid_import_w=10_000, efficiency=0.92, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=30, horizon_hours=4,
        allow_greedy_fallback=False,
    )
    car = module.EVChargePlan(
        vehicle_id="solar_car", max_power_kw=(4, 0, 0, 0),
        energy_needed_kwh=2, charge_efficiency=1, min_power_kw=1,
        allow_solar=(True,) * 4, allow_grid=(False,) * 4, allow_battery=(False,) * 4,
    )
    start = _timestamps(1)[0]
    result = optimizer.optimize(
        import_prices=[0, 0.6, 0.6, 0.6], export_prices=[0] * 4,
        solar_forecast=[7, 0, 0, 0], load_forecast=[3, 6, 6, 6],
        current_soc=0.24, allow_grid_charge=True, allow_battery_export=True,
        cost_neutral_earnings_cap=0.5, cost_neutral_slots=[True] * 4,
        disable_idle=True, ev_plan=car,
        schedule_timestamps=[start + timedelta(minutes=30 * i) for i in range(4)],
    )
    assert result.feasible and result.solver_used == "highs"
    result = optimizer.reconcile_result_with_schedule(
        result, result.schedule, import_prices=[0, 0.6, 0.6, 0.6],
        export_prices=[0] * 4, solar=[7, 0, 0, 0], load=[3, 6, 6, 6],
        initial_soc=0.24, cost_neutral_earnings_cap=0.5,
        cost_neutral_slots=[True] * 4,
    )
    assert result.feasible and not result.degraded
    first = result.schedule.actions[0]
    assert first.action == "charge"
    assert 0 < first.battery_charge_w <= 10_000
    assert first.ev_charge_w == pytest.approx(4_000)
    assert result.ev_source_by_vehicle_w["solar_car"] == {
        "solar": pytest.approx([4_000, 0, 0, 0]),
        "battery": [0.0] * 4, "grid": [0.0] * 4,
    }


@pytest.mark.parametrize("cost_neutral", [True, False], ids=["cost-neutral", "economic"])
@pytest.mark.parametrize("grid_charge_allowed", [False, True], ids=["solar-battery", "grid-battery"])
def test_joint_solar_ev_and_battery_charge_reserve_household_pv(
    optimizer_module, cost_neutral, grid_charge_allowed,
):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=3_000, max_discharge_w=5_000,
        max_grid_import_w=9_000, efficiency=0.92, backup_reserve=0.2,
        hardware_reserve=0.0, interval_minutes=30, horizon_hours=1,
        allow_greedy_fallback=False,
    )
    ev = module.EVChargePlan(
        vehicle_id="solar_car", max_power_kw=(3.0, 0.0),
        energy_needed_kwh=1.5, charge_efficiency=1.0, min_power_kw=1.0,
        allow_grid=(False, False), allow_solar=(True, True), allow_battery=(False, False),
    )
    start = _timestamps(1)[0]
    result = optimizer.optimize(
        import_prices=[0.0 if grid_charge_allowed else 0.05, 0.70], export_prices=[0, 0],
        solar_forecast=[6.0, 0.0], load_forecast=[2.0, 2.0], current_soc=0.6,
        allow_battery_export=True, allow_grid_charge=grid_charge_allowed,
        schedule_timestamps=[start, start + timedelta(minutes=30)], ev_plan=ev,
        cost_neutral_earnings_cap=0.7 if cost_neutral else None,
        cost_neutral_slots=[cost_neutral, cost_neutral],
    )

    assert result.feasible and result.solver_used == "highs"
    first = result.schedule.actions[0]
    assert first.ev_charge_w == pytest.approx(3000)
    assert result.ev_source_by_vehicle_w["solar_car"]["solar"] == pytest.approx([3000, 0])
    assert result.ev_source_by_vehicle_w["solar_car"]["grid"] == [0, 0]
    if grid_charge_allowed:
        assert first.action == "charge"
        assert first.battery_charge_w == pytest.approx(3000)
        assert result.grid_import_w[0] == pytest.approx(2000)
    else:
        assert first.action == "self_consumption"
        assert first.battery_charge_w == pytest.approx(1000)
        assert result.grid_import_w[0] == pytest.approx(0)


def test_joint_ev_solar_budget_keeps_separate_solar_curtailment(optimizer_module):
    module = optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000, max_charge_w=3_000, max_discharge_w=5_000,
        max_grid_import_w=9_000, efficiency=0.92, backup_reserve=0.2,
        interval_minutes=30, horizon_hours=1, allow_greedy_fallback=False,
    )
    ev = module.EVChargePlan(
        vehicle_id="solar_car", max_power_kw=(3.0, 3.0),
        energy_needed_kwh=1.5, charge_efficiency=1.0, min_power_kw=1.0,
        allow_grid=(False, False), allow_solar=(True, True), allow_battery=(False, False),
    )
    result = optimizer.optimize(
        import_prices=[-0.8, 0.2], export_prices=[0, 0],
        solar_forecast=[2.0, 6.0], load_forecast=[2.0, 2.0], current_soc=1.0,
        allow_battery_export=False, allow_grid_charge=False, ev_plan=ev,
    )

    assert result.feasible and result.solver_used == "highs"
    assert result.solar_curtailment_w == pytest.approx([2000, 0])
    assert result.ev_source_by_vehicle_w["solar_car"]["solar"] == pytest.approx([0, 3000])
