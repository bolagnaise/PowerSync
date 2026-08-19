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
        "ENERGY_ACC_PRICE_COVERAGE_SCHEMA": 1,
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


def test_legacy_daily_import_cost_recovers_from_matching_optimizer_totals():
    """Ticket #314: a v2.12.1131 store must not become unknown on upgrade."""
    clock = _Clock(datetime(2026, 8, 18, 8, 45, 0))
    store = _Store(
        {
            "date": "2026-08-18",
            "month": "2026-08",
            "grid_import_kwh": 3.91,
            "grid_export_kwh": 0.01,
            "load_kwh": 3.90,
            "import_cost_today": 0.94,
            "export_earnings_today": 0.0,
            "mtd_grid_import_kwh": 3.91,
            "mtd_grid_export_kwh": 0.01,
            "mtd_load_kwh": 3.90,
            "mtd_import_cost": 0.94,
            "mtd_export_earnings": 0.0,
        }
    )
    restored = _new_accumulator(clock, store)
    asyncio.run(restored.async_restore())

    assert restored.as_dict()["import_cost_today"] is None
    assert restored.reconcile_price_coverage(
        {
            "date": "2026-08-18",
            "import_kwh": 3.91,
            "export_kwh": 0.01,
            "import_cost": 0.94,
            "export_earnings": 0.0,
        }
    )
    asyncio.run(restored.async_flush())

    summary = restored.as_dict()
    assert summary["import_cost_today"] == 0.94
    assert summary["import_cost_covered_kwh"] == 3.91
    assert summary["import_cost_coverage"] == "complete"
    assert summary["mtd_import_cost"] == 0.94
    assert store.data["price_coverage_schema"] == 1

    reloaded = _new_accumulator(clock, store)
    asyncio.run(reloaded.async_restore())
    assert reloaded.as_dict()["import_cost_today"] == 0.94


def test_legacy_daily_import_cost_stays_partial_when_reference_does_not_match():
    clock = _Clock(datetime(2026, 8, 18, 8, 45, 0))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                "date": "2026-08-18",
                "month": "2026-08",
                "grid_import_kwh": 3.91,
                "import_cost_today": 0.94,
                "mtd_grid_import_kwh": 7.0,
                "mtd_import_cost": 1.50,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert not restored.reconcile_price_coverage(
        {
            "date": "2026-08-18",
            "import_kwh": 3.91,
            "export_kwh": 0.0,
            "import_cost": 0.50,
            "export_earnings": 0.0,
        }
    )
    summary = restored.as_dict()
    assert summary["import_cost_today"] is None
    assert summary["import_cost_coverage"] == "partial"
    assert summary["mtd_import_cost"] is None


def test_legacy_daily_import_cost_does_not_hide_small_unpriced_gap():
    clock = _Clock(datetime(2026, 8, 18, 8, 45, 0))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                "date": "2026-08-18",
                "month": "2026-08",
                "grid_import_kwh": 3.91,
                "import_cost_today": 0.94,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert not restored.reconcile_price_coverage(
        {
            "date": "2026-08-18",
            "import_kwh": 3.84,
            "export_kwh": 0.0,
            "import_cost": 0.94,
            "export_earnings": 0.0,
        }
    )
    assert restored.as_dict()["import_cost_today"] is None


def test_non_integrating_sample_without_home_load_does_not_latch_the_month():
    """Ticket #336: only an integrated interval can leave a Home Load hole.

    The first sample after a restart, and any sample beyond the 6-minute
    staleness guard, add nothing to the accumulators.  Latching on those set
    _load_accounting_partial_mtd for the rest of the calendar month and left
    Avg Cost per kWh (Month) reading Unknown until the next month rollover,
    with no energy actually missing.
    """
    clock = _Clock(datetime(2026, 8, 19, 8, 0, 0))
    accumulator = _new_accumulator(clock, _Store())

    # First sample after a restart: no previous timestamp, nothing integrated.
    accumulator.update(1.0, -1.0, 0.0, None, 0.30, 0.05)
    assert accumulator._load_accounting_partial_today is False
    assert accumulator._load_accounting_partial_mtd is False

    # A gap wider than the 6-minute sanity guard also integrates nothing.
    clock.current = datetime(2026, 8, 19, 8, 30, 0)
    accumulator.update(1.0, -1.0, 0.0, None, 0.30, 0.05)
    assert accumulator._load_accounting_partial_mtd is False

    # A normal interval that genuinely lacks Home Load still fails closed.
    clock.current = datetime(2026, 8, 19, 8, 31, 0)
    accumulator.update(1.0, -1.0, 0.0, None, 0.30, 0.05)
    assert accumulator._load_accounting_partial_today is True
    assert accumulator._load_accounting_partial_mtd is True


def test_complete_home_load_samples_publish_both_average_costs():
    clock = _Clock(datetime(2026, 8, 19, 8, 0, 0))
    accumulator = _new_accumulator(clock, _Store())

    accumulator.update(0.0, 1.0, 0.0, 1.0, 0.30, 0.05)
    for minute in range(1, 31):
        clock.current = datetime(2026, 8, 19, 8, minute, 0)
        accumulator.update(0.0, 1.0, 0.0, 1.0, 0.30, 0.05)

    summary = accumulator.as_dict()
    assert summary["avg_cost_per_kwh_today"] is not None
    assert summary["avg_cost_per_kwh_mtd"] is not None
