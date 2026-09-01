"""Regression coverage for Price-Level Charging forward projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "price_level_projection.py"
)


@pytest.fixture()
def projection_module():
    name = "power_sync_price_level_projection"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _timestamps(count: int, *, minutes: int = 5) -> list[datetime]:
    start = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
    return [start + timedelta(minutes=minutes * index) for index in range(count)]


def _vehicle(module, **overrides):
    values = {
        "vehicle_id": "VIN_ONE",
        "loadpoint_id": "VIN_ONE",
        "display_name": "Daily EV",
        "ev_soc_percent": 20.0,
        "location": "home",
        "plugged_in": True,
        "home_battery_soc_percent": 70.0,
        "charger_power_w": 7200.0,
        "charger_power_known": True,
        "battery_capacity_kwh": 60.0,
        "battery_capacity_source": "manual",
    }
    values.update(overrides)
    return module.PriceLevelVehicleSnapshot(**values)


def _build(module, *, prices, vehicles, timestamps=None, **overrides):
    values = {
        "timestamps": timestamps or _timestamps(len(prices)),
        "prices_cents": prices,
        "vehicles": vehicles,
        "enabled": True,
        "recovery_soc": 40,
        "recovery_price_cents": 30,
        "opportunity_price_cents": 10,
        "home_battery_minimum": 20,
        "preserve_home_battery": False,
        "no_grid_import": False,
        "interval_minutes": 5,
    }
    values.update(overrides)
    return module.build_price_level_projection(**values)


@pytest.mark.parametrize(
    ("soc", "price", "should_charge", "mode"),
    [
        (13, 30, True, "price_level_recovery"),
        (13, 30.01, False, ""),
        (40, 10, True, "price_level_opportunity"),
        (40, 10.01, False, ""),
        (None, 10, True, "price_level_opportunity"),
        (None, 30, True, "price_level_recovery"),
        (None, 30.01, False, ""),
        (100, -50, False, ""),
        (20, -50, True, "price_level_recovery"),
        (20, None, False, ""),
    ],
)
def test_policy_boundaries_match_live_semantics(
    projection_module, soc, price, should_charge, mode
):
    decision = projection_module.classify_price_level_policy(
        ev_soc_percent=soc,
        price_cents=price,
        recovery_soc=40,
        recovery_price_cents=30,
        opportunity_price_cents=10,
    )
    assert decision.should_charge is should_charge
    assert decision.mode == mode


def test_optimizer_force_state_does_not_suppress_price_level_projection(
    projection_module,
):
    assert projection_module.manual_force_block_reason(
        {"active": True, "type": "charge", "source": "optimizer", "scope": "optimizer"}
    ) is None
    assert projection_module.manual_force_block_reason(
        {"active": True, "type": "discharge", "source": "user"}
    ) == "Manual force discharge is active"


def test_recovery_energy_is_expected_and_final_slot_is_fractional(projection_module):
    # 1% of a 60 kWh battery at 90% efficiency requires 666.67 Wh from AC.
    result = _build(
        projection_module,
        prices=[30, 30, 30],
        vehicles=[_vehicle(projection_module, ev_soc_percent=39.0)],
    )

    assert result.expected_w[0] == pytest.approx(7200.0)
    assert result.expected_w[1] == pytest.approx(800.0)
    assert result.expected_w[2] == 0
    assert sum(result.expected_w) * (5 / 60) == pytest.approx(666.6667)
    assert [window.classification for window in result.windows] == ["expected", "expected"]
    assert sum(window.expected_energy_wh or 0 for window in result.windows) == pytest.approx(666.6667)


def test_slots_after_recovery_target_transition_to_conditional_opportunity(projection_module):
    result = _build(
        projection_module,
        prices=[30, 10],
        vehicles=[
            _vehicle(
                projection_module,
                ev_soc_percent=39.9,
                battery_capacity_kwh=60,
                charger_power_w=7200,
            )
        ],
    )
    assert result.expected_w[0] == pytest.approx(800)
    assert result.expected_w[1] == 0
    assert result.conditional_cap_w == (0.0, 7200.0)
    assert [window.trigger for window in result.windows] == [
        "recovery",
        "opportunity",
    ]


def test_opportunity_and_unknown_inputs_remain_conditional(projection_module):
    result = _build(
        projection_module,
        prices=[10, 9],
        vehicles=[
            _vehicle(
                projection_module,
                ev_soc_percent=None,
                battery_capacity_kwh=None,
                battery_capacity_source="default_estimate",
            )
        ],
    )

    assert result.expected_w == (0.0, 0.0)
    assert result.conditional_cap_w == (7200.0, 7200.0)
    assert len(result.windows) == 1
    assert result.windows[0].trigger == "opportunity"
    assert result.windows[0].classification == "conditional"
    assert "EV SOC is unavailable" in result.windows[0].assumptions


@pytest.mark.parametrize("option", ["preserve_home_battery", "no_grid_import"])
def test_live_circular_controls_never_become_expected_load(projection_module, option):
    result = _build(
        projection_module,
        prices=[20],
        vehicles=[_vehicle(projection_module)],
        **{option: True},
    )
    assert result.expected_w == (0.0,)
    assert result.conditional_cap_w == (7200.0,)
    assert result.windows[0].classification == "conditional"


def test_demand_window_and_owner_conflict_are_suppressed(projection_module):
    demand = _build(
        projection_module,
        prices=[20],
        vehicles=[_vehicle(projection_module)],
        demand_blocked=[True],
    )
    assert demand.expected_w == (0.0,)
    assert demand.conditional_cap_w == (0.0,)
    assert demand.windows[0].classification == "suppressed"
    assert demand.windows[0].suppressed_by == "demand_window"

    owner = _build(
        projection_module,
        prices=[20],
        vehicles=[
            _vehicle(
                projection_module,
                blocked_by="smart_schedule",
                blocking_reason="Smart Schedule owns this loadpoint",
            )
        ],
    )
    assert owner.windows[0].classification == "suppressed"
    assert owner.windows[0].suppressed_by == "smart_schedule"


def test_distinct_loadpoints_sum_but_duplicate_loadpoint_uses_max(projection_module):
    first = _vehicle(projection_module, vehicle_id="A", loadpoint_id="shared", charger_power_w=3600)
    duplicate = _vehicle(projection_module, vehicle_id="A_ALIAS", loadpoint_id="shared", charger_power_w=7200)
    second = _vehicle(projection_module, vehicle_id="B", loadpoint_id="B", charger_power_w=3600)
    result = _build(
        projection_module,
        prices=[20],
        vehicles=[first, duplicate, second],
    )

    # The pure builder retains per-loadpoint max values for coordinator arbitration.
    assert result.expected_by_loadpoint["shared"] == (7200.0,)
    assert result.expected_by_loadpoint["B"] == (3600.0,)

    conditional = _build(
        projection_module,
        prices=[10],
        vehicles=[
            _vehicle(
                projection_module,
                vehicle_id="A",
                loadpoint_id="shared",
                ev_soc_percent=50,
                charger_power_w=3600,
            ),
            _vehicle(
                projection_module,
                vehicle_id="A_ALIAS",
                loadpoint_id="shared",
                ev_soc_percent=50,
                charger_power_w=7200,
            ),
            _vehicle(
                projection_module,
                vehicle_id="B",
                loadpoint_id="B",
                ev_soc_percent=50,
                charger_power_w=3600,
            ),
        ],
    )
    assert conditional.conditional_cap_w == (10800.0,)


def test_absolute_slot_durations_handle_dst_and_variable_intervals(projection_module):
    # Offset changes while absolute time remains monotonic; slots are 30 and 60 minutes.
    timestamps = [
        datetime.fromisoformat("2026-04-05T01:30:00+11:00"),
        datetime.fromisoformat("2026-04-05T01:00:00+10:00"),
        datetime.fromisoformat("2026-04-05T02:00:00+10:00"),
    ]
    result = _build(
        projection_module,
        timestamps=timestamps,
        prices=[20, 20, 20],
        vehicles=[_vehicle(projection_module, ev_soc_percent=39.0, charger_power_w=1000)],
        interval_minutes=30,
    )
    assert result.expected_w[0] == pytest.approx(1000)
    assert result.expected_w[1] == pytest.approx(166.6667)
    assert result.expected_w[2] == 0


def test_external_suppression_preserves_window_provenance(projection_module):
    result = _build(
        projection_module,
        prices=[20],
        vehicles=[_vehicle(projection_module)],
    ).with_suppressed_expected(
        suppressed_by="external_planned_load",
        reason="External planned EV load is authoritative",
    )
    assert result.expected_w == (0.0,)
    assert result.expected_by_loadpoint == {}
    assert result.windows[0].classification == "suppressed"
    assert result.windows[0].included_in_optimizer is False


def test_unavailable_observations_and_invalid_price_coverage_fail_closed(projection_module):
    uncertain = _vehicle(
        projection_module,
        location="unknown",
        plugged_in=None,
        charger_power_known=False,
    )
    result = _build(
        projection_module,
        prices=[20, 20, 20],
        valid_price_slots=[True, False, True],
        vehicles=[uncertain],
    )
    assert result.expected_w == (0.0, 0.0, 0.0)
    assert result.conditional_cap_w == (7200.0, 0.0, 7200.0)
    assert all(window.classification == "conditional" for window in result.windows)


def test_one_invalid_vehicle_does_not_invalidate_other_loadpoints(projection_module):
    result = _build(
        projection_module,
        prices=[20],
        vehicles=[
            _vehicle(projection_module, vehicle_id="bad", loadpoint_id=""),
            _vehicle(projection_module, vehicle_id="good", loadpoint_id="charger"),
        ],
    )
    assert result.expected_by_loadpoint["charger"] == (7200.0,)
    assert any("no loadpoint identity" in warning for warning in result.warnings)


def test_home_battery_below_minimum_is_conditional(projection_module):
    result = _build(
        projection_module,
        prices=[20],
        vehicles=[_vehicle(projection_module, home_battery_soc_percent=19)],
    )
    assert result.expected_w == (0.0,)
    assert result.conditional_cap_w == (7200.0,)
    assert any(
        "below the 20% minimum" in assumption
        for assumption in result.windows[0].assumptions
    )


def test_price_level_settings_write_only_schedules_nonblocking_replan():
    source = INIT_PATH.read_text()
    start = source.index("class PriceLevelChargingSettingsView")
    end = source.index("class PriceLevelChargingStatusView", start)
    view_source = source[start:end]
    assert "_schedule_settings_reoptimization" in view_source
    assert "await opt_coordinator._run_optimization" not in view_source
    assert "_start_charging(" not in view_source
    assert "_stop_charging(" not in view_source
