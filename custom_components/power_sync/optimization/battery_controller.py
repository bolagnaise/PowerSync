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
        battery_system: str,  # "tesla", "sigenergy", "sungrow"
        force_charge_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
        force_discharge_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
        restore_normal_handler: Callable[[ServiceCall], Coroutine[Any, Any, None]],
    ):
        """
        Initialize the battery controller wrapper.

        Args:
            hass: Home Assistant instance
            battery_system: Type of battery system
            force_charge_handler: Service handler for force charge
            force_discharge_handler: Service handler for force discharge
            restore_normal_handler: Service handler for restore normal
        """
        self.hass = hass
        self.battery_system = battery_system
        self._force_charge = force_charge_handler
        self._force_discharge = force_discharge_handler
        self._restore_normal = restore_normal_handler

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
            _LOGGER.info(f"🔋 ML Optimizer: Force charge {duration_minutes}min at {power_w}W")

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
            _LOGGER.info(f"🔋 ML Optimizer: Force discharge {duration_minutes}min at {power_w}W")

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
            _LOGGER.info("🔋 ML Optimizer: Restoring normal operation")

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

    async def ensure_self_consumption(self) -> bool:
        """
        Ensure battery is in self-consumption mode.

        This is used for CONSUME action (battery→home) where we want the
        battery to naturally offset home load without forcing grid export.

        For Tesla: Restore normal operation (self-consumption mode)
        For others: Same as restore_normal

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 ML Optimizer: Ensuring self-consumption mode (battery→home)")
            # Self-consumption is the default restored mode
            return await self.restore_normal()

        except Exception as e:
            _LOGGER.error(f"Ensure self-consumption failed: {e}", exc_info=True)
            return False
