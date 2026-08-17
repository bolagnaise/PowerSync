"""Regression tests for EnergyAccumulator period-aware persistence."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any


COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "coordinator.py"
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class _Store:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.delayed_callback = None

    async def async_load(self) -> dict[str, Any] | None:
        return self.data

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = data

    def async_delay_save(self, callback, _delay: int) -> None:
        self.delayed_callback = callback


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def _load_accumulator(clock: _Clock):
    """Extract EnergyAccumulator without importing Home Assistant."""
    tree = ast.parse(COORDINATOR_PATH.read_text())
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EnergyAccumulator"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            class_node,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "Store": _Store,
        "HomeAssistant": object,
        "ENERGY_ACC_SAVE_DELAY": 300,
        "dt_util": SimpleNamespace(now=clock.now),
        "math": math,
        "_LOGGER": _Logger(),
    }
    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
    return namespace["EnergyAccumulator"]


def _new_accumulator(clock: _Clock, store: _Store | None = None):
    accumulator = _load_accumulator(clock)(None)
    accumulator._store = store
    return accumulator


def test_delayed_save_keeps_yesterday_date_after_midnight():
    clock = _Clock(datetime(2026, 6, 30, 23, 59, 0))
    store = _Store()
    accumulator = _new_accumulator(clock, store)

    accumulator.update(0.0, 0.0, 0.0, 0.0)
    clock.current = datetime(2026, 6, 30, 23, 59, 30)
    accumulator.update(2.0, 0.0, 0.0, 2.0)
    assert store.delayed_callback is not None

    # The coalesced callback runs after local midnight, but the totals still
    # belong to the June 30 update period.
    clock.current = datetime(2026, 7, 1, 0, 1, 0)
    persisted = store.delayed_callback()
    assert persisted["date"] == "2026-06-30"
    assert persisted["month"] == "2026-06"
    assert persisted["solar_kwh"] > 0


def test_flush_then_restore_drops_stale_day_before_first_update():
    clock = _Clock(datetime(2026, 6, 30, 23, 59, 0))
    store = _Store()
    accumulator = _new_accumulator(clock, store)
    accumulator.update(0.0, 0.0, 0.0, 0.0)
    accumulator.solar_kwh = 5.0

    # Unload just after midnight before a new telemetry update arrives.
    clock.current = datetime(2026, 7, 1, 0, 1, 0)
    asyncio.run(accumulator.async_flush())
    assert store.data["date"] == "2026-06-30"

    restored = _new_accumulator(clock, store)
    asyncio.run(restored.async_restore())
    assert restored.solar_kwh == 0.0
    assert restored._last_date is None

    # The first new-day update starts a fresh accumulator; stale June totals
    # cannot reappear through restore bookkeeping.
    restored.update(0.0, 0.0, 0.0, 0.0)
    assert restored.solar_kwh == 0.0
    assert restored._last_date == clock.current.date()


def test_restore_marks_current_period_and_month_rollover_uses_year():
    clock = _Clock(datetime(2026, 12, 31, 23, 59, 0))
    stored = {
        "date": "2026-12-31",
        "month": "2026-12",
        "solar_kwh": 3.0,
        "mtd_solar_kwh": 8.0,
    }
    restored = _new_accumulator(clock, _Store(stored))
    asyncio.run(restored.async_restore())
    assert restored._last_date == clock.current.date()
    assert restored._last_month == "2026-12"
    restored.update(0.0, 0.0, 0.0, 0.0)
    assert restored.solar_kwh == 3.0

    # A December-to-January update clears the old MTD bucket, including when
    # the month number would otherwise be ambiguous across years.
    restored.mtd_solar_kwh = 8.0
    clock.current = datetime(2027, 1, 1, 0, 1, 0)
    restored.update(0.0, 0.0, 0.0, 0.0)
    assert restored.mtd_solar_kwh == 0.0
    assert restored._last_month == "2027-01"


def test_missing_home_load_still_tracks_export_energy_and_earnings():
    """Ticket #336: uncertain EV attribution must only withhold Home Load."""
    clock = _Clock(datetime(2026, 8, 17, 17, 0, 0))
    accumulator = _new_accumulator(clock)

    accumulator.update(0.0, 0.0, 0.0, None, 0.53, 0.26)
    clock.current = datetime(2026, 8, 17, 17, 5, 0)
    accumulator.update(0.0, -12.0, 12.0, None, 0.53, 0.26)
    summary = accumulator.as_dict()

    assert summary["grid_export_today_kwh"] == 1.0
    assert summary["export_earnings_today"] == 0.26
    assert summary["export_earnings_coverage"] == "complete"
    assert summary["load_today_kwh"] == 0.0
    assert summary["avg_cost_per_kwh_today"] is None


def test_missing_export_price_marks_earnings_as_partial():
    clock = _Clock(datetime(2026, 8, 17, 17, 0, 0))
    accumulator = _new_accumulator(clock)

    accumulator.update(0.0, 0.0, 0.0, 1.0, 0.53, None)
    clock.current = datetime(2026, 8, 17, 17, 5, 0)
    accumulator.update(0.0, -12.0, 12.0, 1.0, 0.53, None)
    summary = accumulator.as_dict()

    assert summary["grid_export_today_kwh"] == 1.0
    assert summary["export_earnings_today"] is None
    assert summary["export_earnings_coverage"] == "partial"


def test_nonfinite_export_price_marks_earnings_as_partial():
    clock = _Clock(datetime(2026, 8, 17, 17, 0, 0))
    accumulator = _new_accumulator(clock)

    accumulator.update(0.0, 0.0, 0.0, 1.0, 0.53, float("nan"))
    clock.current = datetime(2026, 8, 17, 17, 5, 0)
    accumulator.update(0.0, -12.0, 12.0, 1.0, 0.53, float("nan"))
    summary = accumulator.as_dict()

    assert summary["grid_export_today_kwh"] == 1.0
    assert summary["export_earnings_today"] is None
    assert summary["export_earnings_coverage"] == "partial"


def test_price_coverage_survives_same_day_restore():
    clock = _Clock(datetime(2026, 8, 17, 17, 0, 0))
    store = _Store()
    accumulator = _new_accumulator(clock, store)
    accumulator.update(0.0, 0.0, 0.0, 1.0, 0.53, 0.26)
    clock.current = datetime(2026, 8, 17, 17, 5, 0)
    accumulator.update(0.0, -12.0, 12.0, 1.0, 0.53, 0.26)
    asyncio.run(accumulator.async_flush())

    restored = _new_accumulator(clock, store)
    asyncio.run(restored.async_restore())
    summary = restored.as_dict()

    assert summary["export_earnings_today"] == 0.26
    assert summary["export_earnings_covered_kwh"] == 1.0
    assert summary["export_earnings_coverage"] == "complete"
