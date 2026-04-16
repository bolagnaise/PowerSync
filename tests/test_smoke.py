"""Smoke tests proving the test infrastructure works end-to-end.

These tests validate that pytest, fixtures, and module imports work correctly.
They do NOT test business logic — that's Plans 02 and 03.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "custom_components" / "power_sync" / "manifest.json"


def test_manifest_valid():
    """Verify manifest.json is valid and contains required fields."""
    assert MANIFEST_PATH.exists(), f"manifest.json not found at {MANIFEST_PATH}"

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    assert manifest["domain"] == "power_sync"
    assert "version" in manifest
    assert "requirements" in manifest
    assert "codeowners" in manifest
    assert manifest["config_flow"] is True


def test_optimizer_module_importable():
    """Verify battery_optimizer module can be imported.

    The optimizer is pure scipy/dataclass code with a single HA import
    (homeassistant.util.dt). If this import fails, conftest stubs are needed.
    """
    try:
        from custom_components.power_sync.optimization import battery_optimizer

        assert hasattr(battery_optimizer, "BatteryOptimizer") or hasattr(
            battery_optimizer, "optimize_battery_schedule"
        ), "Expected BatteryOptimizer class or optimize_battery_schedule function"
    except ImportError as e:
        # Without full HA + dependencies installed, the import chain fails
        # (aiohttp, homeassistant, etc.). Expected in minimal test environments.
        pytest.skip(f"PowerSync dependencies not installed: {e}")


def test_conftest_fixtures(mock_hass):
    """Verify conftest fixtures provide expected mock structure."""
    assert isinstance(mock_hass.data, dict)
    assert mock_hass.config.time_zone == "Australia/Melbourne"


def test_sample_prices_fixture(sample_prices):
    """Verify sample_prices fixture returns valid Amber-format data."""
    assert len(sample_prices) == 48, f"Expected 48 intervals, got {len(sample_prices)}"

    # Check structure of first entry
    first = sample_prices[0]
    assert "perKwh" in first
    assert "nemTime" in first
    assert "channelType" in first
    assert first["channelType"] == "general"
    assert first["duration"] == 30

    # Verify negative price exists (interval 23, at 12:00)
    negative_prices = [p for p in sample_prices if p["perKwh"] < 0]
    assert len(negative_prices) >= 1, "Expected at least one negative price interval"
