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
SERVICES_PATH = ROOT / "custom_components" / "power_sync" / "services.yaml"


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


def test_disabled_optimizer_self_heals_stale_idle_reserve_for_supported_batteries():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    helper = _find_function(
        tree,
        "_restore_disabled_optimizer_reserve_if_stale",
    )
    helper_source = ast.get_source_segment(source, helper)
    setup = _find_function(tree, "async_setup_entry")
    setup_source = ast.get_source_segment(source, setup)

    assert helper_source is not None
    assert setup_source is not None
    assert '"tesla", "sigenergy", "goodwe", BATTERY_SYSTEM_CUSTOM' in helper_source
    assert "hasattr(battery_coordinator, \"set_backup_reserve\")" in helper_source
    assert "live_reserve <= target_reserve + 5" in helper_source
    assert "soc_near_live_reserve" in helper_source
    assert "grid_importing" in helper_source
    assert "battery_idle" in helper_source
    assert "restore_work_mode_from_idle" in helper_source
    assert "restore_normal" in helper_source
    assert "await battery_coordinator.set_backup_reserve(target_reserve)" in helper_source
    assert "not optimization_enabled" in setup_source
    assert "disabled_optimizer_cleanup_targets" in setup_source
    for battery_system in (
        "sungrow",
        "foxess",
        "solax",
        "fronius_reserva",
        "neovolt",
        "solaredge",
        "anker_solix",
    ):
        assert f'"{battery_system}"' in setup_source
    assert "_restore_disabled_optimizer_reserve_if_stale(" in setup_source


def test_sungrow_force_charge_timer_preserves_optimizer_charge_window():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "auto_restore_charge_sungrow")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert '_optimizer_current_force_action_matches("charge")' in function_source
    assert '_clear_force_timer_state_without_restore(' in function_source
    assert '"source": "force_timer"' in function_source


def test_sungrow_force_discharge_timer_preserves_optimizer_export_window():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "auto_restore_discharge_sungrow")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert '_optimizer_current_force_action_matches("discharge")' in function_source
    assert 'still wants discharge/export' in function_source
    assert '"source": "force_timer"' in function_source


def test_sungrow_force_discharge_failure_clears_visible_switch_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    sungrow_section = function_source.split("is_sungrow = bool(entry.data.get(CONF_SUNGROW_HOST))", 1)[1]
    sungrow_section = sungrow_section.split("# Check if this is a GoodWe system", 1)[0]
    failure_section = sungrow_section.split('else:\n                    force_discharge_state["active"] = False', 1)[1]
    assert 'force_discharge_state["expires_at"] = None' in failure_section
    assert 'force_discharge_state["hardware_expires_at"] = None' in failure_section
    assert 'async_dispatcher_send(hass, f"{DOMAIN}_force_discharge_state"' in failure_section
    assert "await persist_force_mode_state()" in failure_section


def test_optimizer_force_action_matcher_distinguishes_charge_and_export():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_optimizer_current_force_action_matches")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert '"effective_current_action"' in function_source
    assert '"planned_current_action"' in function_source
    assert 'getattr(opt_coordinator, "_get_current_action", None)' in function_source
    assert 'if force_type == "charge":' in function_source
    assert 'return "charge" in current_actions' in function_source
    assert 'if force_type == "discharge":' in function_source
    assert '("discharge", "export")' in function_source


def test_preserve_charge_backup_reserve_write_does_not_replace_user_reserve():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_backup_reserve")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert '"automation_preserve_charge"' in function_source
    assert '"hold_soc_restore"' in function_source


def test_tesla_hold_soc_backup_reserve_write_does_not_replace_user_reserve():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_hold_battery_soc")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'DOMAIN, SERVICE_SET_BACKUP_RESERVE' in function_source
    assert '{"percent": target_reserve, "source": "hold_soc"}' in function_source
    assert "_disabled_optimizer_backup_reserve_target(entry)" in function_source
    assert 'hold_soc_state["saved_backup_reserve"] = saved_backup_reserve' in function_source


def test_restore_normal_restores_user_reserve_after_tesla_hold_soc():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "restore_was_hold_soc = bool(hold_soc_state.get(\"active\"))" in function_source
    assert "def _saved_hold_soc_backup_reserve()" in function_source
    assert 'hold_soc_state.get("saved_backup_reserve")' in function_source
    assert "Restore normal: restoring Hold SoC backup reserve to user reserve" in function_source


def test_restore_normal_hold_soc_counts_as_restorable_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    has_saved_index = function_source.index("has_saved_state = (")
    guard_index = function_source.index(
        "if not force_restore and not has_active_force and not has_saved_state:"
    )
    hold_state_index = function_source.index(
        "(restore_was_hold_soc and _saved_hold_soc_backup_reserve() is not None)",
        has_saved_index,
    )

    assert has_saved_index < hold_state_index < guard_index


def test_restore_normal_hold_soc_uses_local_first_reserve_service():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    hold_only_index = function_source.index("hold_only_restore = (")
    site_configs_index = function_source.index("site_configs = _get_tesla_site_configs")
    service_call_index = function_source.index(
        "SERVICE_SET_BACKUP_RESERVE",
        hold_only_index,
    )
    persist_index = function_source.index(
        "await persist_force_mode_state()",
        service_call_index,
    )

    assert hold_only_index < service_call_index < persist_index < site_configs_index
    assert '"source": "hold_soc_restore"' in function_source[hold_only_index:site_configs_index]


def test_monitoring_mode_optimizer_shutdown_skips_hardware_restore():
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
    assert "skipping executor restore writes" in disable_source
    assert "restore_normal=not monitoring_mode or monitoring_enable_restore" in disable_source
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
    restore_guard = "if _monitoring_mode_should_block_control(call) and not monitoring_restore_allowed:"
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


def test_monitoring_mode_restores_persisted_force_without_replay_after_restart():
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
    assert "Persisted Sigenergy force %s will not be" in restore
    assert "Persisted force %s will not be replayed; restoring normal operation" in restore
    assert 'state["active"] = True' in restore
    assert '"_native_control": True' in restore
    assert '"_force_restore": True' in restore
    assert '"_allow_monitoring_restore": True' in restore
    assert "SERVICE_RESTORE_NORMAL" in restore
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


def test_force_handlers_clamp_explicit_power_to_optimizer_max():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    resolve = _find_function(tree, "_resolve_force_command_power_w")
    resolve_source = ast.get_source_segment(source, resolve)

    assert resolve_source is not None
    assert "explicit_power_w > configured_power_w" in resolve_source
    assert "clamping explicit power" in resolve_source
    assert "return configured_power_w" in resolve_source
    assert "return explicit_power_w" in resolve_source


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
    assert "monitoring restore cleanup must not re-enter TOU mode" in sync_source


def test_restore_normal_allows_monitoring_tou_sync_for_force_cleanup():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    restore = _find_function(tree, "handle_restore_normal")
    restore_source = ast.get_source_segment(source, restore)

    assert restore_source is not None
    assert "force_mode_cleanup_restore = restore_was_force_discharging or restore_was_force_charging" in restore_source
    assert "and (optimizer_owned_restore or force_mode_cleanup_restore)" in restore_source
    assert "restore normal is cleaning up an active force tariff" in restore_source
    assert restore_source.index("force_mode_cleanup_restore =") < restore_source.index(
        "if _monitoring_mode_should_block_control(call) and not monitoring_restore_allowed:"
    )


def test_sigenergy_restore_normal_uses_context_aware_native_control():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    owner_helper = _find_function(tree, "_powersync_optimization_control_active")
    owner_helper_source = ast.get_source_segment(source, owner_helper)
    helper = _find_function(tree, "_sigenergy_restore_native_control")
    helper_source = ast.get_source_segment(source, helper)
    restore = _find_function(tree, "handle_restore_normal")
    restore_source = ast.get_source_segment(source, restore)

    assert owner_helper_source is not None
    assert helper_source is not None
    assert restore_source is not None
    assert 'call.data.get("_native_control")' in helper_source
    assert "CONF_OPTIMIZATION_PROVIDER" in owner_helper_source
    assert "CONF_OPTIMIZATION_ENABLED" in owner_helper_source
    assert "OPT_PROVIDER_POWERSYNC" in owner_helper_source
    assert "return not _powersync_optimization_control_active()" in helper_source
    assert "if _is_monitoring_mode()" not in helper_source
    assert "sigenergy_native_control = _sigenergy_restore_native_control(call)" in restore_source
    assert "force_restore = bool(call.data.get(\"_force_restore\"))" in restore_source
    assert "monitoring_restore_allowed = allow_monitoring_restore or sigenergy_native_control or force_restore" in restore_source
    assert "native_control = sigenergy_native_control" in restore_source
    assert "native_control=native_control" in restore_source


def test_provider_config_monitoring_enable_forces_restore_normal():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "ProviderConfigView", "post")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert 'if "monitoring_mode" in data:' in method_source
    assert "SERVICE_RESTORE_NORMAL" in method_source
    assert 'restore_data = {"source": "manual", "_force_restore": True}' in method_source
    assert 'restore_data["_native_control"] = True' not in method_source


def test_restore_normal_force_restore_releases_tesla_even_without_saved_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    restore = _find_function(tree, "handle_restore_normal")
    restore_source = ast.get_source_segment(source, restore)

    assert restore_source is not None
    assert 'force_restore = bool(call.data.get("_force_restore"))' in restore_source
    assert "if not force_restore and not has_active_force and not has_saved_state:" in restore_source
    assert "if force_restore:" in restore_source
    assert "force restore requested without saved tariff" in restore_source
    assert "Force restore: leaving Tesla in self_consumption" in restore_source


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


def test_tesla_tou_upload_reports_accepted_before_readback_failure():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "send_tariff_to_tesla")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "accepted_status: dict[str, bool] | None = None" in function_source
    assert 'accepted_status["accepted"] = True' in function_source
    assert function_source.index('accepted_status["accepted"] = True') < function_source.index(
        "await _confirm_tesla_tariff_uploaded("
    )
    assert function_source.index("site_info did not confirm") < function_source.index("return False")


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
    restore_index = function_source.index('"grid charging restore"')
    assert skip_index < restore_index
    assert "set_grid_charging_enabled(True)" not in function_source
    assert "the next optimizer charge action will re-enable it if needed" in function_source


def test_tesla_force_modes_persist_grid_charging_baseline():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    persist = ast.get_source_segment(
        source,
        _find_function(tree, "persist_force_mode_state"),
    )
    restore = ast.get_source_segment(
        source,
        _find_function(tree, "handle_restore_normal"),
    )

    assert persist is not None
    assert restore is not None
    assert '"saved_grid_charging_enabled": force_charge_state.get("saved_grid_charging_enabled")' in persist
    assert '"saved_grid_charging_enabled": force_discharge_state.get("saved_grid_charging_enabled")' in persist
    assert "_tesla_grid_charging_enabled_from_site_info(site_info)" in source
    assert 'site_state["saved_grid_charging_enabled"] = saved_grid_charging_enabled' in source
    assert '"disallow_charge_from_grid_with_solar_installed": not target_grid_charging_enabled' in restore
    assert "No saved grid charging state" in restore


def test_tesla_self_consumption_clears_force_toggle_state():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_self_consumption")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'json={"default_real_mode": "self_consumption"}' in function_source
    assert 'pop("last_force_toggle_time", None)' in function_source
    assert 'pop("retoggle_attempted", None)' in function_source


def test_self_consumption_service_is_timed_and_persisted():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = ast.get_source_segment(
        source, _find_function(tree, "handle_set_self_consumption")
    )
    persist = ast.get_source_segment(
        source, _find_function(tree, "persist_force_mode_state")
    )
    restore = ast.get_source_segment(
        source, _find_function(tree, "restore_force_mode_from_persistence")
    )

    assert handler is not None
    assert persist is not None
    assert restore is not None
    assert 'raw_duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)' in handler
    assert '_cancel_all_force_timers("new self_consumption command")' in handler
    assert 'self_consumption_state["expires_at"] = (' in handler
    assert 'async_track_point_in_utc_time(' in handler
    assert 'await persist_force_mode_state()' in handler
    assert '"mode": "self_consumption"' in persist
    assert 'self_consumption_state["expires_at"].isoformat()' in persist
    assert 'if mode == "self_consumption":' in restore
    assert "auto_restore_self_consumption_persisted" in restore


def test_self_consumption_service_schema_exposes_duration():
    source = SERVICES_PATH.read_text()
    section = source.split("set_self_consumption:", 1)[1].split(
        "set_backup_reserve:", 1
    )[0]

    assert "duration:" in section
    assert "default: 30" in section
    assert 'value: "240"' in section


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

    assert dynamic_assignments == ['dynamic_providers = ("amber",)']


def test_flow_power_tariff_price_view_prefers_canonical_tariff_schedule():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "TariffPriceView", "get")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert 'if electricity_provider == "flow_power":' in method_source
    assert 'tariff_schedule = entry_data.get("tariff_schedule")' in method_source
    assert "get_current_price_from_tariff_schedule(tariff_schedule)" in method_source
    assert '"source": "flow_power_tariff_schedule"' in method_source


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
    assert "lambda guarded_w: neovolt_coord.force_discharge(" in function_source
    assert "await _guarded_force_discharge_write(" in function_source
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
    force_set_mode = _find_function(tree, "_tesla_force_set_operation_mode")
    force_discharge_source = ast.get_source_segment(source, force_discharge)
    force_charge_source = ast.get_source_segment(source, force_charge)
    force_set_mode_source = ast.get_source_segment(source, force_set_mode)

    assert force_discharge_source is not None
    assert force_charge_source is not None
    assert force_set_mode_source is not None
    assert 'if saved_mode != "autonomous":' not in force_discharge_source
    assert 'if saved_mode != "autonomous":' not in force_charge_source
    assert "_tesla_force_set_operation_mode(" in force_discharge_source
    assert "_tesla_force_set_operation_mode(" in force_charge_source
    assert '"autonomous"' in force_discharge_source
    assert '"autonomous"' in force_charge_source
    assert 'json={"default_real_mode": mode}' in force_set_mode_source
    assert "from .coordinator import _parse_retry_after" in force_set_mode_source
    assert "_tesla_force_confirm_operation_mode(" in force_set_mode_source
    assert "response.status in (429, 500, 502, 503, 504)" in force_set_mode_source


def test_set_operation_mode_verifies_readback_and_raises_for_automation_retries():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_operation_mode")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "transport.read_config(din)" in function_source
    assert "default_real_mode" in function_source
    assert "_confirm_mode" in function_source
    assert "_bounce_to_autonomous" in function_source
    assert "Tesla accepted the mode change but readback did not verify" in function_source
    assert "raise HomeAssistantError" in function_source


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
    assert "lambda guarded_w: solaredge_coord.force_discharge(" in force_discharge_source
    assert "await _guarded_force_discharge_write(" in force_discharge_source
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
    grid_export_method = _find_class_method(tree, "DualSungrowCoordinator", "force_grid_export")
    update_source = ast.get_source_segment(source, update_method)
    discharge_source = ast.get_source_segment(source, discharge_method)
    grid_export_source = ast.get_source_segment(source, grid_export_method)

    assert update_source is not None
    assert discharge_source is not None
    assert grid_export_source is not None
    assert 'discharge_limit_w = self._combined_power_limit_w("discharge")' in update_source
    assert '"battery_max_discharge_power_w": discharge_limit_w' in update_source
    assert 'max_split = self._max_split_kw("discharge")' in discharge_source
    assert "power_w / 1000.0) >= sum(max_split)" in discharge_source
    assert "p1, p2 = max_split" in discharge_source
    assert 'max_split = self._max_split_kw("discharge")' in grid_export_source
    assert "self._coord1.force_grid_export" in grid_export_source
    assert "export_limit_w=export_limit_w" in grid_export_source
    assert "self._coord2.force_discharge" in grid_export_source
    assert "power_w=p2 * 1000" in grid_export_source
    assert "await self.restore_normal()" in grid_export_source


def test_tesla_tariff_fetch_rejects_force_tariffs():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "fetch_tesla_tariff_schedule")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'site_info.get("tariff_content_v2") or site_info.get("tariff_content", {})' in function_source
    assert "if _is_powersync_force_tariff(tariff):" in function_source
    assert '"last_restorable_tesla_tariff"' in function_source


def test_tesla_tariff_startup_summary_always_converts_rates_to_cents():
    source = INIT_PATH.read_text()
    assert "rate * 100 if rate < 1 else rate" not in source
    assert "rate_cents = rate * 100" in source


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
    assert "hass.data.get(DOMAIN, {}).get(entry.entry_id, {})" in function_source
    assert '"optimizer_force_restart_restore_pending"' in function_source
    assert 'self_consumption_state.get("active")' in function_source
    assert '"type": "self_consumption"' in function_source
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


def test_tesla_force_discharge_always_applies_battery_export_before_tariff_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    export_rule_index = function_source.index('"customer_preferred_export_rule": "battery_ok"')
    grid_disable_index = function_source.index(
        '"disallow_charge_from_grid_with_solar_installed": True'
    )
    tariff_upload_index = function_source.index("send_tariff_to_tesla(")

    assert 'site_state.get("saved_export_rule") != "battery_ok"' not in function_source
    assert 'await update_cached_export_rule("battery_ok")' in function_source
    assert export_rule_index < grid_disable_index < tariff_upload_index


def test_tesla_restore_updates_cached_export_rule_after_saved_rule_restore():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    restore_log_index = function_source.index(
        "Restored export rule to %s for site %s"
    )
    cache_update_index = function_source.index(
        "await update_cached_export_rule(saved_export_rule)"
    )
    restore_failed_index = function_source.index(
        "export rule restore failed for site {site_id}"
    )

    assert restore_log_index < cache_update_index < restore_failed_index


def test_tesla_force_discharge_tariff_discourages_grid_import():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_create_discharge_tariff")

    rates = {
        node.targets[0].id: node.value.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"buy_rate_discharge", "sell_rate_discharge"}
        and isinstance(node.value, ast.Constant)
    }

    assert rates["buy_rate_discharge"] == 99.0
    assert rates["sell_rate_discharge"] == 99.0


def test_tesla_force_discharge_applies_backup_reserve_after_tariff_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    tariff_upload_index = function_source.index("send_tariff_to_tesla(")
    reserve_payload_index = function_source.index('json={"backup_reserve_percent": percent}')
    reserve_nudge_index = function_source.index('"force discharge apply nudge"')
    reserve_final_index = function_source.index('"force discharge final apply"')

    assert 'site_state["force_discharge_start_reserve"] = api_reserve' in function_source
    assert 'site_state.get("force_discharge_start_reserve") == 0' in function_source
    assert "await asyncio.sleep(3)" in function_source
    assert tariff_upload_index < reserve_payload_index
    assert reserve_nudge_index < reserve_final_index


def test_tesla_force_discharge_arms_cleanup_for_unconfirmed_accepted_upload():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_force_discharge")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "accepted_sites: list[str] = []" in function_source
    assert "unconfirmed_sites: list[str] = []" in function_source
    assert "accepted_status=upload_status" in function_source
    assert 'upload_status.get("accepted")' in function_source
    assert "FORCE DISCHARGE CLEANUP ARMED" in function_source
    assert "if all_success or accepted_sites:" in function_source
    assert '"_allow_monitoring_restore": True' in function_source
    assert function_source.index("FORCE DISCHARGE CLEANUP ARMED") < function_source.rindex(
        "async def auto_restore"
    )


def test_tesla_force_timers_ignore_callbacks_before_current_expiry():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    charge_callbacks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "auto_restore_charge"
    ]
    discharge_callbacks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "auto_restore"
    ]
    tesla_charge_source = ast.get_source_segment(
        source,
        max(charge_callbacks, key=lambda node: node.lineno),
    )
    tesla_discharge_source = ast.get_source_segment(
        source,
        max(discharge_callbacks, key=lambda node: node.lineno),
    )

    assert tesla_charge_source is not None
    assert tesla_discharge_source is not None
    assert 'current_expiry = force_charge_state.get("expires_at")' in tesla_charge_source
    assert "if current_expiry and _now < current_expiry:" in tesla_charge_source
    assert 'current_expiry = force_discharge_state.get("expires_at")' in tesla_discharge_source
    assert "if current_expiry and _now < current_expiry:" in tesla_discharge_source


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
    assert "_tesla_force_set_operation_mode(" in function_source


def test_tesla_restore_failure_keeps_force_state_for_retry():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    retry_helper_index = function_source.index("def _schedule_tesla_restore_retry")
    failure_guard_index = function_source.index("if tesla_restore_failed:")
    clear_index = function_source.index(
        '# Clear discharge state',
        failure_guard_index,
    )

    assert retry_helper_index < failure_guard_index < clear_index
    assert '_mark_tesla_restore_failed(f"operation mode restore failed for site {site_id}")' in function_source
    assert "backup reserve restore failed for site" in function_source
    assert "grid charging restore failed for site" in function_source
    assert '"_restore_retry": next_retry' in function_source
    assert "await persist_force_mode_state()" in function_source[failure_guard_index:clear_index]


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


def test_tesla_charge_kick_reenables_grid_charging_after_force_charge_bounce():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "_tesla_charge_kick")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'ensure_grid_charging = reason in {"force_charge", "backup_reserve_100"}' in function_source
    assert "async def _enable_grid_charging_after_bounce()" in function_source
    assert '"disallow_charge_from_grid_with_solar_installed": False' in function_source
    assert function_source.count("await _enable_grid_charging_after_bounce()") >= 2


def test_optimizer_backup_reserve_writes_do_not_persist_as_user_reserve():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_backup_reserve")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'reserve_source = call.data.get("source")' in function_source
    assert '"optimizer"' in function_source
    assert '"automation_preserve_charge"' in function_source
    assert '"hold_soc"' in function_source
    assert '"hold_soc_restore"' in function_source
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


def test_tesla_setting_writes_refresh_local_readback_and_invalidate_fleet_cache():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)

    # set_backup_reserve is untouched by PW-9/10/11 and still awaits the
    # refresh directly. set_operation_mode and set_grid_export fire-and-forget
    # it (PW-10/PW-11 fix) so it can never block manual-override / force-toggle
    # flag updates behind up to ~15s of Powerwall local readback I/O.
    function_source = ast.get_source_segment(
        source,
        _find_function(tree, "handle_set_backup_reserve"),
    )
    assert function_source is not None
    assert "_tesla_coord_for_cache.invalidate_site_info_cache()" in function_source
    assert 'await refresh_powerwall_local_after_settings_write("set_backup_reserve")' in function_source

    for function_name, refresh_label in (
        ("handle_set_operation_mode", "set_operation_mode"),
        ("handle_set_grid_export", "set_grid_export"),
    ):
        function_source = ast.get_source_segment(
            source,
            _find_function(tree, function_name),
        )

        assert function_source is not None
        assert "_tesla_coord_for_cache.invalidate_site_info_cache()" in function_source
        assert (
            f'hass.async_create_task(refresh_powerwall_local_after_settings_write("{refresh_label}"))'
            in function_source
        )


def test_tesla_grid_export_write_sets_manual_override_before_cache_persist():
    """PW-9/PW-10: the in-memory manual_export_override flags must be set
    before update_cached_export_rule is awaited, so a store exception or a
    concurrently-scheduled curtailment-cycle read can never see them unset
    after the Tesla write already succeeded."""
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_set_grid_export")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "await update_cached_export_rule(rule)" in function_source
    assert function_source.index("manual_export_override") < (
        function_source.index("await update_cached_export_rule(rule)")
    )


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


def test_foxess_direct_modbus_curtailment_uses_shared_modbus_session():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    curtail = _find_class_method(tree, "FoxESSEnergyCoordinator", "curtail")
    restore = _find_class_method(tree, "FoxESSEnergyCoordinator", "restore_curtailment")

    curtail_source = ast.get_source_segment(source, curtail)
    restore_source = ast.get_source_segment(source, restore)

    assert curtail_source is not None
    assert restore_source is not None
    assert "async with self._modbus_lock, self._controller:" in curtail_source
    assert "return await self._controller.curtail(home_load_w)" in curtail_source
    assert "async with self._modbus_lock, self._controller:" in restore_source
    assert "return await self._controller.restore()" in restore_source


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


def test_foxess_curtailment_restore_defers_during_force_remote_control():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = _find_function(tree, "handle_foxess_curtailment")
    handler_source = ast.get_source_segment(source, handler)
    force_discharge = _find_function(tree, "handle_force_discharge")
    force_discharge_source = ast.get_source_segment(source, force_discharge)
    force_charge = _find_function(tree, "handle_force_charge")
    force_charge_source = ast.get_source_segment(source, force_charge)

    assert handler_source is not None
    assert force_discharge_source is not None
    assert force_charge_source is not None
    assert 'force_charge_state.get("active") or force_discharge_state.get("active")' in handler_source
    assert "remote-control override remains owned by force mode" in handler_source
    assert 'entry_data["foxess_curtailment_state"] = "normal"' in force_discharge_source
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in force_discharge_source
    assert 'entry_data["foxess_curtailment_state"] = "normal"' in force_charge_source
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in force_charge_source


def test_foxess_curtailment_skips_apply_during_force_remote_control():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    handler = _find_function(tree, "handle_foxess_curtailment")
    handler_source = ast.get_source_segment(source, handler)

    assert handler_source is not None
    assert "def _foxess_force_dispatch_active()" in handler_source
    assert 'active_getter = getattr(entry_data.get("optimization_coordinator"), "get_active_force_state", None)' in handler_source
    assert '_optimizer_current_force_action_matches("charge")' in handler_source
    assert '_optimizer_current_force_action_matches("discharge")' in handler_source
    assert "if _foxess_force_dispatch_active():" in handler_source
    assert 'entry_data["foxess_curtailment_state"] = "normal"' in handler_source
    assert 'entry_data.pop("_last_foxess_curtailment_reapply", None)' in handler_source
    assert "FoxESS curtailment skipped while force dispatch is active" in handler_source
    assert "remote-control override remains owned by force mode" in handler_source


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

    assert '"charge_pv", power_w, fallback_option="charge_battery"' in charge_source
    assert '"sell_power", power_w, fallback_option="discharge_battery"' in discharge_source
    assert '"auto",' in restore_source
    assert "reset_power_limit=True" in restore_source
    assert "restore_operation_mode=True" in restore_source
    assert "restore_limit" in ems_source
    assert "GOODWE_EMS_MAX_W" in ems_source
    assert "rated_power_w = (self.data or {}).get(\"rated_power_w\")" in ems_source
    assert "restore_limit = int(float(rated_power_w))" in ems_source
    assert '"value": restore_limit' in ems_source
    assert ems_source.index("rated_power_w") < ems_source.index('state.attributes.get("max")')
    assert "select.{p}_inverter_operation_mode" in ems_restore_source
    assert "general_mode" in ems_restore_source
    assert '"options"' in attempts_source
    assert "fallback_option" in ems_source


def test_goodwe_entity_telemetry_uses_direct_polling_only_for_rated_power_probe():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    init = _find_class_method(tree, "GoodWeEnergyCoordinator", "__init__")
    update = _find_class_method(tree, "GoodWeEnergyCoordinator", "_async_update_data")
    probe = _find_class_method(
        tree,
        "GoodWeEnergyCoordinator",
        "_probe_entity_telemetry_rated_power",
    )

    init_source = ast.get_source_segment(source, init)
    update_source = ast.get_source_segment(source, update)
    probe_source = ast.get_source_segment(source, probe)

    assert init_source is not None
    assert update_source is not None
    assert probe_source is not None
    assert "GoodWeEntityTelemetryController" in init_source
    assert "entity_telemetry_prefix" in init_source
    assert "_entity_telemetry_rated_power_probe_attempted" in init_source
    entity_branch = update_source.split("if self._using_entity_telemetry:", 1)[1]
    entity_branch = entity_branch.split("else:", 1)[0]
    assert "self._telemetry_controller.connect()" in entity_branch
    assert "self._telemetry_controller.get_runtime_data()" in entity_branch
    assert "self._probe_entity_telemetry_rated_power()" in entity_branch
    assert "self._controller.connect()" not in entity_branch
    assert "self._controller.get_runtime_data()" not in entity_branch
    assert "self._controller.connect()" in probe_source
    assert "self._controller.get_runtime_data()" in probe_source
    assert "self._entity_telemetry_rated_power_probe_attempted = True" in probe_source
    assert "timeout=5.0" in probe_source


def test_amber_nem_region_map_accepts_sa_power_short_name():
    source = INIT_PATH.read_text()

    assert '"SA Power Networks": "SA1"' in source
    assert '"SA Power": "SA1"' in source


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


def test_sungrow_restore_normal_does_not_clear_state_on_failed_restore():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    branch = function_source.split(
        "# Check if this is a Sungrow system",
        1,
    )[1].split(
        "# Guard: if no force mode is active",
        1,
    )[0]

    restore_index = branch.index(
        "restore_result = await sungrow_coord.restore_normal()"
    )
    failure_index = branch.index("if not restore_result:")
    return_index = branch.index("return", failure_index)
    clear_index = branch.index('force_charge_state["active"] = False')

    assert restore_index < failure_index < return_index < clear_index
    assert '"Restore Normal Failed"' in branch
    assert '"Sungrow Modbus communication error"' in branch


def test_optimizer_retries_sungrow_restore_when_self_consumption_drift_detected():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    function = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_execute_optimizer_action",
    )
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert 'self.battery_system == "sungrow"' in function_source
    assert 'coord_data.get("ems_mode_name")' in function_source
    assert "charge_cmd_int in (0xAA, 0xBB)" in function_source
    assert "Sungrow still reports forced mode" in function_source
    assert "apply_self_consumption = True" in function_source


# ---------------------------------------------------------------------------
# OB-39 (remaining API-view sites): `AEMOSpikeView.post` (enabled + region
# branches) and `ProviderConfigView.post` (tariff-provider save) each set
# `_skip_reload = True` before `async_update_entry` with no comparison of
# old vs. new persisted state, so a no-op resubmit from the mobile app
# strands the flag and swallows the NEXT genuine structural reload (the
# same failure mode as OB-21/RSV-6, on the API-view axis). The fix nests
# the `_skip_reload` write one level deeper than the paired
# `async_update_entry` call, behind an `if <new> != <current>:` guard —
# assert that nesting relationship structurally so this doesn't regress.
# ---------------------------------------------------------------------------


def _build_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _if_nesting_depth(node: ast.AST, parents: dict[ast.AST, ast.AST], root: ast.AST) -> int:
    """Count `ast.If` ancestors between `node` and `root`. Using raw AST node
    depth would overcount an `async_update_entry(...)` call by one level (it
    sits inside its own `ast.Expr` statement wrapper) relative to a bare
    `entry_data["_skip_reload"] = True` assignment, which is itself already a
    statement — so compare `if`-nesting specifically instead."""
    depth = 0
    current = node
    while current is not root:
        parent = parents.get(current)
        if parent is None:
            break
        if isinstance(parent, ast.If):
            depth += 1
        current = parent
    return depth


def _skip_reload_vs_update_entry_depth_deltas(
    method: ast.AST, parents: dict[ast.AST, ast.AST]
) -> list[int]:
    """For each `_skip_reload` write, find the next `async_update_entry` call
    after it (by source line) and return how much *more* deeply nested the
    write is than that call. A positive delta means the write sits behind an
    extra `if` guard the call is not subject to (the no-op gate); zero means
    the write is an unconditional sibling of the call (the OB-39 bug)."""
    assigns = sorted(
        (n for n in ast.walk(method) if _writes_skip_reload(n)), key=lambda n: n.lineno
    )
    calls = sorted(
        (n for n in ast.walk(method) if _is_async_update_entry_call(n)), key=lambda n: n.lineno
    )
    deltas = []
    for assign in assigns:
        paired = next((c for c in calls if c.lineno > assign.lineno), None)
        assert paired is not None, f"no async_update_entry call follows skip_reload write at line {assign.lineno}"
        deltas.append(_if_nesting_depth(assign, parents, method) - _if_nesting_depth(paired, parents, method))
    return deltas


def test_aemo_spike_view_post_skip_reload_gated_on_persisted_change():
    tree = ast.parse(INIT_PATH.read_text())
    method = _find_class_method(tree, "AEMOSpikeView", "post")
    parents = _build_parents(tree)

    deltas = _skip_reload_vs_update_entry_depth_deltas(method, parents)

    assert len(deltas) == 2, "expected one _skip_reload write per branch (enabled, region)"
    assert all(delta > 0 for delta in deltas), (
        "AEMOSpikeView.post sets _skip_reload unconditionally — a no-op "
        "resubmit strands the flag and swallows the next genuine reload (OB-39)"
    )


def test_provider_config_view_post_skip_reload_gated_on_persisted_change():
    tree = ast.parse(INIT_PATH.read_text())
    method = _find_class_method(tree, "ProviderConfigView", "post")
    parents = _build_parents(tree)

    deltas = _skip_reload_vs_update_entry_depth_deltas(method, parents)

    assert len(deltas) == 1, "expected exactly one _skip_reload write in the tariff-provider save path"
    assert deltas[0] > 0, (
        "ProviderConfigView.post sets _skip_reload unconditionally — a no-op "
        "resubmit strands the flag and swallows the next genuine reload (OB-39)"
    )


def test_unclampable_tesla_export_paths_are_denied_by_network_envelope():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)

    force_discharge = ast.get_source_segment(
        source, _find_function(tree, "handle_force_discharge")
    )
    spike_entry = ast.get_source_segment(
        source, _find_class_method(tree, "AEMOSpikeManager", "_enter_spike_mode")
    )
    session_entry = ast.get_source_segment(
        source,
        _find_class_method(
            tree, "SavingSessionTariffManager", "_enter_session_mode"
        ),
    )

    assert force_discharge is not None
    assert spike_entry is not None
    assert session_entry is not None
    assert "Tesla tariff-driven force discharge blocked" in force_discharge
    for function_source in (spike_entry, session_entry):
        assert 'entry_runtime.get("network_export_guard")' in function_source
        assert "cannot be clamped to a watt-level headroom" in function_source
