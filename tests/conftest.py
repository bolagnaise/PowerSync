"""Shared test fixtures for PowerSync tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# Melbourne timezone — used throughout PowerSync for NEM calculations
MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")

# Default controllable "now" for tests: 2026-01-15 14:00 AEDT (peak period)
DEFAULT_NOW = datetime(2026, 1, 15, 14, 0, 0, tzinfo=MELBOURNE_TZ)
DEFAULT_UTC = DEFAULT_NOW.astimezone(timezone.utc)


@pytest.fixture
def mock_hass() -> MagicMock:
    """Provide a minimal mock HomeAssistant instance.

    Sufficient for unit-testing pure logic modules that accept hass
    as a parameter but only use hass.data, hass.config, or hass.loop.
    """
    hass = MagicMock()
    hass.data = {}
    hass.config.time_zone = "Australia/Melbourne"
    hass.loop = None  # Set by caller if async tests need it
    return hass


@pytest.fixture
def mock_dt_util():
    """Patch homeassistant.util.dt so tests control time.

    Yields a MagicMock whose .now() returns DEFAULT_NOW and .utcnow()
    returns the UTC equivalent. Tests can override:

        mock_dt_util.now.return_value = custom_datetime
    """
    with patch("homeassistant.util.dt") as dt_mock:
        dt_mock.now.return_value = DEFAULT_NOW
        dt_mock.utcnow.return_value = DEFAULT_UTC
        dt_mock.as_local.side_effect = lambda dt: dt.astimezone(MELBOURNE_TZ)
        dt_mock.UTC = timezone.utc
        yield dt_mock


@pytest.fixture
def sample_prices() -> list[dict[str, Any]]:
    """Return 48 half-hour Amber-format price entries covering 24 hours.

    Price profile (c/kWh):
      00:00-06:00  — cheap overnight (5-8 c/kWh)
      06:00-15:00  — shoulder (15-25 c/kWh)
      15:00-21:00  — peak (30-55 c/kWh, spike at 18:00)
      21:00-24:00  — off-peak (10-15 c/kWh)

    Includes one negative price interval at 12:00 (-2 c/kWh) to test
    negative price handling.
    """
    base_date = datetime(2026, 1, 15, 0, 0, 0, tzinfo=MELBOURNE_TZ)
    prices: list[dict[str, Any]] = []

    # Price curve: index -> c/kWh
    price_curve = [
        # 00:00-06:00 (12 intervals) — cheap overnight
        5.2, 5.0, 4.8, 5.1, 5.5, 6.0, 6.2, 6.5, 7.0, 7.2, 7.5, 8.0,
        # 06:00-12:00 (12 intervals) — morning shoulder
        15.0, 16.5, 18.0, 19.5, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 24.5, -2.0,
        # 12:00-18:00 (12 intervals) — afternoon/peak
        22.0, 25.0, 28.0, 30.0, 32.0, 35.0, 38.0, 42.0, 48.0, 52.0, 55.0, 50.0,
        # 18:00-24:00 (12 intervals) — evening decline
        45.0, 38.0, 30.0, 25.0, 20.0, 15.0, 12.0, 11.0, 10.5, 10.0, 10.0, 10.0,
    ]

    for i, price_ckwh in enumerate(price_curve):
        interval_start = base_date + timedelta(minutes=30 * i)
        interval_end = interval_start + timedelta(minutes=30)

        prices.append({
            "type": "CurrentInterval" if i < 2 else "ForecastInterval",
            "channelType": "general",
            "perKwh": round(price_ckwh / 100, 6),  # Convert c/kWh to $/kWh
            "nemTime": interval_end.isoformat(),
            "startTime": interval_start.isoformat(),
            "duration": 30,
        })

    return prices
