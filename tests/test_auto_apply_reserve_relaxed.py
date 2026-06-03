"""Regression tests: an infeasible solve must hold in self-consumption.

Background: with Profit Max + Auto-Apply Optimizer Reserve, the constrained LP
often went infeasible. The old fallback temporarily dropped backup_reserve to
5% and re-solved, so the "optimal" relaxed plan would discharge the battery to
~5% just to satisfy the objective — draining users' batteries overnight, and
Auto-Apply then wrote that 5% floor back to the battery.

The fallback now holds in self-consumption instead: the battery only serves
home load, never exports to the grid, never charges from the grid, and never
drops below the genuine reserve floor. These tests pin that invariant and the
"no usable reserve recommendation" invariant Auto-Apply relies on.
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


def _hold_kwargs(*, soc_0: float, n: int = 12):
    """A horizon with home load and no solar — pure discharge-to-load case.

    Export price sits far above import to make battery export *look* attractive;
    the hold must refuse to export the battery regardless.
    """
    return dict(
        n=n,
        import_prices=[0.20] * n,
        export_prices=[1.50] * n,
        solar=[0.0] * n,
        load=[1.0] * n,
        soc_0=soc_0,
        cost_function="cost",
    )


def _min_soc(schedule) -> float:
    """Lowest SOC across the produced schedule actions (0-1)."""
    socs = [getattr(a, "soc", None) for a in schedule.actions]
    socs = [s for s in socs if s is not None]
    return min(socs) if socs else 1.0


def test_hold_never_exports_battery_or_drops_below_reserve(
    battery_optimizer_module,
):
    """Healthy SOC, lucrative export prices: hold serves load, never exports."""
    module = battery_optimizer_module
    optimizer = _optimizer(module)

    result = optimizer._solve_self_consumption_hold(**_hold_kwargs(soc_0=0.80))

    # No solar surplus exists, so the grid never sees exported energy — the
    # battery is never discharged to the grid.
    assert max(result.grid_export_w) == pytest.approx(0.0, abs=1e-6)
    # SOC never crosses below the genuine reserve floor (30%).
    assert _min_soc(result.schedule) >= 0.30 - 1e-6
    # It is a fallback: no reserve recommendation, marked infeasible/relaxed.
    assert result.reserve_recommendation == {}
    assert result.feasible is False
    assert result.solver_used == "self_consumption_hold"
    # The user's configured reserve is never mutated.
    assert optimizer.backup_reserve == pytest.approx(0.30)


def test_hold_below_reserve_holds_at_hardware_floor(
    battery_optimizer_module,
):
    """Already below the optimiser reserve: never drain past the hardware floor."""
    module = battery_optimizer_module
    optimizer = _optimizer(module)

    # soc_0 (20%) is below the 30% optimiser reserve; the hold may still serve
    # home load down to the 5% hardware floor, but never deeper and never to
    # the grid.
    result = optimizer._solve_self_consumption_hold(**_hold_kwargs(soc_0=0.20))

    assert max(result.grid_export_w) == pytest.approx(0.0, abs=1e-6)
    assert _min_soc(result.schedule) >= 0.05 - 1e-6
    assert result.reserve_recommendation == {}
    assert result.feasible is False
    assert optimizer.backup_reserve == pytest.approx(0.30)


def test_relaxed_5pct_fallback_is_gone(battery_optimizer_module):
    """The reserve-relaxing fallback must not exist — only the safe hold."""
    module = battery_optimizer_module
    optimizer = _optimizer(module)

    assert not hasattr(optimizer, "_solve_lp_relaxed")
    assert hasattr(optimizer, "_solve_self_consumption_hold")
