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
CURTAILMENT_CONFIG_PATH = SENSOR_PATH.parent / "curtailment_config.py"


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


def _load_effective_configuration_helper():
    tree = ast.parse(CURTAILMENT_CONFIG_PATH.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "get_effective_solar_curtailment_configuration"
    )
    namespace = {
        "Any": Any,
        "BATTERY_SYSTEM_SIGENERGY": "sigenergy",
        "BATTERY_SYSTEM_ALPHAESS": "alphaess",
        "BATTERY_SYSTEM_SOLAREDGE": "solaredge",
        "CONF_BATTERY_CURTAILMENT_ENABLED": "battery_curtailment_enabled",
        "CONF_BATTERY_SYSTEM": "battery_system",
        "CONF_SIGENERGY_DC_CURTAILMENT_ENABLED": "sigenergy_dc_curtailment_enabled",
        "CONF_ALPHAESS_DC_CURTAILMENT_ENABLED": "alphaess_dc_curtailment_enabled",
        "CONF_SOLAREDGE_DC_CURTAILMENT_ENABLED": "solaredge_dc_curtailment_enabled",
    }
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    exec(compile(module, str(CURTAILMENT_CONFIG_PATH), "exec"), namespace)
    return namespace[helper.name]


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


def test_dc_only_curtailment_requires_the_selected_brand_and_its_opt_in():
    effective_configuration = _load_effective_configuration_helper()

    for battery_system, dc_key in (
        ("sigenergy", "sigenergy_dc_curtailment_enabled"),
        ("alphaess", "alphaess_dc_curtailment_enabled"),
        ("solaredge", "solaredge_dc_curtailment_enabled"),
    ):
        entry = type("Entry", (), {"options": {"battery_system": battery_system, dc_key: True}, "data": {}})()
        assert effective_configuration(entry) == (False, True)

    assert effective_configuration(
        type("Entry", (), {"options": {"sigenergy_dc_curtailment_enabled": True}, "data": {}})()
    ) == (False, False)
    assert effective_configuration(
        type("Entry", (), {"options": {"battery_system": "alphaess", "sigenergy_dc_curtailment_enabled": True}, "data": {}})()
    ) == (False, False)
    assert effective_configuration(
        type(
            "Entry",
            (),
            {
                "options": {},
                "data": {
                    "battery_system": "solaredge",
                    "solaredge_dc_curtailment_enabled": True,
                },
            },
        )()
    ) == (False, True)
    assert effective_configuration(
        type(
            "Entry",
            (),
            {
                "options": {
                    "battery_system": "solaredge",
                    "solaredge_dc_curtailment_enabled": False,
                },
                "data": {},
            },
        )()
    ) == (False, False)


def test_every_automatic_entry_point_uses_the_effective_configuration():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    names = {
        "handle_solar_curtailment_check",
        "handle_solar_curtailment_with_websocket_data",
        "websocket_sync_callback",
        "trigger_curtailment_check",
        "auto_curtailment_check",
        "_startup_curtailment_check",
    }
    for node in ast.walk(setup):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            if node.name == "websocket_sync_callback":
                assert "trigger_curtailment_check" in segment
            else:
                assert "_effective_solar_curtailment_enabled()" in segment

    sensor_source = SENSOR_PATH.read_text()
    assert "any(get_effective_solar_curtailment_configuration(entry))" in sensor_source
    assert "return any(get_effective_solar_curtailment_configuration(self._entry))" in sensor_source


def test_backend_feature_metadata_uses_the_effective_configuration():
    source = INIT_PATH.read_text()

    assert source.count(
        "solar_curtailment_enabled = any(\n"
        "                get_effective_solar_curtailment_configuration(entry)\n"
        "            )"
    ) == 3
    assert '"solar_curtailment": any(\n                    get_effective_solar_curtailment_configuration(entry)\n                )' in source


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


def _load_sigenergy_status_helper():
    tree = ast.parse(SENSOR_PATH.read_text())
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {"_foxess_curtailment_visible_status", "_sigenergy_curtailment_visible_status"}
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
    return namespace["_sigenergy_curtailment_visible_status"]


def _load_goodwe_description_helper():
    tree = ast.parse(SENSOR_PATH.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_goodwe_curtailment_description"
    )
    namespace: dict[str, Any] = {}
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    exec(compile(module, str(SENSOR_PATH), "exec"), namespace)
    return namespace[helper.name]


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


def _sigenergy_status(**overrides):
    now = datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)
    values = {
        "curtailment_enabled": True,
        "is_curtailed": True,
        "export_limit_kw": 0.0,
        "grid_power_kw": -0.03,
        "telemetry_ready": True,
        "last_update_success": True,
        "last_update": now - timedelta(seconds=15),
        "update_interval": timedelta(seconds=30),
        "now": now,
    }
    values.update(overrides)
    return _load_sigenergy_status_helper()(**values)


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


def test_goodwe_unsupported_status_does_not_claim_a_command_was_acknowledged():
    description = _load_goodwe_description_helper()
    assert description(
        visible_state="Pending",
        control_state="unsupported",
        force_dispatch_active=False,
    ) == (
        "Export limiting is not supported on this control profile; "
        "no curtailment command was sent"
    )
    assert description(
        visible_state="Pending",
        control_state="pending",
        force_dispatch_active=False,
    ) == "Curtailment command acknowledged, but physical zero-export is not confirmed"


def test_sigenergy_readback_confirms_effect_without_claiming_command_ownership():
    """#352: manual or pre-restart zero limit still has physical proof."""
    assert _sigenergy_status() == ("Active", 30.0, True)
    assert _sigenergy_status(is_curtailed=False) == ("Pending", None, False)
    assert _sigenergy_status(export_limit_kw=5.0) == ("Pending", None, False)
    assert _sigenergy_status(grid_power_kw=-0.3) == ("Pending", 300.0, False)
    assert _sigenergy_status(
        last_update=datetime(2026, 9, 13, 2, 50, tzinfo=timezone.utc)
    ) == ("Pending", None, False)


def test_sigenergy_status_uses_live_readback_and_listens_for_updates():
    source = SENSOR_PATH.read_text()
    assert "return self._sigenergy_status()[0]" in source
    assert 'coordinator_data.get("is_curtailed")' in source
    assert 'coordinator_data.get("export_limit_kw")' in source
    assert 'self._unsub_sigenergy = sigenergy_coordinator.async_add_listener(' in source
    assert '"control_owner": "curtailment" if owned_by_powersync else "external"' in source


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
