"""Regression tests for force-mode control persistence."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
SELECT_PATH = ROOT / "custom_components" / "power_sync" / "select.py"


def _find_class_method(
    tree: ast.AST,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _find_function(
    tree: ast.AST,
    function_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _is_async_update_entry_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_update_entry"
    )


def _writes_skip_reload(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    for target in node.targets:
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "_skip_reload"
        ):
            return True
    return False


def test_force_duration_select_updates_options_without_reload():
    tree = ast.parse(SELECT_PATH.read_text())
    method = _find_class_method(tree, "PowerSyncDurationSelect", "async_select_option")

    skip_reload_lines = [
        node.lineno
        for node in ast.walk(method)
        if _writes_skip_reload(node)
    ]
    update_entry_lines = [
        node.lineno
        for node in ast.walk(method)
        if _is_async_update_entry_call(node)
    ]

    assert skip_reload_lines
    assert update_entry_lines
    assert min(skip_reload_lines) < min(update_entry_lines)


def test_force_mode_persistence_uses_setup_store_reference():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "persist_force_mode_state")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'hass.data[DOMAIN][entry.entry_id]["store"]' not in function_source
    assert "await store.async_load()" in function_source


def test_force_mode_persistence_keeps_requested_power_setpoint():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    persist = _find_function(tree, "persist_force_mode_state")
    restore = _find_function(tree, "restore_force_mode_from_persistence")
    persist_source = ast.get_source_segment(source, persist)
    restore_source = ast.get_source_segment(source, restore)

    assert persist_source is not None
    assert restore_source is not None
    assert '"duration": force_discharge_state.get("duration")' in persist_source
    assert '"power_w": _coerce_force_power_w(force_discharge_state.get("power_w", 0))' in persist_source
    assert '"duration": force_charge_state.get("duration")' in persist_source
    assert '"power_w": _coerce_force_power_w(force_charge_state.get("power_w", 0))' in persist_source
    assert "persisted_power_w = _coerce_force_power_w" in restore_source
    assert 'service_data["power_w"] = persisted_power_w' in restore_source
    assert "SERVICE_FORCE_DISCHARGE" in restore_source
    assert "SERVICE_FORCE_CHARGE" in restore_source


def test_force_handlers_capture_power_before_persisting_restart_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    discharge = _find_function(tree, "handle_force_discharge")
    charge = _find_function(tree, "handle_force_charge")
    discharge_source = ast.get_source_segment(source, discharge)
    charge_source = ast.get_source_segment(source, charge)

    assert discharge_source is not None
    assert charge_source is not None
    assert 'command_power_w = _coerce_force_power_w(call.data.get("power_w", 0))' in discharge_source
    assert 'force_discharge_state["power_w"] = command_power_w' in discharge_source
    assert 'command_power_w = _coerce_force_power_w(call.data.get("power_w", 0))' in charge_source
    assert 'force_charge_state["power_w"] = command_power_w' in charge_source


def test_force_tariff_filter_matches_names_and_codes():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_is_powersync_force_tariff")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "force charge" in source
    assert "force discharge" in source
    assert "_FORCE_TARIFF_CODE_PREFIXES" in function_source
    assert "_iter_tariff_strings" in function_source


def test_restore_normal_filters_force_tariffs_before_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "saved_tariff = _select_restorable_tesla_tariff" in function_source
    assert "site_tariff = _select_restorable_tesla_tariff" in function_source
    assert "send_tariff_to_tesla" in function_source


def test_aemo_vpp_restore_uses_saved_tariff_not_dynamic_sync():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    dynamic_assignments = [
        ast.get_source_segment(source, node)
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "dynamic_providers"
            for target in node.targets
        )
    ]
    assert dynamic_assignments == ['dynamic_providers = ("amber", "flow_power")']
    assert 'if electricity_provider in ("globird", "aemo_vpp"):' in function_source


def test_aemo_vpp_tariff_price_view_uses_tariff_schedule_path():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "TariffPriceView", "get")

    dynamic_assignments = [
        ast.get_source_segment(source, node)
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "dynamic_providers"
            for target in node.targets
        )
    ]

    assert dynamic_assignments == ['dynamic_providers = ("amber", "flow_power")']


def test_powerwall_settings_view_rejects_neovolt_systems():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "PowerwallSettingsView", "get")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "is_neovolt_pw = bool(_get_neovolt_entry_ids(entry.data, self._hass))" in method_source
    assert 'if is_neovolt_pw:' in method_source
    assert '"reason": "neovolt_not_supported"' in method_source
    assert '"battery_system": BATTERY_SYSTEM_NEOVOLT' in method_source


def test_neovolt_force_discharge_hardware_extension_preserves_restore_modes():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'neovolt_coord = entry_data.get("neovolt_coordinator")' in function_source
    assert "await neovolt_coord.force_discharge(" in function_source
    assert "preserve_restore_modes=True" in function_source


def test_neovolt_energy_coordinator_passes_force_discharge_restore_mode_flag():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "NeovoltEnergyCoordinator", "force_discharge")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert any(arg.arg == "preserve_restore_modes" for arg in method.args.kwonlyargs)
    assert "preserve_restore_modes=preserve_restore_modes" in method_source


def test_dual_sungrow_discharge_max_uses_each_inverter_limit():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    update_method = _find_class_method(tree, "DualSungrowCoordinator", "_async_update_data")
    discharge_method = _find_class_method(tree, "DualSungrowCoordinator", "force_discharge")
    update_source = ast.get_source_segment(source, update_method)
    discharge_source = ast.get_source_segment(source, discharge_method)

    assert update_source is not None
    assert discharge_source is not None
    assert 'discharge_limit_w = self._combined_power_limit_w("discharge")' in update_source
    assert '"battery_max_discharge_power_w": discharge_limit_w' in update_source
    assert 'max_split = self._max_split_kw("discharge")' in discharge_source
    assert "power_w / 1000.0) >= sum(max_split)" in discharge_source
    assert "p1, p2 = max_split" in discharge_source


def test_tesla_tariff_fetch_rejects_force_tariffs():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "fetch_tesla_tariff_schedule")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "if _is_powersync_force_tariff(tariff):" in function_source
    assert '"last_restorable_tesla_tariff"' in function_source


def test_optimizer_force_modes_are_not_reissued_after_restart():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "restore_force_mode_from_persistence")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'persisted_source = persisted_force_state.get("source", "user")' in function_source
    assert 'if persisted_source == "optimizer":' in function_source

    optimizer_branch = function_source.split(
        'if persisted_source == "optimizer":',
        1,
    )[1].split("if now >= expires_at:", 1)[0]
    assert "SERVICE_RESTORE_NORMAL" in optimizer_branch
    assert '"set_self_consumption"' in optimizer_branch
    assert 'stored_data["force_mode_state"] = None' in optimizer_branch
    assert "SERVICE_FORCE_DISCHARGE" not in optimizer_branch
    assert "SERVICE_FORCE_CHARGE" not in optimizer_branch


def test_foxess_force_charge_accepts_optimizer_min_timeout():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "FoxESSEnergyCoordinator", "force_charge")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    arg_names = [arg.arg for arg in method.args.args]
    assert "min_timeout_seconds" in arg_names
    assert "min_timeout_seconds: int = 600" in method_source
    assert "min_timeout_seconds=min_timeout_seconds" in method_source


def test_goodwe_entity_mode_prefers_solar_first_charge_and_export_discharge_modes():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)

    charge = _find_class_method(tree, "GoodWeEnergyCoordinator", "force_charge")
    discharge = _find_class_method(tree, "GoodWeEnergyCoordinator", "force_discharge")
    ems_set_mode = _find_class_method(tree, "GoodWeEnergyCoordinator", "_ems_set_mode")
    mode_attempts = _find_class_method(tree, "GoodWeEnergyCoordinator", "_goodwe_ems_mode_attempts")

    charge_source = ast.get_source_segment(source, charge)
    discharge_source = ast.get_source_segment(source, discharge)
    ems_source = ast.get_source_segment(source, ems_set_mode)
    attempts_source = ast.get_source_segment(source, mode_attempts)

    assert charge_source is not None
    assert discharge_source is not None
    assert ems_source is not None
    assert attempts_source is not None

    assert '"charge_battery", power_w, fallback_option="buy_power"' in charge_source
    assert '"sell_power", power_w, fallback_option="discharge_battery"' in discharge_source
    assert '"options"' in attempts_source
    assert "fallback_option" in ems_source


def _saj_force_charge_branch() -> str:
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_charge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    return function_source.split(
        "is_saj_h2_local = bool(entry.data.get(CONF_SAJ_CONFIG_ENTRY_ID))",
        1,
    )[1].split("is_neovolt_local = bool(entry.data.get(CONF_NEOVOLT_CONFIG_ENTRY_ID))", 1)[0]


def test_saj_force_charge_success_keeps_force_state_contract():
    branch = _saj_force_charge_branch()

    assert "charge_result = await saj_coord.force_charge(duration, power_w=power_w)" in branch
    assert 'force_charge_state["active"] = True' in branch
    assert 'force_charge_state["source"] = source' in branch
    assert 'force_charge_state["duration"] = duration' in branch
    assert 'force_charge_state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)' in branch
    assert 'async_dispatcher_send(hass, f"{DOMAIN}_force_charge_state"' in branch
    assert "await persist_force_mode_state()" in branch


def test_saj_force_charge_false_result_clears_state_and_notifies():
    branch = _saj_force_charge_branch()
    failure_branch = branch.split(
        'else:\n                    force_charge_state["active"] = False\n                    _LOGGER.error("SAJ H2 force charge failed")',
        1,
    )[1].split("return", 1)[0]

    assert '_notify_api_error(hass, "Force Charge Failed", "SAJ H2 entity write error")' in failure_branch
