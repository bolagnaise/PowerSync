"""
Schedule executor for battery optimization.

Executes the optimized battery schedule using MPC (Model Predictive Control)
approach: re-optimize every interval and execute only the immediate action.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .engine import BatteryOptimiser, OptimizationConfig, OptimizationResult, CostFunction

_LOGGER = logging.getLogger(__name__)


class BatteryAction(Enum):
    """Battery control actions."""
    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"  # Legacy - generic discharge
    CONSUME = "consume"      # Battery → Home load (powering home)
    EXPORT = "export"        # Battery → Grid (exporting to grid)


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
    Executes the optimized battery schedule.

    Uses MPC (Model Predictive Control) approach:
    1. Re-optimize every interval (default: 5 minutes)
    2. Execute only the immediate action
    3. Adapt to changes in prices, solar, load, and battery state

    This ensures robustness against forecast errors and changing conditions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        optimiser: BatteryOptimiser,
        battery_controller: Any,  # Battery-specific controller (Tesla/Sigenergy/Sungrow)
        interval_minutes: int = 5,
    ):
        """
        Initialize the schedule executor.

        Args:
            hass: Home Assistant instance
            optimiser: BatteryOptimiser instance
            battery_controller: Controller for battery system commands
            interval_minutes: Execution interval in minutes
        """
        self.hass = hass
        self.optimiser = optimiser
        self.battery_controller = battery_controller
        self.interval_minutes = interval_minutes

        self._enabled = False
        self._cancel_timer: Callable | None = None
        self._current_schedule: OptimizationResult | None = None
        self._status = ExecutionStatus()

        # Callbacks for getting current data
        self._get_prices_callback: Callable | None = None
        self._get_solar_callback: Callable | None = None
        self._get_load_callback: Callable | None = None
        self._get_battery_state_callback: Callable | None = None

        # Callback for optimization (allows coordinator to handle add-on vs local)
        self._optimize_callback: Callable | None = None

        # Configuration
        self._config: OptimizationConfig | None = None
        self._cost_function = CostFunction.COST_MINIMIZATION

    @property
    def status(self) -> ExecutionStatus:
        """Get current execution status."""
        return self._status

    @property
    def enabled(self) -> bool:
        """Check if executor is enabled."""
        return self._enabled

    @property
    def current_schedule(self) -> OptimizationResult | None:
        """Get the current optimization schedule."""
        return self._current_schedule

    def set_data_callbacks(
        self,
        get_prices: Callable | None = None,
        get_solar: Callable | None = None,
        get_load: Callable | None = None,
        get_battery_state: Callable | None = None,
        optimize: Callable | None = None,
    ) -> None:
        """
        Set callbacks for getting current data.

        Args:
            get_prices: Callback returning (import_prices, export_prices) lists
            get_solar: Callback returning solar forecast list
            get_load: Callback returning load forecast list
            get_battery_state: Callback returning (soc, capacity_wh) tuple
            optimize: Callback for running optimization (allows coordinator to use add-on)
        """
        self._get_prices_callback = get_prices
        self._get_solar_callback = get_solar
        self._get_load_callback = get_load
        self._get_battery_state_callback = get_battery_state
        self._optimize_callback = optimize

    def set_config(self, config: OptimizationConfig) -> None:
        """Set optimization configuration."""
        self._config = config

    def set_cost_function(self, cost_function: CostFunction) -> None:
        """Set the optimization cost function."""
        self._cost_function = cost_function
        if self._config:
            self._config.cost_function = cost_function

    async def start(self, use_periodic_timer: bool = True) -> bool:
        """
        Start the schedule executor.

        Args:
            use_periodic_timer: If True, run optimization every interval_minutes.
                               If False, optimization is triggered externally (e.g., by price updates).

        Returns:
            True if started successfully
        """
        if self._enabled:
            _LOGGER.warning("Schedule executor already running")
            return True

        _LOGGER.info("Starting optimization schedule executor")

        # Validate callbacks
        if not all([
            self._get_prices_callback,
            self._get_solar_callback,
            self._get_load_callback,
            self._get_battery_state_callback,
        ]):
            _LOGGER.error("Missing data callbacks - cannot start executor")
            self._status.error_message = "Missing data callbacks"
            return False

        self._enabled = True
        self._status.enabled = True
        self._status.error_message = None

        # Run initial optimization
        await self._tick()

        # Schedule periodic execution only for static/TOU pricing
        # For dynamic pricing (Amber/AEMO), optimization is triggered by price updates
        if use_periodic_timer:
            # Calculate minute values aligned to interval_minutes boundaries
            # For 5-minute interval: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
            aligned_minutes = list(range(0, 60, self.interval_minutes))

            # Use async_track_time_change to fire at specific minutes aligned to clock
            # This ensures ticks happen at :00, :05, :10, etc. instead of arbitrary times
            self._cancel_timer = async_track_time_change(
                self.hass,
                self._tick,
                minute=aligned_minutes,
                second=0,  # Fire at :00 seconds
            )
            _LOGGER.info(
                f"Schedule executor started (interval: {self.interval_minutes}min, "
                f"aligned to minutes: {aligned_minutes})"
            )
        else:
            _LOGGER.info("Schedule executor started (price-triggered mode - no periodic timer)")

        return True

    async def stop(self) -> None:
        """Stop the schedule executor."""
        if not self._enabled:
            return

        _LOGGER.info("Stopping optimization schedule executor")

        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

        self._enabled = False
        self._status.enabled = False

        # Restore battery to normal operation
        await self._restore_normal_operation()

    async def _tick(self, now: datetime | None = None) -> None:
        """
        Periodic tick: re-optimize and execute.

        This is the MPC control loop:
        1. Get current state
        2. Re-run optimization with latest data
        3. Execute the immediate action
        """
        now = now or dt_util.now()
        _LOGGER.debug(f"Optimization tick fired at {now.strftime('%H:%M:%S')}")

        if not self._enabled:
            _LOGGER.debug("Executor not enabled, skipping tick")
            return

        try:
            # If optimize callback is set (coordinator handles add-on vs local), use it
            if self._optimize_callback:
                result = await self._optimize_callback()
                if result:
                    self._current_schedule = result
                    self._status.last_optimization = now

                    if not result.success:
                        _LOGGER.warning(f"Optimization failed: {result.status}")
                        self._status.optimization_status = f"failed: {result.status}"
                        return

                    self._status.optimization_status = "success"

                    # Execute the immediate action (first interval)
                    await self._execute_action(result, 0)

                    # Update status with next action
                    if len(result.charge_schedule_w) > 1:
                        next_action = result.get_action_at_index(1)
                        self._status.next_action = BatteryAction(next_action["action"])
                        self._status.next_action_time = now + timedelta(minutes=self.interval_minutes)

                    _LOGGER.info(
                        f"Optimization complete (via callback): cost=${result.total_cost:.2f}, "
                        f"savings=${result.savings:.2f}, action={self._status.current_action.value}"
                    )
                    return
                else:
                    _LOGGER.debug("Optimize callback returned None, falling back to local")

            # Fallback: use local optimiser directly
            # Get current data
            prices = await self._get_prices()
            solar = await self._get_solar()
            load = await self._get_load()
            soc, capacity = await self._get_battery_state()

            if not all([prices, solar, load]):
                _LOGGER.warning("Missing data for optimization")
                self._status.optimization_status = "missing_data"
                return

            import_prices, export_prices = prices

            # Update config with current battery capacity
            config = self._config or OptimizationConfig()
            config.battery_capacity_wh = capacity
            config.cost_function = self._cost_function

            # Run optimization
            result = self.optimiser.optimize(
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                initial_soc=soc,
                start_time=now,
                config=config,
            )

            self._current_schedule = result
            self._status.last_optimization = now

            if not result.success:
                _LOGGER.warning(f"Optimization failed: {result.status}")
                self._status.optimization_status = f"failed: {result.status}"
                return

            self._status.optimization_status = "success"

            # Execute the immediate action (first interval)
            await self._execute_action(result, 0)

            # Update status with next action
            if len(result.charge_schedule_w) > 1:
                next_action = result.get_action_at_index(1)
                self._status.next_action = BatteryAction(next_action["action"])
                self._status.next_action_time = now + timedelta(minutes=self.interval_minutes)

            _LOGGER.info(
                f"Optimization complete (local): cost=${result.total_cost:.2f}, "
                f"savings=${result.savings:.2f}, action={self._status.current_action.value}"
            )

        except Exception as e:
            _LOGGER.error(f"Error in optimization tick: {e}", exc_info=True)
            self._status.optimization_status = f"error: {str(e)}"
            self._status.error_message = str(e)

    async def _execute_action(self, result: OptimizationResult, index: int) -> None:
        """Execute the action for a specific interval."""
        action_data = result.get_action_at_index(index)
        action = BatteryAction(action_data["action"])
        power_w = action_data["power_w"]

        # Get detailed breakdown for logging
        to_load_w = action_data.get("to_load_w", 0)
        to_grid_w = action_data.get("to_grid_w", 0)

        if action in (BatteryAction.CONSUME, BatteryAction.EXPORT):
            _LOGGER.info(
                f"🔋 Executing interval {index}: action={action.value}, power={power_w:.0f}W "
                f"(→home: {to_load_w:.0f}W, →grid: {to_grid_w:.0f}W), soc={action_data.get('soc', 'N/A')}"
            )
        else:
            _LOGGER.info(f"🔋 Executing interval {index}: action={action.value}, power={power_w:.0f}W, soc={action_data.get('soc', 'N/A')}")

        # Track previous action to know if we need to restore
        previous_action = self._status.current_action

        self._status.current_action = action
        self._status.current_power_w = power_w
        self._status.last_execution = dt_util.now()

        # Determine if previous action was a "forced" mode that modified tariff
        forced_modes = (BatteryAction.CHARGE, BatteryAction.EXPORT)
        was_in_forced_mode = previous_action in forced_modes

        try:
            if action == BatteryAction.CHARGE:
                # In self_consumption mode, only force-charge when electricity is free/negative
                # Otherwise let battery charge naturally from excess solar
                if self._cost_function == CostFunction.SELF_CONSUMPTION:
                    # Check current import price
                    current_price = await self._get_current_import_price()
                    if current_price is not None and current_price <= 0:
                        # Electricity is free or negative - charge is allowed
                        _LOGGER.info(
                            f"Self-consumption mode: allowing charge at {power_w:.0f}W "
                            f"(price={current_price:.2f}c is free/negative)"
                        )
                        await self._command_charge(power_w)
                    else:
                        # Electricity costs money - don't force charge
                        _LOGGER.info(
                            f"Self-consumption mode: ignoring charge command (price={current_price}c > 0). "
                            f"Letting battery charge naturally from solar."
                        )
                        # Update status to reflect actual action (idle, not charge)
                        self._status.current_action = BatteryAction.IDLE
                        self._status.current_power_w = 0
                        # Treat as idle - set self-consumption mode if coming from forced mode
                        if was_in_forced_mode:
                            await self._set_self_consumption_mode()
                else:
                    await self._command_charge(power_w)
            elif action == BatteryAction.EXPORT:
                # EXPORT: Force battery to export to grid using high sell tariff
                await self._command_discharge(power_w)
            elif action in (BatteryAction.DISCHARGE, BatteryAction.CONSUME):
                # CONSUME/DISCHARGE: Battery should power home load naturally
                # Only set self-consumption if we were in a forced mode (charge/export)
                # Otherwise battery is already in self-consumption mode
                if was_in_forced_mode:
                    _LOGGER.info("Transitioning from forced mode to consume - setting self-consumption mode")
                    await self._set_self_consumption_mode()
                else:
                    _LOGGER.debug(f"Consume action: battery already in self-consumption mode (prev={previous_action.value})")
            else:
                # IDLE: Only set self-consumption if we were in a forced mode
                if was_in_forced_mode:
                    _LOGGER.info("Transitioning from forced mode to idle - setting self-consumption mode")
                    await self._set_self_consumption_mode()
                else:
                    _LOGGER.debug(f"Action is idle, no restore needed (prev={previous_action.value})")

        except Exception as e:
            _LOGGER.error(f"Failed to execute action {action.value}: {e}")
            self._status.error_message = f"Execution failed: {str(e)}"

    async def _command_charge(self, power_w: float) -> None:
        """Command battery to charge."""
        _LOGGER.info(f"Commanding charge at {power_w:.0f}W")

        if hasattr(self.battery_controller, "force_charge"):
            # Duration until next optimization interval
            duration_minutes = self.interval_minutes + 5  # Add buffer
            await self.battery_controller.force_charge(
                duration_minutes=duration_minutes,
                power_w=power_w,
            )
        else:
            _LOGGER.warning("Battery controller does not support force_charge")

    async def _command_discharge(self, power_w: float) -> None:
        """Command battery to discharge/export to grid."""
        _LOGGER.info(f"Commanding discharge/export at {power_w:.0f}W")

        if hasattr(self.battery_controller, "force_discharge"):
            duration_minutes = self.interval_minutes + 5
            await self.battery_controller.force_discharge(
                duration_minutes=duration_minutes,
                power_w=power_w,
            )
        else:
            _LOGGER.warning("Battery controller does not support force_discharge")

    async def _set_self_consumption_mode(self) -> None:
        """Set battery to pure self-consumption mode (no TOU optimization).

        Used during active optimization for CONSUME/IDLE actions.
        Battery offsets home load naturally without TOU-based decisions.
        """
        _LOGGER.debug("Setting pure self-consumption mode (no TOU)")

        if hasattr(self.battery_controller, "set_self_consumption_mode"):
            await self.battery_controller.set_self_consumption_mode()
        elif hasattr(self.battery_controller, "restore_normal"):
            # Fallback for older controllers
            _LOGGER.debug("No set_self_consumption_mode, falling back to restore_normal")
            await self.battery_controller.restore_normal()

    async def _restore_normal_operation(self) -> None:
        """Restore battery to normal autonomous TOU operation.

        Used when ML optimization is stopped/disabled.
        Returns battery to original TOU-based operation mode.
        """
        _LOGGER.debug("Restoring normal autonomous operation (with TOU)")

        if hasattr(self.battery_controller, "restore_normal"):
            await self.battery_controller.restore_normal()

    async def _get_prices(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts from callback."""
        if self._get_prices_callback:
            try:
                return await self._get_prices_callback()
            except Exception as e:
                _LOGGER.error(f"Error getting prices: {e}")
        return None

    async def _get_current_import_price(self) -> float | None:
        """Get the current import price (first interval) in $/kWh."""
        prices = await self._get_prices()
        if prices and prices[0] and len(prices[0]) > 0:
            # Return price in cents for easier reading in logs
            # prices[0] is import prices in $/kWh, convert to c/kWh
            return prices[0][0] * 100
        return None

    async def _get_solar(self) -> list[float] | None:
        """Get solar forecast from callback."""
        if self._get_solar_callback:
            try:
                return await self._get_solar_callback()
            except Exception as e:
                _LOGGER.error(f"Error getting solar forecast: {e}")
        return None

    async def _get_load(self) -> list[float] | None:
        """Get load forecast from callback."""
        if self._get_load_callback:
            try:
                return await self._get_load_callback()
            except Exception as e:
                _LOGGER.error(f"Error getting load forecast: {e}")
        return None

    async def _get_battery_state(self) -> tuple[float, float]:
        """Get battery state from callback."""
        if self._get_battery_state_callback:
            try:
                return await self._get_battery_state_callback()
            except Exception as e:
                _LOGGER.error(f"Error getting battery state: {e}")
        return 0.5, 13500  # Default: 50% SOC, 13.5kWh capacity

    async def force_reoptimize(self) -> OptimizationResult | None:
        """Force an immediate re-optimization."""
        _LOGGER.info("Forcing re-optimization")
        await self._tick()
        return self._current_schedule

    def get_schedule_summary(self) -> dict[str, Any]:
        """Get a summary of the current schedule for display."""
        if not self._current_schedule:
            return {
                "status": "no_schedule",
                "enabled": self._enabled,
            }

        result = self._current_schedule
        return {
            "status": "active" if self._enabled else "paused",
            "enabled": self._enabled,
            "optimization_status": self._status.optimization_status,
            "current_action": self._status.current_action.value,
            "current_power_w": self._status.current_power_w,
            "next_action": self._status.next_action.value,
            "next_action_time": self._status.next_action_time.isoformat() if self._status.next_action_time else None,
            "last_optimization": self._status.last_optimization.isoformat() if self._status.last_optimization else None,
            "predicted_cost": result.total_cost,
            "predicted_savings": result.savings,
            "total_intervals": len(result.charge_schedule_w),
            "cost_function": self._cost_function.value,
        }
