"""
Optimizer Configurator for PowerSync.

Integrates with HAEO (Home Assistant Energy Optimizer) for LP-based
battery scheduling optimization.

NOTE: HAEO does not support programmatic config entry creation.
Users must manually configure HAEO through the UI, pointing it to
PowerSync's forecast sensors:
- sensor.powersync_price_import_forecast
- sensor.powersync_price_export_forecast
- sensor.powersync_solar_forecast
- sensor.powersync_load_forecast

This module:
- Detects if HAEO is installed
- Checks if a HAEO network exists
- Provides configuration guidance
- Reads HAEO output sensors
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import OPTIMIZER_DOMAIN

_LOGGER = logging.getLogger(__name__)

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
        """Get existing HAEO network if it exists.

        Returns:
            Network configuration dict or None if not found
        """
        if not await self.ensure_optimizer_installed():
            return None

        # Check for existing HAEO config entries
        optimizer_entries = self.hass.config_entries.async_entries(OPTIMIZER_DOMAIN)

        # Any HAEO config entry counts as a network
        # Users configure HAEO manually through the UI
        for entry in optimizer_entries:
            return {
                "network_id": entry.entry_id,
                "name": entry.title or entry.data.get("hub_common", {}).get("name", "HAEO"),
                "data": entry.data,
            }

        return None

    async def check_manual_setup_needed(self) -> dict:
        """Check if HAEO needs manual setup and return guidance.

        Returns:
            Dict with:
                - installed: bool - whether HAEO is installed
                - configured: bool - whether a HAEO network exists
                - sensors_ready: bool - whether PowerSync forecast sensors exist
                - setup_instructions: str - guidance for user
        """
        installed = await self.ensure_optimizer_installed()
        network = await self.get_optimizer_network() if installed else None
        configured = network is not None

        # Check if PowerSync forecast sensors exist
        sensors_ready = all(
            self.hass.states.get(sensor) is not None
            for sensor in [IMPORT_PRICE_SENSOR, EXPORT_PRICE_SENSOR, SOLAR_SENSOR, LOAD_SENSOR]
        )

        if not installed:
            instructions = (
                "HAEO is not installed. Install via HACS:\n"
                "1. Open HACS → Integrations\n"
                "2. Add custom repository: https://github.com/hass-energy/haeo\n"
                "3. Download and restart Home Assistant"
            )
        elif not configured:
            instructions = (
                "HAEO is installed but needs configuration:\n"
                "1. Go to Settings → Devices & Services → Add Integration → HAEO\n"
                "2. Create a network with 48-hour horizon, 5-minute resolution\n"
                "3. Add elements pointing to PowerSync sensors:\n"
                f"   - Grid: {IMPORT_PRICE_SENSOR}, {EXPORT_PRICE_SENSOR}\n"
                f"   - Solar: {SOLAR_SENSOR}\n"
                f"   - Load: {LOAD_SENSOR}\n"
                "4. Add your battery with capacity/efficiency settings\n"
                "5. Create connections between elements"
            )
        elif not sensors_ready:
            instructions = (
                "HAEO is configured but PowerSync sensors are not ready.\n"
                "Ensure PowerSync has price data (Amber/Octopus) and solar forecasts (Solcast)."
            )
        else:
            instructions = "HAEO is fully configured and ready."

        return {
            "installed": installed,
            "configured": configured,
            "sensors_ready": sensors_ready,
            "setup_instructions": instructions,
        }

    async def create_optimizer_network(
        self,
        battery_config: dict,
    ) -> str | None:
        """Check for HAEO network and provide setup guidance.

        NOTE: HAEO does not support programmatic config entry creation.
        Users must configure HAEO manually through the UI.

        Args:
            battery_config: Battery configuration (for logging guidance)

        Returns:
            Network ID if HAEO is already configured, None otherwise
        """
        if not await self.ensure_optimizer_installed():
            _LOGGER.warning(
                "HAEO integration not installed. Install via HACS: "
                "https://github.com/hass-energy/haeo"
            )
            return None

        # Check if network already exists (user configured manually)
        existing = await self.get_optimizer_network()
        if existing:
            _LOGGER.info(f"Found existing HAEO network: {existing.get('name')}")
            return existing.get("network_id")

        # HAEO doesn't support programmatic creation - guide user
        _LOGGER.warning(
            "HAEO is installed but not configured. Manual setup required:\n"
            "1. Go to Settings → Devices & Services → Add Integration → HAEO\n"
            "2. Create a network pointing to PowerSync forecast sensors:\n"
            f"   - Import price: {IMPORT_PRICE_SENSOR}\n"
            f"   - Export price: {EXPORT_PRICE_SENSOR}\n"
            f"   - Solar forecast: {SOLAR_SENSOR}\n"
            f"   - Load forecast: {LOAD_SENSOR}\n"
            "3. Configure battery: %.1f kWh capacity, %.1f kW charge/discharge" % (
                battery_config.get("capacity_wh", 13500) / 1000,
                battery_config.get("max_charge_w", 5000) / 1000,
            )
        )
        return None

    def get_recommended_config(self, battery_config: dict) -> dict:
        """Get recommended HAEO configuration for reference.

        This returns the configuration that users should set up manually
        in HAEO. HAEO does not support programmatic configuration.

        Args:
            battery_config: Battery configuration dict

        Returns:
            Recommended HAEO network configuration dict
        """
        capacity_wh = battery_config.get("capacity_wh", 13500)
        max_charge_w = battery_config.get("max_charge_w", 5000)
        max_discharge_w = battery_config.get("max_discharge_w", 5000)
        efficiency = battery_config.get("efficiency", DEFAULT_BATTERY_EFFICIENCY)
        backup_reserve = battery_config.get("backup_reserve", 0.2)

        return {
            "name": "PowerSync",
            "horizon_hours": DEFAULT_HORIZON_HOURS,
            "resolution_minutes": DEFAULT_RESOLUTION_MINUTES,
            "elements": {
                "battery": {
                    "type": "battery",
                    "name": "Home Battery",
                    "capacity_kwh": capacity_wh / 1000,
                    "max_charge_kw": max_charge_w / 1000,
                    "max_discharge_kw": max_discharge_w / 1000,
                    "efficiency": efficiency,
                    "min_soc_percent": int(backup_reserve * 100),
                    "max_soc_percent": 100,
                },
                "grid": {
                    "type": "grid",
                    "name": "Grid Connection",
                    "import_price_sensor": IMPORT_PRICE_SENSOR,
                    "export_price_sensor": EXPORT_PRICE_SENSOR,
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
                "grid ↔ battery",
                "solar → battery",
                "solar → load",
                "solar → grid",
                "battery → load",
                "grid → load",
            ],
        }

    async def update_optimizer_network(
        self,
        battery_config: dict,
    ) -> bool:
        """Check if HAEO network exists and log any configuration changes needed.

        NOTE: HAEO must be configured manually. This method only checks status.

        Args:
            battery_config: Updated battery configuration

        Returns:
            True if HAEO network exists
        """
        if not await self.ensure_optimizer_installed():
            return False

        existing = await self.get_optimizer_network()
        if not existing:
            # Log guidance for manual setup
            await self.create_optimizer_network(battery_config)
            return False

        # HAEO is configured - log recommended settings if they differ
        recommended = self.get_recommended_config(battery_config)
        _LOGGER.debug(
            "HAEO network exists. Recommended battery config: "
            "%.1f kWh, %.1f kW charge/discharge, %d%% min SOC",
            recommended["elements"]["battery"]["capacity_kwh"],
            recommended["elements"]["battery"]["max_charge_kw"],
            recommended["elements"]["battery"]["min_soc_percent"],
        )
        return True

    async def delete_optimizer_network(self) -> bool:
        """Check if HAEO network should be removed.

        NOTE: HAEO networks are user-configured and should be removed
        manually through the UI if no longer needed.

        Returns:
            True (always - we don't delete user-configured networks)
        """
        existing = await self.get_optimizer_network()
        if existing:
            _LOGGER.info(
                "HAEO network '%s' exists. If you want to remove it, "
                "go to Settings → Devices & Services → HAEO → Delete",
                existing.get("name", "unknown"),
            )
        return True

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
