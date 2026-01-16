"""
OpenWeatherMap integration for weather-based automation triggers.

Provides weather condition classification:
- sunny: Clear sky, few clouds
- partly_sunny: Scattered/broken clouds
- cloudy: Overcast, rain, storms
"""

import logging
from typing import Dict, Any, Optional

import aiohttp

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# OpenWeatherMap API configuration
OPENWEATHERMAP_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"

# Default coordinates (Brisbane, Australia) - used if user location unknown
DEFAULT_LAT = -27.4698
DEFAULT_LON = 153.0251

# Weather condition ID ranges from OpenWeatherMap
# https://openweathermap.org/weather-conditions
WEATHER_CONDITION_MAP = {
    # Thunderstorm (2xx) -> cloudy
    range(200, 300): "cloudy",
    # Drizzle (3xx) -> cloudy
    range(300, 400): "cloudy",
    # Rain (5xx) -> cloudy
    range(500, 600): "cloudy",
    # Snow (6xx) -> cloudy
    range(600, 700): "cloudy",
    # Atmosphere (7xx) - mist, fog, etc. -> partly_sunny
    range(700, 800): "partly_sunny",
    # Clear (800) -> sunny
    range(800, 801): "sunny",
    # Clouds (801-804)
    range(801, 803): "partly_sunny",  # Few/scattered clouds
    range(803, 805): "cloudy",  # Broken/overcast clouds
}

# Map Australian timezones to approximate coordinates
TIMEZONE_COORDS = {
    "Australia/Brisbane": (-27.47, 153.03),
    "Australia/Sydney": (-33.87, 151.21),
    "Australia/Melbourne": (-37.81, 144.96),
    "Australia/Adelaide": (-34.93, 138.60),
    "Australia/Perth": (-31.95, 115.86),
    "Australia/Darwin": (-12.46, 130.84),
    "Australia/Hobart": (-42.88, 147.33),
    "Australia/Canberra": (-35.28, 149.13),
}


async def async_get_current_weather(
    hass: HomeAssistant,
    api_key: str,
    timezone: str = "Australia/Brisbane"
) -> Optional[Dict[str, Any]]:
    """
    Get current weather conditions based on timezone.

    Args:
        hass: Home Assistant instance
        api_key: OpenWeatherMap API key
        timezone: User's timezone (used to estimate location)

    Returns:
        Dict with weather data:
        {
            'condition': 'sunny' | 'partly_sunny' | 'cloudy',
            'description': str,
            'temperature_c': float,
            'humidity': int,
            'cloud_cover': int,
            'weather_id': int,
        }
        Or None if weather data unavailable
    """
    if not api_key:
        _LOGGER.warning("OpenWeatherMap API key not configured")
        return None

    # Get coordinates based on timezone
    lat, lon = _get_coordinates_for_timezone(timezone)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OPENWEATHERMAP_BASE_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": api_key,
                    "units": "metric",
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                data = await response.json()

        # Extract weather info
        weather_list = data.get("weather", [])
        if not weather_list:
            return None

        weather = weather_list[0]
        weather_id = weather.get("id", 0)

        # Classify condition
        condition = _classify_weather_condition(weather_id)

        return {
            "condition": condition,
            "description": weather.get("description", ""),
            "temperature_c": data.get("main", {}).get("temp"),
            "humidity": data.get("main", {}).get("humidity"),
            "cloud_cover": data.get("clouds", {}).get("all", 0),
            "weather_id": weather_id,
        }

    except aiohttp.ClientError as e:
        _LOGGER.error(f"Failed to fetch weather: {e}")
        return None
    except (KeyError, ValueError) as e:
        _LOGGER.error(f"Failed to parse weather response: {e}")
        return None


def _get_coordinates_for_timezone(timezone: str) -> tuple:
    """
    Get approximate coordinates based on timezone.

    This is a rough approximation - for better accuracy, users could
    configure their location explicitly.
    """
    return TIMEZONE_COORDS.get(timezone, (DEFAULT_LAT, DEFAULT_LON))


def _classify_weather_condition(weather_id: int) -> str:
    """
    Classify OpenWeatherMap condition ID into simple categories.

    Args:
        weather_id: OpenWeatherMap condition ID

    Returns:
        'sunny', 'partly_sunny', or 'cloudy'
    """
    for id_range, condition in WEATHER_CONDITION_MAP.items():
        if weather_id in id_range:
            return condition

    # Default to partly_sunny for unknown conditions
    return "partly_sunny"
