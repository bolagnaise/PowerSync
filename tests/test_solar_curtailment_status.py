"""Regression tests for truthful FoxESS curtailment status."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any


SENSOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "sensor.py"
)
INIT_PATH = SENSOR_PATH.parent / "__init__.py"


def _load_status_helper():
    tree = ast.parse(SENSOR_PATH.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_foxess_curtailment_visible_status"
    )
    namespace = {
        "Any": Any,
        "datetime": datetime,
        "math": math,
        "timedelta": timedelta,
        "timezone": timezone,
    }
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    exec(compile(module, str(SENSOR_PATH), "exec"), namespace)
    return namespace[helper.name]


def _status(**overrides):
    now = datetime(2026, 8, 13, 5, 12, tzinfo=timezone.utc)
    values = {
        "curtailment_enabled": True,
        "control_state": "curtailed",
        "grid_power_kw": -2.8,
        "grid_power_valid": True,
        "telemetry_ready": True,
        "last_update_success": True,
        "last_update": now - timedelta(seconds=15),
        "update_interval": timedelta(seconds=30),
        "now": now,
    }
    values.update(overrides)
    return _load_status_helper()(**values)


def test_reported_material_export_is_pending_not_active():
    assert _status() == ("Pending", 2800.0, False)


def test_active_requires_acknowledged_state_and_fresh_zero_export():
    assert _status(grid_power_kw=-0.2) == ("Active", 200.0, True)
    assert _status(control_state="normal", grid_power_kw=-0.2) == (
        "Normal",
        None,
        False,
    )


def test_stale_or_invalid_telemetry_cannot_confirm_physical_effect():
    stale = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)
    assert _status(grid_power_kw=-0.1, last_update=stale) == (
        "Pending",
        None,
        False,
    )
    assert _status(grid_power_kw=None) == ("Pending", None, False)


def test_disabled_curtailment_is_normal_even_at_negative_price():
    assert _status(curtailment_enabled=False) == ("Normal", None, False)


def test_sensor_and_dashboard_expose_pending_as_distinct_state():
    source = SENSOR_PATH.read_text()
    init_source = INIT_PATH.read_text()
    frontend = (
        SENSOR_PATH.parent / "frontend" / "power-sync-strategy.js"
    ).read_text()

    assert "return self._foxess_status()[0]" in source
    assert 'entry_data.get("foxess_curtailment_state", "normal")' in source
    assert init_source.count(
        'f"power_sync_curtailment_updated_{entry.entry_id}"'
    ) >= 3
    assert "PENDING - Export not confirmed" in frontend
