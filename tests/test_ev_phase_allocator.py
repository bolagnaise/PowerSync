"""Focused safety tests for per-phase EV current allocation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components/power_sync/automations/ev_phase_allocator.py"
)
SPEC = importlib.util.spec_from_file_location("ev_phase_allocator_under_test", MODULE_PATH)
phase = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = phase
SPEC.loader.exec_module(phase)


def _samples(l1: float, l2: float, l3: float):
    return {
        name: phase.PhaseSample(name, amps, 1, f"sensor.{name}_current")
        for name, amps in (("l1", l1), ("l2", l2), ("l3", l3))
    }


def _request(
    loadpoint_id: str,
    requested: float,
    *,
    current: float = 0,
    observed: float | None = None,
    footprint=frozenset(("l1", "l2", "l3")),
    minimum: int = 6,
):
    return phase.LoadpointRequest(
        loadpoint_id=loadpoint_id,
        requested_amps=requested,
        min_amps=minimum,
        max_amps=32,
        phases=footprint,
        current_amps=current,
        observed_amps=observed,
    )


def test_legacy_settings_remain_disabled_without_migration():
    settings = phase.normalize_home_power_settings({"phase_type": "three"})

    assert settings["phase_load_management_enabled"] is False
    assert settings["phase_current_entity_l1"] == ""
    assert settings["phase_current_safety_margin_amps"] == 2.0
    assert phase.validate_home_power_settings(settings) is None


def test_enabled_settings_require_unique_phase_entities_and_valid_margin():
    settings = phase.normalize_home_power_settings({
        "phase_type": "three",
        "max_grid_import_amps": 32,
        "phase_load_management_enabled": True,
        "phase_current_entity_l1": "sensor.grid_l1_current",
        "phase_current_entity_l2": "sensor.grid_l2_current",
        "phase_current_entity_l3": "sensor.grid_l3_current",
        "phase_current_safety_margin_amps": 2,
    })

    assert phase.validate_home_power_settings(settings) is None
    settings["phase_current_entity_l3"] = settings["phase_current_entity_l2"]
    assert "different" in phase.validate_home_power_settings(settings)
    settings["phase_current_entity_l3"] = "sensor.grid_l3_current"
    settings["phase_current_safety_margin_amps"] = 32
    assert "lower" in phase.validate_home_power_settings(settings)


def test_worst_phase_limits_three_phase_charger_without_summing_phases():
    result = phase.allocate_phase_currents(
        samples=_samples(29, 18, 17),
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[_request("tesla", 16, current=10, observed=10)],
    )

    # L1 base load is 19 A, leaving 11 A for the owned three-phase charger.
    assert result.allocations == {"tesla": 11}
    assert result.limiting_phase == "l1"
    assert result.reasons["tesla"] == "phase_limited"


def test_unverified_setpoint_is_not_reclaimed_from_phase_measurement():
    result = phase.allocate_phase_currents(
        samples=_samples(29, 10, 10),
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[_request("tesla", 16, current=16, observed=None)],
    )

    assert result.allocations["tesla"] == 0
    assert result.reasons["tesla"] == "below_minimum"


def test_two_owned_vehicles_share_one_budget_without_double_spending():
    result = phase.allocate_phase_currents(
        samples=_samples(26, 26, 26),
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[
            _request("active", 10, current=10, observed=10),
            _request("new", 16, current=0, observed=2),
        ],
    )

    assert result.phase_budgets_amps == {"l1": 16, "l2": 16, "l3": 16}
    assert result.allocations == {"active": 10, "new": 6}
    assert sum(result.allocations.values()) == 16


def test_unknown_single_phase_footprint_is_conservatively_all_phases():
    result = phase.allocate_phase_currents(
        samples=_samples(10, 10, 28),
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[_request("unknown", 16)],
    )

    assert result.allocations["unknown"] == 0
    assert result.limiting_phase == "l3"


def test_subminimum_and_fractional_headroom_round_down_to_stop():
    result = phase.allocate_phase_currents(
        samples=_samples(24.2, 10, 10),
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[_request("tesla", 16, minimum=6)],
    )

    assert result.phase_budgets_amps["l1"] == pytest.approx(5.8)
    assert result.allocations["tesla"] == 0


def test_invalid_or_stale_telemetry_fails_closed_for_every_owned_loadpoint():
    result = phase.allocate_phase_currents(
        samples={},
        breaker_amps=32,
        safety_margin_amps=2,
        loadpoints=[
            _request("one", 16, current=16),
            _request("two", 16, current=8),
        ],
        telemetry_reason="L2 reading is stale",
    )

    assert result.telemetry_valid is False
    assert result.telemetry_reason == "L2 reading is stale"
    assert result.allocations == {"one": 0, "two": 0}
