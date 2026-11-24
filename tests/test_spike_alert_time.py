"""Tests for price spike alert timestamp formatting."""

from __future__ import annotations

import ast
import logging
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, call
from zoneinfo import ZoneInfo

import pytest

from conftest import load_source_node, pin_power_sync_clock


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

homeassistant = types.ModuleType("homeassistant")
homeassistant_util = types.ModuleType("homeassistant.util")
homeassistant_dt = types.ModuleType("homeassistant.util.dt")
homeassistant_dt.now = lambda: datetime.now().astimezone()
homeassistant_util.dt = homeassistant_dt
homeassistant.util = homeassistant_util
sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.util", homeassistant_util)
sys.modules.setdefault("homeassistant.util.dt", homeassistant_dt)

from power_sync import tariff_converter  # noqa: E402
from power_sync.tariff_converter import (  # noqa: E402
    format_spike_alert_time,
    spike_alert_key_time,
    spike_is_imminent,
)


BRISBANE = ZoneInfo("Australia/Brisbane")


def test_spike_alert_time_labels_today_in_local_time():
    now = datetime(2026, 5, 17, 12, 46, tzinfo=BRISBANE)

    assert (
        format_spike_alert_time("2026-05-17T18:30:00+10:00", now=now)
        == "Today 6:30 pm"
    )


def test_spike_alert_time_labels_tomorrow():
    now = datetime(2026, 5, 17, 12, 46, tzinfo=BRISBANE)

    assert (
        format_spike_alert_time("2026-05-18T17:00:00+10:00", now=now)
        == "Tomorrow 5:00 pm"
    )


def test_spike_alert_time_converts_utc_to_local_day():
    now = datetime(2026, 5, 17, 12, 46, tzinfo=BRISBANE)

    assert (
        format_spike_alert_time("2026-05-17T07:30:00Z", now=now)
        == "Today 5:30 pm"
    )


def test_spike_alert_key_distinguishes_same_time_on_different_days():
    today_key = spike_alert_key_time("2026-05-17T17:00:00+10:00")
    tomorrow_key = spike_alert_key_time("2026-05-18T17:00:00+10:00")

    assert today_key != tomorrow_key


def test_spike_is_imminent_only_for_current_or_next_hour():
    now = datetime(2026, 5, 17, 12, 46, tzinfo=BRISBANE)

    assert spike_is_imminent("2026-05-17T13:30:00+10:00", now=now) is True
    assert spike_is_imminent("2026-05-17T14:00:00+10:00", now=now) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("lead_minutes", "time_label"), [(10, "6:36 pm"), (70, "7:36 pm")])
async def test_spike_push_labels_forecast_and_pairs_each_interval(monkeypatch, lead_minutes, time_label):
    from homeassistant.util import dt

    now = datetime(2026, 11, 19, 18, 26, tzinfo=BRISBANE)
    monkeypatch.setattr(dt, "now", lambda: now)
    forecast = [
        {
            "type": interval_type,
            "channelType": channel,
            "nemTime": (now + timedelta(minutes=offset)).isoformat(),
            "perKwh": price,
        }
        for interval_type, channel, offset, price in (
            ("CurrentInterval", "feedIn", 0, -20),
            ("ForecastInterval", "general", lead_minutes, 213),
            ("ForecastInterval", "feedIn", lead_minutes, -180),
            ("ForecastInterval", "general", lead_minutes + 5, 456),
            ("ForecastInterval", "feedIn", lead_minutes + 5, -394),
        )
    ]
    sync = load_source_node(ROOT / "__init__.py", "_handle_sync_tou_internal")
    alert_block = next(
        node for node in ast.walk(sync)
        if isinstance(node, ast.If) and "price_spike_alert_enabled" in ast.unparse(node.test)
    )
    run = ast.parse("async def run():\n    pass").body[0]
    run.body = [alert_block]
    module = ast.fix_missing_locations(ast.Module(body=[run], type_ignores=[]))

    push = AsyncMock()
    actions = types.ModuleType("test_spike_alert.automations.actions")
    actions._send_expo_push = push
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    runtime = {}
    hass = object()
    namespace = dict(
        __package__="test_spike_alert",
        sync_mode="settled_price",
        price_spike_alert_enabled=True,
        build_only=False,
        forecast_data=forecast,
        forecast_type="predicted",
        entry=types.SimpleNamespace(options={}, data={}),
        CONF_PRICE_SPIKE_IMPORT_THRESHOLD="price_spike_import_threshold",
        CONF_PRICE_SPIKE_EXPORT_THRESHOLD="price_spike_export_threshold",
        DEFAULT_PRICE_SPIKE_IMPORT_THRESHOLD=100.0,
        DEFAULT_PRICE_SPIKE_EXPORT_THRESHOLD=50.0,
        detect_price_spikes=tariff_converter.detect_price_spikes,
        format_spike_alert_time=format_spike_alert_time,
        spike_alert_key_time=spike_alert_key_time,
        spike_is_imminent=spike_is_imminent,
        _aemo_dispatch_entry_data=lambda: runtime,
        _LOGGER=logging.getLogger(__name__),
        hass=hass,
    )
    exec(compile(module, str(ROOT / "__init__.py"), "exec"), namespace)
    with pin_power_sync_clock(tariff_converter, now=lambda: now):
        await namespace["run"]()
        await namespace["run"]()

    assert push.await_args_list == [
        call(hass, "High Import Price Forecast", f"$2.13/kWh forecast for Today {time_label}",
             notification_type="high_import_price_forecast"),
        call(hass, "High Export Price Forecast", f"$1.80/kWh forecast for Today {time_label}",
             notification_type="high_export_price_forecast"),
    ]
