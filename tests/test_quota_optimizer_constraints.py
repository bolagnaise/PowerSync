from __future__ import annotations

import importlib
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
    dt.now = lambda: datetime(2026, 7, 14, tzinfo=timezone.utc)
    dt.utcnow = dt.now
    dt.UTC = timezone.utc
    util.dt = dt
    ha.util = util
    sys.modules.update({"homeassistant": ha, "homeassistant.util": util, "homeassistant.util.dt": dt})
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
        capacity_wh=13_500,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        backup_reserve=0.05,
        interval_minutes=5,
        horizon_hours=2,
    )


def _kwargs(n=12):
    start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    return {
        "import_prices": [0.35] * n,
        "export_prices": [0.50] * n,
        "solar_forecast": [0.0] * n,
        "load_forecast": [0.0] * n,
        "current_soc": 1.0,
        "allow_battery_export": [True] * n,
        "priority_export_slots": [True] * n,
        "priority_export_enabled": True,
        "schedule_timestamps": [start + timedelta(minutes=5 * idx) for idx in range(n)],
    }


@pytest.mark.parametrize("use_highs", [False, True])
def test_per_slot_zero_and_downward_limits_apply_to_lp_and_greedy(optimizer_module, use_highs):
    if use_highs and not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("highspy unavailable")
    old = optimizer_module.HIGHS_AVAILABLE
    optimizer_module.HIGHS_AVAILABLE = use_highs
    try:
        kwargs = _kwargs(12)
        kwargs["grid_export_limits_w"] = [10_000] * 4 + [1_500] * 4 + [0] * 4
        result = _optimizer(optimizer_module).optimize(**kwargs)
    finally:
        optimizer_module.HIGHS_AVAILABLE = old
    assert max(result.grid_export_w[:4], default=0) <= 10_000 + 1e-6
    assert max(result.grid_export_w[4:8], default=0) <= 1_500 + 1e-6
    assert max(result.grid_export_w[8:12], default=0) <= 1e-6


def test_highs_direction_binary_prevents_quota_passthrough(optimizer_module):
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("highspy unavailable")
    n = 24
    start = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    result = _optimizer(optimizer_module).optimize(
        import_prices=[0.35] * n,
        export_prices=[0.05] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.5,
        allow_battery_export=[True] * n,
        allow_grid_charge=True,
        import_bonus_prices=[0.35] * n,
        import_bonus_cap_kwh=50,
        export_bonus_prices=[0.10] * n,
        export_bonus_cap_kwh=30,
        priority_export_slots=[True] * n,
        priority_export_enabled=True,
        prevent_simultaneous_grid_flow=True,
        schedule_timestamps=[start + timedelta(minutes=5 * idx) for idx in range(n)],
    )
    assert result.solver_used == "highs"
    assert all(
        imported <= 1e-5 or exported <= 1e-5
        for imported, exported in zip(result.grid_import_w, result.grid_export_w)
    )


def test_highs_allocates_each_daily_import_and_export_quota_cap_row(optimizer_module):
    if not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("highspy unavailable")
    n = 24
    groups = ["day-1"] * 12 + ["day-2"] * 12
    optimizer = _optimizer(optimizer_module)
    optimizer.set_quota_bonus_groups(
        import_group_ids=groups,
        import_caps_by_group={"day-1": 25.0, "day-2": 25.0},
        export_group_ids=groups,
        export_caps_by_group={"day-1": 15.0, "day-2": 15.0},
    )
    start = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)

    result = optimizer.optimize(
        import_prices=[0.35] * n,
        export_prices=[0.05] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.5,
        allow_battery_export=[True] * n,
        allow_grid_charge=True,
        import_bonus_prices=[0.35] * n,
        import_bonus_cap_kwh=50,
        export_bonus_prices=[0.10] * n,
        export_bonus_cap_kwh=30,
        priority_export_slots=[True] * n,
        priority_export_enabled=True,
        prevent_simultaneous_grid_flow=True,
        schedule_timestamps=[start + timedelta(minutes=5 * idx) for idx in range(n)],
    )

    assert result.solver_used == "highs"


def test_lp_periods_split_at_quota_group_boundary_after_near_horizon(optimizer_module):
    n = 90
    boundary = 75  # Inside the 30-minute coarse period covering slots 72-78.
    groups = ["day-1"] * boundary + ["day-2"] * (n - boundary)
    optimizer = _optimizer(optimizer_module)
    optimizer.set_quota_bonus_groups(
        import_group_ids=groups,
        import_caps_by_group={"day-1": 25.0, "day-2": 25.0},
        export_group_ids=groups,
        export_caps_by_group={"day-1": 15.0, "day-2": 15.0},
    )

    periods = optimizer._build_lp_periods(
        n,
        [0.35] * n,
        [0.05] * n,
        [0.0] * n,
        [0.0] * n,
        [True] * n,
        [False] * n,
        [True] * n,
        [0.10] * n,
        [0.35] * n,
        [True] * n,
        [None] * n,
        [0.0] * n,
    )

    assert any(period.end == boundary for period in periods)
    assert any(period.start == boundary for period in periods)
    assert not any(period.start < boundary < period.end for period in periods)


@pytest.mark.parametrize("use_highs", [False, True])
def test_daily_export_bonus_groups_do_not_share_caps(optimizer_module, use_highs):
    if use_highs and not optimizer_module.HIGHS_AVAILABLE:
        pytest.skip("highspy unavailable")
    optimizer = _optimizer(optimizer_module)
    groups = ["day-1"] * 6 + ["day-2"] * 6
    optimizer.set_quota_bonus_groups(
        import_group_ids=None,
        import_caps_by_group=None,
        export_group_ids=groups,
        export_caps_by_group={"day-1": 0.2, "day-2": 0.4},
    )
    kwargs = _kwargs(12)
    kwargs.update(
        import_prices=[0.35] * 12,
        export_prices=[0.05] * 12,
        export_bonus_prices=[0.50] * 12,
        export_bonus_cap_kwh=0.6,
    )
    old = optimizer_module.HIGHS_AVAILABLE
    optimizer_module.HIGHS_AVAILABLE = use_highs
    try:
        result = optimizer.optimize(**kwargs)
    finally:
        optimizer_module.HIGHS_AVAILABLE = old

    if use_highs:
        assert result.solver_used == "highs"
    dt_hours = optimizer.interval_minutes / 60
    day_1_kwh = sum(result.grid_export_w[:6]) / 1000 * dt_hours
    day_2_kwh = sum(result.grid_export_w[6:]) / 1000 * dt_hours
    assert day_1_kwh <= 0.2 + 1e-6
    assert day_2_kwh <= 0.4 + 1e-6
    assert day_1_kwh > 0
    assert day_2_kwh > 0
