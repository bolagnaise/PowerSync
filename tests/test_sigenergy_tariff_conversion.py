"""Regression tests for Sigenergy tariff conversion."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()


@pytest.fixture()
def sigenergy_api_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in ("power_sync", "power_sync.sigenergy_api", "power_sync.const")
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    try:
        yield importlib.import_module("power_sync.sigenergy_api")
    finally:
        sys.modules.pop("power_sync.sigenergy_api", None)
        for name, module in saved_modules.items():
            if module is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture()
def tariff_converter_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in (
            "power_sync",
            "power_sync.tariff_converter",
            "power_sync.currency",
            "homeassistant",
            "homeassistant.util",
            "homeassistant.util.dt",
        )
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync
    ha_root = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_util.dt = ha_dt
    ha_root.util = ha_util
    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    try:
        yield importlib.import_module("power_sync.tariff_converter")
    finally:
        sys.modules.pop("power_sync.tariff_converter", None)
        for name, module in saved_modules.items():
            if module is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _interval(ts: datetime, price: float, channel: str = "general") -> dict:
    return {
        "nemTime": ts.isoformat(),
        "duration": 30,
        "type": "ForecastInterval",
        "channelType": channel,
        "advancedPrice": {"predicted": price},
        "perKwh": price,
    }


def _day_intervals(day: datetime) -> list[dict]:
    prices = []
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)

    for slot in range(48):
        slot_start = start + timedelta(minutes=slot * 30)
        slot_end = slot_start + timedelta(minutes=30)
        price = 10.0 + slot
        prices.append(_interval(slot_end, price, "general"))
        prices.append(_interval(slot_end, -5.0, "feedIn"))

    return prices


def test_sigenergy_converter_prefers_next_24h_date_for_past_clock_slots(
    sigenergy_api_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 6, 20, 10, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(sigenergy_api_module, "datetime", FixedDatetime)

    today_midnight = datetime(2026, 5, 6, 0, 30, tzinfo=brisbane)
    tomorrow_midnight = datetime(2026, 5, 7, 0, 30, tzinfo=brisbane)
    current = datetime(2026, 5, 6, 20, 30, tzinfo=brisbane)

    prices = [
        _interval(today_midnight, 99.0),
        _interval(tomorrow_midnight, 12.0),
        _interval(current, 35.0),
    ]

    converted = sigenergy_api_module.convert_amber_prices_to_sigenergy(
        prices,
        price_type="buy",
        forecast_type="predicted",
        nem_region="QLD1",
    )

    by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in converted}

    assert by_start["00:00"] == 12.0
    assert by_start["20:00"] == 35.0


def test_sigenergy_converter_does_not_average_multiple_dates_for_same_slot(
    sigenergy_api_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 6, 19, 40, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(sigenergy_api_module, "datetime", FixedDatetime)

    today_slot = datetime(2026, 5, 6, 21, 30, tzinfo=brisbane)
    tomorrow_slot = today_slot + timedelta(days=1)

    prices = [
        _interval(today_slot, 40.0),
        _interval(tomorrow_slot, 10.0),
    ]

    converted = sigenergy_api_module.convert_amber_prices_to_sigenergy(
        prices,
        price_type="buy",
        forecast_type="predicted",
        nem_region="QLD1",
    )

    by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in converted}

    assert by_start["21:00"] == 40.0


def test_sigenergy_upload_prices_use_canonical_tariff_rates(
    sigenergy_api_module,
    tariff_converter_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 6, 20, 38, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(sigenergy_api_module, "datetime", FixedDatetime)
    monkeypatch.setattr(tariff_converter_module, "datetime", FixedDatetime)

    forecast = _day_intervals(datetime(2026, 5, 6, tzinfo=brisbane))
    current_actual = {
        "general": {"perKwh": 35.33},
        "feedIn": {"perKwh": -9.82},
    }

    tariff = tariff_converter_module.convert_amber_to_tesla_tariff(
        forecast,
        tesla_energy_site_id="none",
        forecast_type="predicted",
        powerwall_timezone="Australia/Brisbane",
        current_actual_interval=current_actual,
        electricity_provider="amber",
    )
    canonical_rates = tariff["energy_charges"]["Summer"]["rates"]

    converted = sigenergy_api_module.convert_tariff_rates_to_sigenergy(canonical_rates)
    by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in converted}

    assert by_start["00:00"] == canonical_rates["PERIOD_00_00"] * 100
    assert by_start["20:30"] == canonical_rates["PERIOD_20_30"] * 100
    assert by_start["20:30"] == 35.33


def test_sigenergy_visible_upload_mirrors_import_not_feed_in_for_current_slot(
    sigenergy_api_module,
    tariff_converter_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 6, 20, 38, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(sigenergy_api_module, "datetime", FixedDatetime)
    monkeypatch.setattr(tariff_converter_module, "datetime", FixedDatetime)

    forecast = _day_intervals(datetime(2026, 5, 6, tzinfo=brisbane))
    current_actual = {
        "general": {"perKwh": 35.33},
        "feedIn": {"perKwh": -9.82},
    }

    tariff = tariff_converter_module.convert_amber_to_tesla_tariff(
        forecast,
        tesla_energy_site_id="none",
        forecast_type="predicted",
        powerwall_timezone="Australia/Brisbane",
        current_actual_interval=current_actual,
        electricity_provider="amber",
    )
    buy_prices = sigenergy_api_module.convert_tariff_rates_to_sigenergy(
        tariff["energy_charges"]["Summer"]["rates"]
    )
    sell_prices = [dict(slot) for slot in buy_prices]

    buy_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in buy_prices}
    sell_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in sell_prices}

    assert buy_by_start["20:30"] == 35.33
    assert sell_by_start["20:30"] == 35.33
    assert sell_by_start["20:30"] != 9.82
