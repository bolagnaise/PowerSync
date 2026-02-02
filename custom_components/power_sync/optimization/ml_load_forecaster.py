"""
ML-based load forecasting for battery optimization.

Provides advanced load forecasting using:
1. Feature engineering (time-of-day, day-of-week, holidays)
2. Weather-adjusted predictions (temperature, cloud cover)
3. Online learning with exponential weighted averages
4. Optional Prophet integration for complex seasonality

This enhances the basic pattern-matching in load_estimator.py with
machine learning techniques for improved accuracy.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Optional dependency
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Australian public holidays (simplified - major ones)
AUSTRALIAN_HOLIDAYS = {
    (1, 1),   # New Year's Day
    (1, 26),  # Australia Day
    (4, 25),  # ANZAC Day
    (12, 25), # Christmas
    (12, 26), # Boxing Day
}


@dataclass
class WeatherFeatures:
    """Weather features for load prediction."""
    temperature_c: float | None = None
    cloud_cover_percent: float | None = None  # 0-100
    humidity_percent: float | None = None
    is_night: bool = False
    condition: str = "unknown"  # sunny, partly_sunny, cloudy

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature_c": self.temperature_c,
            "cloud_cover_percent": self.cloud_cover_percent,
            "humidity_percent": self.humidity_percent,
            "is_night": self.is_night,
            "condition": self.condition,
        }


@dataclass
class LoadFeatures:
    """Features extracted for load prediction."""
    hour: int
    minute: int
    day_of_week: int  # 0=Monday, 6=Sunday
    is_weekend: bool
    is_holiday: bool
    month: int
    season: str  # summer, autumn, winter, spring
    weather: WeatherFeatures = field(default_factory=WeatherFeatures)

    def to_array(self) -> np.ndarray:
        """Convert to feature array for ML model."""
        # Cyclical encoding for hour and day
        hour_sin = math.sin(2 * math.pi * self.hour / 24)
        hour_cos = math.cos(2 * math.pi * self.hour / 24)
        dow_sin = math.sin(2 * math.pi * self.day_of_week / 7)
        dow_cos = math.cos(2 * math.pi * self.day_of_week / 7)
        month_sin = math.sin(2 * math.pi * self.month / 12)
        month_cos = math.cos(2 * math.pi * self.month / 12)

        features = [
            hour_sin, hour_cos,
            dow_sin, dow_cos,
            month_sin, month_cos,
            1.0 if self.is_weekend else 0.0,
            1.0 if self.is_holiday else 0.0,
        ]

        # Weather features (normalized)
        if self.weather.temperature_c is not None:
            # Normalize temperature: assume range -5 to 45 C
            features.append((self.weather.temperature_c + 5) / 50)
        else:
            features.append(0.5)  # Default mid-range

        if self.weather.cloud_cover_percent is not None:
            features.append(self.weather.cloud_cover_percent / 100)
        else:
            features.append(0.5)

        return np.array(features)


class MLLoadEstimator:
    """
    Machine Learning-based load estimator.

    Uses a combination of:
    1. Historical pattern averages (fast, lightweight)
    2. Exponential weighted moving average for trend adaptation
    3. Weather-based adjustments for temperature-dependent loads
    4. Holiday/weekend adjustments

    Optionally uses Prophet if available for complex seasonality.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        load_entity_id: str | None = None,
        interval_minutes: int = 30,
        weather_entity_id: str | None = None,
        use_prophet: bool = False,
    ):
        """
        Initialize the ML load estimator.

        Args:
            hass: Home Assistant instance
            load_entity_id: Entity ID for load sensor
            interval_minutes: Forecast interval in minutes
            weather_entity_id: Optional weather entity for temperature data
            use_prophet: Whether to use Prophet for forecasting (requires fbprophet)
        """
        self.hass = hass
        self.load_entity_id = load_entity_id
        self.interval_minutes = interval_minutes
        self.weather_entity_id = weather_entity_id
        self.use_prophet = use_prophet

        # Model state
        self._pattern_weights: dict[tuple, list[float]] = defaultdict(list)
        self._ewma_alpha = 0.3  # Exponential smoothing factor
        self._temperature_coefficients: dict[str, float] = {
            "heating": 50.0,   # W per degree below comfort
            "cooling": 100.0,  # W per degree above comfort
            "comfort_min": 18.0,
            "comfort_max": 24.0,
        }

        # Cache
        self._history_cache: list[tuple[datetime, float]] = []
        self._cache_time: datetime | None = None
        self._cache_duration = timedelta(hours=1)
        self._prophet_model = None
        self._model_trained = False

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
        weather_forecast: list[WeatherFeatures] | None = None,
    ) -> list[float]:
        """
        Generate ML-enhanced load forecast.

        Args:
            horizon_hours: Forecast horizon in hours
            start_time: Start time for forecast
            weather_forecast: Optional weather forecast for each interval

        Returns:
            List of forecasted load values in Watts
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        # Load historical data and train/update model
        history = await self._get_load_history(days=14)

        if history:
            # Update pattern weights from history
            self._update_pattern_weights(history)

        # Get current weather if available
        current_weather = await self._get_current_weather()

        # Generate forecast
        forecast = []
        current_time = start_time

        for i in range(n_intervals):
            # Extract features for this interval
            features = self._extract_features(
                current_time,
                weather_forecast[i] if weather_forecast and i < len(weather_forecast) else current_weather,
            )

            # Get base prediction from pattern matching
            base_load = self._predict_from_patterns(features)

            # Apply weather adjustment
            weather_adjustment = self._calculate_weather_adjustment(features.weather)
            adjusted_load = base_load + weather_adjustment

            # Apply weekend/holiday adjustment
            if features.is_weekend:
                adjusted_load *= 1.1  # 10% higher on weekends (people home)
            if features.is_holiday:
                adjusted_load *= 1.15  # 15% higher on holidays

            forecast.append(max(0, adjusted_load))
            current_time += timedelta(minutes=self.interval_minutes)

        # Apply smoothing
        forecast = self._smooth_forecast(forecast)

        return forecast

    async def _get_load_history(self, days: int = 14) -> list[tuple[datetime, float]]:
        """Get historical load data from Home Assistant recorder."""
        if not self.load_entity_id:
            return []

        # Check cache
        now = dt_util.utcnow()
        if (
            self._cache_time
            and now - self._cache_time < self._cache_duration
            and self._history_cache
        ):
            return self._history_cache

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance = get_instance(self.hass)
            start_time = now - timedelta(days=days)
            end_time = now

            history = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start_time,
                end_time,
                [self.load_entity_id],
            )

            if not history or self.load_entity_id not in history:
                return []

            result = []
            for state in history[self.load_entity_id]:
                try:
                    value = float(state.state)
                    if value >= 0:
                        result.append((state.last_changed, value))
                except (ValueError, TypeError):
                    continue

            self._history_cache = result
            self._cache_time = now

            _LOGGER.debug(f"ML Estimator loaded {len(result)} history points")
            return result

        except Exception as e:
            _LOGGER.warning(f"Failed to get load history: {e}")
            return []

    def _update_pattern_weights(self, history: list[tuple[datetime, float]]) -> None:
        """Update pattern weights using exponential weighted moving average."""
        # Group by (day_of_week, hour, half_hour)
        for timestamp, value in history:
            dow = timestamp.weekday()
            hour = timestamp.hour
            half_hour = 0 if timestamp.minute < 30 else 1
            key = (dow, hour, half_hour)

            # EWMA update
            if key in self._pattern_weights and self._pattern_weights[key]:
                old_avg = sum(self._pattern_weights[key]) / len(self._pattern_weights[key])
                new_value = self._ewma_alpha * value + (1 - self._ewma_alpha) * old_avg
                self._pattern_weights[key].append(new_value)
                # Keep only recent values (last 50)
                if len(self._pattern_weights[key]) > 50:
                    self._pattern_weights[key] = self._pattern_weights[key][-50:]
            else:
                self._pattern_weights[key].append(value)

    def _extract_features(
        self,
        timestamp: datetime,
        weather: WeatherFeatures | None = None,
    ) -> LoadFeatures:
        """Extract features for a given timestamp."""
        month = timestamp.month
        day = timestamp.day

        # Determine season (Southern Hemisphere - Australia)
        if month in [12, 1, 2]:
            season = "summer"
        elif month in [3, 4, 5]:
            season = "autumn"
        elif month in [6, 7, 8]:
            season = "winter"
        else:
            season = "spring"

        # Check for holidays
        is_holiday = (month, day) in AUSTRALIAN_HOLIDAYS

        return LoadFeatures(
            hour=timestamp.hour,
            minute=timestamp.minute,
            day_of_week=timestamp.weekday(),
            is_weekend=timestamp.weekday() >= 5,
            is_holiday=is_holiday,
            month=month,
            season=season,
            weather=weather or WeatherFeatures(),
        )

    def _predict_from_patterns(self, features: LoadFeatures) -> float:
        """Get base load prediction from pattern weights."""
        half_hour = 0 if features.minute < 30 else 1
        key = (features.day_of_week, features.hour, half_hour)

        if key in self._pattern_weights and self._pattern_weights[key]:
            return sum(self._pattern_weights[key]) / len(self._pattern_weights[key])

        # Fallback: try same hour any day
        fallback_values = []
        for dow in range(7):
            fallback_key = (dow, features.hour, half_hour)
            if fallback_key in self._pattern_weights:
                values = self._pattern_weights[fallback_key]
                if values:
                    fallback_values.extend(values)

        if fallback_values:
            return sum(fallback_values) / len(fallback_values)

        # Final fallback: default residential profile
        return self._get_default_load(features.hour)

    def _get_default_load(self, hour: int) -> float:
        """Get default load for an hour based on typical residential profile."""
        # Default base load 500W with hourly pattern
        hourly_pattern = {
            0: 0.4, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.3, 5: 0.4,
            6: 0.6, 7: 0.8, 8: 0.9, 9: 0.8, 10: 0.7, 11: 0.7,
            12: 0.8, 13: 0.7, 14: 0.6, 15: 0.6, 16: 0.7, 17: 0.9,
            18: 1.2, 19: 1.3, 20: 1.2, 21: 1.0, 22: 0.7, 23: 0.5,
        }
        return 500.0 * hourly_pattern.get(hour, 0.7)

    def _calculate_weather_adjustment(self, weather: WeatherFeatures) -> float:
        """Calculate load adjustment based on weather conditions."""
        adjustment = 0.0

        if weather.temperature_c is not None:
            temp = weather.temperature_c
            comfort_min = self._temperature_coefficients["comfort_min"]
            comfort_max = self._temperature_coefficients["comfort_max"]

            if temp < comfort_min:
                # Heating load
                degrees_below = comfort_min - temp
                adjustment += degrees_below * self._temperature_coefficients["heating"]
            elif temp > comfort_max:
                # Cooling load (AC)
                degrees_above = temp - comfort_max
                adjustment += degrees_above * self._temperature_coefficients["cooling"]

        return adjustment

    async def _get_current_weather(self) -> WeatherFeatures:
        """Get current weather from Home Assistant."""
        weather = WeatherFeatures()

        try:
            # Try to get weather from weather entity
            if self.weather_entity_id:
                state = self.hass.states.get(self.weather_entity_id)
                if state:
                    weather.temperature_c = state.attributes.get("temperature")
                    weather.humidity_percent = state.attributes.get("humidity")
                    weather.condition = state.state

            # Try to get from power_sync weather data
            from ..const import DOMAIN
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_data in domain_data.values():
                if isinstance(entry_data, dict) and "weather" in entry_data:
                    w = entry_data["weather"]
                    if isinstance(w, dict):
                        weather.temperature_c = w.get("temperature_c", weather.temperature_c)
                        weather.cloud_cover_percent = w.get("cloud_cover")
                        weather.humidity_percent = w.get("humidity", weather.humidity_percent)
                        weather.is_night = w.get("is_night", False)
                        weather.condition = w.get("condition", weather.condition)
                    break

        except Exception as e:
            _LOGGER.debug(f"Could not get weather data: {e}")

        return weather

    def _smooth_forecast(self, values: list[float], window: int = 3) -> list[float]:
        """Apply simple moving average smoothing."""
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

        avg_power = sum(v for _, v in history) / len(history)
        return avg_power * 24 / 1000

    def get_model_stats(self) -> dict[str, Any]:
        """Get statistics about the ML model."""
        return {
            "pattern_keys": len(self._pattern_weights),
            "total_samples": sum(len(v) for v in self._pattern_weights.values()),
            "ewma_alpha": self._ewma_alpha,
            "cache_age_minutes": (
                (dt_util.utcnow() - self._cache_time).total_seconds() / 60
                if self._cache_time else None
            ),
        }


class WeatherAdjustedForecaster:
    """
    Combines load forecasting with weather forecast integration.

    Fetches weather forecasts and adjusts load predictions based on
    temperature-driven HVAC loads.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ml_estimator: MLLoadEstimator,
        weather_api_key: str | None = None,
        location: tuple[float, float] | None = None,  # (lat, lon)
    ):
        self.hass = hass
        self.ml_estimator = ml_estimator
        self.weather_api_key = weather_api_key
        self.location = location
        self._weather_cache: dict[datetime, WeatherFeatures] = {}
        self._cache_time: datetime | None = None

    async def get_forecast_with_weather(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> tuple[list[float], list[WeatherFeatures]]:
        """
        Get load forecast with weather adjustments.

        Returns:
            Tuple of (load_forecast, weather_forecast)
        """
        if start_time is None:
            start_time = dt_util.now()

        # Get weather forecast
        weather_forecast = await self._get_weather_forecast(horizon_hours, start_time)

        # Get load forecast with weather adjustment
        load_forecast = await self.ml_estimator.get_forecast(
            horizon_hours=horizon_hours,
            start_time=start_time,
            weather_forecast=weather_forecast,
        )

        return load_forecast, weather_forecast

    async def _get_weather_forecast(
        self,
        horizon_hours: int,
        start_time: datetime,
    ) -> list[WeatherFeatures]:
        """Get weather forecast for the horizon period."""
        n_intervals = horizon_hours * 60 // self.ml_estimator.interval_minutes
        forecast = []

        # Try to get from OpenWeatherMap if API key available
        if self.weather_api_key and self.location:
            try:
                owm_forecast = await self._fetch_openweathermap_forecast()
                if owm_forecast:
                    return self._interpolate_weather_forecast(owm_forecast, start_time, n_intervals)
            except Exception as e:
                _LOGGER.warning(f"Failed to fetch weather forecast: {e}")

        # Fallback: use current weather for entire horizon
        current_weather = await self.ml_estimator._get_current_weather()
        return [current_weather] * n_intervals

    async def _fetch_openweathermap_forecast(self) -> list[dict[str, Any]]:
        """Fetch forecast from OpenWeatherMap API."""
        import aiohttp

        if not self.weather_api_key or not self.location:
            return []

        lat, lon = self.location
        url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={self.weather_api_key}&units=metric"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("list", [])
        except Exception as e:
            _LOGGER.warning(f"OpenWeatherMap API error: {e}")

        return []

    def _interpolate_weather_forecast(
        self,
        owm_data: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[WeatherFeatures]:
        """Interpolate OWM 3-hour forecast to interval resolution."""
        # Parse OWM data
        weather_points: list[tuple[datetime, WeatherFeatures]] = []

        for item in owm_data:
            try:
                dt = datetime.fromtimestamp(item["dt"], tz=start_time.tzinfo)
                weather = WeatherFeatures(
                    temperature_c=item["main"]["temp"],
                    humidity_percent=item["main"]["humidity"],
                    cloud_cover_percent=item["clouds"]["all"],
                    condition=item["weather"][0]["main"].lower() if item.get("weather") else "unknown",
                )
                weather_points.append((dt, weather))
            except (KeyError, ValueError):
                continue

        if not weather_points:
            return [WeatherFeatures()] * n_intervals

        # Interpolate to intervals
        result = []
        current_time = start_time
        interval_minutes = self.ml_estimator.interval_minutes

        for _ in range(n_intervals):
            # Find surrounding weather points
            before = None
            after = None

            for i, (dt, w) in enumerate(weather_points):
                if dt <= current_time:
                    before = (dt, w)
                if dt > current_time and after is None:
                    after = (dt, w)
                    break

            if before and after:
                # Linear interpolation
                total_seconds = (after[0] - before[0]).total_seconds()
                if total_seconds > 0:
                    ratio = (current_time - before[0]).total_seconds() / total_seconds
                    weather = WeatherFeatures(
                        temperature_c=self._lerp(before[1].temperature_c, after[1].temperature_c, ratio),
                        humidity_percent=self._lerp(before[1].humidity_percent, after[1].humidity_percent, ratio),
                        cloud_cover_percent=self._lerp(before[1].cloud_cover_percent, after[1].cloud_cover_percent, ratio),
                        condition=before[1].condition,
                    )
                else:
                    weather = before[1]
            elif before:
                weather = before[1]
            elif after:
                weather = after[1]
            else:
                weather = WeatherFeatures()

            result.append(weather)
            current_time += timedelta(minutes=interval_minutes)

        return result

    def _lerp(self, a: float | None, b: float | None, t: float) -> float | None:
        """Linear interpolation."""
        if a is None or b is None:
            return a or b
        return a + (b - a) * t
