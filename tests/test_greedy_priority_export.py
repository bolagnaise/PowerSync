"""Regression tests: greedy fallback must honor priority-export bonus windows.

557cf69a exempted priority-export slots from the LP's acquisition-cost
self-consumption cap so ZeroHero-style windows (0c base FiT + capped bonus)
still export. The structurally identical cap in ``_solve_greedy`` was missed,
so any install on the greedy path (no highspy wheel, or a per-solve LP
exception) sat in self_consumption for the whole window. These tests pin
LP/greedy parity for that predicate.
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
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
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


WINDOW = 36  # 3-hour priority window @ 5-minute slots
N_SLOTS = 72  # 6-hour horizon: window, then cheap overnight recharge


def _zerohero_kwargs(priority: bool = True) -> dict:
    """ZeroHero-style evening: 0c base FiT + capped 15c bonus in the window,
    high acquisition cost, no solar, full battery, cheap import afterwards
    (the recharge opportunity that makes exporting economically rational)."""
    n, w = N_SLOTS, WINDOW
    return dict(
        import_prices=[0.418] * w + [0.05] * (n - w),
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.2] * n,
        current_soc=1.0,
        acquisition_cost_kwh=0.418,
        allow_battery_export=[True] * w + [False] * (n - w),
        allow_grid_charge=True,
        export_bonus_prices=[0.15] * w + [0.0] * (n - w),
        export_bonus_cap_kwh=10.0,
        priority_export_slots=[priority] * w + [False] * (n - w),
        priority_export_enabled=priority,
    )


def _optimizer(module):
    return module.BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=6,
    )


def _run_greedy(module, **overrides):
    kwargs = _zerohero_kwargs()
    kwargs.update(overrides)
    saved = module.HIGHS_AVAILABLE
    module.HIGHS_AVAILABLE = False
    try:
        return _optimizer(module).optimize(**kwargs)
    finally:
        module.HIGHS_AVAILABLE = saved


def _window_exports(result):
    actions = result.schedule.actions[:WINDOW]
    return sum(1 for a in actions if a.action == "export")


def test_greedy_exports_in_priority_bonus_window(battery_optimizer_module):
    """Greedy path must export in a priority window even when the effective
    export price (base + bonus) is below the acquisition cost."""
    result = _run_greedy(battery_optimizer_module)

    assert _window_exports(result) > 0, (
        "greedy fallback produced no export actions in a priority-export "
        f"window: {[a.action for a in result.schedule.actions[:WINDOW]]}"
    )
    # Intentional battery export, not just serving the 200 W home load.
    assert max(result.grid_export_w[:WINDOW]) > 500.0


def test_greedy_still_caps_low_value_export_outside_priority_window(
    battery_optimizer_module,
):
    """The acquisition-cost guard must keep applying to non-priority slots:
    same prices, no priority window -> discharge capped to home load."""
    result = _run_greedy(battery_optimizer_module, **_zerohero_kwargs(priority=False))

    assert _window_exports(result) == 0
    assert max(result.grid_export_w[:WINDOW], default=0.0) <= 0.1


def test_greedy_matches_lp_action_in_priority_window(battery_optimizer_module):
    """LP and greedy must agree that a priority bonus window exports (the
    export slot count is bonus-cap-driven, so both land on the same total)."""
    module = battery_optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("highspy not installed — LP side of the parity check unavailable")

    lp_result = _optimizer(module).optimize(**_zerohero_kwargs())
    greedy_result = _run_greedy(module)

    lp_exports = _window_exports(lp_result)
    greedy_exports = _window_exports(greedy_result)
    assert lp_exports > 0
    assert greedy_exports > 0


def test_zerohero_bonus_does_not_force_expensive_prefill(
    battery_optimizer_module,
):
    """ZeroHero's capped bonus must be valued at its actual total export rate.

    A 15c/kWh Super Export bucket is worth exporting against a later cheap
    recharge, but it must not be inflated by the current import-price spread.
    That inflation made a 52.8c/kWh pre-window top-up look profitable.
    """
    module = battery_optimizer_module
    if not module.HIGHS_AVAILABLE:
        pytest.skip("highspy not installed — LP side of the regression unavailable")

    pre_window = 12
    export_window = 36
    post_window = 240
    n = pre_window + export_window + post_window
    result = module.BatteryOptimizer(
        capacity_wh=40300,
        max_charge_w=21000,
        max_discharge_w=23000,
        backup_reserve=0.40,
        hardware_reserve=0.10,
        interval_minutes=5,
        horizon_hours=24,
    ).optimize(
        import_prices=(
            [0.528] * pre_window
            + [5.528] * export_window
            + [0.407] * 156
            + [0.0] * 36
            + [0.407] * (post_window - 156 - 36)
        ),
        export_prices=(
            [0.02] * pre_window + [0.10] * export_window + [0.0] * post_window
        ),
        solar_forecast=[0.0] * n,
        load_forecast=[0.2] * n,
        current_soc=0.98,
        acquisition_cost_kwh=0.40,
        allow_battery_export=(
            [False] * pre_window + [True] * export_window + [False] * post_window
        ),
        block_battery_charge=(
            [False] * pre_window + [True] * export_window + [False] * post_window
        ),
        allow_grid_charge=True,
        grid_charge_allowed=(
            [True] * pre_window + [False] * export_window + [True] * post_window
        ),
        export_bonus_prices=(
            [0.0] * pre_window + [0.05] * export_window + [0.0] * post_window
        ),
        export_bonus_cap_kwh=15.0,
        priority_export_slots=(
            [False] * pre_window + [True] * export_window + [False] * post_window
        ),
        priority_export_enabled=True,
    )

    pre_window_charge_kwh = sum(
        max(0.0, action.battery_charge_w or 0.0)
        for action in result.schedule.actions[:pre_window]
    ) * (5 / 60) / 1000

    assert pre_window_charge_kwh == pytest.approx(0.0)
