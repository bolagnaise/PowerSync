"""Read-only reserve projections for the API and Current Action sensor."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_battery_export_allowed_slots import _coordinator, opt_module as _opt_module_fixture


opt_module = _opt_module_fixture


COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def coordinator(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._config = SimpleNamespace(backup_reserve=0.45)
    coordinator._manual_backup_reserve = 0.20
    coordinator._auto_apply_reserve_enabled = True
    coordinator._startup_backup_reserve = 15
    coordinator._last_update_time = NOW
    return coordinator


def _visibility(coordinator, recommendation=None, *, age=60, stale_after=900):
    if recommendation is None:
        recommendation = {"suggested_optimizer_reserve_percent": 45}
    return coordinator._reserve_visibility(
        recommendation,
        schedule_age_s=age,
        stale_after_s=stale_after,
    )


def test_manual_active_and_hardware_reserves_remain_distinct(coordinator):
    assert _visibility(coordinator) == {
        "manual_minimum_percent": 20.0,
        "recommended_percent": 45.0,
        "active_software_floor_percent": 45.0,
        "hardware_baseline_percent": 15.0,
        "hardware_value_quality": "saved_baseline",
        "auto_apply_enabled": True,
        "forecast_updated_at": NOW.isoformat(),
        "forecast_quality": "current",
    }


def test_unapplied_recommendation_does_not_change_active_floor(coordinator):
    coordinator._auto_apply_reserve_enabled = False
    coordinator._config.backup_reserve = 0.20
    recommendation = {"suggested_optimizer_reserve_percent": 45}
    before = dict(vars(coordinator))

    visibility = _visibility(coordinator, recommendation)

    assert visibility["recommended_percent"] == 45.0
    assert visibility["active_software_floor_percent"] == 20.0
    assert visibility["auto_apply_enabled"] is False
    assert vars(coordinator) == before
    assert coordinator._config.backup_reserve == 0.20
    assert recommendation == {"suggested_optimizer_reserve_percent": 45}


@pytest.mark.parametrize("auto_apply, expected", [(False, 45.0), (True, None)])
def test_missing_manual_uses_config_only_without_auto_apply(
    coordinator, auto_apply, expected
):
    coordinator._manual_backup_reserve = None
    coordinator._auto_apply_reserve_enabled = auto_apply

    assert _visibility(coordinator)["manual_minimum_percent"] == expected


@pytest.mark.parametrize(
    "invalid", [None, "invalid", float("nan"), float("inf"), -float("inf"), -1, 101, True]
)
def test_invalid_percentages_remain_unknown(coordinator, invalid):
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = invalid
    coordinator._config.backup_reserve = invalid
    coordinator._startup_backup_reserve = invalid

    visibility = _visibility(
        coordinator, {"suggested_optimizer_reserve_percent": invalid}
    )

    assert visibility["manual_minimum_percent"] is None
    assert visibility["recommended_percent"] is None
    assert visibility["active_software_floor_percent"] is None
    assert visibility["hardware_baseline_percent"] is None
    assert visibility["hardware_value_quality"] == "unknown"
    assert visibility["forecast_quality"] == "unavailable"


def test_zero_is_known_for_every_reserve(coordinator):
    coordinator._manual_backup_reserve = 0
    coordinator._config.backup_reserve = 0
    coordinator._startup_backup_reserve = 0

    visibility = _visibility(coordinator, {"suggested_optimizer_reserve_percent": 0})

    assert visibility["manual_minimum_percent"] == 0.0
    assert visibility["recommended_percent"] == 0.0
    assert visibility["active_software_floor_percent"] == 0.0
    assert visibility["hardware_baseline_percent"] == 0.0
    assert visibility["hardware_value_quality"] == "saved_baseline"
    assert visibility["forecast_quality"] == "current"


@pytest.mark.parametrize("age, quality", [(900, "current"), (901, "stale")])
def test_forecast_freshness_uses_existing_age_threshold(coordinator, age, quality):
    coordinator._last_update_time = NOW - timedelta(seconds=age)

    visibility = _visibility(coordinator, age=age)

    assert visibility["forecast_quality"] == quality
    assert visibility["forecast_updated_at"] == coordinator._last_update_time.isoformat()
    assert visibility["recommended_percent"] == 45.0


@pytest.mark.parametrize("recommendation", [{}, {"suggested_optimizer_reserve_percent": None}])
def test_no_recommended_value_is_unavailable_even_with_recent_solve(
    coordinator, recommendation
):
    visibility = _visibility(coordinator, recommendation)

    assert visibility["recommended_percent"] is None
    assert visibility["forecast_quality"] == "unavailable"


def test_recommendation_without_solve_timestamp_is_unavailable(coordinator):
    coordinator._last_update_time = None

    visibility = _visibility(coordinator, age=None)

    assert visibility["recommended_percent"] == 45.0
    assert visibility["forecast_updated_at"] is None
    assert visibility["forecast_quality"] == "unavailable"


@pytest.mark.parametrize("age", [None, float("nan"), float("inf")])
def test_unknown_age_does_not_claim_current_forecast(coordinator, age):
    assert _visibility(coordinator, age=age)["forecast_quality"] == "unavailable"


def _current_action_attributes(data):
    """Evaluate the real sensor's attribute projection without importing HA."""
    tree = ast.parse((COMPONENT_ROOT / "sensor.py").read_text())
    descriptions = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "OPTIMIZER_ACTION_SENSORS"
    )
    description = next(
        node
        for node in descriptions.elts
        if any(
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "Current Action"
            for keyword in node.keywords
        )
    )
    projection = next(keyword.value for keyword in description.keywords if keyword.arg == "attr_fn")
    return eval(compile(ast.Expression(projection), "<Current Action attributes>", "eval"))(data)


def test_api_and_current_action_share_block_without_changing_legacy_fields(
    opt_module, monkeypatch
):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator._config.backup_reserve = 0.20
    recommendation = {"suggested_optimizer_reserve_percent": 45}
    initial = {
        "_optimizer": object(),
        "_enabled": True,
        "_cost_function": opt_module.CostFunction("cost"),
        "_current_schedule": None,
        "_last_update_time": NOW - timedelta(minutes=16),
        "_last_optimizer_result": SimpleNamespace(
            solve_time_s=0.5,
            objective_value=1.0,
            solver_used="highs",
            feasible=True,
            reserve_recommendation=recommendation,
        ),
        "_manual_backup_reserve": 0.20,
        "_auto_apply_reserve_enabled": False,
        "_last_executed_action": "self_consumption",
        "_startup_backup_reserve": None,
        "_battery_specs_source": "config",
        "_battery_specs_source_by_field": {},
        "_planned_ev_load_entity_id": None,
        "_ev_integration_enabled": False,
        "_ev_configs": [],
        "_ev_coordinator": None,
        "_last_planned_ev_load_forecast_w": [],
        "_actual_cost_today": 0.0,
        "_actual_baseline_today": 0.0,
        "_actual_import_cost_today": 0.0,
        "_actual_export_earnings_today": 0.0,
    }
    for key, value in initial.items():
        setattr(coordinator, key, value)
    coordinator._get_actual_battery_power_w = lambda: 0
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: NOW)

    data = coordinator.get_api_data()
    visibility = data["reserve_visibility"]
    attrs = _current_action_attributes(data)

    assert visibility["manual_minimum_percent"] == 20.0
    assert visibility["recommended_percent"] == 45.0
    assert visibility["active_software_floor_percent"] == 20.0
    assert visibility["hardware_baseline_percent"] is None
    assert visibility["hardware_value_quality"] == "unknown"
    assert visibility["forecast_quality"] == "stale"
    assert data["optimization_status"] == "stale"
    assert attrs["reserve_visibility"] == visibility
    assert attrs["reserve_recommendation"] == data["reserve_recommendation"]
    assert data["backup_reserve"] == 0.20
    assert data["manual_backup_reserve"] == 0.20
    assert data["config"]["hardware_backup_reserve"] == 0
    assert recommendation == {"suggested_optimizer_reserve_percent": 45}
    assert coordinator._config.backup_reserve == 0.20
    assert coordinator._manual_backup_reserve == 0.20
    assert coordinator._startup_backup_reserve is None


def test_current_action_handles_older_payload_without_reserve_block():
    assert _current_action_attributes({"current_action": "idle"})["reserve_visibility"] == {}
    assert _current_action_attributes(None) == {}
