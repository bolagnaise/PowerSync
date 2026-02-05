"""
Schedule executor for battery optimization.

Executes battery commands based on the optimization schedule from external optimizer.
Simplified from previous MPC implementation - external optimizer now handles optimization.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class BatteryAction(Enum):
    """Battery control actions."""
    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"  # Legacy - generic discharge
    CONSUME = "consume"      # Battery -> Home load (powering home)
    EXPORT = "export"        # Battery -> Grid (exporting to grid)


class CostFunction(Enum):
    """Cost optimization functions."""
    COST_MINIMIZATION = "cost"
    PROFIT_MAXIMIZATION = "profit"
    SELF_CONSUMPTION = "self_consumption"


@dataclass
class ExecutionStatus:
    """Status of the schedule executor."""
    enabled: bool = False
    last_optimization: datetime | None = None
    last_execution: datetime | None = None
    current_action: BatteryAction = BatteryAction.IDLE
    current_power_w: float = 0.0
    next_action: BatteryAction = BatteryAction.IDLE
    next_action_time: datetime | None = None
    optimization_status: str = "not_run"
    error_message: str | None = None


class ScheduleExecutor:
    """
    Executes battery commands based on external optimizer schedule.

    The actual optimization is handled by the external optimizer. This executor:
    1. Provides battery control interface
    2. Tracks execution status
    3. Maintains compatibility with existing API
    """

    def __init__(
        self,
        hass: HomeAssistant,
        optimiser: Any = None,  # Legacy - no longer used with external optimizer
        battery_controller: Any = None,
        interval_minutes: int = 5,
    ):
        """Initialize the schedule executor.

        Args:
            hass: Home Assistant instance
            optimiser: Legacy parameter - not used with external optimizer
            battery_controller: Controller for battery system commands
            interval_minutes: Execution interval in minutes
        """
        self.hass = hass
        self.battery_controller = battery_controller
        self.interval_minutes = interval_minutes

        self._enabled = False
        self._cancel_timer: Callable | None = None
        self._status = ExecutionStatus()

        # Callbacks for getting current data (used by coordinator)
        self._get_prices_callback: Callable | None = None
        self._get_solar_callback: Callable | None = None
        self._get_load_callback: Callable | None = None
        self._get_battery_state_callback: Callable | None = None

        # Configuration
        self._config: Any = None
        self._cost_function = CostFunction.COST_MINIMIZATION

    @property
    def status(self) -> ExecutionStatus:
        """Get current execution status."""
        return self._status

    @property
    def enabled(self) -> bool:
        """Check if executor is enabled."""
        return self._enabled

    def set_data_callbacks(
        self,
        get_prices: Callable | None = None,
        get_solar: Callable | None = None,
        get_load: Callable | None = None,
        get_battery_state: Callable | None = None,
        optimize: Callable | None = None,  # Legacy - not used with external optimizer
    ) -> None:
        """Set callbacks for getting current data.

        Args:
            get_prices: Callback returning (import_prices, export_prices) lists
            get_solar: Callback returning solar forecast list
            get_load: Callback returning load forecast list
            get_battery_state: Callback returning (soc, capacity_wh) tuple
            optimize: Legacy - not used with external optimizer
        """
        self._get_prices_callback = get_prices
        self._get_solar_callback = get_solar
        self._get_load_callback = get_load
        self._get_battery_state_callback = get_battery_state

    def set_config(self, config: Any) -> None:
        """Set optimization configuration."""
        self._config = config

    def set_cost_function(self, cost_function: CostFunction | str) -> None:
        """Set the optimization cost function."""
        if isinstance(cost_function, str):
            try:
                cost_function = CostFunction(cost_function)
            except ValueError:
                cost_function = CostFunction.COST_MINIMIZATION
        self._cost_function = cost_function

    async def start(self, use_periodic_timer: bool = False) -> bool:
        """Start the schedule executor.

        Args:
            use_periodic_timer: Whether to use periodic timer (usually False with external optimizer)

        Returns:
            True if started successfully
        """
        if self._enabled:
            _LOGGER.warning("Schedule executor already running")
            return True

        _LOGGER.info("Starting schedule executor")

        self._enabled = True
        self._status.enabled = True
        self._status.error_message = None

        # Schedule periodic status check if requested
        if use_periodic_timer:
            aligned_minutes = list(range(0, 60, self.interval_minutes))
            self._cancel_timer = async_track_time_change(
                self.hass,
                self._tick,
                minute=aligned_minutes,
                second=0,
            )
            _LOGGER.info(f"Executor started with timer (interval: {self.interval_minutes}min)")
        else:
            _LOGGER.info("Executor started (optimizer-controlled mode)")

        return True

    async def stop(self) -> None:
        """Stop the schedule executor."""
        if not self._enabled:
            return

        _LOGGER.info("Stopping schedule executor")

        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

        self._enabled = False
        self._status.enabled = False

        # Restore battery to normal operation
        await self._restore_normal_operation()

    async def _tick(self, now: datetime | None = None) -> None:
        """Periodic tick (if timer enabled)."""
        # With external optimizer, the coordinator handles schedule reading and execution
        # This tick is mainly for status updates
        pass

    async def execute_action(
        self,
        action: str | BatteryAction,
        power_w: float,
        duration_minutes: int | None = None,
    ) -> None:
        """Execute a battery action.

        Args:
            action: Battery action to execute
            power_w: Power level in watts
            duration_minutes: Duration in minutes (defaults to interval + buffer)
        """
        if isinstance(action, str):
            action = BatteryAction(action)

        duration = duration_minutes or (self.interval_minutes + 5)

        _LOGGER.info(f"Executing action: {action.value} @ {power_w:.0f}W for {duration}min")

        previous_action = self._status.current_action
        self._status.current_action = action
        self._status.current_power_w = power_w
        self._status.last_execution = dt_util.now()

        # Determine if we're transitioning from a forced mode
        forced_modes = (BatteryAction.CHARGE, BatteryAction.EXPORT)
        was_in_forced_mode = previous_action in forced_modes

        try:
            if action == BatteryAction.CHARGE:
                await self._command_charge(power_w, duration)
            elif action == BatteryAction.EXPORT:
                await self._command_discharge(power_w, duration)
            elif action in (BatteryAction.DISCHARGE, BatteryAction.CONSUME):
                if was_in_forced_mode:
                    await self._set_self_consumption_mode()
            else:
                # IDLE
                if was_in_forced_mode:
                    await self._set_self_consumption_mode()

        except Exception as e:
            _LOGGER.error(f"Failed to execute action {action.value}: {e}")
            self._status.error_message = f"Execution failed: {str(e)}"

    async def _command_charge(self, power_w: float, duration_minutes: int) -> None:
        """Command battery to charge."""
        _LOGGER.info(f"Commanding charge at {power_w:.0f}W for {duration_minutes}min")

        if hasattr(self.battery_controller, "force_charge"):
            await self.battery_controller.force_charge(
                duration_minutes=duration_minutes,
                power_w=power_w,
            )
        else:
            _LOGGER.warning("Battery controller does not support force_charge")

    async def _command_discharge(self, power_w: float, duration_minutes: int) -> None:
        """Command battery to discharge/export to grid."""
        _LOGGER.info(f"Commanding discharge/export at {power_w:.0f}W for {duration_minutes}min")

        if hasattr(self.battery_controller, "force_discharge"):
            await self.battery_controller.force_discharge(
                duration_minutes=duration_minutes,
                power_w=power_w,
            )
        else:
            _LOGGER.warning("Battery controller does not support force_discharge")

    async def _set_self_consumption_mode(self) -> None:
        """Set battery to self-consumption mode."""
        _LOGGER.debug("Setting self-consumption mode")

        if hasattr(self.battery_controller, "set_self_consumption_mode"):
            await self.battery_controller.set_self_consumption_mode()
        elif hasattr(self.battery_controller, "restore_normal"):
            await self.battery_controller.restore_normal()

    async def _restore_normal_operation(self) -> None:
        """Restore battery to normal autonomous operation."""
        _LOGGER.debug("Restoring normal autonomous operation")

        if hasattr(self.battery_controller, "restore_normal"):
            await self.battery_controller.restore_normal()

    def update_status(
        self,
        current_action: str | BatteryAction | None = None,
        current_power_w: float | None = None,
        next_action: str | BatteryAction | None = None,
        next_action_time: datetime | None = None,
        optimization_status: str | None = None,
    ) -> None:
        """Update execution status.

        Args:
            current_action: Current battery action
            current_power_w: Current power level
            next_action: Next scheduled action
            next_action_time: Time of next action
            optimization_status: Status string
        """
        if current_action is not None:
            if isinstance(current_action, str):
                current_action = BatteryAction(current_action)
            self._status.current_action = current_action

        if current_power_w is not None:
            self._status.current_power_w = current_power_w

        if next_action is not None:
            if isinstance(next_action, str):
                next_action = BatteryAction(next_action)
            self._status.next_action = next_action

        if next_action_time is not None:
            self._status.next_action_time = next_action_time

        if optimization_status is not None:
            self._status.optimization_status = optimization_status

    def get_schedule_summary(self) -> dict[str, Any]:
        """Get a summary of the current schedule for display."""
        return {
            "status": "active" if self._enabled else "paused",
            "enabled": self._enabled,
            "optimization_status": self._status.optimization_status,
            "current_action": self._status.current_action.value,
            "current_power_w": self._status.current_power_w,
            "next_action": self._status.next_action.value,
            "next_action_time": self._status.next_action_time.isoformat() if self._status.next_action_time else None,
            "last_optimization": self._status.last_optimization.isoformat() if self._status.last_optimization else None,
            "cost_function": self._cost_function.value,
        }
