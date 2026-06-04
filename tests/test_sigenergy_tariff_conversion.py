"""Regression tests for Sigenergy tariff conversion."""

from __future__ import annotations

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
