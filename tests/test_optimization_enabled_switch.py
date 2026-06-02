"""Regression tests for the Smart Optimization configuration switch."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
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


def test_spread_export_setting_is_exposed_through_api_and_coordinator():
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert '"spread_export_enabled": opt_coordinator._config.spread_export_enabled' in init_source
    assert '"spread_import_enabled": opt_coordinator._config.spread_import_enabled' in init_source
    assert 'new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = bool(settings["spread_export_enabled"])' in init_source
    assert 'new_options[CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED] = bool(settings["spread_import_enabled"])' in init_source
    assert '"spread_export_enabled": self._config.spread_export_enabled' in coordinator_source
    assert '"spread_import_enabled": self._config.spread_import_enabled' in coordinator_source
    assert "def set_spread_export_enabled(self, enabled: bool) -> None:" in coordinator_source
    assert "def set_spread_import_enabled(self, enabled: bool) -> None:" in coordinator_source


def test_auto_apply_reserve_setting_is_exposed_through_api_and_coordinator():
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert '"auto_apply_reserve_enabled": opt_coordinator.auto_apply_reserve_enabled' in init_source
    assert '"manual_backup_reserve": (' in init_source
    assert 'if "auto_apply_reserve_enabled" in settings:' in init_source
    assert 'CONF_OPTIMIZATION_MANUAL_RESERVE' in init_source
    assert '"auto_apply_reserve_enabled": self.auto_apply_reserve_enabled' in coordinator_source
    assert '"manual_backup_reserve": self.manual_backup_reserve' in coordinator_source
    assert "async def set_auto_apply_reserve_enabled(self, enabled: bool) -> None:" in coordinator_source
    assert "def _apply_auto_reserve_recommendation(" in coordinator_source


def test_auto_apply_reserve_recommendation_uses_manual_baseline_floor():
    coordinator_source = COORDINATOR_PATH.read_text()

    assert "def _auto_reserve_baseline_floor() -> float | None:" in coordinator_source
    assert 'getattr(self, "_manual_backup_reserve", None)' in coordinator_source
    assert "recommendation_floor = _auto_reserve_baseline_floor()" in coordinator_source
    assert (
        "result: OptimizerResult = await _run_optimizer_once(\n"
        "                recommendation_floor"
    ) in coordinator_source
    assert "used_recommendation_floor = recommendation_floor is not None" in coordinator_source
    assert "if reserve_changed or used_recommendation_floor:" in coordinator_source
    assert "result.reserve_recommendation = reserve_recommendation" in coordinator_source


def test_max_grid_import_setting_is_exposed_through_api_and_coordinator():
    const_source = CONST_PATH.read_text()
    init_source = INIT_PATH.read_text()
    coordinator_source = COORDINATOR_PATH.read_text()

    assert 'CONF_OPTIMIZATION_MAX_GRID_IMPORT_W = "optimization_max_grid_import_w"' in const_source
    assert '"max_grid_import_w": opt_coordinator._config.max_grid_import_w' in init_source
    assert '"max_grid_import_w": self._config.max_grid_import_w' in coordinator_source
    assert '"max_grid_import_w",' in coordinator_source
    assert "CONF_OPTIMIZATION_MAX_GRID_IMPORT_W" in init_source
    assert "CONF_OPTIMIZATION_MAX_GRID_IMPORT_W" in coordinator_source
