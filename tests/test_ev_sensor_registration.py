"""Regression tests for EV sensor registration gates."""

from __future__ import annotations

import ast
from pathlib import Path


SENSOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "sensor.py"
)


def _find_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function '{name}' not found in {SENSOR_PATH}")


def test_generic_charger_enabled_adds_ev_sensor_family():
    """Generic charger setups must create sensor.power_sync_ev_* entities."""
    source = SENSOR_PATH.read_text()
    tree = ast.parse(source)
    setup_source = ast.get_source_segment(source, _find_function(tree, "async_setup_entry"))

    assert setup_source is not None
    assert "CONF_GENERIC_CHARGER_ENABLED" in setup_source
    assert "generic_charger_enabled = entry.options.get(" in setup_source
    assert "or generic_charger_enabled" in setup_source
