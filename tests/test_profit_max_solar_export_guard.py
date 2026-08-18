"""Solve-level regression for the Profit Max solar-export funding guard.

Ticket #383: a Sigenergy/Amber site's own 24-hour plan exported morning solar
at ~2.4c/kWh and, in the same horizon, grid-charged at ~9.6c/kWh to refill the
battery. ``_profit_max_solar_export_slots`` books each deferral against raw
future charge headroom, so on a charge-capacity-constrained day it funds the
hold from capacity the plan already needed. The hold is a hard pre-LP charge
block, so the LP cannot reject the trade.

These tests run the shipped selector (AST-extracted, no reimplementation)
against the real HiGHS LP and assert the post-solve guard removes the losing
holds while leaving a genuinely profitable high-feed-in hold alone.
"""

from __future__ import annotations

import ast
import importlib
import math
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

_METHODS = ("_profit_max_solar_export_slots", "_revise_solar_export_holds")

INTERVAL_MINUTES = 5
SLOTS = 288
CAPACITY_WH = 48000
MAX_CHARGE_W = 25000
EFFICIENCY = 0.92


@pytest.fixture()
def optimizer_module():
    """Import the real LP optimizer with only Home Assistant stubbed out."""
    saved = {name: sys.modules.get(name, _SENTINEL) for name in _STUBS}
    for name in _STUBS:
        sys.modules.pop(name, None)
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: datetime(2026, 8, 18, 22, 35, tzinfo=timezone.utc)
    dt.utcnow = dt.now
    dt.UTC = timezone.utc
    util.dt = dt
    ha.util = util
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.util": util,
            "homeassistant.util.dt": dt,
            "power_sync": package,
            "power_sync.optimization": optimization,
        }
    )
    module = importlib.import_module("power_sync.optimization.battery_optimizer")
    try:
        yield module
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(scope="module")
def coordinator_methods():
    """Exec the two coordinator methods standalone, without a coordinator."""
    path = COMPONENT_ROOT / "optimization" / "coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OptimizationCoordinator"
    )
    methods = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name in _METHODS
    ]
    assert {node.name for node in methods} == set(_METHODS)
    namespace: dict[str, Any] = {"Any": Any, "math": math, "OptimizerResult": Any}
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=methods, type_ignores=[])
            ),
            str(path),
            "exec",
        ),
        namespace,
    )
    return namespace


def _fake_coordinator(export_limit_kw: float = 15.0):
    coordinator = SimpleNamespace()
    coordinator.profit_max_mode = True
    coordinator.charge_by_time_enabled = False
    coordinator._config = SimpleNamespace(
        interval_minutes=INTERVAL_MINUTES,
        max_charge_w=MAX_CHARGE_W,
        battery_capacity_wh=CAPACITY_WH,
        allow_grid_charge=True,
        max_grid_import_w=MAX_CHARGE_W,
    )
    coordinator._optimizer = SimpleNamespace(
        efficiency=EFFICIENCY, pre_window_slot=None
    )
    coordinator._solar_export_capability = lambda: {
        "supported": True,
        "reason": "supported",
        "adapter": "sigenergy.modbus.charge_limit.v1",
        "export_limit_kw": export_limit_kw,
    }
    coordinator._sync_solar_export_capability_notice = lambda *_args: None
    return coordinator


def _minutes(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


def _slot_minutes(index: int) -> int:
    return _minutes(8, 35) + index * INTERVAL_MINUTES


def _reported_prices() -> tuple[list[float], list[float]]:
    """Import prices reproducing every window on the reporter's plan card."""
    import_prices: list[float] = []
    for index in range(SLOTS):
        minute = _slot_minutes(index)
        if minute < _minutes(9, 50):
            price = 0.1001
        elif minute < _minutes(12):
            price = 0.1052
        elif minute < _minutes(13):
            price = 0.0964
        elif minute < _minutes(16):
            price = 0.2110
        elif minute < _minutes(16, 30):
            price = 0.1763
        else:
            price = 0.2872
        import_prices.append(price)
    # Amber's fixed network/retail spread between import and feed-in.
    export_prices = [round(max(0.0, price - 0.076), 6) for price in import_prices]
    return import_prices, export_prices


def _high_feed_in_prices() -> tuple[list[float], list[float]]:
    """A morning the feature is actually designed for: 33c feed-in, 5c grid."""
    import_prices: list[float] = []
    for index in range(SLOTS):
        minute = _slot_minutes(index)
        if minute < _minutes(10):
            price = 0.35
        elif minute < _minutes(15):
            price = 0.05
        elif minute < _minutes(21):
            price = 0.45
        else:
            price = 0.20
        import_prices.append(price)
    export_prices = [round(max(0.0, price - 0.02), 6) for price in import_prices]
    return import_prices, export_prices


def _solar(peak_kw: float) -> list[float]:
    values: list[float] = []
    for index in range(SLOTS):
        minute = _slot_minutes(index) % 1440
        if _minutes(6) <= minute <= _minutes(18):
            offset = (minute - _minutes(12, 30)) / _minutes(3)
            values.append(round(peak_kw * math.exp(-0.5 * offset * offset), 4))
        else:
            values.append(0.0)
    return values


def _solve(optimizer_module, prices, solar, load, soc, mask):
    import_prices, export_prices = prices
    optimizer = optimizer_module.BatteryOptimizer(
        capacity_wh=CAPACITY_WH,
        max_charge_w=MAX_CHARGE_W,
        max_discharge_w=MAX_CHARGE_W,
        max_grid_import_w=MAX_CHARGE_W,
        max_grid_export_w=15000,
        efficiency=EFFICIENCY,
        backup_reserve=0.05,
        interval_minutes=INTERVAL_MINUTES,
        horizon_hours=24,
    )
    base = datetime(2026, 8, 18, 22, 35, tzinfo=timezone.utc)
    timestamps = [
        base + timedelta(minutes=INTERVAL_MINUTES * index) for index in range(SLOTS)
    ]
    return optimizer.optimize(
        import_prices,
        export_prices,
        solar,
        load,
        soc,
        "cost",
        0.0,
        False,
        list(mask),
        True,
        [True] * SLOTS,
        None,
        None,
        None,
        None,
        None,
        timestamps,
        None,
        False,
        False,
        None,
        False,
        None,
        None,
        0.0,
        None,
        None,
        list(mask),
        None,
    )


def _net_cost(result, prices) -> float:
    import_prices, export_prices = prices
    hours = INTERVAL_MINUTES / 60.0
    grid_import = list(result.grid_import_w) or [0.0] * SLOTS
    grid_export = list(result.grid_export_w) or [0.0] * SLOTS
    imported = sum(
        grid_import[index] / 1000.0 * hours * import_prices[index]
        for index in range(SLOTS)
    )
    exported = sum(
        grid_export[index] / 1000.0 * hours * export_prices[index]
        for index in range(SLOTS)
    )
    return imported - exported


def _select(coordinator_methods, coordinator, prices, solar, load, soc):
    import_prices, export_prices = prices
    return coordinator_methods["_profit_max_solar_export_slots"](
        coordinator,
        import_prices,
        export_prices,
        solar,
        load,
        soc,
        [False] * SLOTS,
        [True] * SLOTS,
    )


def test_reported_variant_hold_is_dropped_and_recovers_the_lost_spread(
    optimizer_module, coordinator_methods
):
    """The #383 shape: charge-capacity constrained, low morning feed-in."""
    prices = _reported_prices()
    solar = _solar(6.0)
    load = [0.6] * SLOTS
    soc = 0.33
    coordinator = _fake_coordinator()

    held_mask = _select(coordinator_methods, coordinator, prices, solar, load, soc)
    # The unguarded selector still holds the cheap morning; that is the trigger.
    assert sum(held_mask) > 0
    assert held_mask[0] is True

    held = _solve(optimizer_module, prices, solar, load, soc, held_mask)
    assert held.feasible

    revised_mask = coordinator_methods["_revise_solar_export_holds"](
        coordinator, held, prices[0], prices[1], held_mask
    )
    assert sum(revised_mask) < sum(held_mask)
    # The 2.14-2.72c morning window the reporter complained about is released.
    assert revised_mask[0] is False

    revised = _solve(optimizer_module, prices, solar, load, soc, revised_mask)
    assert revised.feasible

    # Strictly cheaper day at no worse terminal SOC — the invariant the hold
    # violated. Without the guard the plan sells solar below what it then pays
    # to buy the same energy back.
    assert _net_cost(revised, prices) < _net_cost(held, prices) - 0.01
    assert revised.schedule.actions[-1].soc >= held.schedule.actions[-1].soc - 1e-6


def test_high_feed_in_hold_survives_the_guard_and_still_pays(
    optimizer_module, coordinator_methods
):
    """Guard must not disable Profit Max where the deferral genuinely pays."""
    prices = _high_feed_in_prices()
    solar = _solar(10.0)
    load = [0.6] * SLOTS
    soc = 0.33
    coordinator = _fake_coordinator()

    held_mask = _select(coordinator_methods, coordinator, prices, solar, load, soc)
    assert sum(held_mask) > 0

    held = _solve(optimizer_module, prices, solar, load, soc, held_mask)
    assert held.feasible

    revised_mask = coordinator_methods["_revise_solar_export_holds"](
        coordinator, held, prices[0], prices[1], held_mask
    )
    assert revised_mask == held_mask

    unheld = _solve(optimizer_module, prices, solar, load, soc, [False] * SLOTS)
    assert _net_cost(held, prices) < _net_cost(unheld, prices)
