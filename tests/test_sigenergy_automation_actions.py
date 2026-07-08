"""Regression tests for Sigenergy automation actions."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ACTIONS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "actions.py"


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


def test_sigenergy_force_discharge_action_routes_through_service_for_timer():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "controller.force_discharge(power_kw)" not in function_source
    assert "SERVICE_FORCE_DISCHARGE" in function_source
    assert (
        "service_data: Dict[str, Any] = {\"duration\": duration, \"source\": \"automation\"}"
        in function_source
    )
    assert "service_data[\"power_w\"] = int(power_w)" in function_source
    assert "restore_export_limit" not in function_source


def test_sigenergy_force_charge_action_routes_through_service_for_timer():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_force_charge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "controller.force_charge(power_kw)" not in function_source
    assert "SERVICE_FORCE_CHARGE" in function_source
    assert (
        "service_data: Dict[str, Any] = {\"duration\": duration, \"source\": \"automation\"}"
        in function_source
    )
    assert "service_data[\"power_w\"] = int(power_w)" in function_source


def test_sigenergy_restore_normal_action_routes_through_context_service():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "controller.restore_normal" not in function_source
    assert "_get_sigenergy_controller" not in function_source
    assert "SERVICE_RESTORE_NORMAL" in function_source
    assert '{"source": "automation"}' in function_source


def test_disable_optimizer_requests_sigenergy_native_handoff():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_disable_optimizer")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "SERVICE_RESTORE_NORMAL" in function_source
    assert '{"source": "automation", "_native_control": True}' in function_source


def test_enable_optimizer_without_coordinator_allows_reload():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_enable_optimizer")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'entry_data["_skip_reload"] = True' not in function_source
    assert (
        "new_options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC"
        in function_source
    )
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = True" in function_source
    assert "reload required" in function_source


def test_disable_optimizer_without_coordinator_persists_native_provider_option():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_action_disable_optimizer")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "new_data[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_NATIVE" in function_source
    assert (
        "new_options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_NATIVE"
        in function_source
    )
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = False" in function_source


def test_sigenergy_evdc_native_solar_skips_remote_ems_in_native_control():
    source = ACTIONS_PATH.read_text()
    tree = ast.parse(source)
    helper = _find_function(tree, "_sigenergy_native_control_active")
    helper_source = ast.get_source_segment(source, helper)
    function = _find_function(tree, "_dynamic_ev_update_sigenergy_evdc_native_solar")
    function_source = ast.get_source_segment(source, function)

    assert helper_source is not None
    assert function_source is not None
    assert "CONF_MONITORING_MODE" in helper_source
    assert "CONF_OPTIMIZATION_PROVIDER" in helper_source
    assert "CONF_OPTIMIZATION_ENABLED" in helper_source
    assert "OPT_PROVIDER_POWERSYNC" in helper_source
    assert "if _sigenergy_native_control_active(config_entry):" in function_source
    assert 'state["native_solar_mode_skipped"] = "native_control"' in function_source
    assert function_source.index("if _sigenergy_native_control_active(config_entry):") < function_source.index(
        "controller.set_self_consumption_mode()"
    )
