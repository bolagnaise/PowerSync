"""
Schedule Reader for PowerSync.

Reads external optimizer sensor outputs and converts them to
executor-compatible schedule format.

Optimizer outputs:
- sensor.powersync_optimizer_battery_power (with forecast attribute)
  - Positive values = discharge
  - Negative values = charge
  - forecast: [{"time": "2024-01-01T00:00:00", "value": 2500}, ...]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Optimizer sensor entity IDs (created by OptimizerConfigurator)
OPTIMIZER_BATTERY_POWER_SENSOR = "sensor.powersync_optimizer_battery_power"
OPTIMIZER_PREDICTED_COST_SENSOR = "sensor.powersync_optimizer_predicted_cost"
OPTIMIZER_SAVINGS_SENSOR = "sensor.powersync_optimizer_savings"


@dataclass
class ScheduleAction:
    """Single action in the optimization schedule."""
    timestamp: datetime
    action: str  # "idle", "charge", "discharge", "consume", "export"
    power_w: float
    soc: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "power_w": self.power_w,
            "soc": self.soc,
        }


@dataclass
class OptimizationSchedule:
    """Complete optimization schedule from external optimizer."""
    actions: list[ScheduleAction]
    predicted_cost: float
    predicted_savings: float
    last_updated: datetime | None = None

    @property
    def timestamps(self) -> list[str]:
        """Get list of timestamps as ISO strings."""
        return [a.timestamp.isoformat() for a in self.actions]

    @property
    def charge_w(self) -> list[float]:
        """Get charge power schedule (positive = charging)."""
        return [
            a.power_w if a.action == "charge" else 0.0
            for a in self.actions
        ]

    @property
    def discharge_w(self) -> list[float]:
        """Get discharge power schedule (positive = discharging)."""
        return [
            a.power_w if a.action in ("discharge", "consume", "export") else 0.0
            for a in self.actions
        ]

    @property
    def soc(self) -> list[float]:
        """Get SOC schedule (0-1 scale)."""
        return [a.soc or 0.5 for a in self.actions]

    def to_executor_schedule(self) -> list[dict[str, Any]]:
        """Convert to executor-compatible format."""
        return [a.to_dict() for a in self.actions]

    def to_api_response(self) -> dict[str, Any]:
        """Convert to API response format for mobile app."""
        return {
            "timestamps": self.timestamps,
            "charge_w": self.charge_w,
            "discharge_w": self.discharge_w,
            "soc": self.soc,
            "grid_import_w": [],  # Optimizer may provide this
            "grid_export_w": [],  # Optimizer may provide this
        }


class ScheduleReader:
    """Read optimizer sensors and provide schedule to executor."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the schedule reader.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass

    async def get_schedule(self) -> OptimizationSchedule | None:
        """Read optimizer battery power sensor forecast.

        Returns:
            OptimizationSchedule with all scheduled actions, or None if unavailable
        """
        # Get battery power sensor
        power_state = self.hass.states.get(OPTIMIZER_BATTERY_POWER_SENSOR)

        if not power_state or power_state.state in ("unknown", "unavailable"):
            _LOGGER.warning(f"Optimizer battery power sensor not available: {OPTIMIZER_BATTERY_POWER_SENSOR}")
            return None

        # Get forecast attribute
        forecast = power_state.attributes.get("forecast", [])

        if not forecast:
            _LOGGER.warning("Optimizer battery power sensor has no forecast attribute")
            return None

        # Parse forecast into schedule actions
        actions = self._parse_forecast(forecast)

        if not actions:
            _LOGGER.warning("Failed to parse optimizer forecast")
            return None

        # Get predicted cost and savings
        predicted_cost = self._get_sensor_value(OPTIMIZER_PREDICTED_COST_SENSOR, 0.0)
        predicted_savings = self._get_sensor_value(OPTIMIZER_SAVINGS_SENSOR, 0.0)

        return OptimizationSchedule(
            actions=actions,
            predicted_cost=predicted_cost,
            predicted_savings=predicted_savings,
            last_updated=dt_util.now(),
        )

    def _parse_forecast(self, forecast: list[dict]) -> list[ScheduleAction]:
        """Parse optimizer forecast into schedule actions.

        Optimizer forecast format:
        - Positive power = discharge (battery to grid/load)
        - Negative power = charge (grid/solar to battery)

        Args:
            forecast: List of {"time": str, "value": float} dicts

        Returns:
            List of ScheduleAction objects
        """
        actions = []

        for point in forecast:
            try:
                # Parse timestamp
                time_str = point.get("time")
                if not time_str:
                    continue

                if isinstance(time_str, datetime):
                    ts = time_str
                else:
                    ts = datetime.fromisoformat(time_str.replace("Z", "+00:00"))

                # Parse power value (kW or W - optimizer convention)
                power_kw = float(point.get("value", 0))

                # Determine action based on power sign
                # Positive = discharge, negative = charge
                if power_kw > 0.01:
                    # Discharging - could be to load or grid
                    # For now, use generic "discharge"
                    # The executor will determine if it's consume or export
                    action = "discharge"
                    power_w = abs(power_kw) * 1000  # Convert to W if in kW
                elif power_kw < -0.01:
                    action = "charge"
                    power_w = abs(power_kw) * 1000
                else:
                    action = "idle"
                    power_w = 0.0

                # Check if power seems to be in W already (> 100 would be unusual for kW)
                if abs(power_kw) > 100:
                    # Already in W
                    power_w = abs(power_kw)

                # Get SOC if available
                soc = point.get("soc")
                if soc is not None:
                    soc = float(soc)
                    # Normalize to 0-1 if in percentage
                    if soc > 1:
                        soc = soc / 100

                actions.append(ScheduleAction(
                    timestamp=ts,
                    action=action,
                    power_w=power_w,
                    soc=soc,
                ))

            except (ValueError, TypeError, KeyError) as e:
                _LOGGER.debug(f"Error parsing forecast point: {e}")
                continue

        return actions

    def _get_sensor_value(self, entity_id: str, default: float = 0.0) -> float:
        """Get numeric value from a sensor.

        Args:
            entity_id: Sensor entity ID
            default: Default value if sensor unavailable

        Returns:
            Sensor value as float
        """
        state = self.hass.states.get(entity_id)

        if not state or state.state in ("unknown", "unavailable"):
            return default

        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def get_current_action(self) -> ScheduleAction | None:
        """Get the current (immediate) scheduled action.

        Returns:
            Current ScheduleAction or None if unavailable
        """
        schedule = await self.get_schedule()
        if not schedule or not schedule.actions:
            return None

        now = dt_util.now()

        # Find the action for the current time
        for i, action in enumerate(schedule.actions):
            # Check if this action's time window includes now
            if action.timestamp <= now:
                # Check if there's a next action
                if i + 1 < len(schedule.actions):
                    next_action = schedule.actions[i + 1]
                    if now < next_action.timestamp:
                        return action
                else:
                    # Last action in schedule
                    return action

        # Return first action if now is before schedule start
        return schedule.actions[0] if schedule.actions else None

    async def get_next_actions(self, count: int = 12) -> list[ScheduleAction]:
        """Get the next N scheduled actions from current time.

        Args:
            count: Number of actions to return

        Returns:
            List of upcoming ScheduleAction objects
        """
        schedule = await self.get_schedule()
        if not schedule or not schedule.actions:
            return []

        now = dt_util.now()
        upcoming = []

        for action in schedule.actions:
            if action.timestamp >= now:
                upcoming.append(action)
                if len(upcoming) >= count:
                    break

        return upcoming

    def is_available(self) -> bool:
        """Check if optimizer sensors are available.

        Returns:
            True if optimizer battery power sensor exists and is not unavailable
        """
        state = self.hass.states.get(OPTIMIZER_BATTERY_POWER_SENSOR)
        return state is not None and state.state not in ("unknown", "unavailable")

    async def get_summary(self) -> dict[str, Any]:
        """Get optimization summary for display.

        Returns:
            Summary dict with cost, savings, and schedule metrics
        """
        schedule = await self.get_schedule()

        if not schedule:
            return {
                "available": False,
                "predicted_cost": 0.0,
                "predicted_savings": 0.0,
                "total_charge_kwh": 0.0,
                "total_discharge_kwh": 0.0,
            }

        # Calculate totals from schedule
        interval_hours = 5 / 60  # 5-minute intervals

        total_charge_wh = sum(
            a.power_w * interval_hours
            for a in schedule.actions
            if a.action == "charge"
        )

        total_discharge_wh = sum(
            a.power_w * interval_hours
            for a in schedule.actions
            if a.action in ("discharge", "consume", "export")
        )

        return {
            "available": True,
            "predicted_cost": schedule.predicted_cost,
            "predicted_savings": schedule.predicted_savings,
            "total_charge_kwh": total_charge_wh / 1000,
            "total_discharge_kwh": total_discharge_wh / 1000,
            "total_intervals": len(schedule.actions),
            "last_updated": schedule.last_updated.isoformat() if schedule.last_updated else None,
        }
