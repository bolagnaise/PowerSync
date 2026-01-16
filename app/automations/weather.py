"""
OpenWeatherMap integration for weather-based automation triggers.

Provides weather condition classification:
- sunny: Clear sky, few clouds
- partly_sunny: Scattered/broken clouds
- cloudy: Overcast, rain, storms
"""

import logging
import os
import requests
from typing import Dict, Any, Optional

from app.models import User

_LOGGER = logging.getLogger(__name__)

# OpenWeatherMap API configuration
OPENWEATHERMAP_API_KEY = os.environ.get('OPENWEATHERMAP_API_KEY', '')
OPENWEATHERMAP_BASE_URL = 'https://api.openweathermap.org/data/2.5/weather'

# Default coordinates (Brisbane, Australia) - used if user location unknown
DEFAULT_LAT = -27.4698
DEFAULT_LON = 153.0251

# Weather condition ID ranges from OpenWeatherMap
# https://openweathermap.org/weather-conditions
WEATHER_CONDITION_MAP = {
    # Thunderstorm (2xx) -> cloudy
    range(200, 300): 'cloudy',
    # Drizzle (3xx) -> cloudy
    range(300, 400): 'cloudy',
    # Rain (5xx) -> cloudy
    range(500, 600): 'cloudy',
    # Snow (6xx) -> cloudy
    range(600, 700): 'cloudy',
    # Atmosphere (7xx) - mist, fog, etc. -> partly_sunny
    range(700, 800): 'partly_sunny',
    # Clear (800) -> sunny
    range(800, 801): 'sunny',
    # Clouds (801-804)
    range(801, 803): 'partly_sunny',  # Few/scattered clouds
    range(803, 805): 'cloudy',  # Broken/overcast clouds
}


def get_current_weather(user: User) -> Optional[Dict[str, Any]]:
    """
    Get current weather conditions for a user's location.

    Args:
        user: User to get weather for (uses timezone to estimate location)

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
    if not OPENWEATHERMAP_API_KEY:
        _LOGGER.warning("OpenWeatherMap API key not configured")
        return None

    # Get coordinates based on user's timezone
    lat, lon = _get_coordinates_for_user(user)

    try:
        response = requests.get(
            OPENWEATHERMAP_BASE_URL,
            params={
                'lat': lat,
                'lon': lon,
                'appid': OPENWEATHERMAP_API_KEY,
                'units': 'metric',
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        # Extract weather info
        weather_list = data.get('weather', [])
        if not weather_list:
            return None

        weather = weather_list[0]
        weather_id = weather.get('id', 0)

        # Classify condition
        condition = _classify_weather_condition(weather_id)

        return {
            'condition': condition,
            'description': weather.get('description', ''),
            'temperature_c': data.get('main', {}).get('temp'),
            'humidity': data.get('main', {}).get('humidity'),
            'cloud_cover': data.get('clouds', {}).get('all', 0),
            'weather_id': weather_id,
        }

    except requests.RequestException as e:
        _LOGGER.error(f"Failed to fetch weather: {e}")
        return None
    except (KeyError, ValueError) as e:
        _LOGGER.error(f"Failed to parse weather response: {e}")
        return None


def _get_coordinates_for_user(user: User) -> tuple:
    """
    Get approximate coordinates based on user's timezone.

    This is a rough approximation - for better accuracy, users could
    configure their location explicitly.
    """
    timezone = user.timezone or 'Australia/Brisbane'

    # Map Australian timezones to approximate coordinates
    timezone_coords = {
        'Australia/Brisbane': (-27.47, 153.03),
        'Australia/Sydney': (-33.87, 151.21),
        'Australia/Melbourne': (-37.81, 144.96),
        'Australia/Adelaide': (-34.93, 138.60),
        'Australia/Perth': (-31.95, 115.86),
        'Australia/Darwin': (-12.46, 130.84),
        'Australia/Hobart': (-42.88, 147.33),
        'Australia/Canberra': (-35.28, 149.13),
    }

    return timezone_coords.get(timezone, (DEFAULT_LAT, DEFAULT_LON))


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
    return 'partly_sunny'


def get_weather_forecast(user: User, hours: int = 24) -> Optional[list]:
    """
    Get weather forecast for the next N hours.

    This could be used for more advanced automation planning.
    Currently not used but available for future enhancements.

    Args:
        user: User to get forecast for
        hours: Number of hours to forecast (max 120)

    Returns:
        List of forecast periods, or None if unavailable
    """
    if not OPENWEATHERMAP_API_KEY:
        return None

    lat, lon = _get_coordinates_for_user(user)

    try:
        # Use 3-hour forecast API (free tier)
        response = requests.get(
            'https://api.openweathermap.org/data/2.5/forecast',
            params={
                'lat': lat,
                'lon': lon,
                'appid': OPENWEATHERMAP_API_KEY,
                'units': 'metric',
                'cnt': min(hours // 3, 40),  # Max 40 periods (5 days)
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        forecast_list = []
        for item in data.get('list', []):
            weather = item.get('weather', [{}])[0]
            weather_id = weather.get('id', 0)

            forecast_list.append({
                'timestamp': item.get('dt_txt'),
                'condition': _classify_weather_condition(weather_id),
                'description': weather.get('description', ''),
                'temperature_c': item.get('main', {}).get('temp'),
                'cloud_cover': item.get('clouds', {}).get('all', 0),
            })

        return forecast_list

    except requests.RequestException as e:
        _LOGGER.error(f"Failed to fetch weather forecast: {e}")
        return None
