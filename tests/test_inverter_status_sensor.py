"""Regression tests for inverter status sensor state handling."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "sensor.py"


def _method_source(class_name: str, method_name: str) -> str:
    module = ast.parse(SENSOR_PATH.read_text())
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef))
                    and item.name == method_name
                ):
                    return ast.unparse(item)
    raise AssertionError(f"{class_name}.{method_name} not found")


def _function(function_name: str):
    module = ast.parse(SENSOR_PATH.read_text())
    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
    }
    if function_name not in functions:
        raise AssertionError(f"{function_name} not found")

    body = []
    if function_name == "_restored_inverter_daily_attributes":
        body.append(functions["_merge_inverter_status_attributes"])
    body.append(functions[function_name])
    namespace = {}
    function_module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(function_module)
    exec(compile(function_module, str(SENSOR_PATH), "exec"), namespace)
    return namespace[function_name]


def test_cached_curtailed_state_is_only_trusted_for_fronius_simple_mode():
    source = _method_source("InverterStatusSensor", "_async_poll_inverter")

    assert "inverter_brand == 'fronius'" in source
    assert "cached_curtail_state == 'curtailed'" in source
    assert "not fronius_load_following" in source


def test_load_following_status_distinguishes_pending_physical_convergence():
    native_value = _method_source("InverterStatusSensor", "native_value")
    attributes = _method_source("InverterStatusSensor", "extra_state_attributes")

    assert "inverter_curtailment_physical_converged" in native_value
    assert "Load Following Pending" in native_value
    assert "device_limit_confirmed" in attributes
    assert "physical_converged" in attributes
    assert "residual_export_w" in attributes


def test_sleeping_inverter_keeps_same_day_daily_generation():
    merge = _function("_merge_inverter_status_attributes")

    attrs = merge(
        {
            "daily_pv_generation": 18.2,
            "daily_pv_generation_date": "2026-07-27",
            "inverter_source_id": "sungrow:192.0.2.20:502:1",
            "power_output_w": 3119,
        },
        {"host": "192.0.2.20", "model": "SG10RT"},
        "2026-07-27",
        "sungrow:192.0.2.20:502:1",
    )

    assert attrs["daily_pv_generation"] == 18.2
    assert attrs["daily_pv_generation_date"] == "2026-07-27"
    assert "power_output_w" not in attrs


def test_sleeping_inverter_drops_previous_day_generation():
    merge = _function("_merge_inverter_status_attributes")

    attrs = merge(
        {
            "daily_pv_generation": 18.2,
            "daily_pv_generation_date": "2026-07-26",
            "inverter_source_id": "sungrow:192.0.2.20:502:1",
        },
        {"host": "192.0.2.20", "model": "SG10RT"},
        "2026-07-27",
        "sungrow:192.0.2.20:502:1",
    )

    assert "daily_pv_generation" not in attrs
    assert "daily_pv_generation_date" not in attrs


def test_restart_restores_only_same_day_daily_generation():
    restore_daily = _function("_restored_inverter_daily_attributes")

    attrs = restore_daily(
        {
            "daily_pv_generation": 18.2,
            "daily_pv_generation_date": "2026-07-27",
            "inverter_source_id": "sungrow:192.0.2.20:502:1",
            "power_output_w": 3119,
            "last_poll": "2026-07-27T13:00:00+10:00",
        },
        "2026-07-27",
        "sungrow:192.0.2.20:502:1",
    )

    assert attrs == {
        "daily_pv_generation": 18.2,
        "daily_pv_generation_date": "2026-07-27",
        "inverter_source_id": "sungrow:192.0.2.20:502:1",
    }


def test_restart_does_not_restore_previous_sungrow_endpoint():
    restore_daily = _function("_restored_inverter_daily_attributes")

    attrs = restore_daily(
        {
            "daily_pv_generation": 18.2,
            "daily_pv_generation_date": "2026-07-27",
            "inverter_source_id": "sungrow:192.0.2.20:502:1",
        },
        "2026-07-27",
        "sungrow:192.0.2.30:502:1",
    )

    assert attrs == {}


def test_inverter_status_restores_daily_counter_before_initial_poll():
    source = _method_source("InverterStatusSensor", "async_added_to_hass")

    assert "await self.async_get_last_state()" in source
    assert "_restored_inverter_daily_attributes" in source
    assert source.index("await self.async_get_last_state()") < source.index(
        "await self._async_poll_inverter()"
    )
