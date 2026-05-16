"""Regression tests for HAFO-backed load forecasting."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
MODULE_PATH = COMPONENT_ROOT / "optimization" / "load_estimator.py"


def _load_estimator_module(monkeypatch):
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_core.HomeAssistant = object
    ha_dt.now = lambda: datetime(2026, 5, 9, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda: datetime(2026, 5, 9, tzinfo=timezone.utc)
    ha_dt.as_local = lambda value: value
    ha_util.dt = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    const_module = types.ModuleType("power_sync.const")
    const_module.DEFAULT_SOLCAST_ESTIMATE_TYPE = "estimate"
    const_module.HAFO_DOMAIN = "hafo"
    const_module.HAFO_LOAD_SENSOR_PREFIX = "sensor.hafo_"
    const_module.SOLCAST_ESTIMATE = "estimate"
    const_module.SOLCAST_ESTIMATE10 = "estimate10"
    const_module.SOLCAST_ESTIMATE90 = "estimate90"

    monkeypatch.setitem(sys.modules, "homeassistant", ha_root)
    monkeypatch.setitem(sys.modules, "homeassistant.core", ha_core)
    monkeypatch.setitem(sys.modules, "homeassistant.util", ha_util)
    monkeypatch.setitem(sys.modules, "homeassistant.util.dt", ha_dt)
    monkeypatch.setitem(sys.modules, "power_sync", ps_module)
    monkeypatch.setitem(sys.modules, "power_sync.optimization", optimization_module)
    monkeypatch.setitem(sys.modules, "power_sync.const", const_module)
    monkeypatch.delitem(sys.modules, "power_sync.optimization.load_estimator", raising=False)

    spec = importlib.util.spec_from_file_location(
        "power_sync.optimization.load_estimator",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_hafo_parser_stops_at_real_coverage_instead_of_flat_padding(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, tzinfo=timezone.utc)
    forecast_data = [
        {
            "datetime": (start + timedelta(minutes=30 * index)).isoformat(),
            "native_value": 1000 + index,
        }
        for index in range(48)
    ]

    forecast = module.HAFOForecaster(
        SimpleNamespace(),
        interval_minutes=5,
    )._parse_hafo_forecast(forecast_data, start, 576)

    assert len(forecast) < 576
    assert len(forecast) <= 300
    assert len({round(value, 1) for value in forecast[-24:]}) > 1


def test_partial_hafo_forecast_uses_history_for_uncovered_tail(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, tzinfo=timezone.utc)
    hafo_values = [1000.0 + index for index in range(300)]
    history = [(start - timedelta(days=7), 1800.0)]

    class FakeHAFO:
        async def get_forecast(self, horizon_hours, start_time):
            return list(hafo_values)

    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    estimator._hafo_available = True
    estimator._hafo = FakeHAFO()
    estimator._get_load_history = lambda: _async_value(history)
    estimator._forecast_from_history = (
        lambda history_values, tail_start, n_intervals, **kwargs:
        [9000.0 + index for index in range(n_intervals)]
    )

    forecast = _run(estimator.get_forecast(48, start))

    assert len(forecast) == 576
    assert forecast[:300] == hafo_values
    assert forecast[300] == 9000.0
    assert forecast[-1] == 9000.0 + 275


def test_history_fallback_prefers_same_day_type_for_missing_dow(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)

    history = []
    monday = datetime(2026, 5, 4, tzinfo=timezone.utc)
    saturday = datetime(2026, 5, 9, tzinfo=timezone.utc)

    # No Sunday or future Monday buckets are present. Weekday history is low,
    # weekend history is high, so missing days should not collapse to one
    # identical "same time any day" profile.
    for day_offset in range(1, 5):
        day = monday + timedelta(days=day_offset)
        for half_hour in range(48):
            history.append((day + timedelta(minutes=30 * half_hour), 500.0))

    for half_hour in range(48):
        history.append((saturday + timedelta(minutes=30 * half_hour), 2000.0))

    forecast = estimator._forecast_from_history(
        history,
        datetime(2026, 5, 10, tzinfo=timezone.utc),
        576,
    )

    sunday_kwh = sum(forecast[:288]) / 1000 / 12
    monday_kwh = sum(forecast[288:576]) / 1000 / 12

    assert sunday_kwh > monday_kwh * 2
    assert abs(sunday_kwh - 48) < 0.1
    assert abs(monday_kwh - 12) < 0.1


async def _async_value(value):
    return value


def _run(coro):
    import asyncio

    return asyncio.run(coro)
