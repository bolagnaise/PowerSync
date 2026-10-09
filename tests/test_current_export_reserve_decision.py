"""Current controller reserve decisions must describe existing execution only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_battery_export_allowed_slots import (
    _api_action,
    _execution_coordinator,
    _FakeBattery,
    opt_module as _opt_module_fixture,
)

opt_module = _opt_module_fixture
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def clock(opt_module, monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: clock.now)
    monkeypatch.setattr(opt_module.dt_util, "utcnow", lambda: clock.now)
    return clock


def _controller(opt_module, *, soc=0.30, projected=0.18, active=False, targetless=False, battery=None, slots=1):
    battery = battery or _FakeBattery(self_consumption_result=True, restore_normal_result=True)
    coordinator = _execution_coordinator(opt_module, battery, soc=soc)
    coordinator.battery_system = "tesla" if targetless else "goodwe"
    coordinator._enabled = True
    coordinator._last_update_time = NOW
    coordinator._last_executed_action = "export"
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._config.backup_reserve = 0.20
    actions = [
        _api_action(NOW + timedelta(minutes=5 * idx), "export", 5000, projected)
        for idx in range(slots)
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    reads = []

    async def _battery_state():
        reads.append(True)
        return soc, 13500

    coordinator._get_battery_state = _battery_state
    if active:
        coordinator._set_optimizer_force_state("discharge", slots * 5, 5000)
    return coordinator, battery, actions, reads


def _decision(coordinator, **overrides):
    args = {
        "is_stale": False,
        "monitoring_mode": False,
        "force_state": coordinator._get_active_force_state(),
        "plan_snapshot_id": "current-plan",
    }
    args.update(overrides)
    return coordinator._current_export_reserve_decision(coordinator._get_current_action(), **args)


def _run(coordinator, action):
    asyncio.run(coordinator._execute_optimizer_action(action))


@pytest.mark.parametrize("active", [False, True])
def test_live_soc_above_floor_can_be_blocked_by_planned_projection(opt_module, clock, active):
    coordinator, battery, actions, reads = _controller(opt_module, active=active)

    _run(coordinator, actions[0])
    decision = _decision(coordinator)

    assert decision["status"] == "blocked"
    assert decision["reason"] == "projected_below_floor"
    assert decision["current_soc_percent"] == 30.0
    assert decision["projected_soc_percent"] == 18.0
    assert decision["software_floor_percent"] == 20.0
    assert decision["projection_basis"] == "planned_slot"
    assert decision["requested_minutes"] == 5
    assert decision["allowed_minutes"] == 0
    assert decision["control_outcome"] == "self_consumption_accepted"
    assert decision["evaluated_at"] == NOW.isoformat()
    assert decision["slot_start"] == NOW.isoformat()
    assert decision["slot_end"] == (NOW + timedelta(minutes=5)).isoformat()
    assert decision["plan_generated_at"] == NOW.isoformat()
    assert decision["plan_snapshot_id"] == "current-plan"
    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == int(active)
    assert battery.self_consumption_calls == int(not active)
    assert battery.backup_reserve_calls == []
    assert reads == [True]


@pytest.mark.parametrize("active", [False, True])
def test_export_finishing_exactly_at_floor_has_no_block_claim(opt_module, clock, active):
    coordinator, battery, actions, reads = _controller(opt_module, projected=0.20, active=active)

    _run(coordinator, actions[0])

    assert _decision(coordinator) is None
    assert battery.self_consumption_calls == 0
    assert battery.restore_normal_calls == 0
    assert reads == [True]
    if not active:
        assert battery.force_discharge_calls == [(5, 5000, False, None)]


def test_live_soc_at_floor_is_reported_as_decision_time_soc(opt_module, clock):
    coordinator, battery, actions, _ = _controller(opt_module, soc=0.20, projected=0.20)

    _run(coordinator, actions[0])

    assert _decision(coordinator)["reason"] == "at_floor"
    assert _decision(coordinator)["current_soc_percent"] == 20.0
    assert battery.force_discharge_calls == []


def _targetless_controller(opt_module, *, active=False, soc=0.832, slots=6, battery=None):
    coordinator, battery, actions, reads = _controller(
        opt_module, soc=soc, projected=0.66, active=False, targetless=True, slots=slots, battery=battery,
    )
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    for idx, action in enumerate(actions):
        action.power_w = 10000 if idx < 5 else 1281.7
        action.soc = 0.832 - min(idx + 1, 5) * (10000 * (5 / 60) / 0.92 / 27000)
    if slots == 1:
        actions[0].power_w = 1281.7
        actions[0].soc = 0.66
    else:
        actions[-1].soc = 0.66
    if active:
        state = {
            "active": True, "type": "discharge", "source": "optimizer",
            "expires_at": NOW + timedelta(minutes=30),
            "hardware_expires_at": NOW + timedelta(minutes=30),
        }
        coordinator.hass.data = {"power_sync": {"entry-1": {"force_discharge_state": state}}}
        coordinator._force_state_getter = lambda: state
    return coordinator, battery, actions, reads


@pytest.mark.parametrize("active", [False, True])
def test_targetless_partial_interval_is_rejected_before_window_duration(opt_module, clock, active):
    coordinator, battery, actions, _ = _targetless_controller(opt_module, active=active, soc=0.6643, slots=1)

    _run(coordinator, actions[0])
    decision = _decision(coordinator)

    assert decision["status"] == "blocked"
    assert decision["reason"] == "insufficient_safe_duration"
    assert decision["projection_basis"] == "full_power_duration"
    assert decision["current_soc_percent"] > 66.0
    assert decision["projected_soc_percent"] < 66.0
    assert decision["requested_minutes"] == 5
    assert decision["allowed_minutes"] == 0
    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == int(active)
    assert battery.self_consumption_calls == int(not active)


@pytest.mark.parametrize("active", [False, True])
def test_targetless_window_is_shortened_without_changing_force_arguments(opt_module, clock, active):
    coordinator, battery, actions, reads = _targetless_controller(opt_module, active=active)

    _run(coordinator, actions[0])
    decision = _decision(coordinator)

    assert decision["status"] == "limited"
    assert decision["reason"] == "insufficient_safe_duration"
    assert decision["projection_basis"] == "full_power_duration"
    assert decision["requested_minutes"] == 30
    assert decision["allowed_minutes"] == 25
    assert decision["projected_soc_percent"] < decision["software_floor_percent"]
    assert decision["control_outcome"] == "export_accepted"
    assert battery.force_discharge_calls == [(25, 10000, active, None)]
    assert battery.self_consumption_calls == battery.restore_normal_calls == 0
    assert reads == [True]


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("invalid_input", ["soc", "capacity", "efficiency"])
def test_missing_safety_inputs_are_unknown_not_floor_proof(opt_module, clock, active, invalid_input):
    coordinator, battery, actions, _ = _targetless_controller(
        opt_module, active=active, soc=None if invalid_input == "soc" else 0.832,
    )
    if invalid_input == "capacity":
        coordinator._config.battery_capacity_wh = 0
    if invalid_input == "efficiency":
        coordinator._optimizer.efficiency = "invalid"

    _run(coordinator, actions[0])
    decision = _decision(coordinator)

    assert decision["status"] == "unknown"
    assert decision["reason"] == "safe_duration_unverified"
    assert decision["projected_soc_percent"] is None
    assert decision["projection_basis"] is None
    assert decision["allowed_minutes"] == 0
    assert battery.force_discharge_calls == []


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("restore_result", [True, False])
def test_window_safety_becoming_unavailable_keeps_existing_restore_path(opt_module, clock, active, restore_result):
    battery = _FakeBattery(self_consumption_result=restore_result, restore_normal_result=restore_result)
    coordinator, battery, actions, _ = _targetless_controller(opt_module, active=active, battery=battery)
    original = coordinator._targetless_export_safe_duration
    evaluations = []

    def _changing_capacity(*args):
        result = original(*args)
        evaluations.append(True)
        coordinator._config.battery_capacity_wh = 0
        return result

    coordinator._targetless_export_safe_duration = _changing_capacity

    _run(coordinator, actions[0])
    decision = _decision(coordinator)

    assert decision["status"] == "unknown"
    assert decision["reason"] == "safe_duration_unverified"
    assert decision["requested_minutes"] == 30
    assert decision["allowed_minutes"] == 0
    assert decision["projection_basis"] is None
    assert decision["control_outcome"] == (
        "self_consumption_accepted" if restore_result else "restore_unconfirmed"
    )
    assert evaluations == [True, True]
    assert battery.force_discharge_calls == []
    assert battery.backup_reserve_calls == []
    assert battery.restore_normal_calls == int(active)
    assert battery.self_consumption_calls == int(not active)


@pytest.mark.parametrize("projected", [None, float("nan")])
def test_at_floor_with_unknown_projection_has_no_projection_basis(opt_module, clock, projected):
    coordinator, battery, actions, _ = _controller(opt_module, soc=0.20, projected=projected)

    _run(coordinator, actions[0])

    assert _decision(coordinator)["reason"] == "at_floor"
    assert _decision(coordinator)["projected_soc_percent"] is None
    assert _decision(coordinator)["projection_basis"] is None
    assert battery.force_discharge_calls == []


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("command_result", [False, None])
def test_restore_returns_do_not_claim_confirmed_physical_stop(opt_module, clock, active, command_result):
    battery = _FakeBattery(self_consumption_result=command_result, restore_normal_result=command_result)
    coordinator, battery, actions, _ = _controller(opt_module, active=active, battery=battery)

    _run(coordinator, actions[0])

    assert _decision(coordinator)["control_outcome"] == "restore_unconfirmed"
    assert coordinator._last_executed_action == ("export" if command_result is False else "self_consumption")
    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == int(active)
    assert battery.self_consumption_calls == int(not active)
    if active and command_result is False:
        assert coordinator._optimizer_force_state["active"] is True


@pytest.mark.parametrize("active", [False, True])
def test_failed_shortened_export_command_remains_unconfirmed(opt_module, clock, active):
    coordinator, battery, actions, _ = _targetless_controller(
        opt_module, active=active, battery=_FakeBattery(force_discharge_result=False),
    )

    _run(coordinator, actions[0])

    assert _decision(coordinator)["control_outcome"] == "command_unconfirmed"
    assert battery.force_discharge_calls == [(25, 10000, active, None)]


@pytest.mark.parametrize("restore_result", [True, False, None])
def test_tesla_stale_grid_charge_restore_reports_existing_return_value(opt_module, clock, restore_result):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=20, restore_normal_result=restore_result)
    coordinator, battery, actions, reads = _controller(
        opt_module, soc=0.80, projected=0.49, targetless=True, battery=battery,
    )
    coordinator._config.backup_reserve = 0.50
    coordinator._last_executed_action = "self_consumption"
    coordinator._get_energy_data = lambda: {
        "battery_power": -3.34, "grid_power": 3.96,
        "battery_level": 80.0, "grid_services_active": False,
    }

    _run(coordinator, actions[0])

    assert _decision(coordinator)["control_outcome"] == (
        "self_consumption_accepted" if restore_result is True else "restore_unconfirmed"
    )
    assert battery.restore_normal_calls == 1
    assert battery.restore_normal_force_flags == [True]
    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == [20]
    assert battery.force_discharge_calls == []
    assert reads == [True, True]


@pytest.mark.parametrize("active", [False, True])
def test_restore_exception_does_not_claim_success(opt_module, clock, active):
    class _RaisingBattery(_FakeBattery):
        async def restore_normal(self, **kwargs):
            self.restore_normal_calls += 1
            raise RuntimeError("restore unavailable")

        async def set_self_consumption_mode(self):
            self.self_consumption_calls += 1
            raise RuntimeError("mode unavailable")

    coordinator, battery, actions, _ = _controller(opt_module, active=active, battery=_RaisingBattery())

    _run(coordinator, actions[0])

    assert _decision(coordinator)["control_outcome"] == "restore_unconfirmed"
    assert battery.force_discharge_calls == ([(5, 5000, True, None)] if active else [])


def test_replan_during_live_soc_read_cannot_attach_decision_to_new_plan(opt_module, clock):
    coordinator, battery, actions, _ = _controller(opt_module)
    old_schedule = coordinator._current_schedule

    async def _replanning_read():
        coordinator._current_schedule = SimpleNamespace(actions=actions)
        return 0.30, 13500

    coordinator._get_battery_state = _replanning_read

    _run(coordinator, actions[0])

    assert coordinator._export_reserve_decision["schedule"] is old_schedule
    assert _decision(coordinator) is None
    assert battery.force_discharge_calls == []


@pytest.mark.parametrize("invalidated_by", ["new_plan", "same_slot_action", "next_slot", "slot_end", "manual", "monitoring", "disabled", "stale", "restart", "generation"])
def test_summary_is_filtered_when_no_longer_applicable(opt_module, clock, invalidated_by):
    coordinator, battery, actions, reads = _controller(opt_module, slots=2)
    _run(coordinator, actions[0])
    stored = coordinator._export_reserve_decision
    overrides = {}
    if invalidated_by == "new_plan":
        coordinator._current_schedule = SimpleNamespace(actions=actions)
    elif invalidated_by == "same_slot_action":
        coordinator._current_schedule.actions[0] = _api_action(NOW, "export", 5000, 0.18)
    elif invalidated_by in {"next_slot", "slot_end"}:
        clock.now += timedelta(minutes=5 if invalidated_by == "next_slot" else 10)
    elif invalidated_by == "manual":
        overrides["force_state"] = {"active": True, "type": "discharge", "source": "user"}
    elif invalidated_by == "monitoring":
        overrides["monitoring_mode"] = True
    elif invalidated_by == "disabled":
        coordinator._enabled = False
    elif invalidated_by == "stale":
        overrides["is_stale"] = True
    elif invalidated_by == "restart":
        coordinator._export_reserve_decision = None
    elif invalidated_by == "generation":
        coordinator._last_update_time += timedelta(seconds=1)

    assert _decision(coordinator, **overrides) is None
    if invalidated_by != "restart":
        assert coordinator._export_reserve_decision is stored
    assert battery.force_discharge_calls == []
    assert reads == [True]


def test_new_allowed_decision_clears_old_block_and_projection_is_read_only(opt_module, clock):
    coordinator, battery, actions, reads = _controller(opt_module)
    _run(coordinator, actions[0])
    projected = _decision(coordinator)
    projected["status"] = "altered"
    assert _decision(coordinator)["status"] == "blocked"
    actions[0].soc = 0.20

    _run(coordinator, actions[0])

    assert _decision(coordinator) is None
    assert len(reads) == 2
    assert battery.force_discharge_calls == [(5, 5000, False, None)]


def _prepare_api(opt_module, coordinator):
    values = {
        "_cost_function": opt_module.CostFunction("cost"), "_last_optimizer_result": None,
        "_battery_specs_source": "config", "_battery_specs_source_by_field": {},
        "_planned_ev_load_entity_id": None, "_ev_integration_enabled": False,
        "_ev_configs": [], "_ev_coordinator": None, "_last_planned_ev_load_forecast_w": [],
        "_last_import_prices": None, "_last_display_import_prices": None, "_last_display_export_prices": None,
        "_actual_cost_today": 0.0, "_actual_baseline_today": 0.0, "_actual_import_cost_today": 0.0,
        "_actual_export_earnings_today": 0.0, "_actual_import_kwh_today": 0.0, "_actual_export_kwh_today": 0.0,
        "_actual_charge_kwh_today": 0.0, "_actual_discharge_kwh_today": 0.0,
    }
    for key, value in values.items():
        setattr(coordinator, key, value)
    coordinator._get_actual_battery_power_w = lambda: 0
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._get_demand_window_config = lambda: None
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False


def test_api_includes_filtered_decision_and_preserves_accepted_current_action(opt_module, clock):
    coordinator, battery, actions, reads = _controller(
        opt_module, active=True, battery=_FakeBattery(restore_normal_result=False),
    )
    _prepare_api(opt_module, coordinator)
    _run(coordinator, actions[0])
    stored = coordinator._export_reserve_decision

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "export"
    assert data["current_action"] == "discharge"
    assert data["export_reserve_decision"]["control_outcome"] == "restore_unconfirmed"
    assert data["export_reserve_decision"]["plan_snapshot_id"] == data["schedule"]["plan_snapshot_id"]
    assert coordinator._export_reserve_decision is stored
    assert battery.restore_normal_calls == 1
    assert reads == [True]
    coordinator._entry.options["monitoring_mode"] = True
    assert coordinator.get_api_data()["export_reserve_decision"] is None
    coordinator._entry.options["monitoring_mode"] = False
    coordinator._last_update_time = NOW - timedelta(minutes=16)
    assert coordinator.get_api_data()["export_reserve_decision"] is None
