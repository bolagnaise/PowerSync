"""Regression coverage for FoxESS optimizer power-limit auto-detection."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
OPTIMIZATION_PATH = (
    ROOT
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "coordinator.py"
)


def _load_power_limit_helper():
    tree = ast.parse(OPTIMIZATION_PATH.read_text())
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_positive_finite_number", "_foxess_auto_power_limits"}
    ]
    assert {node.name for node in functions} == {
        "_positive_finite_number",
        "_foxess_auto_power_limits",
    }
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"Any": Any, "math": math}
    exec(compile(module, str(OPTIMIZATION_PATH), "exec"), namespace)
    return namespace["_foxess_auto_power_limits"]


def _auto_detect_method_source() -> str:
    source = OPTIMIZATION_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_auto_detect_battery_specs"
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError("_auto_detect_battery_specs not found")


def test_reported_foxess_voltage_derives_50a_as_20920w():
    resolve = _load_power_limit_helper()

    assert resolve(
        {
            "max_charge_current_a": 50,
            "max_discharge_current_a": 50,
            "battery_voltage_v": 418.4,
        }
    ) == (20920, 20920)


def test_manual_power_fields_remain_authoritative_over_live_voltage():
    resolve = _load_power_limit_helper()

    assert resolve(
        {
            "battery_max_charge_power_w": 15000,
            "battery_max_discharge_power_w": 15000,
            "max_charge_current_a": 50,
            "max_discharge_current_a": 50,
            "battery_voltage_v": 418.4,
        }
    ) == (15000, 15000)


def test_missing_or_invalid_voltage_keeps_conservative_300v_fallback():
    resolve = _load_power_limit_helper()

    assert resolve(
        {
            "max_charge_current_a": 50,
            "max_discharge_current_a": 40,
            "battery_voltage_v": "unavailable",
        }
    ) == (15000, 12000)
    assert resolve(
        {
            "max_charge_current_a": 50,
            "max_discharge_current_a": 40,
            "battery_voltage_v": float("nan"),
        }
    ) == (15000, 12000)
    assert resolve(
        {
            "max_charge_current_a": 50,
            "max_discharge_current_a": 40,
            "battery_voltage_v": "not-a-number",
        }
    ) == (15000, 12000)


def test_invalid_or_nonfinite_power_and_current_inputs_fail_closed():
    resolve = _load_power_limit_helper()

    assert resolve(
        {
            "max_charge_current_a": "not-a-number",
            "max_discharge_current_a": float("nan"),
            "battery_voltage_v": 418.4,
        }
    ) is None
    assert resolve(
        {
            "battery_max_charge_power_w": float("inf"),
            "battery_max_discharge_power_w": -1,
            "max_charge_current_a": "bad",
            "max_discharge_current_a": None,
        }
    ) is None


def test_auto_detection_checks_manual_overrides_before_live_foxess_data():
    source = _auto_detect_method_source()

    assert source.index(
        "if saved_capacity or saved_charge or saved_discharge"
    ) < source.index("_foxess_auto_power_limits(data)")
    assert "_battery_specs_source = \"manual\"" in source
    assert "FoxESSEntityEnergyCoordinator" in source
