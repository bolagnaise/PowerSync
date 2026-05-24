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


def test_cached_curtailed_state_is_only_trusted_for_fronius_simple_mode():
    source = _method_source("InverterStatusSensor", "_async_poll_inverter")

    assert "inverter_brand == 'fronius'" in source
    assert "cached_curtail_state == 'curtailed'" in source
    assert "not fronius_load_following" in source
