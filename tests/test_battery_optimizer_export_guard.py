"""Regression tests for battery-to-grid export gating."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_SENTINEL = object()

_STUB_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.schedule_reader",
)


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc
    ha_util.dt = ha_dt
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = ps_module

    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync.optimization"] = optimization_module


@pytest.fixture()
def battery_optimizer_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_stubs()
    module = importlib.import_module("power_sync.optimization.battery_optimizer")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _optimizer(module):
    return module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )


def test_grid_import_limit_caps_grid_sourced_charge(battery_optimizer_module):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=100000,
        max_charge_w=13600,
        max_discharge_w=13600,
        max_grid_import_w=11100,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )
    n = 12

    result = optimizer.optimize(
        import_prices=[0.0] * 6 + [0.50] * 6,
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[1.0] * n,
        current_soc=0.20,
        allow_grid_charge=True,
    )

    assert max(result.grid_import_w) <= 11100.1
    assert max(action.battery_charge_w for action in result.schedule.actions) == pytest.approx(
        10100.0,
        abs=0.1,
    )


def test_grid_import_limit_still_allows_solar_assisted_full_charge(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=100000,
        max_charge_w=13600,
        max_discharge_w=13600,
        max_grid_import_w=11100,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )
    n = 12

    result = optimizer.optimize(
        import_prices=[0.0] * 6 + [0.50] * 6,
        export_prices=[0.0] * n,
        solar_forecast=[5.0] * n,
        load_forecast=[1.0] * n,
        current_soc=0.20,
        allow_grid_charge=True,
    )

    assert max(result.grid_import_w) <= 11100.1
    assert max(action.battery_charge_w for action in result.schedule.actions) == pytest.approx(
        13600.0,
        abs=0.1,
    )


def test_self_consumption_schedule_soc_uses_hardware_floor_above_optimizer_reserve(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.10,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )
    n = 12

    schedule = optimizer._build_schedule(
        n=n,
        grid_import=[5.0] * n,
        grid_export=[0.0] * n,
        battery_charge=[0.0] * n,
        battery_discharge=[0.0] * n,
        solar=[0.0] * n,
        load=[5.0] * n,
        soc_0=0.20,
        import_prices=[0.50] * n,
        export_prices=[0.0] * n,
    )

    assert min(action.soc for action in schedule.actions) >= 0.05
    assert schedule.actions[-1].soc == pytest.approx(0.05, abs=0.001)


def test_self_consumption_schedule_uses_hardware_floor_when_below_optimizer_reserve(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.10,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )
    n = 12

    schedule = optimizer._build_schedule(
        n=n,
        grid_import=[5.0] * n,
        grid_export=[0.0] * n,
        battery_charge=[0.0] * n,
        battery_discharge=[0.0] * n,
        solar=[0.0] * n,
        load=[5.0] * n,
        soc_0=0.07,
        import_prices=[0.50] * n,
        export_prices=[0.0] * n,
    )

    assert min(action.soc for action in schedule.actions) >= 0.05
    assert schedule.actions[-1].soc == pytest.approx(0.05, abs=0.001)


def test_pre_window_target_is_capped_by_grid_import_limit(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=100000,
        max_charge_w=13600,
        max_discharge_w=13600,
        max_grid_import_w=11100,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )
    optimizer.pre_window_slot = 6
    optimizer.pre_window_soc_target = 1.0
    n = 12

    result = optimizer.optimize(
        import_prices=[0.0] * 6 + [0.50] * 6,
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[1.0] * n,
        current_soc=0.50,
        allow_grid_charge=True,
    )

    assert max(result.grid_import_w) <= 11100.1


def test_pre_window_reachability_uses_grid_import_charge_limit_source():
    source = (COMPONENT_ROOT / "optimization" / "battery_optimizer.py").read_text()

    pre_window_block = source.split("pre_window_boundary = self._period_index_for_base_slot", 1)[1]
    pre_window_block = pre_window_block.split("# === Variable bounds ===", 1)[0]

    assert "self._charge_limit_kw(" in pre_window_block
    assert "self.max_charge_kw * eff * sum" not in pre_window_block


def test_lp_solver_uses_extended_time_limit(battery_optimizer_module, monkeypatch):
    captured = {}

    def fake_solve(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
        captured["time_limit"] = time_limit
        return battery_optimizer_module._HighsResult(
            x=None, success=False, message="Time limit reached.", status=0, fun=None,
        )

    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", True)
    monkeypatch.setattr(battery_optimizer_module, "_solve_lp_highs", fake_solve)
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.10] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.80,
    )

    assert captured["time_limit"] == (
        battery_optimizer_module.LP_SOLVER_TIME_LIMIT_SECONDS
    )
    assert captured["time_limit"] == 30.0
    assert result.solver_used == "greedy"


def test_default_blocks_battery_export_when_fit_beats_import(battery_optimizer_module):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.069] * 12,
        export_prices=[0.12] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
    )

    assert max(result.grid_export_w) <= 1e-6
    assert all(action.action != "export" for action in result.schedule.actions)
    assert max(action.battery_discharge_w for action in result.schedule.actions) <= 500.1


def test_explicit_battery_export_true_allows_export_when_profitable(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
        allow_battery_export=True,
    )

    assert max(result.grid_export_w) > 100.0
    assert any(action.action == "export" for action in result.schedule.actions)


def test_grid_export_cap_limits_lp_export_plan_and_api_series(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=48000,
        max_charge_w=15000,
        max_discharge_w=15000,
        max_grid_export_w=5000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.4] * 12,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
        allow_battery_export=True,
    )

    assert result.feasible is True
    assert max(result.grid_export_w) <= 5000.1
    export_actions = [action for action in result.schedule.actions if action.action == "export"]
    assert export_actions
    assert max(action.power_w for action in export_actions) <= 5000.1
    assert max(result.schedule.to_api_response()["battery_export_w"]) <= 5000.1


def test_target_export_cap_is_separate_from_total_discharge(battery_optimizer_module):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=50000,
        max_charge_w=10000,
        max_discharge_w=10000,
        max_battery_export_w=1000,
        backup_reserve=0.10,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[1.00] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[2.0] * 12,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[True] * 12,
    )

    assert any(action.action == "export" for action in result.schedule.actions)
    assert max(result.grid_export_w) <= 1000.1
    assert max(action.power_w for action in result.schedule.actions) <= 1000.1
    assert max(action.battery_discharge_w for action in result.schedule.actions) > 2500


def test_solar_surplus_export_still_works_when_battery_export_blocked(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.069] * 12,
        export_prices=[0.12] * 12,
        solar_forecast=[2.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=1.0,
        acquisition_cost_kwh=0.0,
        allow_battery_export=False,
    )

    assert min(result.grid_export_w) >= 1499.0
    assert max(result.grid_export_w) <= 1500.1
    assert all(action.action != "export" for action in result.schedule.actions)


def test_grid_export_cannot_come_from_grid_passthrough(battery_optimizer_module):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.0] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=True,
    )

    assert max(result.grid_export_w) <= 1e-6
    assert max(result.grid_import_w) <= 1e-6


def test_zerohero_bonus_cap_limits_intentional_battery_export(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("ZeroHero bonus cap is enforced by the LP optimizer")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=50000,
        max_charge_w=10000,
        max_discharge_w=10000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 36,
        export_prices=[0.0] * 36,
        export_bonus_prices=[0.15] * 36,
        export_bonus_cap_kwh=1.0,
        solar_forecast=[0.0] * 36,
        load_forecast=[0.0] * 36,
        current_soc=0.90,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 36,
        block_battery_charge=[True] * 36,
    )

    exported_kwh = sum(w / 1000 * optimizer.dt_hours for w in result.grid_export_w)

    assert result.feasible is True
    assert exported_kwh <= 1.001
    assert any(action.action == "export" for action in result.schedule.actions)


def test_zerohero_solar_surplus_shares_bonus_bucket_before_battery_export(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("ZeroHero bonus cap is enforced by the LP optimizer")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=50000,
        max_charge_w=10000,
        max_discharge_w=10000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 36,
        export_prices=[0.0] * 36,
        export_bonus_prices=[0.15] * 36,
        export_bonus_cap_kwh=1.0,
        solar_forecast=[2.0] * 36,
        load_forecast=[0.0] * 36,
        current_soc=0.90,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 36,
        block_battery_charge=[True] * 36,
    )

    battery_export_kwh = sum(
        max(0.0, w / 1000 - 2.0) * optimizer.dt_hours
        for w in result.grid_export_w
    )

    assert result.feasible is True
    assert battery_export_kwh <= 1.001


def test_below_reserve_can_grid_charge_during_cheap_window(battery_optimizer_module):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.08] * 12 + [0.30] * 24,
        export_prices=[0.05] * 36,
        solar_forecast=[0.0] * 36,
        load_forecast=[1.0] * 36,
        current_soc=0.0,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 36,
    )

    cheap_window = result.schedule.actions[:12]
    assert any(action.action == "charge" for action in cheap_window)
    assert max(action.battery_charge_w for action in cheap_window) > 1000


def test_below_optimizer_reserve_lp_uses_hardware_floor(
    battery_optimizer_module,
    monkeypatch,
):
    captured = {}

    def fake_solve(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
        captured["bounds"] = bounds
        return battery_optimizer_module._HighsResult(
            x=[0.0] * len(c), success=True,
            message="Optimal", status=0, fun=0.0,
        )

    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", True)
    monkeypatch.setattr(battery_optimizer_module, "_solve_lp_highs", fake_solve)
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=10000,
        max_discharge_w=10000,
        backup_reserve=0.50,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    optimizer.optimize(
        import_prices=[0.0] * 12,
        export_prices=[0.20] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.0] * 12,
        current_soc=0.15,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
    )

    assert captured["bounds"][-1][0] == pytest.approx(0.5)


def test_below_optimizer_reserve_allows_natural_self_consumption(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.13,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.30] * 12,
        export_prices=[0.05] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.12,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 12,
    )

    assert result.schedule.actions[0].action == "self_consumption"
    assert result.schedule.actions[0].battery_discharge_w > 0
    assert result.schedule.actions[0].battery_charge_w == 0


def test_below_optimizer_reserve_blocks_lp_battery_export(
    battery_optimizer_module,
    monkeypatch,
):
    captured = {}

    def fake_solve(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
        captured["bounds"] = bounds
        captured["variable_count"] = len(c)
        return battery_optimizer_module._HighsResult(
            x=[0.0] * len(c), success=True,
            message="Optimal", status=0, fun=0.0,
        )

    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", True)
    monkeypatch.setattr(battery_optimizer_module, "_solve_lp_highs", fake_solve)
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.15,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.149,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
    )

    assert result.solver_used == "highs"
    period_count = (captured["variable_count"] - 1) // 5
    grid_export_bounds = captured["bounds"][period_count:period_count * 2]
    assert grid_export_bounds
    assert all(bound[1] == 0.0 for bound in grid_export_bounds)
    assert result.schedule.actions[0].action == "self_consumption"
    assert max(result.grid_export_w) <= 1e-6
    assert all(action.action != "export" for action in result.schedule.actions)


def test_below_optimizer_reserve_allows_later_export_after_recovery(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("Reserve recovery export gating is enforced by the LP optimizer")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.15,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=2,
    )
    n = 24
    export_slots = [False] * 12 + [True] * 12

    result = optimizer.optimize(
        import_prices=[0.05] * 12 + [0.30] * 12,
        export_prices=[0.0] * 12 + [0.50] * 12,
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.04,
        acquisition_cost_kwh=0.0,
        allow_battery_export=export_slots,
        block_battery_charge=export_slots,
        allow_grid_charge=True,
    )

    early_actions = result.schedule.actions[:12]
    later_actions = result.schedule.actions[12:]
    assert any(action.action == "charge" for action in early_actions)
    assert any(action.action == "export" for action in later_actions)
    assert max(result.grid_export_w[12:]) > 1000
    assert min(action.soc for action in later_actions) >= 0.15


def test_below_optimizer_reserve_blocks_greedy_battery_export(
    battery_optimizer_module,
    monkeypatch,
):
    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", False)
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.15,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.149,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
    )

    assert result.solver_used == "greedy"
    assert result.schedule.actions[0].action == "self_consumption"
    assert max(result.grid_export_w) <= 1e-6
    assert all(action.action != "export" for action in result.schedule.actions)


def test_below_optimizer_reserve_greedy_allows_natural_self_consumption(
    battery_optimizer_module,
    monkeypatch,
):
    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", False)
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.13,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.30] * 12,
        export_prices=[0.05] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.12,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 12,
    )

    assert result.solver_used == "greedy"
    assert result.schedule.actions[0].action == "self_consumption"
    assert result.schedule.actions[0].battery_discharge_w > 0
    assert result.schedule.actions[0].battery_charge_w == 0


def test_reserve_floor_self_consumption_forecasts_net_load_drain(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=1,
    )

    schedule = optimizer._build_schedule(
        n=1,
        grid_import=[1.0],
        grid_export=[0.0],
        battery_charge=[0.0],
        battery_discharge=[0.0],
        solar=[0.4],
        load=[1.4],
        soc_0=0.25,
        import_prices=[0.30],
        export_prices=[0.05],
    )

    assert schedule.actions[0].soc < 0.25
    assert schedule.actions[0].battery_discharge_w == 1000.0
    assert schedule.actions[0].action == "self_consumption"
    api = schedule.to_api_response()
    assert api["discharge_w"] == [1000.0]
    assert api["battery_consume_w"] == [1000.0]
    assert api["battery_export_w"] == [0.0]


def test_schedule_soc_display_holds_at_optimizer_reserve(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=1,
    )

    schedule = optimizer._build_schedule(
        n=1,
        grid_import=[0.0],
        grid_export=[0.0],
        battery_charge=[0.0],
        battery_discharge=[1.35],
        solar=[0.0],
        load=[1.35],
        soc_0=0.20,
        import_prices=[0.30],
        export_prices=[0.05],
    )

    assert schedule.actions[0].action == "self_consumption"
    assert schedule.actions[0].soc == pytest.approx(0.20)
    assert schedule.to_api_response()["soc"][0] == schedule.actions[0].soc


def test_schedule_soc_display_uses_hardware_floor_when_known(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    schedule = optimizer._build_schedule(
        n=1,
        grid_import=[0.0],
        grid_export=[0.0],
        battery_charge=[0.0],
        battery_discharge=[1.35],
        solar=[0.0],
        load=[1.35],
        soc_0=0.20,
        import_prices=[0.30],
        export_prices=[0.05],
    )

    assert schedule.actions[0].action == "self_consumption"
    assert schedule.actions[0].soc < 0.20
    assert schedule.to_api_response()["soc"][0] == schedule.actions[0].soc


def test_schedule_export_display_keeps_optimizer_floor_when_hardware_floor_is_lower(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    schedule = optimizer._build_schedule(
        n=1,
        grid_import=[0.0],
        grid_export=[1.35],
        battery_charge=[0.0],
        battery_discharge=[1.35],
        solar=[0.0],
        load=[0.0],
        soc_0=0.20,
        import_prices=[0.30],
        export_prices=[0.50],
    )

    assert schedule.actions[0].action == "export"
    assert schedule.actions[0].soc == pytest.approx(0.20)
    assert schedule.to_api_response()["soc"][0] == schedule.actions[0].soc


def test_schedule_api_reports_self_consumption_discharge_for_charts(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=1,
    )

    schedule = optimizer._build_schedule(
        n=1,
        grid_import=[0.0],
        grid_export=[0.0],
        battery_charge=[0.0],
        battery_discharge=[1.2],
        solar=[0.0],
        load=[1.2],
        soc_0=0.50,
        import_prices=[0.30],
        export_prices=[0.05],
    )

    assert schedule.actions[0].action == "self_consumption"
    api = schedule.to_api_response()
    assert api["discharge_w"] == [1200.0]
    assert api["battery_consume_w"] == [1200.0]
    assert api["battery_export_w"] == [0.0]


def test_battery_export_mask_allows_only_explicit_slots(battery_optimizer_module):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 6 + [True] * 6,
    )

    assert max(result.grid_export_w[:6]) <= 1e-6
    assert max(result.grid_export_w[6:]) > 100.0
    assert all(action.action != "export" for action in result.schedule.actions[:6])
    assert any(action.action == "export" for action in result.schedule.actions[6:])


def test_charge_block_mask_prevents_charging_during_export_window(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    blocked = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.50] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[True] * 12,
    )

    assert max(action.battery_charge_w for action in blocked.schedule.actions) <= 1e-6
    assert all(action.action != "charge" for action in blocked.schedule.actions)


def test_charge_block_mask_prevents_greedy_fallback_charging(
    battery_optimizer_module,
    monkeypatch,
):
    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", False)
    optimizer = _optimizer(battery_optimizer_module)

    unblocked = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.04] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[False] * 12,
    )
    blocked = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.04] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[True] * 12,
    )

    assert max(action.battery_charge_w for action in unblocked.schedule.actions) > 100
    assert max(action.battery_charge_w for action in blocked.schedule.actions) <= 1e-6
    assert all(action.action != "charge" for action in blocked.schedule.actions)


def test_charge_block_mask_overrides_free_import_force_charge(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    unblocked = optimizer.optimize(
        import_prices=[0.0] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[False] * 12,
    )
    blocked = optimizer.optimize(
        import_prices=[0.0] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.1] * 12,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12,
        block_battery_charge=[True] * 12,
    )

    assert any(action.action == "charge" for action in unblocked.schedule.actions)
    assert all(action.action == "charge" for action in unblocked.schedule.actions)
    assert all(action.power_w == 7000 for action in unblocked.schedule.actions)
    assert all(action.battery_charge_w == 7000 for action in unblocked.schedule.actions)
    assert max(action.battery_discharge_w for action in unblocked.schedule.actions) <= 1e-6
    assert unblocked.schedule.charge_w == [7000] * 12
    assert max(action.battery_charge_w for action in blocked.schedule.actions) <= 1e-6
    assert all(action.action != "charge" for action in blocked.schedule.actions)


def test_zerohero_free_import_window_reports_continuous_force_charge(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=48000,
        max_charge_w=12000,
        max_discharge_w=12000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=14,
    )
    free_start = 11 * 12
    free_slots = 3 * 12
    prices = [0.363] * free_start + [0.0] * free_slots

    result = optimizer.optimize(
        import_prices=prices,
        export_prices=[0.0] * len(prices),
        solar_forecast=[0.0] * len(prices),
        load_forecast=[1.0] * len(prices),
        current_soc=0.42,
        acquisition_cost_kwh=0.0,
        allow_battery_export=False,
        block_battery_charge=False,
    )

    free_window = result.schedule.actions[free_start:free_start + free_slots]

    assert len(free_window) == 36
    assert all(action.action == "charge" for action in free_window)
    assert all(action.power_w == 12000 for action in free_window)
    assert all(action.battery_charge_w == 12000 for action in free_window)
    assert max(action.battery_discharge_w for action in free_window) <= 1e-6
    assert result.schedule.charge_w[free_start:free_start + free_slots] == [12000] * 36


def test_zerohero_free_import_before_positive_fit_schedules_export(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=32200,
        max_charge_w=10500,
        max_discharge_w=9900,
        backup_reserve=0.30,
        interval_minutes=5,
        horizon_hours=24,
    )
    n = 24 * 12
    free_start = 11 * 12
    free_slots = 3 * 12
    export_start = 18 * 12
    export_slots = 3 * 12
    import_prices = [0.363] * n
    export_prices = [0.0] * n

    for idx in range(free_start, free_start + free_slots):
        import_prices[idx] = 0.0
    for idx in range(16 * 12, 23 * 12):
        import_prices[idx] = 0.495
    for idx in range(export_start, export_start + export_slots):
        export_prices[idx] = 0.15

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=[0.0] * n,
        load_forecast=[0.4] * n,
        current_soc=0.34,
        acquisition_cost_kwh=0.363,
        allow_battery_export=[price > 0 for price in export_prices],
    )

    free_window = result.schedule.actions[free_start:free_start + free_slots]
    export_window = result.schedule.actions[export_start:export_start + export_slots]

    assert any(action.action == "charge" for action in free_window)
    assert max(action.battery_charge_w for action in free_window) > 10000
    assert any(action.action == "export" for action in export_window)
    assert max(result.grid_export_w[export_start:export_start + export_slots]) > 1000


def test_grid_charge_allowed_by_default_for_profitable_export(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 6 + [0.50] * 6,
        export_prices=[0.0] * 6 + [0.50] * 6,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 6 + [True] * 6,
    )

    assert any(action.action == "charge" for action in result.schedule.actions[:6])
    assert max(action.battery_charge_w for action in result.schedule.actions[:6]) > 1000


def test_cheap_import_charge_not_blocked_by_lower_fit_than_acquisition_cost(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.069] * 12 + [0.2856] * 24,
        export_prices=[0.12] * 36,
        solar_forecast=[0.0] * 36,
        load_forecast=[0.5] * 36,
        current_soc=0.23,
        acquisition_cost_kwh=0.2856,
        allow_battery_export=[True] * 36,
    )

    cheap_window = result.schedule.actions[:12]
    assert any(action.action == "charge" for action in cheap_window)
    assert max(action.battery_charge_w for action in cheap_window) > 1000
    assert result.schedule.actions[-1].soc > 0.20


@pytest.mark.parametrize("acquisition_cost", [0.0, 0.069, 0.12])
def test_cheap_import_charge_not_blocked_by_positive_fit_at_reserve(
    battery_optimizer_module,
    acquisition_cost,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.069] * 12 + [0.2856] * 24,
        export_prices=[0.12] * 36,
        solar_forecast=[0.0] * 36,
        load_forecast=[0.5] * 36,
        current_soc=0.20,
        acquisition_cost_kwh=acquisition_cost,
        allow_battery_export=[True] * 36,
    )

    cheap_window = result.schedule.actions[:12]
    assert any(action.action == "charge" for action in cheap_window)
    assert max(action.battery_charge_w for action in cheap_window) > 1000
    assert result.schedule.actions[-1].soc > 0.20


def test_reserve_recommendation_reports_bridge_floor_before_next_charge(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.45] * 36,
        export_prices=[0.0] * 36,
        solar_forecast=[0.0] * 12 + [5.0] * 12 + [0.0] * 12,
        load_forecast=[1.0] * 36,
        current_soc=0.70,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 36,
    )

    recommendation = result.reserve_recommendation

    assert recommendation["next_charge_reason"] == "forecast_solar_surplus"
    assert recommendation["suggested_optimizer_reserve_percent"] > 20
    assert 55 <= recommendation["minimum_forecast_soc_percent"] <= 65
    assert recommendation["needs_optimizer_reserve_raise"] is True
    assert recommendation["minimum_forecast_soc_time"].startswith("2026-05-04T00:")
    assert recommendation["protects_until"].startswith("2026-05-04T01:")


def test_reserve_recommendation_does_not_hold_full_soc_without_discharge_bridge(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.45] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[5.0] * 12,
        load_forecast=[1.0] * 12,
        current_soc=1.0,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 12,
    )

    recommendation = result.reserve_recommendation

    assert recommendation["next_charge_reason"] == "forecast_solar_surplus"
    assert recommendation["minimum_forecast_soc_percent"] >= 98
    assert recommendation["suggested_optimizer_reserve_percent"] == 20
    assert recommendation["needs_optimizer_reserve_raise"] is False
    assert recommendation["note"] == "No discharge bridge before next charge"


def test_reserve_recommendation_marks_no_charge_in_horizon(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )

    result = optimizer.optimize(
        import_prices=[0.45] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[1.0] * 12,
        current_soc=0.70,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 12,
    )

    recommendation = result.reserve_recommendation

    assert recommendation["next_charge_reason"] == "no_charge_in_horizon"
    assert recommendation["note"] == "No charging opportunity in optimizer horizon"
    assert recommendation["protects_until"].startswith("2026-05-04T00:55")


def test_reserve_recommendation_reports_export_floor_for_home_load_bridge(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.05,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=2,
    )

    start = datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    actions = []
    for idx in range(24):
        action = "export" if idx < 6 else "self_consumption"
        actions.append(
            battery_optimizer_module.ScheduleAction(
                timestamp=start + idx * battery_optimizer_module.timedelta(minutes=5),
                action=action,
                power_w=5000 if action == "export" else 0,
                soc=0.9,
                battery_charge_w=0,
                battery_discharge_w=5000 if action == "export" else 0,
            )
        )
    schedule = battery_optimizer_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    recommendation = optimizer._build_reserve_recommendation(
        schedule,
        solar=[0.0] * 18 + [5.0] * 6,
        load=[1.0] * 24,
    )

    assert recommendation["home_load_bridge_next_charge_reason"] == "forecast_solar_surplus"
    assert recommendation["home_load_bridge_kwh"] == pytest.approx(1.0)
    assert 15 <= recommendation["home_load_export_floor_percent"] <= 17
    assert recommendation["home_load_bridge_start"].startswith("2026-05-04T00:30")
    assert recommendation["home_load_bridge_until"].startswith("2026-05-04T01:30")


def test_export_reserve_floor_limits_planned_battery_export_only(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=5000,
        max_discharge_w=5000,
        backup_reserve=0.05,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=3,
    )
    n = 36

    result = optimizer.optimize(
        import_prices=[0.30] * n,
        export_prices=[0.50] * 12 + [0.0] * (n - 12),
        solar_forecast=[0.0] * n,
        load_forecast=[0.5] * n,
        current_soc=0.90,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * 12 + [False] * (n - 12),
        export_reserve_floor=0.56,
    )

    export_actions = [a for a in result.schedule.actions if a.action == "export"]
    assert export_actions
    assert min(action.soc for action in export_actions) >= 0.55
    assert any(
        action.action == "self_consumption" and action.soc < 0.56
        for action in result.schedule.actions
    )


def test_positive_fit_iog_charge_does_not_create_all_day_export_loop(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=48,
    )

    cheap_slots = 202
    n = 576
    result = optimizer.optimize(
        import_prices=[0.069] * cheap_slots + [0.2856] * (n - cheap_slots),
        export_prices=[0.12] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.7] * n,
        current_soc=0.19,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[True] * n,
    )

    cheap_window = result.schedule.actions[:cheap_slots]
    charge_actions = [action for action in cheap_window if action.action == "charge"]

    assert cheap_window[0].action == "charge"
    assert max(action.battery_charge_w for action in charge_actions) > 1000
    assert len(charge_actions) < 40
    assert max(result.grid_export_w[:cheap_slots]) <= 1e-6
    assert all(action.action != "export" for action in cheap_window)


def test_disallow_grid_charge_blocks_forced_grid_charging(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.05] * 6 + [0.50] * 6,
        export_prices=[0.0] * 6 + [0.50] * 6,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 6 + [True] * 6,
        allow_grid_charge=False,
    )

    assert max(action.battery_charge_w for action in result.schedule.actions) <= 1e-6
    assert all(action.action != "charge" for action in result.schedule.actions)
    assert max(result.grid_import_w) <= 500.1


def test_disallow_grid_charge_ignores_pre_export_fill_target(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)
    optimizer.pre_window_slot = 6
    optimizer.pre_window_soc_target = 1.0

    result = optimizer.optimize(
        import_prices=[0.05] * 6 + [0.50] * 6,
        export_prices=[0.0] * 6 + [0.50] * 6,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 6 + [True] * 6,
        allow_grid_charge=False,
    )

    assert result.feasible is True
    assert max(action.battery_charge_w for action in result.schedule.actions) <= 1e-6
    assert all(action.action != "charge" for action in result.schedule.actions)


def test_pre_export_fill_target_respects_configured_soc(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)
    optimizer.pre_window_slot = 6
    optimizer.pre_window_soc_target = 0.2

    result = optimizer.optimize(
        import_prices=[0.05] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.0] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False] * 12,
        allow_grid_charge=True,
    )

    assert result.feasible is True
    assert result.schedule.actions[5].soc >= 0.195
    assert result.schedule.actions[5].soc < 0.5


def test_pre_export_fill_target_leaves_room_for_forecast_solar(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=10000,
        max_discharge_w=10000,
        efficiency=1.0,
        backup_reserve=0.05,
        interval_minutes=60,
        horizon_hours=5,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 4
    optimizer.pre_window_soc_target = 0.90
    optimizer.pre_window_solar_credit_factor = 0.80
    optimizer.pre_window_solar_buffer_soc = 0.03

    result = optimizer.optimize(
        import_prices=[0.05] * 5,
        export_prices=[0.0, 0.0, 0.0, 0.0, 0.50],
        solar_forecast=[0.0, 0.0, 2.0, 2.0, 0.0],
        load_forecast=[0.0] * 5,
        current_soc=0.50,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False, False, False, False, True],
        allow_grid_charge=True,
    )

    assert result.feasible is True
    assert max(action.soc for action in result.schedule.actions[:2]) <= 0.62
    assert result.schedule.actions[3].soc >= 0.895
    early_grid_import_kwh = sum(result.grid_import_w[:2]) / 1000
    assert early_grid_import_kwh <= 1.2


def test_pre_export_fill_target_still_prefills_without_forecast_solar(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=10000,
        max_discharge_w=10000,
        efficiency=1.0,
        backup_reserve=0.05,
        interval_minutes=60,
        horizon_hours=5,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 4
    optimizer.pre_window_soc_target = 0.90

    result = optimizer.optimize(
        import_prices=[0.05] * 5,
        export_prices=[0.0, 0.0, 0.0, 0.0, 0.50],
        solar_forecast=[0.0] * 5,
        load_forecast=[0.0] * 5,
        current_soc=0.50,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False, False, False, False, True],
        allow_grid_charge=True,
    )

    assert result.feasible is True
    assert result.schedule.actions[0].soc >= 0.895


def test_pre_export_solar_ceiling_does_not_force_discharge_for_headroom(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS LP solver")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=10000,
        max_charge_w=10000,
        max_discharge_w=10000,
        efficiency=1.0,
        backup_reserve=0.05,
        interval_minutes=60,
        horizon_hours=5,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 4
    optimizer.pre_window_soc_target = 0.90

    result = optimizer.optimize(
        import_prices=[0.05] * 5,
        export_prices=[0.0, 0.0, 0.0, 0.0, 0.50],
        solar_forecast=[0.0, 0.0, 2.0, 2.0, 0.0],
        load_forecast=[0.0] * 5,
        current_soc=0.80,
        acquisition_cost_kwh=0.0,
        allow_battery_export=[False, False, False, False, True],
        allow_grid_charge=True,
    )

    assert result.feasible is True
    assert max(action.battery_discharge_w for action in result.schedule.actions[:4]) <= 1e-6
    assert min(action.soc for action in result.schedule.actions[:4]) >= 0.80


def test_disallow_grid_charge_still_allows_solar_surplus_charging(
    battery_optimizer_module,
):
    optimizer = _optimizer(battery_optimizer_module)

    result = optimizer.optimize(
        import_prices=[0.30] * 12,
        export_prices=[0.0] * 12,
        solar_forecast=[5.0] * 12,
        load_forecast=[0.5] * 12,
        current_soc=0.05,
        acquisition_cost_kwh=0.0,
        allow_battery_export=False,
        allow_grid_charge=False,
    )

    assert max(action.battery_charge_w for action in result.schedule.actions) > 1000
    assert all(action.action != "charge" for action in result.schedule.actions)
    assert max(result.grid_import_w) <= 1e-6


def test_tiered_lp_periods_reduce_flat_48h_horizon(battery_optimizer_module):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        interval_minutes=5,
        horizon_hours=48,
    )
    n = 576

    periods = optimizer._build_lp_periods(
        n,
        import_prices=[0.25] * n,
        export_prices=[0.08] * n,
        solar=[0.0] * n,
        load=[0.7] * n,
        allow_battery_export=[False] * n,
        block_battery_charge=[False] * n,
    )

    assert len(periods) == 132
    assert len(periods) < 160
    assert periods[0].slot_count == 1
    assert periods[72].slot_count == 6
    assert periods[-1].slot_count == 12


def test_tiered_lp_periods_split_on_masks_prices_and_deadline(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        interval_minutes=5,
        horizon_hours=48,
    )
    optimizer.pre_window_slot = 100
    n = 144
    allow_export = [False] * n
    allow_export[111:] = [True] * (n - 111)
    import_prices = [0.25] * n
    import_prices[120:] = [0.29] * (n - 120)

    periods = optimizer._build_lp_periods(
        n,
        import_prices=import_prices,
        export_prices=[0.08] * n,
        solar=[0.0] * n,
        load=[0.7] * n,
        allow_battery_export=allow_export,
        block_battery_charge=[False] * n,
    )

    boundaries = {period.end for period in periods}
    assert 100 in boundaries
    assert 111 in boundaries
    assert 120 in boundaries


def test_tiered_lp_periods_split_on_solar_surplus_changes(
    battery_optimizer_module,
):
    optimizer = battery_optimizer_module.BatteryOptimizer(
        interval_minutes=5,
        horizon_hours=12,
    )
    n = 144
    solar = [0.0] * n
    for idx in range(75, 78):
        solar[idx] = 5.0

    periods = optimizer._build_lp_periods(
        n,
        import_prices=[0.30] * n,
        export_prices=[0.08] * n,
        solar=solar,
        load=[0.5] * n,
        allow_battery_export=[False] * n,
        block_battery_charge=[False] * n,
    )

    boundaries = {period.end for period in periods}
    assert 75 in boundaries
    assert 78 in boundaries


def test_no_grid_charge_does_not_expand_solar_charge_into_dark_slots(
    battery_optimizer_module,
):
    if not battery_optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("highspy unavailable")

    optimizer = battery_optimizer_module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=12,
    )
    n = 144
    solar = [0.0] * n
    for idx in range(75, 78):
        solar[idx] = 5.0

    result = optimizer.optimize(
        import_prices=[0.30] * n,
        export_prices=[0.08] * n,
        solar_forecast=solar,
        load_forecast=[0.5] * n,
        current_soc=0.20,
        allow_battery_export=False,
        allow_grid_charge=False,
    )

    assert result.solver_used == "highs"
    assert max(
        result.schedule.actions[idx].battery_charge_w
        for idx in range(72, 75)
    ) <= 1e-6
    assert max(
        result.schedule.actions[idx].battery_charge_w
        for idx in range(75, 78)
    ) > 1000


def test_sparse_lp_stats_and_schedule_expansion(
    battery_optimizer_module,
    monkeypatch,
):
    captured = {}

    def fake_solve(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
        captured["A_eq"] = A_eq
        captured["A_ub"] = A_ub
        captured["bounds"] = bounds
        return battery_optimizer_module._HighsResult(
            x=[0.0] * len(c), success=True,
            message="Optimal", status=0, fun=0.0,
        )

    monkeypatch.setattr(battery_optimizer_module, "HIGHS_AVAILABLE", True)
    monkeypatch.setattr(battery_optimizer_module, "_solve_lp_highs", fake_solve)
    optimizer = battery_optimizer_module.BatteryOptimizer(
        interval_minutes=5,
        horizon_hours=48,
    )
    n = 576

    result = optimizer.optimize(
        import_prices=[0.25] * n,
        export_prices=[0.08] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.7] * n,
        current_soc=0.50,
        allow_battery_export=[False] * n,
    )

    assert result.solver_used == "highs"
    assert len(result.schedule.actions) == n
    assert len(result.grid_import_w) == n
    assert result.lp_stats["backend"] == "highspy"
    assert result.lp_stats["base_steps"] == n
    assert result.lp_stats["period_count"] == 132
    assert result.lp_stats["variables"] == 5 * 132 + 1
    assert result.lp_stats["constraints"] == captured["A_eq"].shape[0] + captured["A_ub"].shape[0]
    assert len(captured["bounds"]) == result.lp_stats["variables"]
