"""Regression tests for the Cost Neutral battery-export earnings cap."""

from __future__ import annotations

import importlib
import inspect
import json
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


@pytest.mark.parametrize("interval_minutes,n,allowance", [(60, 4, 0.10), (5, 12, 0.025)])
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
        solar_forecast=[2.0] * n,
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
            assert result.schedule.actions[-1].soc == pytest.approx(0.775, abs=1e-4)
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
