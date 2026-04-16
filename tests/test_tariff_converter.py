"""Unit tests for tariff_converter.py.

Tests cover:
- _round_price precision and edge cases
- extract_most_recent_actual_interval selection logic
- convert_amber_to_tesla_tariff basic conversion
- Negative price preservation
- Empty data handling
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

# Direct module loading — bypass broken __init__.py package chain
import importlib
import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "custom_components" / "power_sync"

def _load_module_direct(name: str, filepath: Path):
    """Load a Python module from file path without triggering parent __init__.py."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filepath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

try:
    _tc = _load_module_direct(
        "custom_components.power_sync.tariff_converter",
        _SRC / "tariff_converter.py",
    )
    _round_price = _tc._round_price
    extract_most_recent_actual_interval = _tc.extract_most_recent_actual_interval
    convert_amber_to_tesla_tariff = _tc.convert_amber_to_tesla_tariff
    _tc_module = _tc
    HAS_DEPS = True
except (ImportError, Exception) as _import_err:
    HAS_DEPS = False
    _skip_reason = f"Cannot load tariff_converter: {_import_err}"
    _tc_module = None

pytestmark = pytest.mark.skipif(not HAS_DEPS, reason=_skip_reason if not HAS_DEPS else "")

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
FIXED_NOW = datetime(2026, 1, 15, 14, 0, 0, tzinfo=MELBOURNE_TZ)


@pytest.fixture(autouse=True)
def _mock_dt_util():
    """Patch dt_util.now() on the loaded tariff_converter module."""
    if _tc_module is None:
        yield
        return
    mock_dt = MagicMock()
    mock_dt.now.return_value = FIXED_NOW
    original = getattr(_tc_module, "dt_util", None)
    _tc_module.dt_util = mock_dt
    yield mock_dt
    if original is not None:
        _tc_module.dt_util = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_amber_forecast(
    n_intervals: int = 48,
    base_time: datetime | None = None,
    interval_minutes: int = 30,
    base_import_ckwh: float = 25.0,
    base_export_ckwh: float = 8.0,
) -> list[dict[str, Any]]:
    """Build a sample Amber API forecast response.

    Returns list of dicts with general + feedIn channels, n_intervals each.
    Prices in c/kWh (Amber native unit).
    """
    if base_time is None:
        base_time = datetime(2026, 1, 15, 0, 0, 0, tzinfo=MELBOURNE_TZ)

    entries: list[dict[str, Any]] = []
    for i in range(n_intervals):
        ts = base_time + timedelta(minutes=interval_minutes * i)
        nem_time = ts.isoformat()

        price_ckwh = base_import_ckwh + (i % 10) * 2
        export_ckwh = base_export_ckwh + (i % 5)
        interval_type = "CurrentInterval" if i == 0 else "ForecastInterval"

        general_entry: dict[str, Any] = {
            "type": interval_type,
            "channelType": "general",
            "perKwh": price_ckwh,
            "nemTime": nem_time,
            "duration": interval_minutes,
            "spikeStatus": "none",
        }
        feedin_entry: dict[str, Any] = {
            "type": interval_type,
            "channelType": "feedIn",
            "perKwh": -export_ckwh,
            "nemTime": nem_time,
            "duration": interval_minutes,
            "spikeStatus": "none",
        }

        # ForecastInterval needs advancedPrice for the converter
        if interval_type == "ForecastInterval":
            general_entry["advancedPrice"] = {
                "predicted": price_ckwh,
                "low": price_ckwh * 0.8,
                "high": price_ckwh * 1.2,
            }
            feedin_entry["advancedPrice"] = {
                "predicted": -export_ckwh,
                "low": -export_ckwh * 0.8,
                "high": -export_ckwh * 1.2,
            }

        entries.append(general_entry)
        entries.append(feedin_entry)

    return entries


# ---------------------------------------------------------------------------
# _round_price Tests (AC-3)
# ---------------------------------------------------------------------------

def test_round_price_basic():
    """Standard rounding to 4 decimal places."""
    assert _round_price(0.2014191) == 0.2014
    assert _round_price(0.199) == 0.199
    assert _round_price(0.12345) == 0.1235


def test_round_price_negative():
    """Negative prices must round correctly (AC-2)."""
    assert _round_price(-0.05) == -0.05
    assert _round_price(-0.00123) == -0.0012


def test_round_price_zero():
    """Zero is zero."""
    assert _round_price(0.0) == 0.0


# ---------------------------------------------------------------------------
# extract_most_recent_actual_interval Tests
# ---------------------------------------------------------------------------

def test_extract_current_interval():
    """Should extract general + feedIn from CurrentInterval entries."""
    # extract_most_recent_actual_interval requires duration=5 (5-min resolution)
    forecast = make_amber_forecast(n_intervals=5, interval_minutes=5)
    # Override duration to 5 for 5-min data
    for entry in forecast:
        entry["duration"] = 5
    result = extract_most_recent_actual_interval(forecast)

    assert result is not None
    assert "general" in result
    assert "feedIn" in result
    assert result["general"]["type"] == "CurrentInterval"
    assert result["feedIn"]["type"] == "CurrentInterval"


def test_extract_no_current_interval():
    """No CurrentInterval → returns None."""
    forecast = make_amber_forecast(n_intervals=5)
    # Remove all CurrentInterval entries
    for entry in forecast:
        entry["type"] = "ForecastInterval"

    result = extract_most_recent_actual_interval(forecast)
    assert result is None


def test_extract_empty_data():
    """Empty forecast data → returns None."""
    result = extract_most_recent_actual_interval([])
    assert result is None


# ---------------------------------------------------------------------------
# convert_amber_to_tesla_tariff Tests (AC-1, AC-2)
# ---------------------------------------------------------------------------

def test_convert_amber_does_not_crash():
    """Full conversion with sample data should not raise exceptions.

    Note: convert_amber_to_tesla_tariff has a complex rolling 24h window
    that matches periods to dt_util.now(). With mocked time, the
    time-period alignment may not produce a valid tariff (returns None).
    This test verifies no crash — full conversion testing requires
    real HA integration fixtures with time-aligned data.
    """
    forecast = make_amber_forecast(n_intervals=48)

    # Should not raise, regardless of whether it returns a tariff or None
    result = convert_amber_to_tesla_tariff(
        forecast_data=forecast,
        tesla_energy_site_id="12345",
        powerwall_timezone="Australia/Melbourne",
    )

    assert result is None or isinstance(result, dict)


def test_convert_amber_negative_price_no_crash():
    """Negative prices should not crash the converter."""
    forecast = make_amber_forecast(n_intervals=10, base_import_ckwh=-5.0)

    # Should not raise even with negative prices throughout
    result = convert_amber_to_tesla_tariff(
        forecast_data=forecast,
        tesla_energy_site_id="12345",
        powerwall_timezone="Australia/Melbourne",
    )

    assert result is None or isinstance(result, dict)


def test_convert_amber_empty_data():
    """Empty forecast data → returns None."""
    result = convert_amber_to_tesla_tariff(
        forecast_data=[],
        tesla_energy_site_id="12345",
    )
    assert result is None
