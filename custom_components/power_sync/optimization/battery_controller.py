"""
Battery controller wrappers for optimization executor.

Provides unified interface for controlling different battery systems:
- Tesla: Uses TOU tariff trick (upload fake rates to incentivize charge/discharge)
- Sigenergy: Uses Modbus commands
- Sungrow: Uses Modbus commands
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)


class BatteryControllerWrapper:
    """
    Wrapper that provides force_charge/force_discharge/restore_normal interface.

    Delegates to the existing PowerSync service handlers which implement
    the battery-specific logic (Tesla tariff trick, Sigenergy Modbus, etc.)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        battery_system: str,  # "tesla", "sigenergy", "sungrow", "foxess"
        force_charge_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
        force_discharge_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
        restore_normal_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
        set_self_consumption_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]] | None = None,
        set_backup_reserve_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]] | None = None,
    ):
        """
        Initialize the battery controller wrapper.

        Args:
            hass: Home Assistant instance
            battery_system: Type of battery system
            force_charge_handler: Service handler for force charge
            force_discharge_handler: Service handler for force discharge
            restore_normal_handler: Service handler for restore normal
            set_self_consumption_handler: Service handler for pure self-consumption mode
            set_backup_reserve_handler: Service handler for setting backup reserve %
        """
        self.hass = hass
        self.battery_system = battery_system
        self._force_charge = force_charge_handler
        self._force_discharge = force_discharge_handler
        self._restore_normal = restore_normal_handler
        self._set_self_consumption = set_self_consumption_handler
        self._set_backup_reserve = set_backup_reserve_handler

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

            # Create a service call with the duration
            # ServiceCall takes (domain, service, data) positional args
            call = ServiceCall(
                "power_sync",
                "force_charge",
                {"duration": duration_minutes},
            )

            await self._force_charge(call)
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

            # ServiceCall takes (domain, service, data) positional args
            call = ServiceCall(
                "power_sync",
                "force_discharge",
                {"duration": duration_minutes},
            )

            await self._force_discharge(call)
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

            # ServiceCall takes (domain, service, data) positional args
            call = ServiceCall(
                "power_sync",
                "restore_normal",
                {},
            )

            await self._restore_normal(call)
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

            if self._set_self_consumption:
                # Use dedicated handler
                call = ServiceCall(
                    "power_sync",
                    "set_self_consumption",
                    {},
                )
                await self._set_self_consumption(call)
                return True
            else:
                # Fallback to restore_normal if handler not available
                _LOGGER.warning("No set_self_consumption handler, falling back to restore_normal")
                return await self.restore_normal()

        except Exception as e:
            _LOGGER.error(f"Set self-consumption mode failed: {e}", exc_info=True)
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
            if self._set_backup_reserve:
                call = ServiceCall(
                    "power_sync",
                    "set_backup_reserve",
                    {"percent": percent},
                )
                await self._set_backup_reserve(call)
                return True
            else:
                _LOGGER.debug("No set_backup_reserve handler available")
                return False

        except Exception as e:
            _LOGGER.error(f"Set backup reserve failed: {e}", exc_info=True)
            return False
