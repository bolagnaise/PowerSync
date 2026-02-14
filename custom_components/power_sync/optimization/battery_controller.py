"""
Battery controller wrappers for optimization executor.

Provides unified interface for controlling different battery systems:
- Tesla: Uses TOU tariff trick (upload fake rates to incentivize charge/discharge)
- Sigenergy: Uses Modbus commands
- Sungrow: Uses Modbus commands
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class BatteryControllerWrapper:
    """
    Wrapper that provides force_charge/force_discharge/restore_normal interface.

    Delegates to the existing PowerSync service handlers via hass.services.async_call,
    which is the stable HA service API across all versions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        battery_system: str,  # "tesla", "sigenergy", "sungrow", "foxess"
    ):
        """
        Initialize the battery controller wrapper.

        Args:
            hass: Home Assistant instance
            battery_system: Type of battery system
        """
        self.hass = hass
        self.battery_system = battery_system

    async def force_charge(self, duration_minutes: int = 60, power_w: float = 5000) -> bool:
        """
        Command battery to charge.

        For Tesla: Uploads TOU tariff with $0/kWh buy rate to incentivize charging
        For Sigenergy/Sungrow: Uses Modbus to set charge mode

        Args:
            duration_minutes: How long to charge
            power_w: Target charge power (may not be controllable on all systems)

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info(f"🔋 Optimizer: Force charge {duration_minutes}min at {power_w}W")

            await self.hass.services.async_call(
                "power_sync", "force_charge",
                {"duration": duration_minutes},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Force charge failed: {e}", exc_info=True)
            return False

    async def force_discharge(self, duration_minutes: int = 60, power_w: float = 5000) -> bool:
        """
        Command battery to discharge.

        For Tesla: Uploads TOU tariff with $20/kWh sell rate to incentivize discharge
        For Sigenergy/Sungrow: Uses Modbus to set discharge mode

        Args:
            duration_minutes: How long to discharge
            power_w: Target discharge power (may not be controllable on all systems)

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info(f"🔋 Optimizer: Force discharge {duration_minutes}min at {power_w}W")

            await self.hass.services.async_call(
                "power_sync", "force_discharge",
                {"duration": duration_minutes},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Force discharge failed: {e}", exc_info=True)
            return False

    async def restore_normal(self) -> bool:
        """
        Restore battery to normal autonomous operation.

        For Tesla: Uploads original TOU tariff and sets self-consumption mode
        For Sigenergy/Sungrow: Restores normal operating mode via Modbus

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Restoring normal operation")

            await self.hass.services.async_call(
                "power_sync", "restore_normal",
                {},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Restore normal failed: {e}", exc_info=True)
            return False

    async def set_self_consumption_mode(self) -> bool:
        """
        Set battery to pure self-consumption mode (no TOU optimization).

        This is used for CONSUME action (battery→home) where we want the
        battery to naturally offset home load WITHOUT making autonomous
        charge/discharge decisions based on TOU rates.

        Unlike restore_normal, this:
        - Sets mode to self_consumption (not autonomous)
        - Does NOT restore TOU tariff
        - Does NOT send push notifications

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Setting pure self-consumption mode (battery→home, no TOU)")

            await self.hass.services.async_call(
                "power_sync", "set_self_consumption",
                {},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set self-consumption mode failed: {e}", exc_info=True)
            return False

    async def set_autonomous_mode(self) -> bool:
        """
        Set battery to autonomous (TOU) mode.

        In autonomous mode, Tesla respects backup_reserve as a hard floor and
        makes charge/discharge decisions based on TOU rates. This is required
        for IDLE to work correctly — backup_reserve alone is not enough in
        self_consumption mode.

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Setting autonomous (TOU) mode")

            await self.hass.services.async_call(
                "power_sync", "set_autonomous",
                {},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set autonomous mode failed: {e}", exc_info=True)
            return False

    async def set_backup_reserve(self, percent: int) -> bool:
        """
        Set battery backup reserve percentage.

        Used by the optimizer's IDLE action to hold SOC by setting backup
        reserve to the current SOC%. This prevents the battery from
        discharging while the home draws from the grid.

        Args:
            percent: Backup reserve percentage (0-100)

        Returns:
            True if command was sent successfully
        """
        try:
            await self.hass.services.async_call(
                "power_sync", "set_backup_reserve",
                {"percent": percent},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set backup reserve failed: {e}", exc_info=True)
            return False
