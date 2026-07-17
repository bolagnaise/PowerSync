"""Regression tests for custom Smart Optimization telemetry."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"
CONFIG_FLOW_PATH = ROOT / "custom_components" / "power_sync" / "config_flow.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"


def test_custom_battery_system_is_setup_stage_with_entities():
    const_source = CONST_PATH.read_text()
    config_flow_source = CONFIG_FLOW_PATH.read_text()
    init_source = INIT_PATH.read_text()

    assert 'BATTERY_SYSTEM_CUSTOM = "custom"' in const_source
    assert "BATTERY_SYSTEM_CUSTOM: \"Custom / external controller" in const_source
    assert 'CONF_CUSTOM_BATTERY_LEVEL_ENTITY = "custom_battery_level_entity"' in const_source
    assert 'CONF_CUSTOM_BATTERY_POWER_ENTITY = "custom_battery_power_entity"' in const_source
    assert 'CONF_CUSTOM_GRID_POWER_ENTITY = "custom_grid_power_entity"' in const_source
    assert 'CONF_CUSTOM_SOLAR_POWER_ENTITY = "custom_solar_power_entity"' in const_source
    assert 'CONF_CUSTOM_LOAD_POWER_ENTITY = "custom_load_power_entity"' in const_source
    assert "async def async_step_custom_battery(" in config_flow_source
    assert "self._selected_battery_system == BATTERY_SYSTEM_CUSTOM" in config_flow_source
    assert 'EntitySelector(EntitySelectorConfig(domain="sensor"))' in config_flow_source
    assert "CONF_MONITORING_MODE: True" in config_flow_source
    assert "battery_system = BATTERY_SYSTEM_CUSTOM" in init_source
    assert "energy_coordinator = None" in init_source


def test_custom_optimizer_telemetry_parser_reads_selected_entities():
    coordinator_source = COORDINATOR_PATH.read_text()

    assert "def _read_custom_energy_data(self) -> dict[str, Any] | None:" in coordinator_source
    assert "CUSTOM_BATTERY_LEVEL_ENTITY" in coordinator_source
    assert "CUSTOM_BATTERY_POWER_ENTITY" in coordinator_source
    assert "CUSTOM_GRID_POWER_ENTITY" in coordinator_source
    assert "CUSTOM_SOLAR_POWER_ENTITY" in coordinator_source
    assert "CUSTOM_LOAD_POWER_ENTITY" in coordinator_source
    assert "def _read_numeric_state(self, entity_id: str)" in coordinator_source
    assert "source_entities" in coordinator_source
    assert "def _get_energy_data(self) -> dict[str, Any] | None:" in coordinator_source
    assert "custom_data = self._read_custom_energy_data()" in coordinator_source
    assert "if self.battery_system == CUSTOM_BATTERY_SYSTEM:" in coordinator_source
    assert "from ..coordinator import normalize_custom_power_kw" in coordinator_source
    assert "return normalize_custom_power_kw(value, unit)" in coordinator_source
    assert "if not math.isfinite(value):" in coordinator_source


def test_custom_optimizer_telemetry_feeds_state_and_cost_paths():
    coordinator_source = COORDINATOR_PATH.read_text()

    assert "data = self._get_energy_data()\n        if data:\n            soc_value = data.get(\"battery_level\")" in coordinator_source
    assert "data = self._get_energy_data()\n        if data:\n            power = data.get(\"battery_power\", 0)" in coordinator_source
    assert "current_load_kw = float(data.get(\"load_power\"))" in coordinator_source
    assert "grid_power_kw = float(data.get(\"grid_power\", 0) or 0)" in coordinator_source
    assert "solar_power_kw = float(data.get(\"solar_power\", 0) or 0)" in coordinator_source
    assert "battery_power_kw = float(data.get(\"battery_power\", 0) or 0)" in coordinator_source
