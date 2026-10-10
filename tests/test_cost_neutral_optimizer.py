"""Regression tests for the Cost Neutral battery-export earnings cap."""

from __future__ import annotations

import importlib
import inspect
import sys
import types
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
        cost_neutral_earnings_cap=0.2, cost_neutral_slots=[True, True],
    )

    action = result.schedule.actions[0]
    sources = result.ev_source_by_vehicle_w["car"]
    assert action.battery_discharge_w == pytest.approx(1_200.0)
    assert result.grid_export_w[0] == pytest.approx(200.0)
    assert sources["grid"][0] == pytest.approx(0.0)
    assert sources["solar"][0] == pytest.approx(4_000.0)
    assert sources["battery"][0] == pytest.approx(1_000.0)
    assert sum(sources[source][0] for source in sources) == pytest.approx(
        action.ev_charge_w,
    )
    assert result.lp_stats["ev_sources_reconciled_to_schedule"] is True


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
                    power_w=2_000,
                    battery_charge_w=2_000,
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
            grid_import_w=[2_000.0, 0.0],
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
            export_w=0.0 if len(calls) == 1 else 200.0,
            effective_cap=0.20,
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

    assert calls == [(0.0, True), (0.20, True)]
    assert result.schedule.battery_export_w == pytest.approx([0.0, 200.0])
    assert result.lp_stats["cost_neutral_baseline_earnings_cap"] == 0.0
    assert result.lp_stats["cost_neutral_reconciliation_iterations"] == 1


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
                power_w=2_000,
                battery_charge_w=2_000,
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

    assert reconciled.grid_import_w == pytest.approx([2_000.0, 0.0])
    assert reconciled.schedule.battery_export_w == pytest.approx([0.0, 200.0])
    assert reconciled.lp_stats["cost_neutral_earnings_cap"] == pytest.approx(0.20)
    assert reconciled.lp_stats["cost_neutral_planned_earnings"] == pytest.approx(
        0.20
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
