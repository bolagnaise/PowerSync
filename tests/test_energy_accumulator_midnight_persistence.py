"""Regression tests for EnergyAccumulator period-aware persistence."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


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


def _price_coverage_schema() -> int:
    """Read the shipped coverage-schema marker instead of pinning a copy."""
    tree = ast.parse(COORDINATOR_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "ENERGY_ACC_PRICE_COVERAGE_SCHEMA"
            for target in node.targets
        ):
            return int(ast.literal_eval(node.value))
    raise AssertionError("ENERGY_ACC_PRICE_COVERAGE_SCHEMA not found")


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
        "ENERGY_ACC_PRICE_COVERAGE_SCHEMA": _price_coverage_schema(),
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

    # Ticket #384: restore itself now adopts coverage for a payload written
    # before the counters existed, so the value is never unknown in the first
    # place and the optimizer reconciliation has nothing left to recover.
    assert restored.as_dict()["import_cost_today"] == 0.94
    assert not restored.reconcile_price_coverage(
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
    assert store.data["price_coverage_schema"] == _price_coverage_schema()

    reloaded = _new_accumulator(clock, store)
    asyncio.run(reloaded.async_restore())
    assert reloaded.as_dict()["import_cost_today"] == 0.94


def test_legacy_daily_import_cost_recovers_without_a_matching_reference():
    """Ticket #384: legacy recovery must not depend on the optimizer ledger.

    The optimizer keeps an independent ledger that routinely disagrees with
    the accumulator by more than its 0.001 kWh / $0.0001 match tolerance, so
    gating legacy recovery on that match left real installs blanked.  The
    strict reconciliation contract itself is unchanged — it still declines.
    """
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
    assert summary["import_cost_today"] == 0.94
    assert summary["import_cost_coverage"] == "complete"
    assert summary["mtd_import_cost"] == 1.50


def test_post_counter_payload_with_a_real_gap_is_not_migrated():
    """A payload that carries real counters must be trusted, not overwritten.

    v2.12.1132 shipped the counters one release before the
    ``price_coverage_schema`` marker, so the marker cannot be the migration
    signal — the counters' own absence is.  A genuinely short counter must
    survive restore and keep failing closed.
    """
    clock = _Clock(datetime(2026, 8, 18, 8, 45, 0))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                "date": "2026-08-18",
                "month": "2026-08",
                "grid_import_kwh": 3.91,
                "import_cost_today": 0.94,
                # No price_coverage_schema: written by v2.12.1132/1133.
                "import_cost_covered_kwh": 0.12,
                "mtd_grid_import_kwh": 3.91,
                "mtd_import_cost": 0.94,
                "mtd_import_cost_covered_kwh": 0.12,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert restored.import_cost_covered_kwh == 0.12
    summary = restored.as_dict()
    assert summary["import_cost_today"] is None
    assert summary["import_cost_coverage"] == "partial"
    assert summary["import_cost_covered_kwh"] == 0.12

    # The month-to-date bucket is deliberately the other way round (#385): it
    # has no midnight self-heal, so the same marker-less payload is repaired
    # once rather than blanking the month until the next rollover.
    assert restored.mtd_import_cost_covered_kwh == pytest.approx(3.91)


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


def test_small_unpriced_interval_keeps_daily_cost_visible():
    """Ticket #384: a startup-race gap of ~0.2 % must not blank the day.

    The custom-tariff schedule registers a few seconds after the energy
    coordinator's first refresh, so one integrated interval can carry no
    price.  Priced and measured energy advance in the same branch, so the old
    1 Wh absolute band was exact equality and that single interval blanked
    Export Earnings Today and both average sensors for the rest of the day.
    """
    clock = _Clock(datetime(2026, 8, 19, 16, 0, 0))
    accumulator = _new_accumulator(clock)

    accumulator.update(6.0, -5.0, 0.0, 0.5, 0.37, 0.04)
    # One interval integrated before the tariff schedule was registered.
    clock.current = datetime(2026, 8, 19, 16, 0, 17)
    accumulator.update(6.0, -5.0, 0.0, 0.5, None, None)
    for step in range(1, 501):
        clock.current = datetime(2026, 8, 19, 16, 0, 17) + timedelta(
            seconds=17 * step
        )
        accumulator.update(6.0, -5.0, 0.0, 0.5, 0.37, 0.04)

    summary = accumulator.as_dict()
    exported = summary["grid_export_today_kwh"]
    covered = summary["export_earnings_covered_kwh"]
    # ~23.6 Wh unpriced out of ~11.8 kWh exported: 0.2 %, well inside the
    # project's own max(0.05 kWh, 2 %) band.
    assert 0.02 < exported - covered < 0.03
    assert summary["export_earnings_today"] is not None
    assert summary["export_earnings_coverage"] == "complete"
    assert summary["avg_cost_per_kwh_today"] is not None
    assert summary["avg_cost_per_kwh_mtd"] is not None
    # The raw counter must stay raw: coverage is reported, not back-filled.
    assert covered == pytest.approx(11.806, abs=1e-3)


def test_materially_partial_coverage_still_blanks():
    """Ticket #336 must not regress: no $0.00 beside a full day of export."""
    clock = _Clock(datetime(2026, 8, 19, 16, 0, 0))
    accumulator = _new_accumulator(clock)

    accumulator.update(6.0, -5.0, 0.0, 0.5, 0.37, 0.04)
    clock.current = datetime(2026, 8, 19, 16, 0, 17)
    accumulator.update(6.0, -5.0, 0.0, 0.5, 0.37, 0.04)
    # The rest of the day integrates with no price at all.
    for step in range(1, 501):
        clock.current = datetime(2026, 8, 19, 16, 0, 17) + timedelta(
            seconds=17 * step
        )
        accumulator.update(6.0, -5.0, 0.0, 0.5, None, None)

    summary = accumulator.as_dict()
    assert summary["export_earnings_covered_kwh"] < 0.05
    assert summary["grid_export_today_kwh"] > 11.0
    assert summary["export_earnings_today"] is None
    assert summary["export_earnings_coverage"] == "partial"
    assert summary["avg_cost_per_kwh_today"] is None


def test_legacy_payload_without_coverage_counters_adopts_coverage():
    """Ticket #384: the reporter's own restored payload must report numbers."""
    clock = _Clock(datetime(2026, 8, 19, 22, 18, 26))
    store = _Store(
        {
            "date": "2026-08-19",
            "month": "2026-08",
            "solar_kwh": 64.74,
            "grid_import_kwh": 0.20,
            "grid_export_kwh": 35.57,
            "battery_charge_kwh": 22.66,
            "battery_discharge_kwh": 11.26,
            "load_kwh": 13.61,
            "import_cost_today": 0.05,
            "export_earnings_today": 1.74,
            "mtd_grid_import_kwh": 1.10,
            "mtd_grid_export_kwh": 60.0,
            "mtd_load_kwh": 30.0,
            "mtd_import_cost": 0.30,
            "mtd_export_earnings": 2.80,
        }
    )
    restored = _new_accumulator(clock, store)
    asyncio.run(restored.async_restore())

    summary = restored.as_dict()
    assert summary["import_cost_today"] == pytest.approx(0.05)
    assert summary["export_earnings_today"] == pytest.approx(1.74)
    assert summary["avg_cost_per_kwh_today"] is not None
    assert summary["avg_cost_per_kwh_mtd"] is not None
    assert summary["import_cost_coverage"] == "complete"
    assert summary["export_earnings_coverage"] == "complete"
    # Attributes stay self-consistent with the adopted counters.
    assert summary["export_earnings_covered_kwh"] == pytest.approx(35.57)

    # The migration persists, so it runs at most once per install.
    asyncio.run(restored.async_flush())
    assert store.data["price_coverage_schema"] == _price_coverage_schema()
    assert store.data["export_earnings_covered_kwh"] == pytest.approx(35.57)
    reloaded = _new_accumulator(clock, store)
    asyncio.run(reloaded.async_restore())
    assert reloaded.as_dict()["export_earnings_today"] == pytest.approx(1.74)


def test_mtd_average_survives_upgrade_day():
    """Ticket #384: an upgrade must not blank the month until the 1st.

    reconcile_price_coverage can only corroborate MTD when the month contains
    exactly the current day, i.e. only on the 1st.  Every other upgrade day
    left Avg Cost per kWh (Month) unknown until the next rollover.
    """
    clock = _Clock(datetime(2026, 8, 19, 22, 18, 26))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                "date": "2026-08-19",
                "month": "2026-08",
                "grid_import_kwh": 0.20,
                "grid_export_kwh": 35.57,
                "load_kwh": 13.61,
                "import_cost_today": 0.05,
                "export_earnings_today": 1.74,
                # Month-to-date is much larger than today, so the optimizer's
                # daily ledger can never corroborate it.
                "mtd_grid_import_kwh": 18.4,
                "mtd_grid_export_kwh": 420.0,
                "mtd_load_kwh": 260.0,
                "mtd_import_cost": 6.30,
                "mtd_export_earnings": 21.40,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert restored.as_dict()["avg_cost_per_kwh_mtd"] is not None


def _covau_free_window(accumulator, clock, minutes: int = 60) -> None:
    """Integrate a CovaU free-import window priced at exactly 0.0 $/kWh."""
    for _ in range(minutes * 2):  # 30 s SAJ H2 telemetry cadence
        clock.current += timedelta(seconds=30)
        accumulator.update(0.0, 4.8, 0.0, 1.2, 0.0, 0.03)


def test_mtd_coverage_written_before_the_schema_marker_is_repaired():
    """Ticket #385: v2.12.1132/1133 payloads blank the month until the 1st.

    Those two releases shipped the coverage counters one release *before* the
    schema marker.  They restored the month's measured energy in full but
    started its counters from zero mid-month, so the key-absence migration
    added afterwards can never fire on them and Avg Cost per kWh (Month) reads
    Unknown for the rest of the calendar month - updating does not clear it.
    """
    clock = _Clock(datetime(2026, 8, 20, 11, 0, 0))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                # Yesterday's payload: the daily bucket starts fresh, so only
                # the month-to-date state can be responsible.
                "date": "2026-08-19",
                "month": "2026-08",
                "mtd_grid_import_kwh": 210.0,
                "mtd_grid_export_kwh": 40.0,
                "mtd_load_kwh": 300.0,
                "mtd_import_cost": 44.0,
                "mtd_export_earnings": 1.20,
                # Counters present but short, and no price_coverage_schema:
                # only v2.12.1132/1133 can write that combination.
                "mtd_import_cost_covered_kwh": 24.0,
                "mtd_export_earnings_covered_kwh": 6.0,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert restored.mtd_import_cost_covered_kwh == pytest.approx(210.0)
    assert restored.mtd_export_earnings_covered_kwh == pytest.approx(40.0)
    assert restored.as_dict()["avg_cost_per_kwh_mtd"] is not None


def test_stored_month_load_latch_is_cleared_once_on_restore():
    """Ticket #385: v2.12.1153 stopped setting the latch but never cleared it.

    The month latch clears only at month rollover, so an install that latched
    under an earlier build kept reading Unknown after updating.  A genuine
    Home Load hole must still re-latch on the next integrated sample.
    """
    clock = _Clock(datetime(2026, 8, 20, 11, 0, 0))
    restored = _new_accumulator(
        clock,
        _Store(
            {
                "date": "2026-08-19",
                "month": "2026-08",
                "mtd_grid_import_kwh": 210.0,
                "mtd_grid_export_kwh": 40.0,
                "mtd_load_kwh": 300.0,
                "mtd_import_cost": 44.0,
                "mtd_export_earnings": 1.20,
                "mtd_import_cost_covered_kwh": 210.0,
                "mtd_export_earnings_covered_kwh": 40.0,
                "price_coverage_schema": 1,
                "load_accounting_partial_mtd": True,
            }
        ),
    )
    asyncio.run(restored.async_restore())

    assert restored._load_accounting_partial_mtd is False
    assert restored.as_dict()["avg_cost_per_kwh_mtd"] is not None

    # A real hole in an integrated interval still latches the month again.
    restored.update(0.0, 1.0, 0.0, 1.0, 0.30, 0.05)
    clock.current += timedelta(seconds=30)
    restored.update(0.0, 1.0, 0.0, None, 0.30, 0.05)
    assert restored._load_accounting_partial_mtd is True


def test_repaired_month_payload_is_not_migrated_a_second_time():
    """The repair is one-time: a fixed build's own payload is left alone."""
    clock = _Clock(datetime(2026, 8, 20, 11, 0, 0))
    store = _Store(
        {
            "date": "2026-08-19",
            "month": "2026-08",
            "mtd_grid_import_kwh": 210.0,
            "mtd_grid_export_kwh": 40.0,
            "mtd_load_kwh": 300.0,
            "mtd_import_cost": 44.0,
            "mtd_export_earnings": 1.20,
            "mtd_import_cost_covered_kwh": 24.0,
            "mtd_export_earnings_covered_kwh": 6.0,
        }
    )
    restored = _new_accumulator(clock, store)
    asyncio.run(restored.async_restore())
    asyncio.run(restored.async_flush())
    assert store.data["price_coverage_schema"] == _price_coverage_schema()

    # Re-restoring the repaired payload keeps the counters it just wrote, and
    # a genuine later hole in the same month still fails closed.
    reloaded = _new_accumulator(clock, store)
    asyncio.run(reloaded.async_restore())
    assert reloaded.mtd_import_cost_covered_kwh == pytest.approx(210.0)
    reloaded.mtd_grid_import_kwh = 400.0
    assert reloaded.as_dict()["avg_cost_per_kwh_mtd"] is None


def test_covau_free_import_window_prices_at_zero_and_stays_covered():
    """Ticket #385: A$0.00 during a CovaU free window is the right answer.

    The contract's effective import price inside a free-import window is
    exactly 0.0 c/kWh.  Cost must stay flat while coverage keeps advancing, so
    a legitimately free day never trips the fail-closed blanking that would
    make it indistinguishable from a broken sensor.
    """
    clock = _Clock(datetime(2026, 8, 20, 11, 0, 0))
    accumulator = _new_accumulator(clock, _Store())
    _covau_free_window(accumulator, clock)

    summary = accumulator.as_dict()
    assert summary["grid_import_today_kwh"] > 4.0
    assert summary["import_cost_today"] == pytest.approx(0.0)
    assert summary["import_cost_coverage"] == "complete"
    assert summary["avg_cost_per_kwh_today"] == pytest.approx(0.0)
    assert summary["avg_cost_per_kwh_mtd"] == pytest.approx(0.0)
