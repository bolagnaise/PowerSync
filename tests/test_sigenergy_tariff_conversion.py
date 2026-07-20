"""Regression tests for Sigenergy tariff conversion."""

from __future__ import annotations

import copy
import importlib
import asyncio
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


def test_static_tou_tariff_schedule_converts_to_sigenergy_slots(
    sigenergy_api_module,
):
    brisbane = ZoneInfo("Australia/Brisbane")
    tariff_schedule = {
        "plan_name": "Aurora TOU",
        "tou_periods": {
            "PEAK": {
                "periods": [
                    {
                        "fromDayOfWeek": 1,
                        "toDayOfWeek": 5,
                        "fromHour": 16,
                        "fromMinute": 0,
                        "toHour": 21,
                        "toMinute": 0,
                    }
                ]
            },
            "OFF_PEAK": {
                "periods": [
                    {
                        "fromDayOfWeek": 0,
                        "toDayOfWeek": 6,
                        "fromHour": 0,
                        "fromMinute": 0,
                        "toHour": 24,
                        "toMinute": 0,
                    }
                ]
            },
        },
        "buy_rates": {"PEAK": 0.302, "OFF_PEAK": 0.142},
        "sell_rates": {"PEAK": 0.093, "OFF_PEAK": 0.093, "ALL": 0.093},
    }

    buy_prices, sell_prices = sigenergy_api_module.convert_static_tariff_schedule_to_sigenergy(
        tariff_schedule,
        now=datetime(2026, 7, 9, 15, 10, tzinfo=brisbane),
    )

    buy_by_week_start = {
        (slot["weekRange"], slot["timeRange"].split("-")[0]): slot["price"]
        for slot in buy_prices
    }
    sell_by_week_start = {
        (slot["weekRange"], slot["timeRange"].split("-")[0]): slot["price"]
        for slot in sell_prices
    }

    assert {slot["weekRange"] for slot in buy_prices} == {"1-5", "6-7"}
    assert {slot["weekRange"] for slot in sell_prices} == {"1-7"}
    assert len(buy_prices) == 96
    assert len(sell_prices) == 48
    assert buy_by_week_start[("1-5", "15:30")] == 14.2
    assert buy_by_week_start[("1-5", "16:00")] == 30.2
    assert buy_by_week_start[("6-7", "16:00")] == 14.2
    assert sell_by_week_start[("1-7", "16:00")] == 9.3


def test_static_period_tariff_schedule_converts_to_sigenergy_slots(
    sigenergy_api_module,
):
    buy_prices, sell_prices = sigenergy_api_module.convert_static_tariff_schedule_to_sigenergy(
        {
            "buy_prices": {
                "PERIOD_00_00": 0.142,
                "PERIOD_16_00": 0.302,
            },
            "sell_prices": {
                "PERIOD_00_00": 0.08,
                "PERIOD_16_00": 0.093,
            },
        }
    )

    buy_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in buy_prices}
    sell_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in sell_prices}

    assert buy_by_start == {"00:00": 14.2, "16:00": 30.2}
    assert sell_by_start == {"00:00": 8.0, "16:00": 9.3}


def test_sigenergy_upload_groups_day_aware_static_tou_slots(
    sigenergy_api_module,
):
    session = _FakeTariffSession([_FakeTariffResponse(200, payload={"code": 0})])
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id="123",
            buy_prices=[
                {"weekRange": "1-5", "timeRange": "16:00-16:30", "price": 30.2},
                {"weekRange": "6-7", "timeRange": "16:00-16:30", "price": 14.2},
            ],
            sell_prices=[
                {"weekRange": "1-7", "timeRange": "16:00-16:30", "price": 9.3},
            ],
        )
    )

    payload = session.post_kwargs[0]["json"]
    buy_week_prices = payload["buyPrice"]["staticPricing"]["combinedPrices"][0][
        "weekPrices"
    ]
    sell_week_prices = payload["sellPrice"]["staticPricing"]["combinedPrices"][0][
        "weekPrices"
    ]

    assert result == {"success": True, "message": "Tariff updated"}
    assert buy_week_prices == [
        {
            "weekRange": "1-5",
            "timeRange": [{"timeRange": "16:00-16:30", "price": 30.2}],
        },
        {
            "weekRange": "6-7",
            "timeRange": [{"timeRange": "16:00-16:30", "price": 14.2}],
        },
    ]
    assert sell_week_prices == [
        {
            "weekRange": "1-7",
            "timeRange": [{"timeRange": "16:00-16:30", "price": 9.3}],
        }
    ]


def test_sigenergy_upload_uses_provider_label_for_buy_and_sell(
    sigenergy_api_module,
):
    session = _FakeTariffSession([_FakeTariffResponse(200, payload={"code": 0})])
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id="123",
            buy_prices=[{"timeRange": "00:00-00:30", "price": 14.2}],
            sell_prices=[{"timeRange": "00:00-00:30", "price": 9.3}],
            plan_name="PowerSync Flow Power",
            provider_label="Flow Power",
        )
    )

    payload = session.post_kwargs[0]["json"]

    assert result == {"success": True, "message": "Tariff updated"}
    assert payload["buyPrice"]["staticPricing"]["providerName"] == "Flow Power"
    assert payload["sellPrice"]["staticPricing"]["providerName"] == "Flow Power"
    assert payload["buyPrice"]["staticPricing"]["planName"] == (
        "PowerSync Flow Power 30-min"
    )
    assert payload["sellPrice"]["staticPricing"]["planName"] == (
        "PowerSync Flow Power 30-min"
    )


def test_sigenergy_manual_sync_uses_static_tou_without_price_coordinator():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()

    setup_source = init_source[
        init_source.index("def _static_tou_tariff_schedule_for_sync"):
        init_source.index("async def handle_sync_tou")
    ]
    sync_source = init_source[
        init_source.index("async def _handle_sync_tou_internal"):
        init_source.index("# Fetch Powerwall timezone")
    ]
    sigenergy_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    assert "and not _static_tou_tariff_schedule_for_sync()" in setup_source
    assert "use_static_tou = (" in sync_source
    assert "amber_coordinator is None" in sync_source
    assert "forecast_data = []" in sync_source
    assert "convert_static_tariff_schedule_to_sigenergy" in sigenergy_source
    assert 'payload_source = "static_tou_tariff_schedule"' in sigenergy_source


def test_flow_power_canonical_tariff_ignores_raw_current_wholesale_spike(
    tariff_converter_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 23, 18, 38, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(tariff_converter_module, "datetime", FixedDatetime)

    forecast = _day_intervals(datetime(2026, 6, 23, tzinfo=brisbane))
    for point in forecast:
        interval_start = datetime.fromisoformat(point["nemTime"]) - timedelta(
            minutes=point["duration"]
        )
        if point["channelType"] == "general" and (
            interval_start.hour,
            interval_start.minute,
        ) == (18, 30):
            point["perKwh"] = 38.91
            point["advancedPrice"] = {"predicted": 38.91}

    current_actual = {
        "general": {"perKwh": 593.84},
        "feedIn": {"perKwh": -35.0},
    }

    tariff = tariff_converter_module.convert_amber_to_tesla_tariff(
        forecast,
        tesla_energy_site_id="none",
        forecast_type="predicted",
        powerwall_timezone="Australia/Brisbane",
        current_actual_interval=current_actual,
        electricity_provider="flow_power",
    )

    rates = tariff["energy_charges"]["Summer"]["rates"]
    assert rates["PERIOD_18_30"] == 0.3891
    assert rates["PERIOD_18_30"] != 5.9384


def test_flow_power_pea_uses_current_wholesale_without_raw_tariff_injection(
    tariff_converter_module,
    monkeypatch,
):
    brisbane = ZoneInfo("Australia/Brisbane")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 23, 18, 38, tzinfo=brisbane).astimezone(tz)

    monkeypatch.setattr(tariff_converter_module, "datetime", FixedDatetime)

    forecast = _day_intervals(datetime(2026, 6, 23, tzinfo=brisbane))
    for point in forecast:
        interval_start = datetime.fromisoformat(point["nemTime"]) - timedelta(
            minutes=point["duration"]
        )
        if point["channelType"] == "general" and (
            interval_start.hour,
            interval_start.minute,
        ) == (18, 30):
            point["perKwh"] = 65.46
            point["advancedPrice"] = {"predicted": 65.46}

    current_actual = {
        "general": {
            "nemTime": datetime(2026, 6, 23, 18, 35, tzinfo=brisbane).isoformat(),
            "duration": 5,
            "type": "CurrentInterval",
            "channelType": "general",
            "perKwh": 15.55,
        },
        "feedIn": {
            "nemTime": datetime(2026, 6, 23, 18, 35, tzinfo=brisbane).isoformat(),
            "duration": 5,
            "type": "CurrentInterval",
            "channelType": "feedIn",
            "perKwh": 0.0,
        },
    }

    tariff = tariff_converter_module.convert_amber_to_tesla_tariff(
        forecast,
        tesla_energy_site_id="none",
        forecast_type="predicted",
        powerwall_timezone="Australia/Brisbane",
        current_actual_interval=current_actual,
        electricity_provider="flow_power",
    )
    rates = tariff["energy_charges"]["Summer"]["rates"]
    assert rates["PERIOD_18_30"] == 0.6546
    assert rates["PERIOD_18_30"] != 0.1555

    wholesale_lookup = tariff_converter_module.get_wholesale_lookup(
        forecast,
        current_actual_interval=current_actual,
    )
    assert wholesale_lookup["PERIOD_18_30"] == 0.1555

    adjusted = tariff_converter_module.apply_flow_power_pea(
        tariff,
        wholesale_lookup,
        base_rate=31.707,
        twap=13.55,
        tariff_rate_lookup={"PERIOD_18_30": 8.05},
        avg_daily_tariff=8.85,
        bpea=0.0,
        gst_multiplier=1.1,
    )

    adjusted_rates = adjusted["energy_charges"]["Summer"]["rates"]
    assert adjusted_rates["PERIOD_18_30"] == 0.3311
    assert adjusted_rates["PERIOD_18_30"] != 0.4196
    assert adjusted_rates["PERIOD_18_30"] != 0.8801


def test_flow_power_pea_current_interval_fills_leading_forecast_gap(
    tariff_converter_module,
):
    brisbane = ZoneInfo("Australia/Brisbane")
    forecast = [
        {
            "nemTime": datetime(2026, 7, 11, 9, 0, tzinfo=brisbane).isoformat(),
            "duration": 30,
            "type": "ForecastInterval",
            "channelType": "general",
            "perKwh": 2.1,
        }
    ]
    current_actual = {
        "general": {
            "nemTime": datetime(2026, 7, 11, 8, 5, tzinfo=brisbane).isoformat(),
            "duration": 5,
            "type": "CurrentInterval",
            "channelType": "general",
            "perKwh": 5.65,
        },
    }

    wholesale_lookup = tariff_converter_module.get_wholesale_lookup(
        forecast,
        current_actual_interval=current_actual,
    )

    assert wholesale_lookup["PERIOD_08_00"] == 0.0565
    assert wholesale_lookup["PERIOD_08_30"] == 0.021

    tariff = {
        "energy_charges": {
            "Summer": {
                "rates": {
                    "PERIOD_08_00": 0.9999,
                    "PERIOD_08_30": 0.9999,
                },
            },
        },
    }
    adjusted = tariff_converter_module.apply_flow_power_pea(
        tariff,
        wholesale_lookup,
        base_rate=34.0,
        twap=8.0,
        bpea=1.7,
    )

    adjusted_rates = adjusted["energy_charges"]["Summer"]["rates"]
    assert adjusted_rates["PERIOD_08_00"] == 0.2995
    assert adjusted_rates["PERIOD_08_00"] != 0.323
    assert adjusted_rates["PERIOD_08_30"] == 0.264


def test_sigenergy_visible_upload_uses_distinct_30_min_buy_and_sell_slots(
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
    canonical_sell_rates = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    sell_prices = sigenergy_api_module.convert_tariff_rates_to_sigenergy(
        canonical_sell_rates
    )

    buy_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in buy_prices}
    sell_by_start = {slot["timeRange"].split("-")[0]: slot["price"] for slot in sell_prices}

    assert len(buy_prices) == 48
    assert len(sell_prices) == 48
    assert all(slot["timeRange"].endswith((":00", ":30")) for slot in buy_prices)
    assert all(slot["timeRange"].endswith((":00", ":30")) for slot in sell_prices)
    assert buy_by_start["20:30"] == 35.33
    assert sell_by_start["20:30"] == 9.82
    assert sell_by_start["20:30"] != buy_by_start["20:30"]


def test_convert_amber_to_tesla_tariff_demand_artificial_price_uses_ha_local_weekday(
    tariff_converter_module, monkeypatch
):
    """HD-23 regression: when no timezone can be auto-detected from price data
    (detected_tz is None), the demand-artificial-price "Weekdays Only" check
    must use HA's configured clock (dt_util.now()), not the OS-local clock
    (datetime.now())."""
    # OS-local "now" lands on a Saturday - if the buggy code path is used,
    # "Weekdays Only" is (wrongly) treated as invalid and no artificial
    # price increase is applied.
    os_local_now = datetime(2026, 7, 11, 15, 0)
    # HA-local "now" (dt_util.now()) lands on a Wednesday - the correct
    # weekday to use for the "Weekdays Only" check.
    ha_local_now = datetime(2026, 7, 8, 15, 0)

    real_datetime = tariff_converter_module.datetime

    class _FakeDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return os_local_now.replace(tzinfo=tz)
            return os_local_now

    monkeypatch.setattr(tariff_converter_module, "datetime", _FakeDatetime)
    monkeypatch.setattr(
        tariff_converter_module.dt_util, "now", lambda: ha_local_now, raising=False
    )

    # Build a full day of naive (no tzinfo) forecast points so timezone
    # auto-detection fails and detected_tz stays None - the only condition
    # under which the OS-local vs HA-local distinction matters.
    day = real_datetime(2026, 7, 11)
    forecast: list[dict] = []
    for slot in range(48):
        slot_end = day + timedelta(minutes=(slot + 1) * 30)
        for channel, price in (("general", 20.0), ("feedIn", -5.0)):
            forecast.append(
                {
                    "nemTime": slot_end.isoformat(),
                    "duration": 30,
                    "type": "ForecastInterval",
                    "channelType": channel,
                    "advancedPrice": {"predicted": price},
                    "perKwh": price,
                }
            )

    tariff = tariff_converter_module.convert_amber_to_tesla_tariff(
        forecast,
        tesla_energy_site_id="none",
        forecast_type="predicted",
        demand_charge_enabled=True,
        demand_charge_rate=1.0,
        demand_charge_start_time="14:00",
        demand_charge_end_time="20:00",
        demand_charge_days="Weekdays Only",
        demand_artificial_price_enabled=True,
    )

    assert tariff is not None
    buy_price = tariff["energy_charges"]["Summer"]["rates"]["PERIOD_15_00"]
    # $0.20/kWh base price + the $2/kWh artificial increase should land here
    # only if the weekday check used HA-local time (Wednesday), not OS-local
    # time (Saturday).
    assert buy_price == pytest.approx(2.2)


def test_chip_mode_threshold_uses_unboosted_tesla_tariff_price(tariff_converter_module):
    tariff = {
        "sell_tariff": {
            "energy_charges": {
                "Summer": {
                    "rates": {
                        "PERIOD_17_00": 0.208,
                        "PERIOD_17_30": 0.26,
                    }
                }
            }
        }
    }
    reference_tariff = copy.deepcopy(tariff)

    boosted = tariff_converter_module.apply_export_boost(
        tariff,
        offset_cents=10.0,
        boost_start="17:00",
        boost_end="18:00",
        activation_threshold_cents=0.0,
    )
    chipped = tariff_converter_module.apply_chip_mode(
        boosted,
        chip_start="17:00",
        chip_end="18:00",
        threshold_cents=25.0,
        reference_tariff=reference_tariff,
    )
    sell_rates = chipped["sell_tariff"]["energy_charges"]["Summer"]["rates"]

    assert boosted["sell_tariff"]["energy_charges"]["Summer"]["rates"]["PERIOD_17_30"] > 0.25
    assert sell_rates["PERIOD_17_00"] == 0.0
    assert sell_rates["PERIOD_17_30"] > 0.25


def test_sigenergy_canonical_upload_converts_buy_and_sell_in_sync_helper():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    canonical_call_pos = helper_source.index("canonical_tariff = convert_amber_to_tesla_tariff")
    sell_rates_pos = helper_source.index("canonical_sell_rates =")
    sell_convert_pos = helper_source.index("sell_prices = convert_tariff_rates_to_sigenergy")
    sigenergy_upload_pos = helper_source.index("client.set_tariff_rate")

    assert canonical_call_pos < sell_rates_pos < sell_convert_pos < sigenergy_upload_pos


def test_sigenergy_canonical_upload_applies_provider_tariff_adjustments_first():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    canonical_call_pos = helper_source.index("canonical_tariff = convert_amber_to_tesla_tariff")
    provider_adjust_pos = helper_source.index(
        "canonical_tariff = _apply_provider_tariff_adjustments"
    )
    buy_rates_pos = helper_source.index("canonical_buy_rates =")
    sigenergy_upload_pos = helper_source.index("client.set_tariff_rate")

    assert canonical_call_pos < provider_adjust_pos < buy_rates_pos < sigenergy_upload_pos


def test_sigenergy_sync_resolves_demand_settings_inside_helper():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    demand_lookup_pos = helper_source.index("demand_charge_rate = entry.options.get")
    canonical_call_pos = helper_source.index("canonical_tariff = convert_amber_to_tesla_tariff")

    assert demand_lookup_pos < canonical_call_pos


def test_sigenergy_tariff_sync_does_not_require_optional_device_id():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    credentials_guard = "if not all([station_id, username, pass_enc]):"

    assert credentials_guard in helper_source
    assert "if not all([station_id, username, pass_enc, device_id]):" not in helper_source
    assert "device_id=device_id" in helper_source
    assert "cloud_region=cloud_region" in helper_source


def test_sigenergy_region_detection_tolerates_setup_time_cache_miss():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _get_nem_region_from_amber"):
        init_source.index("def _record_flow_power_twap_sample")
    ]

    assert "domain_data = hass.data.get(DOMAIN, {})" in helper_source
    assert "entry_domain_data = domain_data.get(entry.entry_id, {})" in helper_source
    assert 'hass.data[DOMAIN][entry.entry_id].get("amber_nem_region")' not in helper_source
    assert "if entry.entry_id in domain_data:" in helper_source


def test_sigenergy_cloud_region_is_collected_and_persisted():
    config_flow_source = (COMPONENT_ROOT / "config_flow.py").read_text()

    credentials_source = config_flow_source[
        config_flow_source.index("async def async_step_sigenergy_credentials"):
        config_flow_source.index("async def async_step_sigenergy_station")
    ]
    connection_source = config_flow_source[
        config_flow_source.index("async def async_step_sigenergy_connection"):
        config_flow_source.index("async def async_step_sungrow_connection")
    ]
    init_source = config_flow_source[
        config_flow_source.index("async def async_step_init_sigenergy"):
        config_flow_source.index("async def async_step_init_sungrow")
    ]

    assert "CONF_SIGENERGY_CLOUD_REGION" in credentials_source
    assert "CONF_SIGENERGY_CLOUD_REGION: cloud_region" in credentials_source
    assert "CONF_SIGENERGY_CLOUD_REGION" in connection_source
    assert (
        "new_data[CONF_SIGENERGY_CLOUD_REGION] = sigen_cloud_region"
        in connection_source
    )
    assert "new_data.pop(CONF_SIGENERGY_ACCESS_TOKEN, None)" in connection_source
    assert "CONF_SIGENERGY_CLOUD_REGION" in init_source
    assert (
        "new_data[CONF_SIGENERGY_CLOUD_REGION] = sigen_cloud_region"
        in init_source
    )


def test_sigenergy_tariff_sync_caches_numeric_id_without_overwriting_configured_id():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = init_source[
        init_source.index("async def _sync_tariff_to_sigenergy"):
        init_source.index("async def _sync_tariff_to_foxess")
    ]

    assert "CONF_SIGENERGY_TARIFF_STATION_ID" in helper_source
    assert "CONF_SIGENERGY_TARIFF_STATION_SOURCE_ID" in helper_source
    assert "new_data[CONF_SIGENERGY_STATION_ID] = tariff_station_id" not in helper_source
    assert "configured station ID remains" in helper_source
    assert "station_id=tariff_station_id" in helper_source
    assert helper_source.count("hass.data.setdefault(DOMAIN, {}).setdefault") >= 2
    assert 'entry_data["tariff_schedule"]' in helper_source
    assert 'entry_data["sigenergy_tariff"]' in helper_source


def test_sigenergy_station_picker_preserves_system_id_and_caches_tariff_id():
    config_flow_source = (COMPONENT_ROOT / "config_flow.py").read_text()
    helper_source = config_flow_source[
        config_flow_source.index("async def async_step_sigenergy_station"):
        config_flow_source.index("async def async_step_sigenergy_modbus")
    ]

    assert "CONF_SIGENERGY_TARIFF_STATION_ID" in helper_source
    assert "CONF_SIGENERGY_TARIFF_STATION_SOURCE_ID" in helper_source
    assert "not value.isdigit()" in helper_source
    assert "station_tariff_ids[station_id] = tariff_station_id" in helper_source


class _FakeTariffResponse:
    def __init__(
        self,
        status: int,
        *,
        payload: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeTariffSession:
    closed = False

    def __init__(self, responses: list[_FakeTariffResponse]):
        self.responses = responses
        self.post_calls = 0
        self.post_args = []
        self.post_kwargs = []

    def post(self, *args, **kwargs):
        self.post_calls += 1
        self.post_args.append(args)
        self.post_kwargs.append(kwargs)
        return self.responses.pop(0)


def test_sigenergy_client_uses_region_specific_base_url(sigenergy_api_module):
    client = sigenergy_api_module.SigenergyAPIClient(cloud_region="eu")

    assert client.cloud_region == "eu"
    assert client.api_base_url == "https://api-eu.sigencloud.com"
    assert (
        client._url(sigenergy_api_module.SIGENERGY_AUTH_ENDPOINT)
        == "https://api-eu.sigencloud.com/auth/oauth/token"
    )


def test_sigenergy_client_defaults_unknown_region_to_aus(sigenergy_api_module):
    client = sigenergy_api_module.SigenergyAPIClient(cloud_region="mars")

    assert client.cloud_region == "aus"
    assert client.api_base_url == "https://api-aus.sigencloud.com"


def test_sigenergy_tariff_region_map_accepts_sa_power_short_name(sigenergy_api_module):
    source = Path(sigenergy_api_module.__file__).read_text()

    assert '"SA Power Networks": "SA1"' in source
    assert '"SA Power": "SA1"' in source


def test_sigenergy_set_tariff_uses_configured_region_endpoint(
    sigenergy_api_module,
):
    session = _FakeTariffSession([_FakeTariffResponse(200, payload={"code": 0})])
    client = sigenergy_api_module.SigenergyAPIClient(
        cloud_region="eu",
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id="123",
            buy_prices=[{"timeRange": "00:00-00:30", "price": 1.0}],
            sell_prices=[{"timeRange": "00:00-00:30", "price": 0.0}],
        )
    )

    assert result == {"success": True, "message": "Tariff updated"}
    assert session.post_args[0][0] == (
        "https://api-eu.sigencloud.com/device/stationelecsetprice/save"
    )


def test_sigenergy_set_tariff_retries_429_with_retry_after(
    sigenergy_api_module,
    monkeypatch,
):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sigenergy_api_module.asyncio, "sleep", fake_sleep)

    session = _FakeTariffSession(
        [
            _FakeTariffResponse(
                429,
                text="rate limited",
                headers={"Retry-After": "0.25"},
            ),
            _FakeTariffResponse(200, payload={"code": 0}),
        ]
    )
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id="123",
            buy_prices=[{"timeRange": "00:00-00:30", "price": 1.0}],
            sell_prices=[{"timeRange": "00:00-00:30", "price": 0.0}],
        )
    )

    assert result == {"success": True, "message": "Tariff updated"}
    assert session.post_calls == 2
    assert sleeps == [0.25]


def test_sigenergy_set_tariff_rejects_alphanumeric_system_id(
    sigenergy_api_module,
):
    session = _FakeTariffSession([_FakeTariffResponse(200, payload={"code": 0})])
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id=" TUWXW1774845255 ",
            buy_prices=[{"timeRange": "00:00-00:30", "price": 1.0}],
            sell_prices=[{"timeRange": "00:00-00:30", "price": 0.0}],
        )
    )

    assert "Station ID must be numeric" in result["error"]
    assert session.post_calls == 0


def test_sigenergy_extract_tariff_station_id_prefers_numeric_station_id(
    sigenergy_api_module,
):
    station = {
        "id": "ERSUO1757055255",
        "stationId": "102025092300219",
        "stationName": "Home",
    }

    assert sigenergy_api_module.extract_tariff_station_id(station) == "102025092300219"


def test_sigenergy_resolves_configured_system_id_to_numeric_station_id(
    sigenergy_api_module,
):
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
    )

    async def fake_get_stations():
        return {
            "stations": [
                {
                    "id": "ERSUO1757055255",
                    "stationId": "102025092300219",
                    "stationName": "Home",
                }
            ]
        }

    client.get_stations = fake_get_stations

    result = asyncio.run(client.resolve_tariff_station_id(" ERSUO1757055255 "))

    assert result == {"station_id": "102025092300219", "resolved": True}


def test_sigenergy_set_tariff_stops_after_repeated_429(
    sigenergy_api_module,
    monkeypatch,
):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(sigenergy_api_module.asyncio, "sleep", fake_sleep)

    session = _FakeTariffSession(
        [
            _FakeTariffResponse(429, text="first"),
            _FakeTariffResponse(429, text="second"),
            _FakeTariffResponse(429, text="third"),
        ]
    )
    client = sigenergy_api_module.SigenergyAPIClient(
        access_token="token",
        token_expires_at=datetime.utcnow() + timedelta(hours=1),
        session=session,
    )

    result = asyncio.run(
        client.set_tariff_rate(
            station_id="123",
            buy_prices=[{"timeRange": "00:00-00:30", "price": 1.0}],
            sell_prices=[{"timeRange": "00:00-00:30", "price": 0.0}],
        )
    )

    assert result == {"error": "Failed to set tariff: 429"}
    assert session.post_calls == 3
