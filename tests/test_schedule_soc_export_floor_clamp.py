"""Regression test for the reported-SOC export-floor clamp.

When an export reserve floor (e.g. the overnight home-load bridge reserve) is
configured above the current battery SOC, the schedule builder must still report
the *true* simulated SOC. Previously it clamped the reported SOC up to the export
floor, so a battery genuinely at ~23% was plotted at the 45% export floor and
that inflated value also leaked into ``minimum_forecast_soc``. The export floor
should gate discharge/export only, never inflate a genuinely-low SOC.

This reuses the import/stub harness from ``test_battery_optimizer_export_guard``.
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
    # 54 kWh battery, 20% optimizer reserve, like a 4-pack Powerwall system.
    return module.BatteryOptimizer(
        capacity_wh=54000,
        max_charge_w=14700,
        max_discharge_w=14700,
        backup_reserve=0.20,
        interval_minutes=5,
        horizon_hours=1,
    )


def test_reported_soc_not_inflated_to_export_floor(battery_optimizer_module):
    optimizer = _optimizer(battery_optimizer_module)
    n = 12

    # Battery genuinely at 23%, below a 45% export reserve floor (the overnight
    # home-load bridge reserve). Home is drawing load with no solar; grid charge
    # and battery export are both disallowed, so the battery only ever serves
    # load naturally down toward the 20% optimizer reserve.
    result = optimizer.optimize(
        import_prices=[0.50] * n,
        export_prices=[0.05] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[1.0] * n,
        current_soc=0.23,
        allow_battery_export=False,
        allow_grid_charge=False,
        export_reserve_floor=0.45,
    )

    socs = [action.soc for action in result.schedule.actions]
    assert socs, "schedule produced no actions"

    # The reported SOC must reflect the true ~23% battery, never the 45% floor.
    assert max(socs) < 0.40, f"reported SOC inflated to export floor: {socs}"
    # First reported step tracks the real current SOC (it may dip slightly as the
    # battery serves load, but stays near 23% — not jumped up to 45%).
    assert result.schedule.actions[0].soc == pytest.approx(0.23, abs=0.03)
    # It must never be inflated below the real reserve either.
    assert min(socs) >= 0.20 - 1e-6


def test_export_floor_still_caps_reported_soc_from_above(battery_optimizer_module):
    """Above the floor, export still cannot drive reported SOC below it."""
    optimizer = _optimizer(battery_optimizer_module)
    n = 12

    # Battery well above the floor, export allowed and profitable.
    result = optimizer.optimize(
        import_prices=[0.10] * n,
        export_prices=[1.00] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.2] * n,
        current_soc=0.90,
        allow_battery_export=True,
        allow_grid_charge=False,
        export_reserve_floor=0.45,
    )

    socs = [action.soc for action in result.schedule.actions]
    # Forced export must not take the reported SOC below the export floor.
    assert min(socs) >= 0.45 - 1e-3, f"export drove SOC below floor: {socs}"
