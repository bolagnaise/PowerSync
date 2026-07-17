"""Regressions for optimizer-reserve versus hardware-reserve semantics.

Natural self-consumption may use stored energy down to the known hardware
reserve.  The software optimizer reserve remains the floor for intentional
battery-to-grid export.  These tests deliberately use one-hour slots,
10 kWh capacity, unit efficiency, and no terminal value so the expected
energy and cost arithmetic is exact.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
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
    fixed_now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
    ha_dt.now = lambda *args, **kwargs: fixed_now
    ha_dt.utcnow = lambda *args, **kwargs: fixed_now
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
        name: sys.modules.get(name, _SENTINEL) for name in _STUB_MODULE_NAMES
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


def _optimizer(
    module,
    *,
    backup_reserve: float = 0.20,
    hardware_reserve: float | None = 0.10,
    max_charge_w: float = 10_000,
    horizon_hours: int = 4,
):
    return module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=max_charge_w,
        max_discharge_w=10_000,
        efficiency=1.0,
        backup_reserve=backup_reserve,
        hardware_reserve=hardware_reserve,
        interval_minutes=60,
        horizon_hours=horizon_hours,
        terminal_weight=0.0,
    )


def _select_backend(module, monkeypatch, backend: str) -> None:
    if backend == "highs":
        if not module.HIGHS_AVAILABLE:
            pytest.skip("requires HiGHS")
        monkeypatch.setattr(module, "HIGHS_AVAILABLE", True)
    else:
        monkeypatch.setattr(module, "HIGHS_AVAILABLE", False)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_known_hardware_floor_models_natural_self_consumption_below_optimizer_reserve(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Three kWh of load may drain 40% SOC to the 10% hardware floor."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(module, horizon_hours=3)

    result = optimizer.optimize(
        import_prices=[0.50, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 1.0, 1.0],
        current_soc=0.40,
        allow_battery_export=[False, False, False],
    )

    assert [action.action for action in result.schedule.actions] == [
        "self_consumption",
        "self_consumption",
        "self_consumption",
    ]
    assert sum(action.battery_discharge_w for action in result.schedule.actions) == pytest.approx(
        3000.0,
        abs=0.1,
    )
    assert result.schedule.actions[-1].soc == pytest.approx(0.10, abs=1e-6)
    assert sum(result.grid_import_w) == pytest.approx(0.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(0.0, abs=0.01)


def test_known_hardware_floor_is_the_lp_energy_bound_even_when_export_is_permitted(
    battery_optimizer_module,
    monkeypatch,
):
    """An export permission mask must not become a global 20% SOC floor."""
    module = battery_optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("requires HiGHS")
    captured = {}

    def fake_solve(c, A_ub, b_ub, A_eq, b_eq, bounds, time_limit):
        captured["bounds"] = bounds
        return module._HighsResult(
            x=[0.0] * len(c),
            success=True,
            message="Optimal",
            status=0,
            fun=0.0,
        )

    monkeypatch.setattr(module, "HIGHS_AVAILABLE", True)
    monkeypatch.setattr(module, "_solve_lp_highs", fake_solve)
    optimizer = _optimizer(module, horizon_hours=3)

    result = optimizer.optimize(
        import_prices=[0.50, 0.50, 0.50],
        export_prices=[0.05, 0.05, 0.05],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 1.0, 1.0],
        current_soc=0.40,
        allow_battery_export=[True, True, True],
    )

    period_count = result.lp_stats["period_count"]
    energy_bounds = captured["bounds"][-(period_count + 1) :]
    assert energy_bounds[0] == pytest.approx((4.0, 4.0))
    assert all(lower == pytest.approx(1.0) for lower, _upper in energy_bounds[1:])


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_intentional_export_stops_at_optimizer_reserve_then_home_reaches_hardware_floor(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Export eight kWh to 20%, then let one kWh of home load reach 10%."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(module, horizon_hours=2)

    result = optimizer.optimize(
        import_prices=[0.40, 0.40],
        export_prices=[0.60, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 1.0],
        current_soc=1.0,
        allow_battery_export=[True, False],
        block_battery_charge=[True, False],
        priority_export_slots=[True, False],
        priority_export_enabled=True,
    )

    assert result.schedule.actions[0].action == "export"
    assert result.grid_export_w[0] == pytest.approx(8000.0, abs=0.1)
    assert result.schedule.actions[0].soc == pytest.approx(0.20, abs=1e-6)
    assert result.schedule.actions[1].action == "self_consumption"
    assert result.schedule.actions[1].battery_discharge_w == pytest.approx(
        1000.0,
        abs=0.1,
    )
    assert result.schedule.actions[1].soc == pytest.approx(0.10, abs=1e-6)
    assert result.schedule.predicted_cost == pytest.approx(-4.80, abs=0.01)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_uneconomic_top_up_is_not_scheduled(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Buying at 55c to avoid later 50c imports must remain uneconomic."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.55, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 1.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[False, False, False],
        allow_grid_charge=True,
    )

    assert all(action.action != "charge" for action in result.schedule.actions)
    assert sum(action.battery_charge_w for action in result.schedule.actions) == pytest.approx(
        0.0,
        abs=0.1,
    )
    assert sum(result.grid_import_w) == pytest.approx(2000.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_economic_top_up_uses_configured_max_charge_rate(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """One cheap hour at a 1 kW limit adds exactly one kWh, never more."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=1000,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.10, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 1.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[False, False, False],
        allow_grid_charge=True,
    )

    charge_kwh = sum(
        action.battery_charge_w / 1000.0 for action in result.schedule.actions
    )
    assert charge_kwh == pytest.approx(1.0, abs=1e-6)
    assert max(action.battery_charge_w for action in result.schedule.actions) <= 1000.0
    assert result.schedule.actions[0].action == "charge"
    assert sum(result.grid_import_w) == pytest.approx(2000.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(0.60, abs=0.01)


@pytest.mark.parametrize(
    ("backend", "charge_price", "expected_charge_kwh"),
    [
        ("highs", 0.10, 1.0),
        ("greedy", 0.10, 1.0),
        ("highs", 0.55, 0.0),
        ("greedy", 0.55, 0.0),
    ],
)
def test_post_export_top_up_is_an_economic_choice(
    battery_optimizer_module,
    monkeypatch,
    backend,
    charge_price,
    expected_charge_kwh,
):
    """Happy Hour depletion never creates a mandatory recovery charge."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=1000,
        horizon_hours=4,
    )

    result = optimizer.optimize(
        import_prices=[0.70, charge_price, 0.50, 0.50],
        export_prices=[0.60, 0.0, 0.0, 0.0],
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0, 0.0, 1.0, 1.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.10,
        allow_battery_export=[True, False, False, False],
        block_battery_charge=[True, False, False, False],
        priority_export_slots=[True, False, False, False],
        priority_export_enabled=True,
    )

    assert result.schedule.actions[0].action == "export"
    assert result.schedule.actions[0].soc == pytest.approx(0.10, abs=1e-6)
    charge_kwh = sum(
        action.battery_charge_w / 1000.0
        for action in result.schedule.actions
    )
    assert charge_kwh == pytest.approx(expected_charge_kwh, abs=1e-6)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_priority_window_does_not_override_uneconomic_acquisition_cost(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Provider priority is permission, not a synthetic export subsidy."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(module, horizon_hours=2)

    result = optimizer.optimize(
        import_prices=[0.70, 0.70],
        export_prices=[0.60, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 1.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.65,
        allow_battery_export=[True, False],
        block_battery_charge=[True, False],
        priority_export_slots=[True, False],
        priority_export_enabled=True,
    )

    assert result.schedule.actions[0].action != "export"
    assert result.grid_export_w[0] == pytest.approx(0.0, abs=0.1)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_below_acquisition_priority_export_is_paired_to_reachable_recharge(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """One future charge kWh may authorize only one paired export kWh."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=1_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        interval_minutes=60,
        horizon_hours=3,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.10, 0.50],
        export_prices=[0.20, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 0.0, 0.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.50,
        allow_battery_export=[True, False, False],
        block_battery_charge=[True, False, False],
        allow_grid_charge=True,
        priority_export_slots=[True, False, False],
        priority_export_enabled=True,
    )

    assert result.grid_export_w[0] == pytest.approx(1000.0, abs=0.1)
    assert result.schedule.actions[1].battery_charge_w == pytest.approx(
        1000.0, abs=0.1
    )
    assert result.schedule.actions[-1].soc == pytest.approx(1.0, abs=1e-6)
    assert result.schedule.predicted_cost == pytest.approx(-0.10, abs=0.01)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_capped_future_rebate_cannot_fund_unlimited_priority_export(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """A one-kWh future rebate remains quantity-limited in both backends."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=10_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        interval_minutes=60,
        horizon_hours=3,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.50, 0.50],
        export_prices=[0.40, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 0.0, 9.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.50,
        allow_battery_export=[True, False, False],
        block_battery_charge=[True, False, False],
        allow_grid_charge=True,
        import_bonus_prices=[0.0, 0.50, 0.0],
        import_bonus_cap_kwh=1.0,
        priority_export_slots=[True, False, False],
        priority_export_enabled=True,
    )

    assert result.grid_export_w[0] == pytest.approx(1000.0, abs=0.1)
    assert result.schedule.actions[1].battery_charge_w == pytest.approx(
        1000.0, abs=0.1
    )
    assert result.grid_import_w[2] == pytest.approx(0.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(-0.40, abs=0.01)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_paired_recharge_obeys_grid_charge_soc_cap(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """No grid-charge headroom means no below-acquisition paired export."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        grid_charge_soc_cap=0.50,
        interval_minutes=60,
        horizon_hours=2,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.10],
        export_prices=[0.20, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=1.0,
        acquisition_cost_kwh=0.50,
        allow_battery_export=[True, False],
        block_battery_charge=[True, False],
        allow_grid_charge=True,
        priority_export_slots=[True, False],
        priority_export_enabled=True,
    )

    assert result.grid_export_w[0] == pytest.approx(0.0, abs=0.1)
    assert sum(action.battery_charge_w for action in result.schedule.actions) == (
        pytest.approx(0.0, abs=0.1)
    )


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_unknown_hardware_reserve_falls_back_to_optimizer_reserve(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Without a known hardware floor, 20% remains the conservative floor."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.20,
        hardware_reserve=None,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 1.0, 1.0],
        current_soc=0.40,
        allow_battery_export=[False, False, False],
    )

    assert min(action.soc for action in result.schedule.actions) >= 0.20 - 1e-6
    assert result.schedule.actions[-1].soc == pytest.approx(0.20, abs=1e-6)
    assert sum(action.battery_discharge_w for action in result.schedule.actions) == pytest.approx(
        2000.0,
        abs=0.1,
    )
    assert sum(result.grid_import_w) == pytest.approx(1000.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(0.50, abs=0.01)


def test_disable_idle_is_modeled_before_future_export(
    battery_optimizer_module,
    monkeypatch,
):
    """No Idle consumes naturally and reduces the later feasible export."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")

    def _solve(disable_idle: bool):
        optimizer = _optimizer(
            module,
            backup_reserve=0.10,
            hardware_reserve=0.10,
            horizon_hours=2,
        )
        return optimizer.optimize(
            import_prices=[0.20, 0.20],
            export_prices=[0.0, 0.80],
            solar_forecast=[0.0, 0.0],
            load_forecast=[1.0, 0.0],
            current_soc=0.30,
            allow_battery_export=[False, True],
            block_battery_charge=[False, True],
            allow_grid_charge=False,
            disable_idle=disable_idle,
        )

    idle_allowed = _solve(False)
    no_idle = _solve(True)

    assert idle_allowed.schedule.actions[0].action == "idle"
    assert idle_allowed.grid_import_w[0] == pytest.approx(1000.0, abs=0.1)
    assert idle_allowed.grid_export_w[1] == pytest.approx(2000.0, abs=0.1)

    assert no_idle.schedule.actions[0].action == "self_consumption"
    assert no_idle.grid_import_w[0] == pytest.approx(0.0, abs=0.1)
    assert no_idle.grid_export_w[1] == pytest.approx(1000.0, abs=0.1)
    assert no_idle.schedule.actions[-1].soc == pytest.approx(0.10, abs=1e-6)
    assert no_idle.schedule.predicted_cost == pytest.approx(-0.80, abs=0.01)
    graph_data = no_idle.schedule.to_api_response()
    assert graph_data["battery_consume_w"][0] == pytest.approx(1000.0, abs=0.1)
    assert graph_data["battery_export_w"][0] == pytest.approx(0.0, abs=0.1)


def test_disable_idle_stays_self_consumption_at_hardware_floor_before_recovery(
    battery_optimizer_module,
    monkeypatch,
):
    """A future charge must not turn floor-bound No Idle slots back into IDLE."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(
        module,
        backup_reserve=0.15,
        hardware_reserve=0.05,
        max_charge_w=5000,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.10, 0.50],
        export_prices=[0.0, 0.0, 1.00],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 0.0, 0.0],
        current_soc=0.05,
        allow_battery_export=[False, False, True],
        block_battery_charge=[False, False, True],
        allow_grid_charge=True,
        grid_charge_allowed=[False, True, False],
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.schedule.actions[0].action == "self_consumption"
    assert result.schedule.actions[0].soc == pytest.approx(0.05, abs=1e-6)
    assert result.schedule.actions[0].battery_discharge_w == pytest.approx(0.0)
    assert any(action.action == "charge" for action in result.schedule.actions[1:])


def test_reconcile_result_updates_cost_and_grid_flows_after_final_schedule(
    battery_optimizer_module,
    monkeypatch,
):
    """Post-solve physical schedules have one authoritative flow/cost projection."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(module, horizon_hours=1)
    result = optimizer.optimize(
        import_prices=[0.50],
        export_prices=[0.0],
        solar_forecast=[0.0],
        load_forecast=[1.0],
        current_soc=0.20,
        allow_battery_export=[False],
    )
    action = result.schedule.actions[0]
    action.action = "self_consumption"
    action.battery_discharge_w = 1000.0
    action.battery_charge_w = 0.0
    action.power_w = 1000.0

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[0.50],
        export_prices=[0.0],
        solar=[0.0],
        load=[1.0],
    )

    assert reconciled.grid_import_w == pytest.approx([0.0], abs=0.1)
    assert reconciled.grid_export_w == pytest.approx([0.0], abs=0.1)
    assert reconciled.schedule.predicted_cost == pytest.approx(0.0, abs=0.01)


def test_48_hour_mode_alignment_converges_without_fallback(
    battery_optimizer_module,
    monkeypatch,
):
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = module.BatteryOptimizer(
        capacity_wh=13_500,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=0.92,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=48,
    )
    n = 576

    result = optimizer.optimize(
        import_prices=[0.28] * n,
        export_prices=[0.08] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.7] * n,
        current_soc=0.55,
        allow_battery_export=[False] * n,
    )

    assert result.solver_used == "highs"
    assert result.lp_stats["mode_converged"] is True


def test_mode_projection_matcher_compares_self_use_energy_per_run(
    battery_optimizer_module,
):
    """Projection equivalence uses bounded energy, never raw power samples."""
    module = battery_optimizer_module
    modes = ["self_use", "self_use", "idle", "self_use"]
    slot_hours = 5 / 60

    assert module.BatteryOptimizer._mode_constraints_match(
        modes,
        [1.0, 1.0, 0.0, 1.0],
        modes,
        [1.05, 1.05, 0.0, 1.0],
        slot_hours,
    )
    # Ticket #234's 503-slot self-use run drifted by 17.3 Wh while all command
    # modes remained identical. Reproduce that run shape so the physically
    # equivalent projection converges instead of exhausting the iteration loop.
    ticket_modes = ["self_use"] * 503
    ticket_left = [1.0] * 503
    ticket_right = ticket_left.copy()
    ticket_right[-1] += 0.0173 / slot_hours
    assert module.BatteryOptimizer._mode_constraints_match(
        ticket_modes,
        ticket_left,
        ticket_modes,
        ticket_right,
        slot_hours,
    )
    assert not module.BatteryOptimizer._mode_constraints_match(
        modes,
        [1.0, 1.0, 0.0, 1.0],
        modes,
        [1.13, 1.13, 0.0, 1.0],
        slot_hours,
    )
    assert not module.BatteryOptimizer._mode_constraints_match(
        modes,
        [1.0, 1.0, 0.0, 1.0],
        ["self_use", "idle", "idle", "self_use"],
        [1.0, 1.0, 0.0, 1.0],
        slot_hours,
    )
    # Opposite differences in distinct runs must not cancel horizon-wide.
    assert not module.BatteryOptimizer._mode_constraints_match(
        modes,
        [1.0, 1.0, 0.0, 1.0],
        modes,
        [1.13, 1.13, 0.0, 0.74],
        slot_hours,
    )


def test_flat_price_charge_timing_is_stable_across_reserve_margin(
    battery_optimizer_module,
    monkeypatch,
):
    """A rolling solve must not reverse solely at reserve plus two percent."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    n = 576
    first_actions = []
    first_charge_slots = []
    total_charge_kwh = []
    predicted_costs = []

    for current_soc in (0.0401, 0.0400, 0.0399, 0.0190):
        optimizer = module.BatteryOptimizer(
            capacity_wh=32_000,
            max_charge_w=10_000,
            max_discharge_w=19_200,
            max_grid_import_w=20_000,
            max_grid_export_w=20_000,
            efficiency=0.95,
            backup_reserve=0.02,
            hardware_reserve=0.0,
            interval_minutes=5,
            horizon_hours=48,
            terminal_weight=0.30,
        )
        result = optimizer.optimize(
            import_prices=[0.142 if idx < 84 else 0.302 for idx in range(n)],
            export_prices=[0.093] * n,
            solar_forecast=[0.0] * n,
            load_forecast=[1.2] * n,
            current_soc=current_soc,
            allow_battery_export=[False] * n,
            block_battery_charge=[False] * n,
            allow_grid_charge=True,
            grid_charge_allowed=[True] * n,
            priority_export_slots=[False] * n,
            priority_export_enabled=False,
            disable_idle=False,
        )
        actions = result.schedule.actions
        first_actions.append(actions[0].action)
        first_charge_slots.append(
            next(idx for idx, action in enumerate(actions) if action.action == "charge")
        )
        total_charge_kwh.append(
            sum(action.battery_charge_w for action in actions) / 1000.0 * (5 / 60)
        )
        predicted_costs.append(result.schedule.predicted_cost)
        assert all(0.0 <= action.soc <= 1.0 for action in actions)
        assert all(action.battery_charge_w <= 10_000.1 for action in actions)

    assert first_actions[:3] == ["self_consumption"] * 3
    assert first_actions[3] != "charge"
    assert len(set(first_charge_slots[:3])) == 1
    assert all(slot < 84 for slot in first_charge_slots)
    assert total_charge_kwh[:3] == pytest.approx(
        [total_charge_kwh[0]] * 3,
        abs=0.02,
    )
    assert predicted_costs[:3] == pytest.approx(
        [predicted_costs[0]] * 3,
        abs=0.01,
    )


def test_flat_price_pre_window_deadline_still_prefers_earlier_charge(
    battery_optimizer_module,
    monkeypatch,
):
    """A genuine SOC deadline retains the explicit prefer-earlier tie-break."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=2_000,
        max_discharge_w=2_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.05,
        interval_minutes=60,
        horizon_hours=4,
        terminal_weight=0.0,
    )
    optimizer.pre_window_slot = 3
    optimizer.pre_window_soc_target = 0.70

    result = optimizer.optimize(
        import_prices=[0.10, 0.10, 0.10, 0.50],
        export_prices=[0.0] * 4,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
        current_soc=0.30,
        allow_battery_export=[False] * 4,
        allow_grid_charge=True,
    )

    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.actions[2].soc >= 0.695
    assert max(action.battery_charge_w for action in result.schedule.actions) <= 2000.1


def test_export_clipped_by_reserve_with_concurrent_home_load_reclassifies_to_self_use(
    battery_optimizer_module,
):
    """A clipped export command must not survive when no energy reaches grid."""
    module = battery_optimizer_module
    optimizer = _optimizer(module, horizon_hours=1)
    timestamp = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
    action = module.ScheduleAction(
        timestamp=timestamp,
        action="export",
        power_w=1000.0,
        soc=0.10,
        battery_charge_w=0.0,
        battery_discharge_w=2000.0,
    )
    schedule = module.OptimizationSchedule(
        actions=[action],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )
    result = module.OptimizerResult(
        schedule=schedule,
        feasible=True,
        grid_import_w=[0.0],
        grid_export_w=[1000.0],
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        schedule,
        import_prices=[0.50],
        export_prices=[0.60],
        solar=[0.0],
        load=[1.0],
        initial_soc=0.30,
    )

    final = reconciled.schedule.actions[0]
    assert reconciled.grid_export_w == pytest.approx([0.0], abs=0.1)
    assert final.action == "self_consumption"
    assert final.power_w == pytest.approx(1000.0, abs=0.1)
    assert final.battery_discharge_w == pytest.approx(1000.0, abs=0.1)
    assert final.soc == pytest.approx(0.20, abs=1e-6)


def test_disable_idle_keeps_charge_by_time_deadline_feasible(
    battery_optimizer_module,
    monkeypatch,
):
    """No Idle must not consume or block energy needed by an explicit deadline."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=2000,
        horizon_hours=3,
    )
    optimizer.pre_window_slot = 2
    optimizer.pre_window_soc_target = 0.50

    result = optimizer.optimize(
        import_prices=[0.10, 0.10, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 1.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[False, False, False],
        allow_grid_charge=True,
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["mode_converged"] is True
    assert result.schedule.actions[1].soc >= 0.495
    # The LP deliberately leaves its standard 0.5% reachability buffer rather
    # than making a numerically tight deadline row infeasible.
    assert sum(
        action.battery_charge_w / 1000.0
        for action in result.schedule.actions[:2]
    ) == pytest.approx(3.95, abs=0.01)


def test_disable_idle_uses_future_charge_headroom_before_deadline(
    battery_optimizer_module,
    monkeypatch,
):
    """A deadline hold is unnecessary when a later charge slot has headroom."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=3000,
        horizon_hours=3,
    )
    optimizer.pre_window_slot = 2
    optimizer.pre_window_soc_target = 0.60

    result = optimizer.optimize(
        import_prices=[0.10, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 0.0, 0.0],
        current_soc=0.40,
        allow_battery_export=[False, False, False],
        block_battery_charge=[True, False, False],
        allow_grid_charge=True,
        grid_charge_allowed=[False, True, False],
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["mode_converged"] is True
    assert result.schedule.actions[0].action == "self_consumption"
    assert result.schedule.actions[0].battery_discharge_w == pytest.approx(1000.0)
    assert result.schedule.actions[1].action == "charge"
    assert result.schedule.actions[1].battery_charge_w == pytest.approx(3000.0)
    assert result.schedule.actions[1].soc >= 0.595


def test_disable_idle_deadline_reachability_respects_grid_charge_soc_cap(
    battery_optimizer_module,
    monkeypatch,
):
    """Grid charge above its SOC cap cannot justify replacing a deadline hold."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=3000,
        horizon_hours=3,
    )
    optimizer.grid_charge_soc_cap = 0.50
    optimizer.pre_window_slot = 2
    optimizer.pre_window_soc_target = 0.60

    result = optimizer.optimize(
        import_prices=[0.10, 0.50, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 0.0, 0.0],
        current_soc=0.40,
        allow_battery_export=[False, False, False],
        block_battery_charge=[True, False, False],
        allow_grid_charge=True,
        grid_charge_allowed=[False, True, False],
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["mode_converged"] is True
    assert result.schedule.actions[0].action == "idle"
    assert result.schedule.actions[1].soc <= 0.5001


def test_disable_idle_holds_after_last_charge_to_protect_deadline(
    battery_optimizer_module,
    monkeypatch,
):
    """A post-charge home-load slot cannot consume the explicit SOC target."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=2000,
        horizon_hours=3,
    )
    optimizer.pre_window_slot = 3
    optimizer.pre_window_soc_target = 0.60

    result = optimizer.optimize(
        import_prices=[0.10, 0.40, 0.50],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 0.0, 1.0],
        current_soc=0.40,
        allow_battery_export=[False, False, False],
        allow_grid_charge=True,
        grid_charge_allowed=[True, False, False],
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["mode_converged"] is True
    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.actions[2].action == "idle"
    assert result.schedule.actions[2].soc >= 0.595


def test_disable_idle_does_not_hold_for_export_capped_at_reserve(
    battery_optimizer_module,
):
    """A capped future export must not create a false deadline shortfall."""
    module = battery_optimizer_module
    optimizer = _optimizer(
        module,
        backup_reserve=0.76,
        hardware_reserve=0.05,
        horizon_hours=4,
    )
    optimizer.pre_window_slot = 4
    optimizer.pre_window_soc_target = 1.0
    optimizer.export_reserve_floor = 0.76
    start = datetime(2026, 7, 14, 6, 40, tzinfo=timezone.utc)

    schedule = optimizer._build_schedule(
        n=4,
        grid_import=[1.0, 0.0, 0.0, 0.0],
        grid_export=[0.0, 2.4, 0.0, 0.0],
        battery_charge=[0.0, 0.0, 2.4, 0.0],
        battery_discharge=[0.0, 2.4, 0.0, 0.0],
        solar=[0.0, 0.0, 2.4, 0.0],
        load=[1.0, 0.0, 0.0, 0.0],
        soc_0=1.0,
        import_prices=[0.50, 0.50, 0.10, 0.10],
        export_prices=[0.0, 0.45, 0.0, 0.0],
        block_battery_charge=[False] * 4,
        schedule_timestamps=[start + timedelta(hours=slot) for slot in range(4)],
        allow_grid_charge=True,
        grid_charge_allowed=[True] * 4,
        priority_export_slots=[False, True, False, False],
        disable_idle=True,
    )

    assert schedule.actions[0].action == "self_consumption"
    assert schedule.actions[0].battery_discharge_w == pytest.approx(1000.0)
    assert schedule.actions[1].action == "export"
    assert schedule.actions[1].battery_discharge_w == pytest.approx(1400.0)
    assert schedule.actions[1].soc == pytest.approx(0.76)
    assert schedule.actions[2].soc == pytest.approx(1.0)


def test_greedy_charges_when_cheap_energy_can_fund_profitable_export(
    battery_optimizer_module,
    monkeypatch,
):
    """Fallback charging must value a later export, not only future home load."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        interval_minutes=60,
        horizon_hours=2,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.10, 0.50],
        export_prices=[0.0, 0.60],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 0.0],
        current_soc=0.10,
        allow_battery_export=[False, True],
        block_battery_charge=[False, True],
        allow_grid_charge=True,
    )

    assert result.solver_used == "greedy"
    assert result.schedule.actions[0].action == "charge"
    assert result.schedule.actions[0].battery_charge_w == pytest.approx(5000.0)
    assert result.schedule.actions[1].action == "export"
    assert result.grid_export_w[1] == pytest.approx(5000.0, abs=0.1)
    assert result.schedule.actions[-1].soc == pytest.approx(0.10, abs=1e-6)
    assert result.schedule.predicted_cost == pytest.approx(-2.50, abs=0.01)


def test_greedy_future_import_bonus_prevents_uneconomic_earlier_charge(
    battery_optimizer_module,
    monkeypatch,
):
    """A future fully rebated import must not be valued at its raw retail rate."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        horizon_hours=2,
    )

    result = optimizer.optimize(
        import_prices=[0.20, 0.50],
        export_prices=[0.0, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[False, False],
        allow_grid_charge=True,
        import_bonus_prices=[0.0, 0.50],
        import_bonus_cap_kwh=1.0,
    )

    assert result.solver_used == "greedy"
    assert all(action.action != "charge" for action in result.schedule.actions)
    assert sum(
        action.battery_charge_w for action in result.schedule.actions
    ) == pytest.approx(0.0, abs=0.1)
    assert result.grid_import_w == pytest.approx([0.0, 1000.0], abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(0.0, abs=0.01)


def test_greedy_import_bonus_cap_applies_only_to_marginal_charge_kwh(
    battery_optimizer_module,
    monkeypatch,
):
    """A 1 kWh credit cannot make all 5 kWh of charging appear free."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=5000,
        horizon_hours=2,
    )

    result = optimizer.optimize(
        import_prices=[0.60, 0.40],
        export_prices=[0.0, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 5.0],
        current_soc=0.10,
        allow_battery_export=[False, False],
        allow_grid_charge=True,
        import_bonus_prices=[0.60, 0.0],
        import_bonus_cap_kwh=1.0,
    )

    assert result.schedule.actions[0].battery_charge_w == pytest.approx(1000.0)
    assert result.grid_import_w == pytest.approx([1000.0, 4000.0], abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(1.60, abs=0.01)


def test_greedy_import_bonus_is_consumed_by_concurrent_home_load_first(
    battery_optimizer_module,
    monkeypatch,
):
    """Battery charging cannot reuse a credit already settling home import."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        horizon_hours=2,
    )

    result = optimizer.optimize(
        import_prices=[0.50, 0.40],
        export_prices=[0.0, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[1.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[False, False],
        allow_grid_charge=True,
        import_bonus_prices=[0.50, 0.0],
        import_bonus_cap_kwh=1.0,
    )

    assert all(action.action != "charge" for action in result.schedule.actions)
    assert result.grid_import_w == pytest.approx([1000.0, 1000.0], abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(0.40, abs=0.01)


def test_greedy_reallocates_charge_after_chronological_export_headroom(
    battery_optimizer_module,
    monkeypatch,
):
    """A full battery must use the cheap slot after export, not a clipped one before."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=5000,
        horizon_hours=5,
    )

    result = optimizer.optimize(
        import_prices=[0.10, 0.50, 0.50, 0.10, 0.50],
        export_prices=[0.0, 0.0, 0.60, 0.0, 0.0],
        solar_forecast=[0.0] * 5,
        load_forecast=[0.0, 0.0, 0.0, 0.0, 5.0],
        current_soc=1.0,
        allow_battery_export=[False, False, True, False, False],
        block_battery_charge=[False, False, True, False, False],
        allow_grid_charge=True,
        priority_export_slots=[False, False, True, False, False],
        priority_export_enabled=True,
    )

    assert result.schedule.actions[0].battery_charge_w == pytest.approx(0.0, abs=0.1)
    assert result.schedule.actions[2].action == "export"
    assert result.schedule.actions[3].battery_charge_w == pytest.approx(5000.0)
    assert result.grid_import_w[4] == pytest.approx(0.0, abs=0.1)
    assert result.schedule.predicted_cost == pytest.approx(-4.90, abs=0.01)


def test_greedy_import_bonus_is_allocated_chronologically(
    battery_optimizer_module,
    monkeypatch,
):
    """Earlier battery import may consume a cap before later household import."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=1000,
        horizon_hours=3,
    )

    result = optimizer.optimize(
        import_prices=[1.20, 0.40, 0.40],
        export_prices=[0.0, 0.0, 1.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 1.0, 0.0],
        current_soc=0.10,
        allow_battery_export=[False, False, True],
        block_battery_charge=[False, True, True],
        allow_grid_charge=True,
        import_bonus_prices=[1.20, 0.40, 0.0],
        import_bonus_cap_kwh=1.0,
        priority_export_slots=[False, False, True],
        priority_export_enabled=True,
    )

    assert result.schedule.actions[0].battery_charge_w == pytest.approx(1000.0)
    assert result.schedule.actions[2].action == "export"
    assert result.schedule.predicted_cost == pytest.approx(-0.60, abs=0.01)


@pytest.mark.parametrize(
    ("load_kw", "export_price", "expected_cost"),
    [(5.0, 0.0, 0.30), (0.0, 0.50, -2.20)],
)
def test_greedy_adds_newly_funded_output_to_existing_discharge(
    battery_optimizer_module,
    monkeypatch,
    load_kw,
    export_price,
    expected_cost,
):
    """Initial battery energy and newly charged energy must both be emitted."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "greedy")
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        interval_minutes=60,
        horizon_hours=2,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.10, 0.50 if load_kw > 0 else 0.70],
        export_prices=[0.0, export_price],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, load_kw],
        current_soc=0.30,
        acquisition_cost_kwh=0.10 if export_price > 0 else 0.0,
        allow_battery_export=[False, export_price > 0],
        block_battery_charge=[False, export_price > 0],
        allow_grid_charge=True,
        priority_export_slots=[False, export_price > 0],
        priority_export_enabled=export_price > 0,
    )

    assert result.schedule.actions[0].battery_charge_w == pytest.approx(3000.0)
    assert result.schedule.actions[1].battery_discharge_w == pytest.approx(5000.0)
    assert result.schedule.actions[-1].soc == pytest.approx(0.10, abs=1e-6)
    assert result.schedule.predicted_cost == pytest.approx(expected_cost, abs=0.01)


def test_finalizer_uses_solve_local_optimizer_reserve(
    battery_optimizer_module,
):
    """A concurrent config update must not change the floor of an in-flight solve."""
    module = battery_optimizer_module
    optimizer = _optimizer(module, backup_reserve=0.20, horizon_hours=1)
    timestamp = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
    action = module.ScheduleAction(
        timestamp=timestamp,
        action="export",
        power_w=2000.0,
        soc=0.10,
        battery_charge_w=0.0,
        battery_discharge_w=2000.0,
    )
    schedule = module.OptimizationSchedule(
        actions=[action],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )
    result = module.OptimizerResult(
        schedule=schedule,
        feasible=True,
        grid_import_w=[0.0],
        grid_export_w=[2000.0],
    )
    # Simulate a settings update landing after the solve but before its final
    # schedule is reconciled and published.
    optimizer.update_config(backup_reserve=0.05)

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        schedule,
        import_prices=[0.50],
        export_prices=[0.60],
        solar=[0.0],
        load=[0.0],
        initial_soc=0.30,
        optimizer_reserve=0.20,
    )

    final = reconciled.schedule.actions[0]
    assert final.battery_discharge_w == pytest.approx(1000.0, abs=0.1)
    assert final.soc == pytest.approx(0.20, abs=1e-6)
    assert reconciled.grid_export_w == pytest.approx([1000.0], abs=0.1)


def test_off_grid_final_schedule_reports_zero_grid_flows(
    battery_optimizer_module,
):
    """An islanded overlay cannot report either grid import or grid export."""
    module = battery_optimizer_module
    optimizer = _optimizer(module, horizon_hours=1)
    timestamp = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    action = module.ScheduleAction(
        timestamp=timestamp,
        action="off_grid",
        power_w=0.0,
        soc=0.10,
        battery_charge_w=0.0,
        battery_discharge_w=0.0,
    )
    schedule = module.OptimizationSchedule(
        actions=[action],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )
    result = module.OptimizerResult(
        schedule=schedule,
        feasible=True,
        grid_import_w=[1000.0],
        grid_export_w=[0.0],
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        schedule,
        import_prices=[0.50],
        export_prices=[-0.10],
        solar=[0.0],
        load=[1.0],
        initial_soc=0.10,
    )

    assert reconciled.schedule.actions[0].action == "off_grid"
    assert reconciled.grid_import_w == pytest.approx([0.0], abs=0.1)
    assert reconciled.grid_export_w == pytest.approx([0.0], abs=0.1)
    assert reconciled.schedule.predicted_cost == pytest.approx(0.0, abs=0.01)


def test_off_grid_finalizer_accounts_for_local_load_in_soc(
    battery_optimizer_module,
):
    """Islanding must serve local load from the battery, not erase the energy."""
    module = battery_optimizer_module
    optimizer = _optimizer(module, horizon_hours=1)
    timestamp = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    action = module.ScheduleAction(
        timestamp=timestamp,
        action="off_grid",
        power_w=0.0,
        soc=0.80,
        battery_charge_w=0.0,
        battery_discharge_w=0.0,
    )
    schedule = module.OptimizationSchedule(
        actions=[action],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )
    result = module.OptimizerResult(schedule=schedule)

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        schedule,
        import_prices=[0.50],
        export_prices=[-0.10],
        solar=[0.0],
        load=[1.0],
        initial_soc=0.80,
    )

    final = reconciled.schedule.actions[0]
    assert final.battery_discharge_w == pytest.approx(1000.0, abs=0.1)
    assert final.soc == pytest.approx(0.70, abs=1e-6)
    assert reconciled.grid_import_w == pytest.approx([0.0], abs=0.1)


def test_finalizer_caps_battery_export_to_remaining_site_headroom(
    battery_optimizer_module,
):
    """The final command and grid flow must share the configured site cap."""
    module = battery_optimizer_module
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=10_000,
        max_discharge_w=10_000,
        max_grid_export_w=1_000,
        efficiency=1.0,
        backup_reserve=0.20,
        hardware_reserve=0.10,
        interval_minutes=60,
        horizon_hours=1,
        terminal_weight=0.0,
    )
    timestamp = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
    action = module.ScheduleAction(
        timestamp=timestamp,
        action="export",
        power_w=5000.0,
        soc=0.30,
        battery_charge_w=0.0,
        battery_discharge_w=5000.0,
    )
    schedule = module.OptimizationSchedule(
        actions=[action],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.50],
        export_prices=[0.60],
        solar=[0.0],
        load=[1.0],
        initial_soc=0.80,
    )

    final = reconciled.schedule.actions[0]
    assert final.action == "export"
    assert final.power_w == pytest.approx(1000.0, abs=0.1)
    assert final.battery_discharge_w == pytest.approx(2000.0, abs=0.1)
    assert reconciled.grid_export_w == pytest.approx([1000.0], abs=0.1)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_full_battery_free_import_keeps_full_slot_charge_command(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """A free-import command persists even when no battery energy can be accepted."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        horizon_hours=1,
    )
    result = optimizer.optimize(
        import_prices=[0.0],
        export_prices=[0.0],
        solar_forecast=[0.0],
        load_forecast=[0.0],
        current_soc=1.0,
        allow_battery_export=[False],
        allow_grid_charge=True,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[0.0],
        export_prices=[0.0],
        solar=[0.0],
        load=[0.0],
        initial_soc=1.0,
    )

    final = reconciled.schedule.actions[0]
    assert final.action == "charge"
    assert final.power_w == pytest.approx(10_000.0, abs=0.1)
    assert final.battery_charge_w == pytest.approx(0.0, abs=0.1)
    assert final.battery_discharge_w == pytest.approx(0.0, abs=0.1)
    assert final.soc == pytest.approx(1.0, abs=1e-6)
    assert reconciled.grid_import_w == pytest.approx([0.0], abs=0.1)
    assert reconciled.grid_export_w == pytest.approx([0.0], abs=0.1)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_free_import_emission_obeys_grid_charge_soc_cap(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """The free-import override cannot bypass the modeled grid-charge cap."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        max_grid_import_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        grid_charge_soc_cap=0.40,
        interval_minutes=60,
        horizon_hours=1,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[-0.10],
        export_prices=[0.0],
        solar_forecast=[0.0],
        load_forecast=[0.0],
        current_soc=0.10,
        allow_battery_export=[False],
        allow_grid_charge=True,
    )

    action = result.schedule.actions[0]
    assert action.action == "charge"
    assert action.power_w == pytest.approx(5000.0, abs=0.1)
    assert action.battery_charge_w == pytest.approx(3000.0, abs=0.1)
    assert action.soc == pytest.approx(0.40, abs=1e-6)
    assert result.grid_import_w == pytest.approx([3000.0], abs=0.1)

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[-0.10],
        export_prices=[0.0],
        solar=[0.0],
        load=[0.0],
        initial_soc=0.10,
    )
    action = reconciled.schedule.actions[0]
    assert action.action == "charge"
    assert action.power_w == pytest.approx(5000.0, abs=0.1)
    assert action.battery_charge_w == pytest.approx(3000.0, abs=0.1)
    assert action.soc == pytest.approx(0.40, abs=1e-6)
    assert reconciled.grid_import_w == pytest.approx([3000.0], abs=0.1)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
@pytest.mark.parametrize("load_kw", [0.0, 1.0])
def test_free_import_at_grid_soc_cap_keeps_charge_command(
    battery_optimizer_module,
    monkeypatch,
    backend,
    load_kw,
):
    """The SOC cap clips accepted energy, not the free-slot command mode."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        max_grid_import_w=6_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        grid_charge_soc_cap=0.40,
        interval_minutes=60,
        horizon_hours=1,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[0.0],
        export_prices=[0.0],
        solar_forecast=[0.0],
        load_forecast=[load_kw],
        current_soc=0.40,
        allow_battery_export=[False],
        allow_grid_charge=True,
    )

    emitted = result.schedule.actions[0]
    assert emitted.action == "charge"
    assert emitted.power_w == pytest.approx(5000.0, abs=0.1)
    assert emitted.battery_charge_w == pytest.approx(0.0, abs=0.1)
    assert emitted.battery_discharge_w == pytest.approx(0.0, abs=0.1)
    assert emitted.soc == pytest.approx(0.40, abs=1e-6)

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[0.0],
        export_prices=[0.0],
        solar=[0.0],
        load=[load_kw],
        initial_soc=0.40,
    )
    final = reconciled.schedule.actions[0]
    assert final.action == "charge"
    assert final.power_w == pytest.approx(5000.0, abs=0.1)
    assert final.battery_charge_w == pytest.approx(0.0, abs=0.1)
    assert final.battery_discharge_w == pytest.approx(0.0, abs=0.1)
    assert final.soc == pytest.approx(0.40, abs=1e-6)
    assert reconciled.grid_import_w == pytest.approx([load_kw * 1000.0], abs=0.1)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_free_import_command_persists_after_cap_headroom_is_exhausted(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Later free intervals keep charging mode without inventing energy or value."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        max_grid_import_w=6_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        grid_charge_soc_cap=0.40,
        interval_minutes=60,
        horizon_hours=3,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[-0.10, -0.10, -0.10],
        export_prices=[0.0, 0.0, 0.0],
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[1.0, 1.0, 1.0],
        current_soc=0.30,
        allow_battery_export=[False, False, False],
        allow_grid_charge=True,
    )
    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[-0.10, -0.10, -0.10],
        export_prices=[0.0, 0.0, 0.0],
        solar=[0.0, 0.0, 0.0],
        load=[1.0, 1.0, 1.0],
        initial_soc=0.30,
    )

    assert [action.action for action in reconciled.schedule.actions] == [
        "charge",
        "charge",
        "charge",
    ]
    assert [action.power_w for action in reconciled.schedule.actions] == pytest.approx(
        [5000.0, 5000.0, 5000.0], abs=0.1
    )
    assert [
        action.battery_charge_w for action in reconciled.schedule.actions
    ] == pytest.approx([1000.0, 0.0, 0.0], abs=0.1)
    assert [action.soc for action in reconciled.schedule.actions] == pytest.approx(
        [0.40, 0.40, 0.40], abs=1e-6
    )
    assert reconciled.grid_import_w == pytest.approx(
        [2000.0, 1000.0, 1000.0], abs=0.1
    )
    assert reconciled.grid_export_w == pytest.approx([0.0, 0.0, 0.0], abs=0.1)
    assert reconciled.schedule.predicted_cost == pytest.approx(-0.40, abs=0.01)
    assert reconciled.schedule.predicted_savings == pytest.approx(0.10, abs=0.01)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_solar_only_charge_above_grid_soc_cap_keeps_free_slot_command(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    """Solar remains physical flow while free-slot charge intent persists."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = module.BatteryOptimizer(
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        max_grid_import_w=5_000,
        efficiency=1.0,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        grid_charge_soc_cap=0.40,
        interval_minutes=60,
        horizon_hours=1,
        terminal_weight=0.0,
    )

    result = optimizer.optimize(
        import_prices=[-0.10],
        export_prices=[0.0],
        solar_forecast=[3.0],
        load_forecast=[0.0],
        current_soc=0.50,
        allow_battery_export=[False],
        allow_grid_charge=True,
    )
    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=[-0.10],
        export_prices=[0.0],
        solar=[3.0],
        load=[0.0],
        initial_soc=0.50,
    )

    action = reconciled.schedule.actions[0]
    assert action.action == "charge"
    assert action.power_w == pytest.approx(5000.0, abs=0.1)
    assert action.battery_charge_w == pytest.approx(3000.0, abs=0.1)
    assert action.soc == pytest.approx(0.80, abs=1e-6)
    assert reconciled.grid_import_w == pytest.approx([0.0], abs=0.1)


def test_non_free_clipped_charge_still_returns_to_self_consumption(
    battery_optimizer_module,
):
    """The full-slot command exception stops immediately above the free threshold."""
    module = battery_optimizer_module
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=5000,
        horizon_hours=1,
    )
    timestamp = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
    schedule = module.OptimizationSchedule(
        actions=[
            module.ScheduleAction(
                timestamp=timestamp,
                action="charge",
                power_w=5000.0,
                soc=1.0,
                battery_charge_w=5000.0,
                battery_discharge_w=0.0,
            )
        ],
        predicted_cost=0.0,
        predicted_savings=0.0,
        last_updated=timestamp,
    )

    reconciled = optimizer.reconcile_result_with_schedule(
        module.OptimizerResult(schedule=schedule),
        schedule,
        import_prices=[0.0011],
        export_prices=[0.0],
        solar=[0.0],
        load=[0.0],
        initial_soc=1.0,
    )

    final = reconciled.schedule.actions[0]
    assert final.action == "self_consumption"
    assert final.power_w == pytest.approx(0.0, abs=0.1)
    assert final.battery_charge_w == pytest.approx(0.0, abs=0.1)
    assert final.battery_discharge_w == pytest.approx(0.0, abs=0.1)
    assert final.soc == pytest.approx(1.0, abs=1e-6)


def test_realistic_48_hour_flow_power_shape_does_not_collapse_to_greedy(
    battery_optimizer_module,
    monkeypatch,
):
    """Two Happy Hours plus cheap import and solar must converge under HiGHS."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    n = 576
    import_prices = [0.35] * n
    export_prices = [0.05] * n
    solar = [0.0] * n
    load = [0.8] * n
    allow_export = [False] * n
    block_charge = [False] * n
    priority_export = [False] * n

    for start, end, price in ((20, 44, 0.50), (400, 424, 0.60)):
        for idx in range(start, end):
            export_prices[idx] = price
            allow_export[idx] = True
            block_charge[idx] = True
            priority_export[idx] = True
    for idx in range(80, 104):
        import_prices[idx] = 0.10
    for idx in range(200, 236):
        solar[idx] = 4.0

    optimizer = module.BatteryOptimizer(
        capacity_wh=13_500,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        max_grid_import_w=7_000,
        max_grid_export_w=5_000,
        efficiency=0.92,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=48,
        terminal_weight=0.30,
    )

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.80,
        allow_battery_export=allow_export,
        block_battery_charge=block_charge,
        allow_grid_charge=True,
        grid_charge_allowed=[True] * n,
        priority_export_slots=priority_export,
        priority_export_enabled=True,
        disable_idle=False,
    )

    assert result.solver_used == "highs"
    assert result.lp_stats["mode_converged"] is True
    assert result.lp_stats["mode_iterations"] <= module.MODE_PROJECTION_MAX_ITERATIONS
    assert any(action.action == "charge" for action in result.schedule.actions)
    assert any(action.action == "export" for action in result.schedule.actions)
    assert max(result.grid_export_w) > 100.0
    assert result.lp_stats["mode_iterations"] <= module.MODE_PROJECTION_MAX_ITERATIONS
    assert len(result.schedule.actions) == n


def test_infeasible_later_mode_pass_keeps_last_feasible_highs_plan(
    battery_optimizer_module,
    monkeypatch,
):
    """A projected No-Idle pass cannot erase an already feasible two-day plan."""
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, "highs")
    n = 576
    import_prices = [0.25] * n
    export_prices = [0.05] * n
    solar = [0.0] * n
    load = [1.2] * n
    allow_export = [False] * n
    block_charge = [False] * n
    priority_export = [False] * n
    for start, end, price in ((42, 66, 0.70), (380, 410, 0.55)):
        for idx in range(start, end):
            export_prices[idx] = price
            allow_export[idx] = True
            block_charge[idx] = True
            priority_export[idx] = True
    cheap = [
        0.15, 0.10, 0.15, 0.15, 0.10, 0.15, 0.15, 0.15,
        0.10, 0.10, 0.15, 0.05, 0.05, 0.15, 0.10, 0.10,
        0.15, 0.05, 0.10, 0.05, 0.05, 0.15, 0.15, 0.10,
    ]
    solar_shape = [
        4.0, 4.0, 6.0, 6.0, 4.0, 4.0, 4.0, 2.5,
        2.5, 2.5, 2.5, 2.5, 2.5, 2.5, 2.5, 4.0,
        6.0, 2.5, 2.5, 6.0, 4.0, 4.0, 4.0, 4.0,
    ]
    import_prices[81:105] = cheap
    solar[212:236] = solar_shape
    optimizer = module.BatteryOptimizer(
        capacity_wh=27_000,
        max_charge_w=7_000,
        max_discharge_w=3_000,
        max_grid_import_w=7_000,
        max_grid_export_w=5_000,
        efficiency=0.95,
        backup_reserve=0.20,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=48,
        terminal_weight=0.30,
    )

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.3327574593,
        allow_battery_export=allow_export,
        block_battery_charge=block_charge,
        allow_grid_charge=True,
        grid_charge_allowed=[True] * n,
        priority_export_slots=priority_export,
        priority_export_enabled=True,
        disable_idle=True,
    )

    assert result.solver_used == "highs"
    assert result.feasible is True
    assert result.lp_stats["fallback_reason"] == (
        "mode_projection_infeasible_projected_highs"
    )
    assert any(action.action == "charge" for action in result.schedule.actions)


@pytest.mark.parametrize("backend", ["highs", "greedy"])
def test_negative_import_does_not_create_same_slot_charge_export_loop(
    battery_optimizer_module,
    monkeypatch,
    backend,
):
    module = battery_optimizer_module
    _select_backend(module, monkeypatch, backend)
    optimizer = _optimizer(
        module,
        backup_reserve=0.10,
        hardware_reserve=0.10,
        max_charge_w=1000,
        horizon_hours=2,
    )

    result = optimizer.optimize(
        import_prices=[-0.10, 0.50],
        export_prices=[0.20, 0.0],
        solar_forecast=[0.0, 0.0],
        load_forecast=[0.0, 1.0],
        current_soc=0.10,
        allow_battery_export=[True, False],
    )

    first = result.schedule.actions[0]
    assert first.action == "charge"
    assert first.battery_charge_w == pytest.approx(1000.0, abs=0.1)
    assert first.battery_discharge_w == pytest.approx(0.0, abs=0.1)
    assert result.grid_export_w[0] == pytest.approx(0.0, abs=0.1)
