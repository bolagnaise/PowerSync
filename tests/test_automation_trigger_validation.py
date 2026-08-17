"""Validation and capability regressions for automation trigger payloads."""

from __future__ import annotations

import ast
from datetime import datetime
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _validation_function():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_automation_trigger_validation_error"
    )
    namespace = {
        "Any": Any,
        "datetime": datetime,
        "math": math,
        "_AUTOMATION_TRIGGER_TYPES": {
            "time",
            "battery",
            "flow",
            "grid_import_energy",
            "grid_export_energy",
            "price",
            "grid",
            "weather",
            "solar_forecast",
            "ev",
            "ocpp",
        },
    }
    exec(ast.get_source_segment(source, function), namespace)
    return namespace["_automation_trigger_validation_error"]


def test_grid_energy_trigger_validation_accepts_both_directions():
    validate = _validation_function()

    for direction in ("import", "export"):
        trigger_type = f"grid_{direction}_energy"
        assert validate({
            "trigger_type": trigger_type,
            f"grid_{direction}_energy_threshold_kwh": 10,
            "time_window_start": "23:00",
            "time_window_end": "01:00",
        }) is None


def test_grid_energy_trigger_validation_rejects_inert_or_unsafe_payloads():
    validate = _validation_function()

    assert "Unsupported" in validate({"trigger_type": "grid_net_energy"})
    assert "greater than 0" in validate({
        "trigger_type": "grid_export_energy",
        "grid_export_energy_threshold_kwh": float("nan"),
        "time_window_start": "09:00",
        "time_window_end": "17:00",
    })
    assert "HH:MM" in validate({
        "trigger_type": "grid_export_energy",
        "grid_export_energy_threshold_kwh": 10,
        "time_window_start": "9am",
        "time_window_end": "17:00",
    })


def test_backend_advertises_export_trigger_capability():
    source = INIT_PATH.read_text()

    assert source.count('"grid_export_energy": True') >= 2
    assert "validation_error = _automation_trigger_validation_error" in source
