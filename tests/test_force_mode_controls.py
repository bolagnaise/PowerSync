"""Regression tests for force-mode control persistence."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
OPTIMIZATION_COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)
OPTIMIZATION_EXECUTOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "executor.py"
)
OPTIMIZATION_BATTERY_CONTROLLER_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "battery_controller.py"
)
SELECT_PATH = ROOT / "custom_components" / "power_sync" / "select.py"
NUMBER_PATH = ROOT / "custom_components" / "power_sync" / "number.py"
SWITCH_PATH = ROOT / "custom_components" / "power_sync" / "switch.py"
FOXESS_INVERTER_PATH = ROOT / "custom_components" / "power_sync" / "inverters" / "foxess.py"


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


def test_monitoring_mode_optimizer_shutdown_releases_active_control():
    coordinator_source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    coordinator_tree = ast.parse(coordinator_source)
    monitoring_helper = _find_class_method(
        coordinator_tree,
        "OptimizationCoordinator",
        "_monitoring_mode_active",
    )
    monitoring_helper_source = ast.get_source_segment(
        coordinator_source,
        monitoring_helper,
    )
    disable = _find_class_method(coordinator_tree, "OptimizationCoordinator", "disable")
    disable_source = ast.get_source_segment(coordinator_source, disable)

    executor_source = OPTIMIZATION_EXECUTOR_PATH.read_text()
    executor_tree = ast.parse(executor_source)
    stop = _find_class_method(executor_tree, "ScheduleExecutor", "stop")
    stop_source = ast.get_source_segment(executor_source, stop)

    assert disable_source is not None
    assert stop_source is not None
    assert monitoring_helper_source is not None
    assert "CONF_MONITORING_MODE" in monitoring_helper_source
    battery_source = OPTIMIZATION_BATTERY_CONTROLLER_PATH.read_text()
    battery_tree = ast.parse(battery_source)
    wrapper_restore = _find_class_method(
        battery_tree,
        "BatteryControllerWrapper",
        "restore_normal",
    )
    wrapper_restore_source = ast.get_source_segment(
        battery_source,
        wrapper_restore,
    )

    assert "monitoring_mode = self._monitoring_mode_active()" in disable_source
    assert 'if not monitoring_mode and self._last_executed_action == "idle":' in disable_source
    assert "skipping IDLE cleanup writes" in disable_source
    assert "skipping scheduled EV no-discharge release" in disable_source
    assert "before handing off to monitoring mode" in disable_source
    assert "await self._executor.stop(restore_normal=True)" in disable_source
    assert "restore_normal: bool = True" in stop_source
    assert "if restore_normal:" in stop_source
    assert "await self._restore_normal_operation()" in stop_source
    assert wrapper_restore_source is not None
    assert '"source": "optimizer"' in wrapper_restore_source
    assert '"_allow_monitoring_restore": True' in wrapper_restore_source


def test_monitoring_mode_blocks_automation_but_allows_manual_controls():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)

    source_helper = ast.get_source_segment(
        source, _find_function(tree, "_control_call_source")
    )
    assert source_helper is not None
    assert 'call.data.get("source", "")' in source_helper
    assert 'getattr(call.context, "user_id", None)' in source_helper
    assert 'return "unknown"' in source_helper

    helper = ast.get_source_segment(
        source, _find_function(tree, "_monitoring_mode_should_block_control")
    )
    assert helper is not None
    assert "_control_call_source(call)" in helper
    assert "user" in helper
    assert "manual" in helper

    guarded_functions = {
        "handle_force_discharge": "extend_hardware = call.data.get",
        "handle_force_charge": "extend_hardware = call.data.get",
        "handle_hold_battery_soc": "for coord_key, brand in",
        "handle_set_self_consumption": 'self_consumption_state["active"] = True',
        "handle_set_autonomous": "is_foxess = bool",
        "handle_set_backup_reserve": "is_sigenergy = bool",
        "handle_set_operation_mode": "dispatch_powerwall_write",
        "handle_set_grid_export": 'entry_data.get("alphaess_coordinator")',
        "handle_set_grid_charging": "_get_tesla_site_configs",
        "handle_set_storm_watch": '_get_tesla_coordinator_for_service("set_storm_watch")',
        "handle_set_off_grid_ev_reserve": '_get_tesla_coordinator_for_service("set_off_grid_ev_reserve")',
        "handle_set_vpp_enrollment": '_get_tesla_coordinator_for_service("set_vpp_enrollment")',
    }
    for function_name, later_marker in guarded_functions.items():
        function_source = ast.get_source_segment(
            source, _find_function(tree, function_name)
        )
        assert function_source is not None
        guard = "if _monitoring_mode_should_block_control(call):"
        assert guard in function_source
        assert function_source.index(guard) < function_source.index(later_marker)

    restore = ast.get_source_segment(
        source, _find_function(tree, "handle_restore_normal")
    )
    assert restore is not None
    restore_guard = "if _monitoring_mode_should_block_control(call) and not allow_monitoring_restore:"
    assert restore_guard in restore
    assert restore.index(restore_guard) < restore.index(
        '_cancel_all_force_timers("restore_normal")'
    )
    assert restore.index(restore_guard) < restore.index(
        'entry_data.get("goodwe_coordinator")'
    )

    number_source = NUMBER_PATH.read_text()
    select_source = SELECT_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()
    automation_source = (
        ROOT / "custom_components" / "power_sync" / "automations" / "actions.py"
    ).read_text()

    assert '{"percent": int(value), "source": "user"}' in number_source
    assert '{"mode": option, "source": "user"}' in select_source
    assert '{"rule": option, "source": "user"}' in select_source
    assert '{"enabled": True, "source": "user"}' in switch_source
    assert '{"enabled": False, "source": "user"}' in switch_source

    assert '{"percent": reserve_percent, "source": "automation"}' in automation_source
    assert '{"mode": mode, "source": "automation"}' in automation_source
    assert '{"rule": rule, "source": "automation"}' in automation_source
    assert '{"enabled": enabled, "source": "automation"}' in automation_source
    assert '{"duration": duration, "source": "automation"}' in automation_source


def test_monitoring_mode_blocks_persisted_force_replay_after_restart():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    restore = ast.get_source_segment(
        source, _find_function(tree, "restore_force_mode_from_persistence")
    )

    assert restore is not None
    assert "if _is_monitoring_mode():" in restore
    assert restore.index("if _is_monitoring_mode():") < restore.index(
        'if persisted_source == "optimizer":'
    )
    assert restore.index("if _is_monitoring_mode():") < restore.index(
        "SERVICE_FORCE_CHARGE"
    )
    assert restore.index("if _is_monitoring_mode():") < restore.index(
        "SERVICE_FORCE_DISCHARGE"
    )
    assert 'stored_data["force_mode_state"] = None' in restore


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
    assert 'command_power_w = _resolve_force_command_power_w(' in discharge_source
    assert '"discharge",' in discharge_source
    assert 'force_discharge_state["power_w"] = command_power_w' in discharge_source
    assert 'command_power_w = _resolve_force_command_power_w(' in charge_source
    assert '"charge",' in charge_source
    assert 'force_charge_state["power_w"] = command_power_w' in charge_source


def test_force_handlers_use_optimizer_power_when_force_power_is_unset():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    resolve = _find_function(tree, "_resolve_force_command_power_w")
    discharge = _find_function(tree, "handle_force_discharge")
    charge = _find_function(tree, "handle_force_charge")
    resolve_source = ast.get_source_segment(source, resolve)
    discharge_source = ast.get_source_segment(source, discharge)
    charge_source = ast.get_source_segment(source, charge)

    assert resolve_source is not None
    assert discharge_source is not None
    assert charge_source is not None
    assert "CONF_OPTIMIZATION_MAX_CHARGE_W" in source
    assert "CONF_OPTIMIZATION_MAX_DISCHARGE_W" in source
    assert "explicit_power_w > 0" in resolve_source
    assert "configured_power_w = _configured_force_power_w(direction)" in resolve_source
    assert "using optimizer max" in resolve_source
    assert 'power_w = command_power_w' in discharge_source
    assert 'power_w = command_power_w' in charge_source
    assert 'power_w = call.data.get("power_w", 0)' not in discharge_source
    assert 'power_w = call.data.get("power_w", 0)' not in charge_source


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


def test_restore_normal_suppresses_tesla_force_toggle_during_dynamic_sync():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    restore = _find_function(tree, "handle_restore_normal")
    sync = _find_function(tree, "_handle_sync_tou_internal")
    restore_source = ast.get_source_segment(source, restore)
    sync_source = ast.get_source_segment(source, sync)

    assert restore_source is not None
    assert sync_source is not None
    assert '"_suppress_force_mode_toggle_once"' in restore_source
    assert "allow_monitoring_restore" in restore_source
    assert '"_allow_monitoring_restore"' in restore_source
    assert '"_allow_monitoring_tou_sync_once"' in restore_source
    assert "restore_was_force_discharging" in restore_source
    assert 'force_discharge_state["active"] = False' in restore_source
    assert "SERVICE_SYNC_TOU" in restore_source
    assert '"_suppress_force_mode_toggle_once"' in sync_source
    assert '"_allow_monitoring_tou_sync_once"' in sync_source
    assert "Allowing one Tesla TOU sync during restore cleanup" in sync_source
    assert "Skipping force mode toggle" in sync_source


def test_restore_normal_treats_octopus_as_dynamic_sync_provider():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    restore = _find_function(tree, "handle_restore_normal")
    restore_source = ast.get_source_segment(source, restore)

    assert restore_source is not None
    assert 'dynamic_providers = ("amber", "flow_power", "octopus")' in restore_source


def test_tesla_tou_upload_waits_for_site_info_readback():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "send_tariff_to_tesla")
    confirm = _find_function(tree, "_confirm_tesla_tariff_uploaded")
    matcher = _find_function(tree, "_tesla_tariff_matches_readback")
    function_source = ast.get_source_segment(source, function)
    confirm_source = ast.get_source_segment(source, confirm)
    matcher_source = ast.get_source_segment(source, matcher)

    assert function_source is not None
    assert confirm_source is not None
    assert matcher_source is not None
    assert "confirm_readback: bool = True" in function_source
    assert "await _confirm_tesla_tariff_uploaded(" in function_source
    assert "site_info" in confirm_source
    assert "tariff_content_v2" in confirm_source
    assert "_tesla_tariff_matches_readback(tariff_data, observed)" in confirm_source
    assert "_tariff_charge_rates(expected, sell=False)" in matcher_source


def test_optimizer_restore_keeps_tesla_self_consumption_during_handoff():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "optimizer_owned_restore" in function_source
    assert 'restore_mode = "self_consumption"' in function_source
    assert "instead of restoring saved mode" in function_source


def test_optimizer_restore_does_not_reenable_grid_charging_during_handoff():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    skip_index = function_source.index("if optimizer_owned_restore:")
    reenable_index = function_source.index("set_grid_charging_enabled(True)")
    assert skip_index < reenable_index
    assert "the next optimizer charge action will re-enable it if needed" in function_source


def test_tesla_self_consumption_clears_force_toggle_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_self_consumption")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'json={"default_real_mode": "self_consumption"}' in function_source
    assert 'pop("last_force_toggle_time", None)' in function_source
    assert 'pop("retoggle_attempted", None)' in function_source


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
    assert dynamic_assignments == ['dynamic_providers = ("amber", "flow_power", "octopus")']
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


def test_saved_tariff_prices_calculate_period_before_rate_lookup():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "TariffPriceView", "_calculate_prices_from_saved_tariff")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    period_index = method_source.index("current_period = find_matching_tou_period(")
    buy_lookup_index = method_source.index("buy_rate = buy_rates.get(current_period")
    assert period_index < buy_lookup_index


def test_tesla_force_modes_always_reissue_autonomous_mode():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    force_discharge = _find_function(tree, "handle_force_discharge")
    force_charge = _find_function(tree, "handle_force_charge")
    force_discharge_source = ast.get_source_segment(source, force_discharge)
    force_charge_source = ast.get_source_segment(source, force_charge)

    assert force_discharge_source is not None
    assert force_charge_source is not None
    assert 'if saved_mode != "autonomous":' not in force_discharge_source
    assert 'if saved_mode != "autonomous":' not in force_charge_source
    assert force_discharge_source.count('json={"default_real_mode": "autonomous"}') >= 1
    assert force_charge_source.count('json={"default_real_mode": "autonomous"}') >= 1


def test_neovolt_energy_coordinator_passes_force_discharge_restore_mode_flag():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "NeovoltEnergyCoordinator", "force_discharge")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert any(arg.arg == "preserve_restore_modes" for arg in method.args.kwonlyargs)
    assert "preserve_restore_modes=preserve_restore_modes" in method_source


def test_solaredge_dispatch_is_routed_through_services_and_coordinator():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    force_discharge = _find_function(tree, "handle_force_discharge")
    force_charge = _find_function(tree, "handle_force_charge")
    restore = _find_function(tree, "handle_restore_normal")
    reserve = _find_function(tree, "handle_set_backup_reserve")
    hold = _find_function(tree, "handle_hold_battery_soc")

    force_discharge_source = ast.get_source_segment(source, force_discharge)
    force_charge_source = ast.get_source_segment(source, force_charge)
    restore_source = ast.get_source_segment(source, restore)
    reserve_source = ast.get_source_segment(source, reserve)
    hold_source = ast.get_source_segment(source, hold)

    assert force_discharge_source is not None
    assert force_charge_source is not None
    assert restore_source is not None
    assert reserve_source is not None
    assert hold_source is not None

    assert 'solaredge_coord = entry_data.get("solaredge_coordinator")' in force_discharge_source
    assert "await solaredge_coord.force_discharge(duration, power_w=power_w)" in force_discharge_source
    assert "await solaredge_coord.force_charge(duration, power_w=power_w)" in force_charge_source
    assert "await solaredge_coord.restore_normal()" in restore_source
    assert "await solaredge_coord.set_backup_reserve(percent)" in reserve_source
    assert '("solaredge_coordinator", "solaredge")' in hold_source


def test_solaredge_optimizer_wrapper_no_longer_blocks_dispatch():
    source = (ROOT / "custom_components" / "power_sync" / "optimization" / "battery_controller.py").read_text()
    tree = ast.parse(source)

    for method_name in (
        "force_charge",
        "force_discharge",
        "restore_normal",
        "set_self_consumption_mode",
        "set_autonomous_mode",
        "set_backup_reserve",
    ):
        method = _find_class_method(tree, "BatteryControllerWrapper", method_name)
        method_source = ast.get_source_segment(source, method)
        assert method_source is not None
        assert 'self.battery_system == "solaredge"' not in method_source


def test_solaredge_energy_coordinator_exposes_control_surface():
    tree = ast.parse(COORDINATOR_PATH.read_text())
    expected_methods = {
        "force_charge",
        "force_discharge",
        "restore_normal",
        "set_backup_mode",
        "restore_work_mode_from_idle",
        "set_backup_reserve",
        "get_backup_reserve",
        "set_operation_mode",
    }

    for method_name in expected_methods:
        _find_class_method(tree, "SolarEdgeEnergyCoordinator", method_name)


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
    assert '"set_self_consumption"' not in optimizer_branch
    assert 'stored_data["force_mode_state"] = None' in optimizer_branch
    assert '"optimizer_force_restart_restore_pending"] = False' in optimizer_branch
    assert "SERVICE_FORCE_DISCHARGE" not in optimizer_branch
    assert "SERVICE_FORCE_CHARGE" not in optimizer_branch


def test_optimizer_restart_restore_is_hidden_from_force_getter():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "get_force_state")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert '"optimizer_force_restart_restore_pending"' in function_source
    assert 'return {"active": False}' in function_source


def test_optimizer_startup_ignores_stale_force_restore_window():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "OptimizationCoordinator", "_deferred_enable_restore")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "_restart_restore_pending" in method_source
    assert '"optimizer_force_restart_restore_pending"' in method_source
    assert "not _restart_restore_pending" in method_source
    assert "stale force restore pending" in method_source


def test_optimizer_waits_for_restart_force_restore_before_solving():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    run_method = _find_class_method(tree, "OptimizationCoordinator", "_run_optimization")
    wait_method = _find_class_method(tree, "OptimizationCoordinator", "_wait_for_restart_force_restore")
    run_source = ast.get_source_segment(source, run_method)
    wait_source = ast.get_source_segment(source, wait_method)

    assert run_source is not None
    assert wait_source is not None
    assert "if await self._wait_for_restart_force_restore():" in run_source
    assert "optimizer_force_restart_restore_pending" in wait_source
    assert "await asyncio.sleep(1)" in wait_source
    assert "return True" in wait_source


def test_tou_sync_does_not_skip_optimizer_owned_force_modes():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_handle_sync_tou_internal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "=== Starting TOU sync ===" in function_source
    assert "Optimizer force %s active" not in function_source
    assert 'opt_force_state.get("source") == "optimizer"' not in function_source


def test_price_update_skips_optimizer_owned_force_reoptimization():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "OptimizationCoordinator", "_on_price_update")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "force_state = self._get_active_force_state()" in method_source
    assert 'force_state.get("source") == "optimizer"' in method_source
    assert "skipping LP re-optimization" in method_source
    assert "self.hass.async_create_background_task" in method_source


def test_optimization_coordinator_exposes_optimizer_force_state():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "get_active_force_state",
    )
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "return self._get_active_force_state()" in method_source


def test_tesla_force_discharge_disables_grid_charging_before_tariff_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    grid_disable_index = function_source.index(
        '"disallow_charge_from_grid_with_solar_installed": True'
    )
    tariff_upload_index = function_source.index("send_tariff_to_tesla(")
    assert grid_disable_index < tariff_upload_index


def test_restore_normal_does_not_clear_newer_force_command():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    generation_index = function_source.index("_restore_generation = _command_generation[0]")
    helper_index = function_source.index("def _restore_superseded")
    clear_index = function_source.index('force_discharge_state["active"] = False')
    dispatch_index = function_source.index(
        f'async_dispatcher_send(hass, f"{{DOMAIN}}_force_discharge_state"'
    )

    assert generation_index < helper_index < clear_index < dispatch_index
    assert '_restore_superseded("initial mode handoff")' in function_source
    assert '_restore_superseded("tariff restore")' in function_source
    assert '_restore_superseded("mode/reserve restore")' in function_source


def test_tesla_force_charge_enables_grid_charging_before_tariff_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_charge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    grid_enable_index = function_source.index(
        '"disallow_charge_from_grid_with_solar_installed": False'
    )
    tariff_upload_index = function_source.index("send_tariff_to_tesla(")
    assert grid_enable_index < tariff_upload_index


def test_optimizer_backup_reserve_writes_do_not_persist_as_user_reserve():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_backup_reserve")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'reserve_source = call.data.get("source")' in function_source
    assert 'optimizer_write = reserve_source == "optimizer" or optimizer_is_idle' in function_source
    assert "if not optimizer_write:" in function_source
    persistence_branch = function_source.split("if not optimizer_write:", 1)[1]
    assert '"_user_backup_reserve": percent' in persistence_branch


def test_tesla_local_backup_reserve_write_uses_hidden_reserve_offset():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_backup_reserve")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "detect_local_backup_reserve_offset" in function_source
    assert '"powerwall_local_low_soe_reserve_pct"' in function_source
    assert "local_backup_reserve_write_percent" in function_source
    assert "local_percent = local_backup_reserve_write_percent(" in function_source
    assert '"site_info.backup_reserve_percent": local_percent' in function_source
    assert 'json={"backup_reserve_percent": percent}' in function_source


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


def test_foxess_cloud_coordinator_exposes_modbus_control_surface():
    tree = ast.parse(COORDINATOR_PATH.read_text())
    expected_methods = {
        "_async_update_data",
        "force_charge",
        "force_discharge",
        "restore_normal",
        "set_backup_mode",
        "restore_work_mode_from_idle",
        "set_backup_reserve",
        "set_work_mode",
        "set_charge_rate_limit",
        "set_discharge_rate_limit",
        "curtail",
        "restore_curtailment",
    }

    for method_name in expected_methods:
        _find_class_method(tree, "FoxESSCloudEnergyCoordinator", method_name)


def test_foxess_entity_coordinator_exposes_modbus_control_surface():
    tree = ast.parse(COORDINATOR_PATH.read_text())
    expected_methods = {
        "_async_update_data",
        "force_charge",
        "force_discharge",
        "restore_normal",
        "set_backup_mode",
        "restore_work_mode_from_idle",
        "set_backup_reserve",
        "set_work_mode",
        "set_operation_mode",
        "set_charge_rate_limit",
        "set_discharge_rate_limit",
        "curtail",
        "restore_curtailment",
    }

    for method_name in expected_methods:
        _find_class_method(tree, "FoxESSEntityEnergyCoordinator", method_name)


def test_foxess_cloud_force_modes_snapshot_and_restore_scheduler():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    force_charge = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "force_charge")
    force_discharge = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "force_discharge")
    restore = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "restore_normal")

    force_charge_source = ast.get_source_segment(source, force_charge)
    force_discharge_source = ast.get_source_segment(source, force_discharge)
    restore_source = ast.get_source_segment(source, restore)

    assert force_charge_source is not None
    assert force_discharge_source is not None
    assert restore_source is not None
    assert "_save_current_scheduler()" in force_charge_source
    assert "_client.force_charge" in force_charge_source
    assert "_save_current_scheduler()" in force_discharge_source
    assert "_client.force_discharge" in force_discharge_source
    assert "_restore_stored_scheduler()" in restore_source
    assert 'set_work_mode("SelfUse")' in restore_source


def test_foxess_cloud_realtime_maps_battery_power_across_model_variables():
    """KH/K-series report invBatPower / batChargePower / batDischargePower rather
    than batPower; the cloud coordinator must read all of them so battery and grid
    power populate instead of staying at zero."""
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    update = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "_async_update_data")
    update_source = ast.get_source_segment(source, update)

    assert update_source is not None
    # Battery power falls back across the per-model variable names.
    assert '"invBatPower"' in update_source
    assert '"batChargePower"' in update_source
    assert '"batDischargePower"' in update_source
    assert "discharge_kw - charge_kw" in update_source
    # Grid power prefers the meter reading before net import/export.
    assert '"meterPower"' in update_source


def test_foxess_cloud_curtailment_uses_export_active_power_and_scheduler_limits():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    curtail = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "curtail")
    restore = _find_class_method(tree, "FoxESSCloudEnergyCoordinator", "restore_curtailment")

    curtail_source = ast.get_source_segment(source, curtail)
    restore_source = ast.get_source_segment(source, restore)

    assert curtail_source is not None
    assert restore_source is not None
    assert '"ExportLimit"' in curtail_source
    assert '"ExportLimitPower"' in curtail_source
    assert '"ActivePowerLimit"' in curtail_source
    assert '"exportLimit": limit' in curtail_source
    assert '"pvLimit": limit' in curtail_source
    assert "set_scheduler_v3" in curtail_source
    assert "_restore_stored_scheduler()" in restore_source
    assert '"ActivePowerLimit"' in restore_source
    assert '"ExportLimit"' in restore_source


def test_foxess_direct_curtailment_uses_verified_grid_remote_control():
    source = FOXESS_INVERTER_PATH.read_text()
    tree = ast.parse(source)
    curtail = _find_class_method(tree, "FoxESSController", "curtail")
    curtail_source = ast.get_source_segment(source, curtail)

    assert curtail_source is not None
    assert "_write_remote_control(" in curtail_source
    assert "REMOTE_CONTROL_GRID" in curtail_source
    assert 'label="curtailment"' in curtail_source
    assert "_write_holding_register(reg.remote_enable, 1)" not in curtail_source


def test_foxess_dc_curtailment_reapplies_before_remote_timeout():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = _find_function(tree, "handle_foxess_curtailment")
    handler_source = ast.get_source_segment(source, handler)

    assert handler_source is not None
    assert "_last_foxess_curtailment_reapply" in handler_source
    assert "_foxess_reapply_interval = 480" in handler_source
    assert 'current_state != "curtailed" or _needs_reapply' in handler_source
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in handler_source


def test_foxess_dc_curtailment_reapplies_when_live_export_continues():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = _find_function(tree, "handle_foxess_curtailment")
    handler_source = ast.get_source_segment(source, handler)

    assert handler_source is not None
    assert "_live_export_reapply = False" in handler_source
    assert 'coord_data = getattr(fc, "data", None) or {}' in handler_source
    assert 'grid_power_kw = float(coord_data.get("grid_power", 0) or 0)' in handler_source
    assert 'current_state == "curtailed" and grid_export_w > 250' in handler_source
    assert ") or _live_export_reapply" in handler_source


def test_sigenergy_curtailment_reapplies_when_live_export_continues():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = _find_function(tree, "handle_sigenergy_curtailment")
    handler_source = ast.get_source_segment(source, handler)

    assert handler_source is not None
    assert "_last_sigenergy_curtailment_reapply" in handler_source
    assert "_live_export_reapply = False" in handler_source
    assert 'coord_data = getattr(sig_coord, "data", None) or {}' in handler_source
    assert 'grid_power_kw = float(coord_data.get("grid_power", 0) or 0)' in handler_source
    assert 'current_state == "curtailed" and grid_export_w > 250' in handler_source
    assert ") or _live_export_reapply" in handler_source
    assert 'entry_data.pop("_last_sigenergy_curtailment_reapply", None)' in handler_source


def test_foxess_optimizer_self_consumption_preserves_active_curtailment():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_self_consumption")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'source == "optimizer"' in function_source
    assert 'entry_data.get("foxess_curtailment_state") == "curtailed"' in function_source
    assert "leaving remote-control curtailment in place" in function_source
    assert 'entry_data["foxess_curtailment_state"] = "normal"' in function_source
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in function_source


def test_foxess_restore_normal_clears_curtailment_cache():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    foxess_branch = function_source.split("# Check if this is a FoxESS system", 1)[1]
    foxess_branch = foxess_branch.split("# Check if this is a GoodWe system", 1)[0]
    assert 'entry_data["foxess_curtailment_state"] = "normal"' in foxess_branch
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in foxess_branch


def test_goodwe_entity_mode_prefers_solar_first_charge_and_export_discharge_modes():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)

    charge = _find_class_method(tree, "GoodWeEnergyCoordinator", "force_charge")
    discharge = _find_class_method(tree, "GoodWeEnergyCoordinator", "force_discharge")
    restore = _find_class_method(tree, "GoodWeEnergyCoordinator", "restore_normal")
    ems_set_mode = _find_class_method(tree, "GoodWeEnergyCoordinator", "_ems_set_mode")
    ems_restore_operation = _find_class_method(
        tree, "GoodWeEnergyCoordinator", "_ems_restore_operation_mode"
    )
    mode_attempts = _find_class_method(tree, "GoodWeEnergyCoordinator", "_goodwe_ems_mode_attempts")

    charge_source = ast.get_source_segment(source, charge)
    discharge_source = ast.get_source_segment(source, discharge)
    restore_source = ast.get_source_segment(source, restore)
    ems_source = ast.get_source_segment(source, ems_set_mode)
    ems_restore_source = ast.get_source_segment(source, ems_restore_operation)
    attempts_source = ast.get_source_segment(source, mode_attempts)

    assert charge_source is not None
    assert discharge_source is not None
    assert restore_source is not None
    assert ems_source is not None
    assert ems_restore_source is not None
    assert attempts_source is not None

    assert '"charge_battery", power_w, fallback_option="buy_power"' in charge_source
    assert '"sell_power", power_w, fallback_option="discharge_battery"' in discharge_source
    assert '"auto",' in restore_source
    assert "reset_power_limit=True" in restore_source
    assert "restore_operation_mode=True" in restore_source
    assert '"value": 0' in ems_source
    assert "select.{p}_inverter_operation_mode" in ems_restore_source
    assert "general_mode" in ems_restore_source
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
