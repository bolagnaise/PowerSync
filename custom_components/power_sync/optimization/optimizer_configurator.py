"""
Optimizer Configurator for PowerSync.

Auto-configures external optimizer from PowerSync settings.
Creates optimizer config entry programmatically.

Optimizer Network structure:
- Battery element (capacity, efficiency from PowerSync config)
- Grid element (import/export price sensors)
- Solar element (forecast sensor)
- Load element (forecast sensor)
- Connections between elements
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# External optimizer domain (configurable)
OPTIMIZER_DOMAIN = "energy_optimizer"

# Default network configuration
DEFAULT_HORIZON_HOURS = 48
DEFAULT_RESOLUTION_MINUTES = 5
DEFAULT_BATTERY_EFFICIENCY = 0.92  # Round-trip efficiency

# PowerSync forecast sensor entity IDs
IMPORT_PRICE_SENSOR = "sensor.powersync_price_import_forecast"
EXPORT_PRICE_SENSOR = "sensor.powersync_price_export_forecast"
SOLAR_SENSOR = "sensor.powersync_solar_forecast"
LOAD_SENSOR = "sensor.powersync_load_forecast"


class OptimizerConfigurator:
    """Auto-configure external optimizer from PowerSync settings."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the optimizer configurator.

        Args:
            hass: Home Assistant instance
            entry: PowerSync config entry
        """
        self.hass = hass
        self._entry = entry

    async def ensure_optimizer_installed(self) -> bool:
        """Check if external optimizer integration is available.

        Returns:
            True if optimizer is installed and loaded
        """
        return OPTIMIZER_DOMAIN in self.hass.config.components

    async def get_optimizer_network(self) -> dict | None:
        """Get existing optimizer network for PowerSync if it exists.

        Returns:
            Network configuration dict or None if not found
        """
        if not await self.ensure_optimizer_installed():
            return None

        # Check for existing optimizer config entries
        optimizer_entries = self.hass.config_entries.async_entries(OPTIMIZER_DOMAIN)

        for entry in optimizer_entries:
            # Look for PowerSync-created network
            if entry.data.get("source") == "powersync":
                return entry.data

            # Also check by name
            if entry.data.get("name", "").lower() == "powersync":
                return entry.data

        return None

    async def create_optimizer_network(
        self,
        battery_config: dict,
    ) -> str | None:
        """Create optimizer config entry programmatically.

        Args:
            battery_config: Battery configuration dict with:
                - capacity_wh: Battery capacity in Wh
                - max_charge_w: Max charge power in W
                - max_discharge_w: Max discharge power in W
                - efficiency: Round-trip efficiency (0-1)
                - backup_reserve: Minimum SOC to maintain (0-1)

        Returns:
            Network ID if created successfully, None otherwise
        """
        if not await self.ensure_optimizer_installed():
            _LOGGER.error("External optimizer integration not installed")
            return None

        # Check if network already exists
        existing = await self.get_optimizer_network()
        if existing:
            _LOGGER.info("Optimizer network already exists for PowerSync")
            return existing.get("network_id")

        # Build optimizer network configuration
        network_config = self._build_network_config(battery_config)

        try:
            # Create config entry via optimizer's config flow
            result = await self.hass.config_entries.flow.async_init(
                OPTIMIZER_DOMAIN,
                context={"source": "import"},
                data=network_config,
            )

            if result.get("type") == "create_entry":
                network_id = result.get("result", {}).get("entry_id")
                _LOGGER.info(f"Created optimizer network: {network_id}")
                return network_id
            else:
                _LOGGER.error(f"Failed to create optimizer network: {result}")
                return None

        except Exception as e:
            _LOGGER.error(f"Error creating optimizer network: {e}")
            return None

    def _build_network_config(self, battery_config: dict) -> dict:
        """Build optimizer network configuration.

        Args:
            battery_config: Battery configuration dict

        Returns:
            Optimizer network configuration dict
        """
        capacity_wh = battery_config.get("capacity_wh", 13500)
        max_charge_w = battery_config.get("max_charge_w", 5000)
        max_discharge_w = battery_config.get("max_discharge_w", 5000)
        efficiency = battery_config.get("efficiency", DEFAULT_BATTERY_EFFICIENCY)
        backup_reserve = battery_config.get("backup_reserve", 0.2)

        return {
            "name": "PowerSync",
            "source": "powersync",  # Mark as created by PowerSync
            "horizon_hours": DEFAULT_HORIZON_HOURS,
            "resolution_minutes": DEFAULT_RESOLUTION_MINUTES,
            "elements": {
                "battery": {
                    "type": "battery",
                    "name": "Home Battery",
                    "capacity_wh": capacity_wh,
                    "max_charge_w": max_charge_w,
                    "max_discharge_w": max_discharge_w,
                    "efficiency": efficiency,
                    "min_soc": backup_reserve,
                    "max_soc": 1.0,
                },
                "grid": {
                    "type": "grid",
                    "name": "Grid Connection",
                    "import_price_sensor": IMPORT_PRICE_SENSOR,
                    "export_price_sensor": EXPORT_PRICE_SENSOR,
                    "max_import_w": 30000,  # Typical residential
                    "max_export_w": 10000,  # Typical export limit
                },
                "solar": {
                    "type": "solar",
                    "name": "Solar PV",
                    "forecast_sensor": SOLAR_SENSOR,
                },
                "load": {
                    "type": "load",
                    "name": "Home Load",
                    "forecast_sensor": LOAD_SENSOR,
                },
            },
            "connections": [
                # Grid can charge battery
                {"from": "grid", "to": "battery"},
                # Battery can export to grid
                {"from": "battery", "to": "grid"},
                # Solar charges battery
                {"from": "solar", "to": "battery"},
                # Solar powers load
                {"from": "solar", "to": "load"},
                # Solar exports to grid
                {"from": "solar", "to": "grid"},
                # Battery powers load
                {"from": "battery", "to": "load"},
                # Grid powers load
                {"from": "grid", "to": "load"},
            ],
            "objective": "minimize_cost",  # or "maximize_self_consumption"
        }

    async def update_optimizer_network(
        self,
        battery_config: dict,
    ) -> bool:
        """Update existing optimizer network with new configuration.

        Args:
            battery_config: Updated battery configuration

        Returns:
            True if updated successfully
        """
        if not await self.ensure_optimizer_installed():
            return False

        existing = await self.get_optimizer_network()
        if not existing:
            # Create new network instead
            return await self.create_optimizer_network(battery_config) is not None

        try:
            # Find the optimizer config entry
            optimizer_entries = self.hass.config_entries.async_entries(OPTIMIZER_DOMAIN)

            for entry in optimizer_entries:
                if entry.data.get("source") == "powersync":
                    # Update the entry with new configuration
                    new_config = self._build_network_config(battery_config)

                    # Merge with existing config to preserve user customizations
                    updated_data = {**entry.data, **new_config}

                    self.hass.config_entries.async_update_entry(
                        entry,
                        data=updated_data,
                    )
                    _LOGGER.info("Updated optimizer network configuration")
                    return True

            return False

        except Exception as e:
            _LOGGER.error(f"Error updating optimizer network: {e}")
            return False

    async def delete_optimizer_network(self) -> bool:
        """Delete the PowerSync optimizer network.

        Returns:
            True if deleted successfully
        """
        if not await self.ensure_optimizer_installed():
            return True  # Nothing to delete

        try:
            optimizer_entries = self.hass.config_entries.async_entries(OPTIMIZER_DOMAIN)

            for entry in optimizer_entries:
                if entry.data.get("source") == "powersync":
                    await self.hass.config_entries.async_remove(entry.entry_id)
                    _LOGGER.info("Deleted optimizer network")
                    return True

            return True  # No network to delete

        except Exception as e:
            _LOGGER.error(f"Error deleting optimizer network: {e}")
            return False

    def get_battery_power_sensor(self) -> str:
        """Get the optimizer battery power sensor entity ID.

        Returns:
            Entity ID for the optimizer battery power output sensor
        """
        return "sensor.powersync_optimizer_battery_power"

    def get_predicted_cost_sensor(self) -> str:
        """Get the optimizer predicted cost sensor entity ID.

        Returns:
            Entity ID for the optimizer predicted cost sensor
        """
        return "sensor.powersync_optimizer_predicted_cost"

    def get_savings_sensor(self) -> str:
        """Get the optimizer savings sensor entity ID.

        Returns:
            Entity ID for the optimizer savings sensor
        """
        return "sensor.powersync_optimizer_savings"

    async def verify_optimizer_sensors(self) -> dict[str, bool]:
        """Verify that required optimizer sensors exist.

        Returns:
            Dict mapping sensor names to existence status
        """
        registry = er.async_get(self.hass)

        sensors = {
            "battery_power": self.get_battery_power_sensor(),
            "predicted_cost": self.get_predicted_cost_sensor(),
            "savings": self.get_savings_sensor(),
        }

        results = {}
        for name, entity_id in sensors.items():
            entry = registry.async_get(entity_id)
            results[name] = entry is not None

        return results
