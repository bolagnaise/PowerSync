"""Regression tests for truthful solar curtailment status."""

from __future__ import annotations

import ast
import re
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
        "force_dispatch_active": False,
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
    assert _status(grid_power_valid=False, grid_power_kw=-0.1) == (
        "Pending",
        None,
        False,
    )
    assert _status(telemetry_ready=False, grid_power_kw=-0.1) == (
        "Pending",
        None,
        False,
    )


def test_force_dispatch_ownership_cannot_report_curtailment_active():
    assert _status(grid_power_kw=-0.1, force_dispatch_active=True) == (
        "Pending",
        None,
        False,
    )


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
    assert '"get_active_force_state"' in source
    assert 'f"{DOMAIN}_force_charge_state"' in source
    assert init_source.count(
        'f"power_sync_curtailment_updated_{entry.entry_id}"'
    ) >= 3
    assert "PENDING - Export not confirmed" in frontend
    assert "state === 'Active' || state === 'Pending'" in frontend


# --- Discord #386: non-FoxESS status must not be a price predicate ----------


def _load_generic_status_helper():
    tree = ast.parse(SENSOR_PATH.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_generic_curtailment_visible_status"
    )
    namespace: dict[str, Any] = {}
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    exec(compile(module, str(SENSOR_PATH), "exec"), namespace)
    return namespace[helper.name]


def _load_goodwe_status_helper():
    tree = ast.parse(SENSOR_PATH.read_text())
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {"_foxess_curtailment_visible_status", "_goodwe_curtailment_visible_status"}
    ]
    namespace = {
        "Any": Any,
        "datetime": datetime,
        "math": math,
        "timedelta": timedelta,
        "timezone": timezone,
    }
    module = ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[]))
    exec(compile(module, str(SENSOR_PATH), "exec"), namespace)
    return namespace["_goodwe_curtailment_visible_status"]


def _generic_status(**overrides):
    values = {
        "curtailment_enabled": True,
        "control_state": "normal",
        "export_uneconomic": True,
    }
    values.update(overrides)
    return _load_generic_status_helper()(**values)


def _goodwe_status(**overrides):
    now = datetime(2026, 8, 24, 5, 12, tzinfo=timezone.utc)
    values = {
        "curtailment_enabled": True,
        "control_state": "curtailed",
        "export_uneconomic": True,
        "grid_power_kw": -2.8,
        "telemetry_ready": True,
        "last_update_success": True,
        "force_dispatch_active": False,
        "last_update": now - timedelta(seconds=15),
        "update_interval": timedelta(seconds=30),
        "now": now,
    }
    values.update(overrides)
    return _load_goodwe_status_helper()(**values)


def test_uncommanded_curtailment_is_pending_for_non_foxess_brands():
    """#386: a GoodWe ESA exported 5.92 kW under a "CURTAILED" marker.

    The marker was ``-feedin_price < 1.0`` and nothing else, so it asserted
    "Export confirmed stopped" on an entry whose curtailment handler had
    returned without issuing any command at all.
    """
    assert _generic_status() == "Pending"
    assert _generic_status(control_state="curtailed") == "Active"


def test_generic_curtailment_status_is_normal_when_export_is_economic():
    assert _generic_status(export_uneconomic=False) == "Normal"
    # An acknowledged command outranks the price: still curtailed until restored.
    assert _generic_status(export_uneconomic=False, control_state="curtailed") == "Active"


def test_disabled_curtailment_is_normal_for_non_foxess_brands_too():
    assert _generic_status(curtailment_enabled=False) == "Normal"
    assert (
        _generic_status(curtailment_enabled=False, control_state="curtailed") == "Normal"
    )


def test_unverified_command_remains_pending_after_price_has_cleared():
    assert _generic_status(control_state="pending", export_uneconomic=False) == "Pending"


def test_goodwe_active_requires_fresh_physical_zero_export_proof():
    """#386: direct GoodWe register readback alone is not a physical effect."""
    assert _goodwe_status() == ("Pending", 2800.0, False)
    assert _goodwe_status(grid_power_kw=-0.2) == ("Active", 200.0, True)
    assert _goodwe_status(grid_power_kw=None) == ("Pending", None, False)
    assert _goodwe_status(
        grid_power_kw=-0.2,
        last_update=datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc),
    ) == ("Pending", None, False)
    assert _goodwe_status(grid_power_kw=-0.2, force_dispatch_active=True) == (
        "Pending",
        None,
        False,
    )
    assert _goodwe_status(control_state="unsupported") == ("Pending", None, False)
    assert _goodwe_status(control_state="normal") == ("Pending", None, False)
    assert _goodwe_status(
        control_state="normal", export_uneconomic=False
    ) == ("Normal", None, False)


def test_status_marker_consults_every_brand_control_state_key():
    """No brand that can command curtailment may be missing from the marker."""
    source = SENSOR_PATH.read_text()
    init_source = INIT_PATH.read_text()

    tree = ast.parse(source)
    keys = next(
        set(ast.literal_eval(node.value))
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "CURTAILMENT_CONTROL_STATE_KEYS"
            for target in node.targets
        )
    )
    commanded = {
        match
        for match in re.findall(r"\"([a-z_]+_curtailment_state)\"\]\s*=", init_source)
    }
    assert commanded, "no brand curtailment lifecycle writes found"
    assert commanded <= keys

    # And the price is no longer sufficient on its own.
    assert "return export_earnings < 1.0" not in source
