"""
Load estimation for battery optimization.

Estimates household load forecast using historical data from Home Assistant.
Since PowerSync doesn't have ML-based load forecasting, we use simple
pattern-based estimation from recent history.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class LoadEstimator:
    """
    Estimate household load forecast from historical data.

    Methods:
    1. Historical average: Same time on recent days, smoothed
    2. Day-of-week pattern: Average by time-of-day and day-of-week
    3. Current extrapolation: Recent average extended forward

    The estimator queries Home Assistant recorder for historical load data
    and generates a forecast for the optimization horizon.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        load_entity_id: str | None = None,
        interval_minutes: int = 30,
    ):
        """
        Initialize the load estimator.

        Args:
            hass: Home Assistant instance
            load_entity_id: Entity ID for load sensor (e.g., sensor.power_sync_home_load)
            interval_minutes: Forecast interval in minutes
        """
        self.hass = hass
        self.load_entity_id = load_entity_id
        self.interval_minutes = interval_minutes
        self._history_cache: dict[str, list[tuple[datetime, float]]] = {}
        self._cache_time: datetime | None = None
        self._cache_duration = timedelta(hours=1)

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float]:
        """
        Generate load forecast in Watts for each interval.

        Args:
            horizon_hours: Forecast horizon in hours
            start_time: Start time for forecast (default: now)

        Returns:
            List of load values in Watts for each interval
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        # Try to get historical pattern
        try:
            history = await self._get_load_history(days=7)
            if history:
                return self._forecast_from_history(history, start_time, n_intervals)
        except Exception as e:
            _LOGGER.warning(f"Failed to get load history: {e}")

        # Fallback: use current load or default
        current_load = self._get_current_load()
        return self._simple_forecast(current_load, start_time, n_intervals)

    async def _get_load_history(self, days: int = 7) -> list[tuple[datetime, float]]:
        """
        Get historical load data from Home Assistant recorder.

        Args:
            days: Number of days of history to fetch

        Returns:
            List of (timestamp, load_watts) tuples
        """
        if not self.load_entity_id:
            return []

        # Check cache
        now = dt_util.utcnow()
        if (
            self._cache_time
            and now - self._cache_time < self._cache_duration
            and self.load_entity_id in self._history_cache
        ):
            return self._history_cache[self.load_entity_id]

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance = get_instance(self.hass)

            start_time = now - timedelta(days=days)
            end_time = now

            # Get history from recorder
            history = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start_time,
                end_time,
                [self.load_entity_id],
            )

            if not history or self.load_entity_id not in history:
                _LOGGER.warning(f"No history found for {self.load_entity_id}")
                return []

            # Parse states into (timestamp, value) tuples
            result = []
            for state in history[self.load_entity_id]:
                try:
                    value = float(state.state)
                    if value >= 0:  # Filter invalid values
                        result.append((state.last_changed, value))
                except (ValueError, TypeError):
                    continue

            # Cache the result
            self._history_cache[self.load_entity_id] = result
            self._cache_time = now

            _LOGGER.debug(f"Loaded {len(result)} history points for {self.load_entity_id}")
            return result

        except ImportError:
            _LOGGER.warning("Recorder not available for load history")
            return []
        except Exception as e:
            _LOGGER.error(f"Error fetching load history: {e}")
            return []

    def _forecast_from_history(
        self,
        history: list[tuple[datetime, float]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Generate forecast using historical pattern matching.

        Groups historical data by day-of-week and time-of-day, then
        generates forecast by looking up the average for each future interval.
        """
        # Group by (day_of_week, hour, half_hour)
        pattern: dict[tuple[int, int, int], list[float]] = defaultdict(list)

        for timestamp, value in history:
            dow = timestamp.weekday()
            hour = timestamp.hour
            half_hour = 0 if timestamp.minute < 30 else 1
            key = (dow, hour, half_hour)
            pattern[key].append(value)

        # Calculate averages
        averages: dict[tuple[int, int, int], float] = {}
        for key, values in pattern.items():
            if values:
                averages[key] = sum(values) / len(values)

        # Generate forecast
        forecast = []
        current_time = start_time

        for _ in range(n_intervals):
            dow = current_time.weekday()
            hour = current_time.hour
            half_hour = 0 if current_time.minute < 30 else 1
            key = (dow, hour, half_hour)

            if key in averages:
                forecast.append(averages[key])
            else:
                # Fallback: use same time any day
                fallback_values = [
                    averages.get((d, hour, half_hour))
                    for d in range(7)
                    if (d, hour, half_hour) in averages
                ]
                if fallback_values:
                    forecast.append(sum(fallback_values) / len(fallback_values))
                else:
                    # Last resort: use overall average or default
                    if averages:
                        forecast.append(sum(averages.values()) / len(averages))
                    else:
                        forecast.append(500.0)  # Default 500W

            current_time += timedelta(minutes=self.interval_minutes)

        # Apply smoothing
        forecast = self._smooth_forecast(forecast)

        return forecast

    def _get_current_load(self) -> float:
        """Get current load from Home Assistant state."""
        if not self.load_entity_id:
            return 500.0  # Default 500W

        try:
            state = self.hass.states.get(self.load_entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                # Load is typically in kW, convert to W
                value = float(state.state)
                # Check unit - if already in W, use as-is; if in kW, convert
                unit = state.attributes.get("unit_of_measurement", "kW")
                if unit.lower() == "kw":
                    value *= 1000
                return max(0, value)
        except (ValueError, TypeError, AttributeError):
            pass

        return 500.0  # Default

    def _simple_forecast(
        self,
        base_load: float,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Generate simple forecast based on typical daily pattern.

        Uses a generic residential load profile when no history is available.
        """
        forecast = []
        current_time = start_time

        # Generic residential load pattern (multipliers by hour)
        # Lower at night, peaks in morning and evening
        hourly_pattern = {
            0: 0.4, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.3, 5: 0.4,
            6: 0.6, 7: 0.8, 8: 0.9, 9: 0.8, 10: 0.7, 11: 0.7,
            12: 0.8, 13: 0.7, 14: 0.6, 15: 0.6, 16: 0.7, 17: 0.9,
            18: 1.2, 19: 1.3, 20: 1.2, 21: 1.0, 22: 0.7, 23: 0.5,
        }

        for _ in range(n_intervals):
            hour = current_time.hour
            multiplier = hourly_pattern.get(hour, 0.7)
            forecast.append(base_load * multiplier)
            current_time += timedelta(minutes=self.interval_minutes)

        return self._smooth_forecast(forecast)

    def _smooth_forecast(self, values: list[float], window: int = 3) -> list[float]:
        """Apply simple moving average smoothing to forecast."""
        if len(values) <= window:
            return values

        smoothed = []
        for i in range(len(values)):
            start = max(0, i - window // 2)
            end = min(len(values), i + window // 2 + 1)
            smoothed.append(sum(values[start:end]) / (end - start))

        return smoothed

    async def get_average_daily_load(self) -> float:
        """Get average daily load in kWh."""
        history = await self._get_load_history(days=7)
        if not history:
            return 15.0  # Default 15 kWh/day

        # Calculate average power in W
        avg_power = sum(v for _, v in history) / len(history)

        # Convert to daily kWh
        return avg_power * 24 / 1000


class SolcastForecaster:
    """
    Wrapper for Solcast solar forecasts.

    Retrieves solar production forecasts from the Solcast coordinator
    if available in Home Assistant.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        solcast_entity: str | None = None,
        interval_minutes: int = 30,
    ):
        """
        Initialize Solcast forecaster.

        Args:
            hass: Home Assistant instance
            solcast_entity: Solcast sensor entity ID
            interval_minutes: Forecast interval in minutes
        """
        self.hass = hass
        self.solcast_entity = solcast_entity
        self.interval_minutes = interval_minutes

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float]:
        """
        Get solar forecast in Watts for each interval.

        Returns:
            List of solar generation values in Watts
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        # Try to get Solcast forecast from coordinator data
        forecast = await self._get_solcast_forecast(start_time, n_intervals)
        if forecast:
            return forecast

        # Fallback: generate simple solar curve
        return self._generate_default_solar_curve(start_time, n_intervals)

    async def _get_solcast_forecast(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Get forecast from Solcast integration if available."""
        try:
            # Check for Solcast data in hass.data
            from ..const import DOMAIN

            domain_data = self.hass.data.get(DOMAIN, {})

            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Look for Solcast forecast data
                solcast_data = entry_data.get("solcast_forecast")
                if solcast_data and "forecasts" in solcast_data:
                    return self._parse_solcast_data(
                        solcast_data["forecasts"],
                        start_time,
                        n_intervals,
                    )

            return None

        except Exception as e:
            _LOGGER.debug(f"Could not get Solcast forecast: {e}")
            return None

    def _parse_solcast_data(
        self,
        forecasts: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse Solcast forecast data into interval values."""
        # Create time index for forecasts
        forecast_by_time: dict[datetime, float] = {}

        for item in forecasts:
            try:
                # Solcast provides period_end and pv_estimate (kW)
                end_time = datetime.fromisoformat(item["period_end"].replace("Z", "+00:00"))
                pv_kw = item.get("pv_estimate", 0) or 0
                forecast_by_time[end_time] = pv_kw * 1000  # Convert to W
            except (KeyError, ValueError):
                continue

        # Generate interval forecast
        result = []
        current_time = start_time

        for _ in range(n_intervals):
            # Find closest forecast
            closest_time = min(
                forecast_by_time.keys(),
                key=lambda t: abs((t - current_time).total_seconds()),
                default=None,
            )

            if closest_time and abs((closest_time - current_time).total_seconds()) < 3600:
                result.append(forecast_by_time[closest_time])
            else:
                result.append(0.0)

            current_time += timedelta(minutes=self.interval_minutes)

        return result

    def _generate_default_solar_curve(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Generate a default solar production curve.

        Uses a simple bell curve centered at noon with seasonal adjustment.
        """
        forecast = []
        current_time = start_time

        # Assume 5kW peak system as default
        peak_power = 5000

        for _ in range(n_intervals):
            hour = current_time.hour + current_time.minute / 60.0

            # Simple bell curve: sunrise ~6am, sunset ~6pm, peak at noon
            if 6 <= hour <= 18:
                # Normalized position in day (0 at 6am, 1 at 6pm)
                t = (hour - 6) / 12

                # Bell curve: peak at t=0.5 (noon)
                # Using cosine for smooth curve
                import math
                solar_factor = math.sin(t * math.pi)
                solar_factor = max(0, solar_factor)

                # Seasonal adjustment (simple - assume ~80% of peak)
                forecast.append(peak_power * solar_factor * 0.8)
            else:
                forecast.append(0.0)

            current_time += timedelta(minutes=self.interval_minutes)

        return forecast
