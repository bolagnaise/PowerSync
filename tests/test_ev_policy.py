"""Tests for dashboard EV charging policy mapping."""

from __future__ import annotations

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

from power_sync.ev_policy import (  # noqa: E402
    EV_POLICY_FULL_GRID_SOLAR,
    EV_POLICY_LIMITED_GRID_SOLAR,
    EV_POLICY_SOLAR_ONLY,
    build_ev_policy_action,
)


def test_solar_only_maps_to_manual_solar_surplus_dynamic_action():
    action = build_ev_policy_action(EV_POLICY_SOLAR_ONLY, 90)

    assert action.action_type == "start_ev_charging_dynamic"
    assert action.params["dynamic_mode"] == "solar_surplus"
    assert action.params["owner_mode"] == "manual_solar_surplus"
    assert action.params["duration_minutes"] == 90
    assert action.params["source_mode"] == EV_POLICY_SOLAR_ONLY
    assert action.params["quick_control"] is True


def test_limited_grid_solar_maps_to_battery_target_defaults():
    action = build_ev_policy_action(EV_POLICY_LIMITED_GRID_SOLAR, "120")

    assert action.action_type == "start_ev_charging_dynamic"
    assert action.params["dynamic_mode"] == "battery_target"
    assert action.params["owner_mode"] == "manual_limited_grid_solar"
    assert "max_grid_import_kw" not in action.params
    assert "no_grid_import" not in action.params
    assert action.params["duration_minutes"] == 120


def test_full_grid_solar_maps_to_manual_grid_allowed_start():
    action = build_ev_policy_action(EV_POLICY_FULL_GRID_SOLAR, 45)

    assert action.action_type == "start_ev_charging"
    assert action.params["source_mode"] == "grid_allowed"
    assert action.params["source_policy"] == EV_POLICY_FULL_GRID_SOLAR
    assert action.params["duration_minutes"] == 45


def test_policy_validation_rejects_unknown_policy():
    try:
        build_ev_policy_action("battery_only", 60)
    except ValueError as err:
        assert "policy must be one of" in str(err)
    else:
        raise AssertionError("expected policy validation error")


def test_policy_duration_validation_rejects_out_of_range_values():
    for value in (0, 1441, "bad"):
        try:
            build_ev_policy_action(EV_POLICY_SOLAR_ONLY, value)
        except ValueError as err:
            assert "duration_minutes" in str(err)
        else:
            raise AssertionError(f"expected duration validation error for {value!r}")
