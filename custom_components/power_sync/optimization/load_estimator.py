"""
Load estimation for battery optimization.

Supports multiple forecast sources:
1. HAFO (Home Assistant Forecaster) - ML-based forecasting from hafo.haeo.io
2. Local pattern-based estimation from Home Assistant history

HAFO provides superior forecasting by analyzing historical patterns with ML,
but falls back to local estimation if HAFO is not installed.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import HAFO_DOMAIN, HAFO_LOAD_SENSOR_PREFIX

_LOGGER = logging.getLogger(__name__)


class HAFOForecaster:
    """
    HAFO (Home Assistant Forecaster) integration for load prediction.

    HAFO is a Home Assistant integration that creates forecast sensors from
    entity history using ML-based pattern recognition. It provides superior
    load forecasting compared to simple historical averaging.

    Reference: https://hafo.haeo.io/
    """

    def __init__(
        self,
        hass: HomeAssistant,
        load_entity_id: str | None = None,
        interval_minutes: int = 5,
    ):
        """
        Initialize HAFO forecaster.

        Args:
            hass: Home Assistant instance
            load_entity_id: Source entity ID for load (HAFO creates forecast from this)
            interval_minutes: Forecast interval in minutes
        """
        self.hass = hass
        self.load_entity_id = load_entity_id
        self.interval_minutes = interval_minutes
        self._hafo_sensor_id: str | None = None

    def is_available(self) -> bool:
        """Check if HAFO integration is installed and configured."""
        # Check if HAFO domain is loaded
        if HAFO_DOMAIN not in self.hass.config.components:
            return False

        # Check if we have a HAFO forecast sensor for our load entity
        if self.load_entity_id:
            self._hafo_sensor_id = self._find_hafo_sensor()
            return self._hafo_sensor_id is not None

        return False

    def _find_hafo_sensor(self) -> str | None:
        """Find the HAFO forecast sensor for the load entity."""
        if not self.load_entity_id:
            return None

        # HAFO creates sensors with naming pattern based on source entity
        # Try common patterns
        base_name = self.load_entity_id.replace("sensor.", "").replace(".", "_")

        potential_sensors = [
            f"{HAFO_LOAD_SENSOR_PREFIX}{base_name}_forecast",
            f"{HAFO_LOAD_SENSOR_PREFIX}{base_name}",
            f"sensor.{base_name}_forecast",
            # Also check for PowerSync-specific HAFO sensor
            f"{HAFO_LOAD_SENSOR_PREFIX}powersync_load_forecast",
            f"{HAFO_LOAD_SENSOR_PREFIX}home_load_forecast",
        ]

        for sensor_id in potential_sensors:
            state = self.hass.states.get(sensor_id)
            if state and state.state not in ("unknown", "unavailable"):
                _LOGGER.info(f"Found HAFO load forecast sensor: {sensor_id}")
                return sensor_id

        # Search all HAFO sensors
        for state in self.hass.states.async_all():
            if state.entity_id.startswith(HAFO_LOAD_SENSOR_PREFIX) and "load" in state.entity_id.lower():
                _LOGGER.info(f"Found HAFO load sensor: {state.entity_id}")
                return state.entity_id

        return None

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float] | None:
        """
        Get load forecast from HAFO sensor.

        HAFO sensors store forecast data in the 'forecast' attribute as a list of
        {"datetime": "ISO8601", "value": float} objects.

        Args:
            horizon_hours: Forecast horizon in hours
            start_time: Start time for forecast (default: now)

        Returns:
            List of load values in Watts, or None if unavailable
        """
        if not self._hafo_sensor_id:
            self._hafo_sensor_id = self._find_hafo_sensor()

        if not self._hafo_sensor_id:
            return None

        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        try:
            state = self.hass.states.get(self._hafo_sensor_id)
            if not state or state.state in ("unknown", "unavailable"):
                return None

            # Get forecast attribute (standard Home Assistant forecast format)
            forecast_data = state.attributes.get("forecast", [])

            if not forecast_data:
                # Try alternative attribute names
                forecast_data = (
                    state.attributes.get("forecasts", []) or
                    state.attributes.get("predictions", []) or
                    state.attributes.get("values", [])
                )

            if not forecast_data:
                _LOGGER.debug(f"HAFO sensor {self._hafo_sensor_id} has no forecast data")
                return None

            return self._parse_hafo_forecast(forecast_data, start_time, n_intervals)

        except Exception as e:
            _LOGGER.warning(f"Error reading HAFO forecast: {e}")
            return None

    def _parse_hafo_forecast(
        self,
        forecast_data: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Parse HAFO forecast data into interval values.

        HAFO forecast format (standard HA forecast):
        [
            {"datetime": "2024-01-01T00:00:00+00:00", "native_value": 1500.0},
            {"datetime": "2024-01-01T00:30:00+00:00", "native_value": 1450.0},
            ...
        ]

        Or alternative format:
        [
            {"time": "2024-01-01T00:00:00", "value": 1500.0},
            ...
        ]
        """
        # Build time-indexed lookup
        forecast_by_time: dict[datetime, float] = {}

        for item in forecast_data:
            try:
                # Try different datetime field names
                time_str = (
                    item.get("datetime") or
                    item.get("time") or
                    item.get("timestamp") or
                    item.get("period_end")
                )

                if not time_str:
                    continue

                if isinstance(time_str, datetime):
                    item_time = time_str
                else:
                    item_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))

                # Try different value field names
                value = (
                    item.get("native_value") or
                    item.get("value") or
                    item.get("load") or
                    item.get("power") or
                    0.0
                )

                if value is not None:
                    # Ensure value is in Watts
                    value = float(value)
                    # If value seems to be in kW (< 50), convert to W
                    if 0 < value < 50:
                        value *= 1000

                    forecast_by_time[item_time] = value

            except (ValueError, TypeError, KeyError) as e:
                _LOGGER.debug(f"Error parsing HAFO forecast item: {e}")
                continue

        if not forecast_by_time:
            _LOGGER.warning("HAFO forecast data could not be parsed")
            return []

        # Generate interval forecast
        result = []
        current_time = start_time
        sorted_times = sorted(forecast_by_time.keys())

        for _ in range(n_intervals):
            # Find the closest forecast time
            closest_time = None
            min_diff = float('inf')

            for ft in sorted_times:
                diff = abs((ft - current_time).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    closest_time = ft

            if closest_time and min_diff < 3600:  # Within 1 hour
                result.append(forecast_by_time[closest_time])
            elif result:
                # Use last known value
                result.append(result[-1])
            else:
                # Default fallback
                result.append(500.0)

            current_time += timedelta(minutes=self.interval_minutes)

        _LOGGER.debug(f"HAFO forecast: {len(result)} intervals, avg={sum(result)/len(result):.0f}W")
        return result


class LoadEstimator:
    """
    Estimate household load forecast from multiple sources.

    Priority order:
    1. HAFO (Home Assistant Forecaster) - ML-based, most accurate
    2. Historical pattern matching from Home Assistant recorder
    3. Simple pattern-based fallback

    The estimator queries HAFO first for ML-based forecasts, then falls back
    to local pattern matching if HAFO is not available.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        load_entity_id: str | None = None,
        interval_minutes: int = 5,
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

        # Initialize HAFO forecaster
        self._hafo = HAFOForecaster(hass, load_entity_id, interval_minutes)
        self._hafo_available: bool | None = None

    @property
    def hafo_available(self) -> bool:
        """Check if HAFO is available for load forecasting."""
        if self._hafo_available is None:
            self._hafo_available = self._hafo.is_available()
        return self._hafo_available

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float]:
        """
        Generate load forecast in Watts for each interval.

        Tries HAFO first (ML-based), then falls back to historical patterns.

        Args:
            horizon_hours: Forecast horizon in hours
            start_time: Start time for forecast (default: now)

        Returns:
            List of load values in Watts for each interval
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        # Try HAFO first (ML-based forecasting)
        if self.hafo_available:
            try:
                hafo_forecast = await self._hafo.get_forecast(horizon_hours, start_time)
                if hafo_forecast and len(hafo_forecast) >= n_intervals * 0.5:
                    _LOGGER.debug("Using HAFO for load forecast")
                    # Pad if needed
                    while len(hafo_forecast) < n_intervals:
                        hafo_forecast.append(hafo_forecast[-1] if hafo_forecast else 500.0)
                    return hafo_forecast[:n_intervals]
            except Exception as e:
                _LOGGER.warning(f"HAFO forecast failed: {e}")

        # Fallback to historical pattern
        try:
            history = await self._get_load_history(days=7)
            if history:
                _LOGGER.debug("Using historical pattern for load forecast")
                return self._forecast_from_history(history, start_time, n_intervals)
        except Exception as e:
            _LOGGER.warning(f"Failed to get load history: {e}")

        # Final fallback: use current load or default
        _LOGGER.debug("Using simple forecast fallback")
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
        interval_minutes: int = 5,
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
            # First, try the Solcast Solar integration (solcast_solar domain)
            # This is the preferred source as it's a dedicated integration
            solcast_solar_data = self.hass.data.get("solcast_solar")
            if solcast_solar_data:
                forecast = self._extract_from_solcast_solar_integration(
                    solcast_solar_data, start_time, n_intervals
                )
                if forecast:
                    _LOGGER.debug("Using solar forecast from Solcast Solar integration")
                    return forecast

            # Fallback: Check for Solcast data in PowerSync's own coordinator
            from ..const import DOMAIN

            domain_data = self.hass.data.get(DOMAIN, {})

            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Try solcast_coordinator first (primary source)
                solcast_coordinator = entry_data.get("solcast_coordinator")
                if solcast_coordinator and solcast_coordinator.data:
                    coordinator_data = solcast_coordinator.data

                    # Check for raw forecast periods (preferred - full 48h data)
                    forecasts = coordinator_data.get("forecasts")
                    if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
                        return self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )

                    # Fallback to hourly_forecast (processed format from Solcast HA integration)
                    hourly = coordinator_data.get("hourly_forecast")
                    if hourly and isinstance(hourly, list) and len(hourly) > 0:
                        return self._parse_hourly_forecast(
                            hourly,
                            start_time,
                            n_intervals,
                        )

                # Fallback to solcast_forecast key
                solcast_data = entry_data.get("solcast_forecast")
                if solcast_data:
                    forecasts = solcast_data.get("forecasts")
                    if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
                        return self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )

            return None

        except Exception as e:
            _LOGGER.warning(f"Could not get Solcast forecast: {e}")
            return None

    def _extract_from_solcast_solar_integration(
        self,
        solcast_data: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Extract forecast data from the Solcast Solar integration (solcast_solar domain).

        The Solcast Solar integration stores data in various formats depending on version.
        """
        try:
            # The integration may store a coordinator or direct data
            # Try common data structures used by solcast_solar integration

            # Check if it's a coordinator with data attribute
            if hasattr(solcast_data, 'data') and solcast_data.data:
                data = solcast_data.data
            elif isinstance(solcast_data, dict):
                data = solcast_data
            else:
                # Try to find coordinator in the data structure
                for key, value in (solcast_data.items() if hasattr(solcast_data, 'items') else []):
                    if hasattr(value, 'data') and value.data:
                        data = value.data
                        break
                    if isinstance(value, dict) and ('forecasts' in value or 'detailedForecast' in value):
                        data = value
                        break
                else:
                    return None

            # Try various forecast formats used by solcast_solar
            # Format 1: detailedForecast (list of period dicts with pv_estimate)
            detailed = data.get('detailedForecast') if isinstance(data, dict) else None
            if detailed and isinstance(detailed, list) and len(detailed) > 0:
                return self._parse_detailed_forecast(detailed, start_time, n_intervals)

            # Format 2: forecasts (raw API response format)
            forecasts = data.get('forecasts') if isinstance(data, dict) else None
            if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
                return self._parse_solcast_data(forecasts, start_time, n_intervals)

            # Format 3: forecast_today / forecast_tomorrow
            forecast_today = data.get('forecast_today', []) if isinstance(data, dict) else []
            forecast_tomorrow = data.get('forecast_tomorrow', []) if isinstance(data, dict) else []
            combined = forecast_today + forecast_tomorrow
            if combined:
                return self._parse_solcast_data(combined, start_time, n_intervals)

            return None

        except Exception as e:
            _LOGGER.debug(f"Could not extract from Solcast Solar integration: {e}")
            return None

    def _parse_detailed_forecast(
        self,
        detailed: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse detailedForecast format from Solcast Solar integration."""
        # Build a time-indexed lookup of power values
        forecast_by_time: dict[datetime, float] = {}

        for item in detailed:
            try:
                # Parse period_end timestamp
                period_end_str = item.get('period_end')
                if not period_end_str:
                    continue

                if isinstance(period_end_str, datetime):
                    period_end = period_end_str
                else:
                    period_end = datetime.fromisoformat(period_end_str.replace('Z', '+00:00'))

                # Get power estimate (could be pv_estimate, pv_estimate10, pv_estimate90)
                pv_kw = item.get('pv_estimate', 0) or item.get('pv_estimate50', 0) or 0
                forecast_by_time[period_end] = pv_kw * 1000  # Convert kW to W

            except (KeyError, ValueError, TypeError) as e:
                _LOGGER.debug(f"Error parsing forecast item: {e}")
                continue

        if not forecast_by_time:
            return []

        # Generate interval forecast
        result = []
        current_time = start_time
        sorted_times = sorted(forecast_by_time.keys())

        for _ in range(n_intervals):
            # Find the closest forecast time
            power_w = 0.0
            for ft in sorted_times:
                if ft >= current_time:
                    power_w = forecast_by_time[ft]
                    break

            result.append(power_w)
            current_time += timedelta(minutes=self.interval_minutes)

        return result

    def _parse_hourly_forecast(
        self,
        hourly: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse hourly forecast format (from Solcast HA integration) into interval values.

        This format has: time (HH:MM), hour (int), pv_estimate_kw (float)
        """
        # Build lookup by hour
        forecast_by_hour: dict[int, float] = {}
        for item in hourly:
            try:
                hour = item.get("hour", 0)
                pv_kw = item.get("pv_estimate_kw", 0) or 0
                forecast_by_hour[hour] = pv_kw * 1000  # Convert kW to W
            except (KeyError, ValueError, TypeError):
                continue

        # Generate interval forecast
        result = []
        current_time = start_time

        for _ in range(n_intervals):
            hour = current_time.hour
            # Use the hour's value, or 0 if not available (likely nighttime or future day)
            result.append(forecast_by_hour.get(hour, 0.0))
            current_time += timedelta(minutes=self.interval_minutes)

        return result

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
