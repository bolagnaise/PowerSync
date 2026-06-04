"""Regression tests for provider pricing/account sensor integration."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def test_provider_pricing_device_helper_links_to_entry_device():
    """Provider pricing devices should be linked under the PowerSync entry."""
    source = (COMPONENT_ROOT / "const.py").read_text()

    assert "def provider_pricing_device_info(entry_id: str, provider: str) -> dict:" in source
    assert '"name": name' in source
    assert '"model": "Electricity Pricing"' in source
    assert '"via_device": (DOMAIN, entry_id)' in source
    assert 'f"{entry_id}_{provider_key}_pricing"' in source
    assert 'name = "GloBird Pricing"' in source
    assert 'name = "Flow Power Pricing"' in source


def test_globird_setup_creates_coordinator_and_gated_sensors():
    """GloBird portal sensors should only be added when credentials are configured."""
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    sensor_source = (COMPONENT_ROOT / "sensor.py").read_text()

    assert 'if electricity_provider == "globird":' in init_source
    assert "CONF_GLOBIRD_EMAIL" in init_source
    assert "CONF_GLOBIRD_PASSWORD" in init_source
    assert "GloBirdCoordinator(hass, entry)" in init_source
    assert "async_config_entry_first_refresh()" in init_source
    assert '"globird_coordinator": globird_coordinator' in init_source
    assert "await globird_coordinator.async_shutdown()" in init_source

    assert 'if electricity_provider == "globird" and globird_coordinator:' in sensor_source
    assert "build_globird_entities(globird_coordinator, entry)" in sensor_source


def test_globird_sensors_use_linked_device_and_stable_object_ids():
    """Ported GloBird sensors should use PowerSync-native IDs and device info."""
    source = (COMPONENT_ROOT / "globird_sensors.py").read_text()

    assert "def build_globird_entities(" in source
    assert "GloBirdLatestDayCostSensor" in source
    assert 'sensor_key = "latest_day_cost"' in source
    assert "provider_pricing_device_info(" in source
    assert "SENSOR_FAMILY_GLOBIRD" in source
    assert 'return "_".join(["power_sync", SENSOR_FAMILY_GLOBIRD, *safe_parts])' in source
    assert "usage_attributes(" in source
    assert "cost_attributes(" in source


def test_flow_power_portal_sensors_use_provider_device_and_object_ids():
    """Flow Power portal/account sensors should live under the pricing device."""
    source = (COMPONENT_ROOT / "sensor.py").read_text()
    const_source = (COMPONENT_ROOT / "const.py").read_text()

    assert "FLOW_POWER_PORTAL_SENSORS = [" in const_source
    assert '"fp_account_pea"' in const_source
    assert '"fp_account_lwap"' in const_source
    assert '"fp_account_avg_usage"' in const_source
    assert '"fp_account_max_usage"' in const_source
    assert "return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)" in source
    assert 'self._attr_suggested_object_id = f"power_sync_{sensor_type}"' in source
