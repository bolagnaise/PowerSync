"""Tests for EV demand modeled inside the LP battery optimizer.

The home-load forecast excludes EV charging, so before this the optimizer
planned battery charge windows against the full site import limit while the
car quietly consumed part of it. These cover the EV decision variable, the
soft delivery constraint, and the greedy fallback's known-load treatment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
MODULE_PATH = ROOT / "optimization" / "ev_load_plan.py"
SPEC = importlib.util.spec_from_file_location("ev_load_plan", MODULE_PATH)
ev_load_plan = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
# dataclass() resolves annotations through sys.modules, so register first.
sys.modules["ev_load_plan"] = ev_load_plan
SPEC.loader.exec_module(ev_load_plan)

EVChargePlan = ev_load_plan.EVChargePlan


def _plan(**overrides):
    kwargs = {
        "vehicle_id": "car",
        "max_power_kw": (7.0,) * 8,
        "energy_needed_kwh": 21.0,
        "charge_efficiency": 1.0,
        "min_power_kw": 1.4,
    }
    kwargs.update(overrides)
    return EVChargePlan(**kwargs)


# ---------------------------------------------------------------------------
# Plan normalization
# ---------------------------------------------------------------------------


def test_plan_is_padded_to_the_solve_horizon():
    normalized = ev_load_plan.normalize_ev_charge_plan(_plan(), 12)

    assert len(normalized.max_power_kw) == 12
    assert normalized.max_power_kw[8:] == (0.0, 0.0, 0.0, 0.0)


def test_plan_is_truncated_to_the_solve_horizon():
    normalized = ev_load_plan.normalize_ev_charge_plan(_plan(), 4)

    assert len(normalized.max_power_kw) == 4
    assert normalized.deadline_index == 3


def test_plan_outside_the_horizon_disappears():
    plan = _plan(max_power_kw=(0.0,) * 8)

    assert ev_load_plan.normalize_ev_charge_plan(plan, 8) is None


def test_plan_with_no_energy_need_disappears():
    assert ev_load_plan.normalize_ev_charge_plan(_plan(energy_needed_kwh=0.0), 8) is None


def test_absent_plan_stays_absent():
    assert ev_load_plan.normalize_ev_charge_plan(None, 8) is None


def test_implausible_efficiency_falls_back_to_the_default():
    normalized = ev_load_plan.normalize_ev_charge_plan(
        _plan(charge_efficiency=0.0), 8
    )

    assert normalized.charge_efficiency == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Combining vehicles
# ---------------------------------------------------------------------------


def test_two_vehicles_combine_into_one_site_demand():
    combined = ev_load_plan.combine_ev_charge_plans(
        [
            _plan(vehicle_id="a", max_power_kw=(7.0,) * 8, energy_needed_kwh=21.0),
            _plan(vehicle_id="b", max_power_kw=(3.0,) * 8, energy_needed_kwh=9.0),
        ],
        8,
    )

    assert combined.max_power_kw[0] == pytest.approx(10.0)
    assert combined.energy_needed_kwh == pytest.approx(30.0)


def test_combining_nothing_returns_nothing():
    assert ev_load_plan.combine_ev_charge_plans([None, None], 8) is None


def test_a_single_vehicle_is_not_relabeled():
    combined = ev_load_plan.combine_ev_charge_plans([_plan(vehicle_id="a")], 8)

    assert combined.vehicle_id == "a"


# ---------------------------------------------------------------------------
# Period bounds and the greedy profile
# ---------------------------------------------------------------------------


def test_period_bounds_average_the_slots_they_cover():
    plan = _plan(max_power_kw=(7.0, 7.0, 0.0, 0.0, 7.0, 7.0, 7.0, 7.0))

    bounds = ev_load_plan.ev_charge_bounds_kw(plan, [(0, 4), (4, 8)])

    # A period that is only half available carries half the power, so its
    # energy budget over the period matches the slots it stands for.
    assert bounds == [pytest.approx(3.5), pytest.approx(7.0)]


def test_greedy_profile_charges_as_soon_as_the_window_allows():
    profile = ev_load_plan.expected_ev_load_kw(_plan(), 8, 1.0)

    assert profile[:3] == [pytest.approx(7.0)] * 3
    assert profile[3:] == [pytest.approx(0.0)] * 5


def test_greedy_profile_skips_unavailable_slots():
    plan = _plan(max_power_kw=(0.0, 0.0, 7.0, 7.0, 7.0, 7.0, 7.0, 7.0))

    profile = ev_load_plan.expected_ev_load_kw(plan, 8, 1.0)

    assert profile[:2] == [pytest.approx(0.0)] * 2
    assert profile[2:5] == [pytest.approx(7.0)] * 3


def test_greedy_profile_respects_charge_efficiency():
    plan = _plan(energy_needed_kwh=6.3, charge_efficiency=0.9)

    profile = ev_load_plan.expected_ev_load_kw(plan, 8, 1.0)

    # 7 kW for one hour at 90% lands 6.3 kWh, so one slot is enough.
    assert profile[0] == pytest.approx(7.0)
    assert profile[1] == pytest.approx(0.0)


def test_greedy_profile_is_empty_without_a_plan():
    assert ev_load_plan.expected_ev_load_kw(None, 4, 1.0) == [0.0, 0.0, 0.0, 0.0]


def test_unmet_energy_reports_the_undelivered_remainder():
    plan = ev_load_plan.normalize_ev_charge_plan(_plan(), 8)

    assert ev_load_plan.unmet_ev_energy_kwh(plan, [7.0, 7.0], 1.0) == pytest.approx(7.0)
    assert ev_load_plan.unmet_ev_energy_kwh(plan, [7.0] * 3, 1.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Building a plan from the EV planner's figures
# ---------------------------------------------------------------------------


def _timestamps(count=8):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc)
    return [start + timedelta(hours=index) for index in range(count)]


def test_demand_builds_the_physical_window_up_to_the_deadline():
    from datetime import datetime, timezone

    plan = ev_load_plan.ev_plan_from_demand(
        vehicle_id="car",
        energy_needed_kwh=14.0,
        charger_power_kw=7.0,
        schedule_timestamps=_timestamps(),
        deadline=datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc),
    )

    # Four hours of window, then nothing.
    assert plan.max_power_kw[:4] == (7.0, 7.0, 7.0, 7.0)
    assert plan.max_power_kw[4:] == (0.0, 0.0, 0.0, 0.0)


def test_demand_respects_a_later_availability_start():
    from datetime import datetime, timezone

    plan = ev_load_plan.ev_plan_from_demand(
        vehicle_id="car",
        energy_needed_kwh=14.0,
        charger_power_kw=7.0,
        schedule_timestamps=_timestamps(),
        available_from=datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc),
    )

    assert plan.max_power_kw[:2] == (0.0, 0.0)
    assert plan.max_power_kw[2] == 7.0


def test_demand_without_energy_or_a_charger_is_no_plan():
    assert (
        ev_load_plan.ev_plan_from_demand(
            vehicle_id="car",
            energy_needed_kwh=0.0,
            charger_power_kw=7.0,
            schedule_timestamps=_timestamps(),
        )
        is None
    )
    assert (
        ev_load_plan.ev_plan_from_demand(
            vehicle_id="car",
            energy_needed_kwh=14.0,
            charger_power_kw=0.0,
            schedule_timestamps=_timestamps(),
        )
        is None
    )


def test_a_deadline_already_past_leaves_no_window():
    from datetime import datetime, timezone

    assert (
        ev_load_plan.ev_plan_from_demand(
            vehicle_id="car",
            energy_needed_kwh=14.0,
            charger_power_kw=7.0,
            schedule_timestamps=_timestamps(),
            deadline=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
        )
        is None
    )
