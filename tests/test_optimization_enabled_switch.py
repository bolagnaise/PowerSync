"""Regression tests for the Smart Optimization configuration switch."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
SETTINGS_METADATA_PATH = ROOT / "custom_components" / "power_sync" / "settings_metadata.py"
SWITCH_PATH = ROOT / "custom_components" / "power_sync" / "switch.py"


def test_optimization_enabled_switch_is_registered_as_config_entity():
    const_source = CONST_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()

    assert 'SWITCH_TYPE_OPTIMIZATION_ENABLED = "optimization_enabled"' in const_source
    assert "OptimizationEnabledSwitch(" in switch_source
    assert "key=SWITCH_TYPE_OPTIMIZATION_ENABLED" in switch_source
    assert 'name="Enable Smart Optimization"' in switch_source
    assert "class OptimizationEnabledSwitch(SwitchEntity):" in switch_source
    assert "_attr_entity_category = EntityCategory.CONFIG" in switch_source


def test_auto_apply_optimizer_reserve_switch_is_registered_as_config_entity():
    const_source = CONST_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()

    assert (
        'SWITCH_TYPE_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"'
        in const_source
    )
    assert (
        'CONF_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"'
        in const_source
    )
    assert 'CONF_OPTIMIZATION_MANUAL_RESERVE = "optimization_manual_reserve"' in const_source
    assert "AutoApplyOptimizerReserveSwitch(" in switch_source
    assert "key=SWITCH_TYPE_OPTIMIZATION_AUTO_APPLY_RESERVE" in switch_source
    assert 'name="Auto-Apply Optimizer Reserve"' in switch_source
    assert "class AutoApplyOptimizerReserveSwitch(SwitchEntity):" in switch_source
    assert "set_auto_apply_reserve_enabled(True)" in switch_source
    assert "set_auto_apply_reserve_enabled(False)" in switch_source
    assert "CONF_OPTIMIZATION_MANUAL_RESERVE" in switch_source


def test_optimization_enabled_switch_persists_provider_and_enabled_flag():
    switch_source = SWITCH_PATH.read_text()

    assert "new_data[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC" in switch_source
    assert "new_options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC" in switch_source
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = True" in switch_source
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = False" in switch_source


def test_spread_export_switch_is_registered_and_capability_gated():
    const_source = CONST_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()

    assert 'CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED = "optimization_spread_export_enabled"' in const_source
    assert 'CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED = "optimization_spread_import_enabled"' in const_source
    assert 'SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT = "optimization_spread_export"' in const_source
    assert 'SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT = "optimization_spread_import"' in const_source
    assert "TARGET_EXPORT_POWER_BATTERY_SYSTEMS = {" in const_source
    assert "TARGET_CHARGE_POWER_BATTERY_SYSTEMS = {" in const_source
    assert "if battery_system in TARGET_EXPORT_POWER_BATTERY_SYSTEMS:" in switch_source
    assert "if battery_system in TARGET_CHARGE_POWER_BATTERY_SYSTEMS:" in switch_source
    assert "class SpreadExportSwitch(SwitchEntity):" in switch_source
    assert "class SpreadImportSwitch(SwitchEntity):" in switch_source
    assert "_attr_entity_category = EntityCategory.CONFIG" in switch_source
    assert "set_spread_export_enabled(True)" in switch_source
    assert "set_spread_export_enabled(False)" in switch_source
    assert "set_spread_import_enabled(True)" in switch_source
    assert "set_spread_import_enabled(False)" in switch_source
    assert "await _reoptimize_if_enabled(self._coordinator, changed)" in switch_source


def test_disable_idle_switch_is_registered_for_supported_providers():
    const_source = CONST_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()
    init_source = INIT_PATH.read_text()

    assert 'CONF_OPTIMIZATION_DISABLE_IDLE = "optimization_disable_idle"' in const_source
    assert 'SWITCH_TYPE_OPTIMIZATION_DISABLE_IDLE = "optimization_disable_idle"' in const_source
    assert "NO_IDLE_MODE_PROVIDERS = frozenset({" in const_source
    for provider in ("flow_power", "globird", "aemo_vpp", "other", "tou_only", "nz"):
        assert f'"{provider}"' in const_source
    assert "def supports_no_idle_mode_provider(provider: str | None) -> bool:" in const_source
    assert "supports_no_idle_mode_provider(electricity_provider)" in switch_source
    assert 'hass.data[DOMAIN][entry.entry_id]["switch_add_disable_idle"]' in switch_source
    assert "DisableIdleModeSwitch(" in switch_source
    assert "class DisableIdleModeSwitch(SwitchEntity):" in switch_source
    assert 'self._attr_name = "No Idle Mode"' in switch_source
    assert "set_disable_idle_enabled(True)" in switch_source
    assert "set_disable_idle_enabled(False)" in switch_source
    assert "def set_disable_idle_enabled(self, enabled: bool) -> bool:" in coordinator_source
    assert "supports_no_idle_mode_provider(self._provider_key())" in coordinator_source
    assert '"disable_idle_enabled": self.disable_idle_enabled' in coordinator_source
    assert '"disable_idle_enabled": opt_coordinator.disable_idle_enabled' in init_source
    assert "CONF_OPTIMIZATION_DISABLE_IDLE" in init_source
    assert "supports_no_idle_mode_provider(electricity_provider)" in init_source
    assert "await _reoptimize_if_enabled(self._coordinator, changed)" in switch_source


def test_spread_export_setting_is_exposed_through_api_and_coordinator():
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert '"spread_export_enabled": opt_coordinator._config.spread_export_enabled' in init_source
    assert '"spread_import_enabled": opt_coordinator._config.spread_import_enabled' in init_source
    assert 'new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = bool(settings["spread_export_enabled"])' in init_source
    assert 'new_options[CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED] = bool(settings["spread_import_enabled"])' in init_source
    assert '"spread_export_enabled": self._config.spread_export_enabled' in coordinator_source
    assert '"spread_import_enabled": self._config.spread_import_enabled' in coordinator_source
    assert "def set_spread_export_enabled(self, enabled: bool) -> bool:" in coordinator_source
    assert "def set_spread_import_enabled(self, enabled: bool) -> bool:" in coordinator_source


def test_optimizer_mode_switches_reoptimize_after_change():
    switch_source = SWITCH_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert "async def _reoptimize_if_enabled(coordinator: Any, changed: bool) -> None:" in switch_source
    assert 'and bool(getattr(coordinator, "enabled", False))' in switch_source
    assert "await coordinator.force_reoptimize()" in switch_source
    assert "changed = self._coordinator.set_profit_max_mode(True)" in switch_source
    assert "changed = self._coordinator.set_profit_max_mode(False)" in switch_source
    assert "changed = self._coordinator.set_charge_by_time_enabled(True)" in switch_source
    assert "changed = self._coordinator.set_charge_by_time_enabled(False)" in switch_source
    assert "changed = self._coordinator.set_disable_idle_enabled(True)" in switch_source
    assert "changed = self._coordinator.set_disable_idle_enabled(False)" in switch_source
    assert "changed = self._coordinator.set_spread_export_enabled(True)" in switch_source
    assert "changed = self._coordinator.set_spread_export_enabled(False)" in switch_source
    assert "changed = self._coordinator.set_spread_import_enabled(True)" in switch_source
    assert "changed = self._coordinator.set_spread_import_enabled(False)" in switch_source
    assert "def set_profit_max_mode(self, enabled: bool) -> bool:" in coordinator_source
    assert "def set_charge_by_time_enabled(self, enabled: bool) -> bool:" in coordinator_source


def test_charge_by_time_switch_is_registered_as_config_entity():
    const_source = CONST_PATH.read_text()
    switch_source = SWITCH_PATH.read_text()
    init_source = INIT_PATH.read_text()

    assert 'CONF_CHARGE_BY_TIME_ENABLED = "charge_by_time_enabled"' in const_source
    assert 'CONF_CHARGE_BY_TIME_TARGET_TIME = "charge_by_time_target_time"' in const_source
    assert 'CONF_CHARGE_BY_TIME_TARGET_SOC = "charge_by_time_target_soc"' in const_source
    assert 'SWITCH_TYPE_CHARGE_BY_TIME = "charge_by_time"' in const_source
    assert "ChargeByTimeSwitch(" in switch_source
    assert "class ChargeByTimeSwitch(SwitchEntity):" in switch_source
    assert 'self._attr_name = "Charge By Time"' in switch_source
    assert 'hass.data[DOMAIN][entry.entry_id]["switch_add_charge_by_time"]' in switch_source
    assert "switch_add_charge_by_time" in init_source


def test_charge_by_time_config_migration_preserves_legacy_profit_max_targets():
    init_source = INIT_PATH.read_text()
    config_flow_source = (ROOT / "custom_components" / "power_sync" / "config_flow.py").read_text()

    assert "VERSION = 8" in config_flow_source
    assert "if config_entry.version == 6:" in init_source
    assert "CONF_CHARGE_BY_TIME_ENABLED" in init_source
    assert "_read_legacy(CONF_PROFIT_MAX_ENABLED, False)" in init_source
    assert "CONF_CHARGE_BY_TIME_TARGET_TIME" in init_source
    assert "CONF_CHARGE_BY_TIME_TARGET_SOC" in init_source
    assert "version=7" in init_source


def test_auto_apply_reserve_setting_is_exposed_through_api_and_coordinator():
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert '"auto_apply_reserve_enabled": opt_coordinator.auto_apply_reserve_enabled' in init_source
    assert '"manual_backup_reserve": (' in init_source
    assert 'if "auto_apply_reserve_enabled" in settings:' in init_source
    assert 'CONF_OPTIMIZATION_MANUAL_RESERVE' in init_source
    assert '"auto_apply_reserve_enabled": self.auto_apply_reserve_enabled' in coordinator_source
    assert '"manual_backup_reserve": self.manual_backup_reserve' in coordinator_source
    assert "async def set_auto_apply_reserve_enabled(" in coordinator_source
    assert "rerun: bool = True" in coordinator_source
    assert "self._schedule_settings_reoptimization()" in coordinator_source
    assert "def _apply_auto_reserve_recommendation(" in coordinator_source


def test_auto_apply_reserve_recommendation_uses_manual_reference_floor():
    coordinator_source = COORDINATOR_PATH.read_text()

    assert "reference_reserve_floor = (" in coordinator_source
    assert 'getattr(self, "_manual_backup_reserve", None)' in coordinator_source
    assert "solve_reserve_override = (" in coordinator_source
    assert (
        "result: OptimizerResult = await _run_optimizer_once(\n"
        "                solve_reserve_override"
    ) in coordinator_source
    assert "used_reference_override = solve_reserve_override is not None" in coordinator_source
    assert "if reserve_changed or used_reference_override:" in coordinator_source
    assert "result = await _run_optimizer_once()" in coordinator_source
    assert "result = self._optimizer.reconcile_result_with_schedule(" in coordinator_source
    assert "self._set_active_export_reserve_floor_slots(None, None)" in coordinator_source


def test_max_grid_import_setting_is_exposed_through_api_and_coordinator():
    const_source = CONST_PATH.read_text()
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()
    metadata_source = SETTINGS_METADATA_PATH.read_text()

    assert 'CONF_OPTIMIZATION_MAX_GRID_IMPORT_W = "optimization_max_grid_import_w"' in const_source
    assert 'CONF_OPTIMIZATION_MAX_GRID_EXPORT_W = "optimization_max_grid_export_w"' in const_source
    assert 'CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE = "optimization_max_grid_charge_price"' in const_source
    assert 'CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP = "optimization_grid_charge_soc_cap"' in const_source
    assert '"max_grid_import_w": opt_coordinator._config.max_grid_import_w' in init_source
    assert '"max_grid_export_w": opt_coordinator._config.max_grid_export_w' in init_source
    assert '"max_grid_charge_price": (' in init_source
    assert '"grid_charge_soc_cap": max(' in init_source
    assert '"settings_groups": _optimizer_settings_groups()' in init_source
    assert '"max_grid_import_w": self._config.max_grid_import_w' in coordinator_source
    assert '"max_grid_export_w": self._config.max_grid_export_w' in coordinator_source
    assert '"max_grid_charge_price": (' in coordinator_source
    assert '"grid_charge_soc_cap": int(' in coordinator_source
    assert '"max_grid_import_w": {"category": "system"' in metadata_source
    assert '"max_grid_export_w": {"category": "system"' in metadata_source
    assert '"max_grid_charge_price": {' in metadata_source
    assert '"grid_charge_soc_cap": {' in metadata_source
    assert '"settings_schema": optimizer_settings_schema()' in init_source
    assert '"settings_schema": optimizer_settings_schema()' in coordinator_source
    assert "CONF_OPTIMIZATION_MAX_GRID_IMPORT_W" in init_source
    assert "CONF_OPTIMIZATION_MAX_GRID_EXPORT_W" in init_source
    assert "CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE" in init_source
    assert "CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP" in init_source
    assert "CONF_OPTIMIZATION_MAX_GRID_IMPORT_W" in coordinator_source
    assert "CONF_OPTIMIZATION_MAX_GRID_EXPORT_W" in coordinator_source
