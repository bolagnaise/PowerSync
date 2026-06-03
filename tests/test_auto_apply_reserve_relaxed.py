"""Regression tests: a relaxed (infeasible) solve must not lower the reserve.

Background: with Profit Max + Auto-Apply Optimizer Reserve, the constrained LP
often went infeasible. The relaxed fallback temporarily drops backup_reserve to
5% and re-solves, so any reserve recommendation it builds reflects that
artificial floor. Auto-Apply was then writing that 5% "recommendation" to the
battery, collapsing the optimiser reserve to the hardware floor and never
recovering. These tests pin the invariant that a relaxed solve emits no usable
reserve recommendation.
"""

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
        backup_reserve=0.30,
        hardware_reserve=0.05,
        interval_minutes=5,
        horizon_hours=1,
    )


def _result_with_recommendation(module, *, feasible: bool):
    return module.OptimizerResult(
        schedule=module.OptimizationSchedule(
            actions=[],
            predicted_cost=0.0,
            predicted_savings=0.0,
            last_updated=module.dt_util.now(),
        ),
        solver_used="highs",
        feasible=feasible,
        reserve_recommendation={"suggested_optimizer_reserve_percent": 5},
    )


def _relaxed_kwargs():
    return dict(
        n=1,
        import_prices=[0.5],
        export_prices=[0.0],
        solar=[0.0],
        load=[1.0],
        soc_0=0.20,
        cost_function="cost",
    )


def test_relaxed_solve_discards_recommendation_and_restores_reserve(
    battery_optimizer_module,
):
    """The recursive-solve path: recommendation built at the 5% floor is dropped."""
    module = battery_optimizer_module
    optimizer = _optimizer(module)
    captured: dict[str, float] = {}

    def fake_solve_lp(*args, **kwargs):
        # Mimic the recursive solve running while the reserve is relaxed to 5%.
        captured["reserve_during_solve"] = optimizer.backup_reserve
        return _result_with_recommendation(module, feasible=True)

    optimizer._solve_lp = fake_solve_lp

    result = optimizer._solve_lp_relaxed(**_relaxed_kwargs())

    # Inner solve saw the artificially lowered floor...
    assert captured["reserve_during_solve"] == pytest.approx(0.05)
    # ...but the wrapper discards the tainted recommendation and marks it relaxed.
    assert result.reserve_recommendation == {}
    assert result.feasible is False
    # ...and the user's optimiser reserve is restored afterwards.
    assert optimizer.backup_reserve == pytest.approx(0.30)


def test_relaxed_solve_greedy_fallback_also_discards_recommendation(
    battery_optimizer_module,
):
    """The except path: even a greedy fallback (feasible=True) is sanitised."""
    module = battery_optimizer_module
    optimizer = _optimizer(module)

    def boom(*args, **kwargs):
        raise RuntimeError("solver exploded")

    def fake_solve_greedy(*args, **kwargs):
        # Greedy returns feasible=True with a 5%-floor recommendation.
        return _result_with_recommendation(module, feasible=True)

    optimizer._solve_lp = boom
    optimizer._solve_greedy = fake_solve_greedy

    result = optimizer._solve_lp_relaxed(**_relaxed_kwargs())

    assert result.reserve_recommendation == {}
    assert result.feasible is False
    assert optimizer.backup_reserve == pytest.approx(0.30)
