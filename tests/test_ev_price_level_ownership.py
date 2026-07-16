"""Tests for price-level EV charging ownership guards."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)
_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)
_ha_er = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)
_ha_dr = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_aiohttp_client = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
_ha_dr.async_get = lambda hass: SimpleNamespace(devices={})
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_dt.utcnow = getattr(_ha_dt, "utcnow", lambda *args, **kwargs: None)
_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_helpers.aiohttp_client = _ha_aiohttp_client
_ha_root.helpers = _ha_helpers
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


VIN = "LRWYHCEK3PC907290"


def test_price_log_value_formats_unknown_without_cents_suffix():
    assert ev_planner._format_price_log_value(None) == "unknown"
    assert ev_planner._format_price_log_value(12) == "12.0c"


def test_amber_forecast_utc_start_times_normalize_to_ha_local(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 7, 3, 19, 20, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["amber_coordinator"] = SimpleNamespace(
        data={
            "forecast": [
                {
                    "startTime": "2026-07-03T10:00:00Z",
                    "channelType": "general",
                    "perKwh": 18.0,
                },
                {
                    "startTime": "2026-07-03T10:00:00Z",
                    "channelType": "feedIn",
                    "perKwh": 5.0,
                },
            ]
        }
    )
    config_entry = _FakeConfigEntry()
    config_entry.data = {"amber_api_token": "token"}

    forecaster = ev_planner.PriceForecaster(hass, config_entry)

    forecast = asyncio.run(forecaster._get_amber_forecast(1))

    assert forecast is not None
    assert forecast[0].hour == "2026-07-03T20:00:00"
    assert forecast[0].import_cents == 18.0
    assert forecast[0].export_cents == 5.0


def test_amber_forecast_drops_past_hours_before_horizon_limit(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda *args, **kwargs: datetime(
            2026, 7, 16, 18, 10, tzinfo=brisbane_tz
        ),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )

    forecast_rows = []
    for hour, price in (
        ("2026-07-15T22:00:00Z", 12.0),
        ("2026-07-15T23:00:00Z", 13.0),
        ("2026-07-16T08:00:00Z", 37.0),
        ("2026-07-16T09:00:00Z", 20.0),
        ("2026-07-16T10:00:00Z", 8.0),
    ):
        forecast_rows.extend(
            (
                {
                    "startTime": hour,
                    "channelType": "general",
                    "perKwh": price,
                },
                {
                    "startTime": hour,
                    "channelType": "feedIn",
                    "perKwh": 5.0,
                },
            )
        )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["amber_coordinator"] = SimpleNamespace(
        data={"forecast": forecast_rows}
    )
    config_entry = _FakeConfigEntry()
    config_entry.data = {"amber_api_token": "token"}

    forecast = asyncio.run(
        ev_planner.PriceForecaster(hass, config_entry)._get_amber_forecast(2)
    )

    assert forecast is not None
    assert [row.hour for row in forecast] == [
        "2026-07-16T18:00:00",
        "2026-07-16T19:00:00",
    ]
    assert [row.import_cents for row in forecast] == [37.0, 20.0]


def test_solar_preferred_excludes_past_and_post_deadline_windows(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 7, 16, 18, 10, tzinfo=brisbane_tz)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda: now)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )

    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour=hour,
            solar_kw=3.0,
            load_kw=0.0,
            surplus_kw=3.0,
            confidence=0.8,
        )
        for hour in (
            "2026-07-16T12:00:00",
            "2026-07-17T11:00:00",
            "2026-07-17T16:00:00",
        )
    ]
    price_forecast = [
        ev_planner.PriceForecast(
            hour=hour,
            import_cents=price,
            export_cents=5.0,
            period="shoulder",
        )
        for hour, price in (
            ("2026-07-16T12:00:00", 1.0),
            ("2026-07-17T13:00:00", 10.0),
            ("2026-07-17T15:00:00", -5.0),
        )
    ]

    plan = asyncio.run(
        planner._plan_solar_preferred(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=80,
            target_time=datetime(2026, 7, 17, 15, 0, tzinfo=brisbane_tz),
            energy_needed_kwh=8.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    assert [window.start_time for window in plan.windows] == [
        "2026-07-17T11:00:00",
        "2026-07-17T13:00:00",
    ]
    assert plan.estimated_solar_kwh == pytest.approx(7.36)
    assert plan.estimated_grid_kwh == pytest.approx(0.64)
    assert plan.can_meet_target is True


def test_cost_optimized_matches_solar_to_price_by_local_hour(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 7, 16, 18, 0, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )
    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    price_forecast = [
        ev_planner.PriceForecast(
            hour="2026-07-16T18:00:00",
            import_cents=5.0,
            export_cents=0.0,
            period="offpeak",
        ),
        ev_planner.PriceForecast(
            hour="2026-07-16T19:00:00",
            import_cents=40.0,
            export_cents=0.0,
            period="peak",
        ),
    ]
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour="2026-07-16T19:10:00",
            solar_kw=3.0,
            load_kw=0.0,
            surplus_kw=3.0,
            confidence=0.8,
        ),
        ev_planner.SurplusForecast(
            hour="2026-07-16T18:10:00",
            solar_kw=0.0,
            load_kw=0.0,
            surplus_kw=0.0,
            confidence=0.8,
        ),
    ]

    plan = asyncio.run(
        planner._plan_cost_optimized(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=70,
            target_time=datetime(2026, 7, 16, 21, 0, tzinfo=brisbane_tz),
            energy_needed_kwh=3.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    assert len(plan.windows) == 1
    assert plan.windows[0].start_time == "2026-07-16T19:00:00"
    assert plan.windows[0].source == "solar_surplus"
    assert plan.estimated_solar_kwh == pytest.approx(3.0)


def test_time_critical_matches_grid_price_to_surplus_hour(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 7, 16, 18, 0, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )
    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour="2026-07-16T18:10:00",
            solar_kw=0.0,
            load_kw=0.0,
            surplus_kw=0.0,
            confidence=0.8,
        ),
        ev_planner.SurplusForecast(
            hour="2026-07-16T19:10:00",
            solar_kw=3.0,
            load_kw=0.0,
            surplus_kw=3.0,
            confidence=0.8,
        ),
    ]
    price_forecast = [
        ev_planner.PriceForecast(
            hour="2026-07-16T19:00:00",
            import_cents=40.0,
            export_cents=0.0,
            period="peak",
        ),
        ev_planner.PriceForecast(
            hour="2026-07-16T18:00:00",
            import_cents=5.0,
            export_cents=0.0,
            period="offpeak",
        ),
    ]

    plan = asyncio.run(
        planner._plan_time_critical(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=80,
            target_time=datetime(2026, 7, 16, 21, 0, tzinfo=brisbane_tz),
            energy_needed_kwh=10.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    grid_windows = [window for window in plan.windows if window.source.startswith("grid")]
    assert len(grid_windows) == 1
    assert grid_windows[0].start_time == "2026-07-16T18:00:00"
    assert grid_windows[0].price_cents_kwh == pytest.approx(5.0)


def test_time_critical_keeps_priced_grid_hours_when_solar_horizon_is_short(
    monkeypatch,
):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 7, 16, 18, 0, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )
    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour="2026-07-16T19:00:00",
            solar_kw=3.0,
            load_kw=0.0,
            surplus_kw=3.0,
            confidence=0.8,
        )
    ]
    price_forecast = [
        ev_planner.PriceForecast(
            hour="2026-07-16T18:00:00",
            import_cents=5.0,
            export_cents=0.0,
            period="offpeak",
        ),
        ev_planner.PriceForecast(
            hour="2026-07-16T19:00:00",
            import_cents=40.0,
            export_cents=0.0,
            period="peak",
        ),
    ]

    plan = asyncio.run(
        planner._plan_time_critical(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=80,
            target_time=datetime(2026, 7, 16, 21, 0, tzinfo=brisbane_tz),
            energy_needed_kwh=10.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    assert [window.start_time for window in plan.windows] == [
        "2026-07-16T18:00:00",
        "2026-07-16T19:00:00",
    ]
    assert plan.estimated_grid_kwh == pytest.approx(7.0)
    assert plan.estimated_solar_kwh == pytest.approx(3.0)
    assert plan.can_meet_target is True


def test_forecast_hour_key_preserves_repeated_dst_hours():
    first = ev_planner._forecast_hour_key("2026-11-01T01:00:00-04:00")
    second = ev_planner._forecast_hour_key("2026-11-01T01:00:00-05:00")

    assert first == "2026-11-01T05:00:00+00:00"
    assert second == "2026-11-01T06:00:00+00:00"
    assert first != second


def test_amber_forecast_preserves_repeated_dst_hours(monkeypatch):
    new_york = ZoneInfo("America/New_York")
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 11, 1, 0, 0, tzinfo=new_york),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(new_york),
        raising=False,
    )
    rows = []
    for hour, price in (
        ("2026-11-01T01:00:00-04:00", 10.0),
        ("2026-11-01T01:00:00-05:00", 20.0),
    ):
        rows.extend(
            (
                {
                    "startTime": hour,
                    "channelType": "general",
                    "perKwh": price,
                },
                {
                    "startTime": hour,
                    "channelType": "feedIn",
                    "perKwh": 5.0,
                },
            )
        )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["amber_coordinator"] = SimpleNamespace(
        data={"forecast": rows}
    )
    config_entry = _FakeConfigEntry()
    config_entry.data = {"amber_api_token": "token"}

    forecast = asyncio.run(
        ev_planner.PriceForecaster(hass, config_entry)._get_amber_forecast(2)
    )

    assert forecast is not None
    assert [row.hour for row in forecast] == [
        "2026-11-01T01:00:00-04:00",
        "2026-11-01T01:00:00-05:00",
    ]
    assert [row.import_cents for row in forecast] == [10.0, 20.0]


def test_ev_planners_keep_repeated_dst_hours_distinct(monkeypatch):
    new_york = ZoneInfo("America/New_York")
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 11, 1, 0, 0, tzinfo=new_york),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(new_york),
        raising=False,
    )
    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    price_forecast = [
        ev_planner.PriceForecast(
            hour=hour,
            import_cents=price,
            export_cents=0.0,
            period="offpeak",
        )
        for hour, price in (
            ("2026-11-01T01:00:00-04:00", 10.0),
            ("2026-11-01T01:00:00-05:00", 20.0),
        )
    ]
    target = datetime(2026, 11, 1, 3, 0, tzinfo=new_york)

    cheapest = asyncio.run(
        planner._plan_cost_optimized(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=80,
            target_time=target,
            energy_needed_kwh=10.0,
            charger_power_kw=7.36,
            surplus_forecast=[],
            price_forecast=price_forecast,
        )
    )
    deadline = asyncio.run(
        planner._plan_time_critical(
            vehicle_id=VIN,
            current_soc=60,
            target_soc=80,
            target_time=target,
            energy_needed_kwh=10.0,
            charger_power_kw=7.36,
            surplus_forecast=[],
            price_forecast=price_forecast,
        )
    )

    expected_starts = [
        "2026-11-01T01:00:00-04:00",
        "2026-11-01T01:00:00-05:00",
    ]
    assert [window.start_time for window in cheapest.windows] == expected_starts
    assert [window.start_time for window in deadline.windows] == expected_starts
    assert cheapest.can_meet_target is True
    assert deadline.can_meet_target is True


@pytest.mark.parametrize(
    ("current_time", "expected_source"),
    (
        (datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-4))), "first"),
        (datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-5))), "second"),
    ),
)
def test_should_charge_now_activates_each_repeated_dst_window(
    monkeypatch,
    current_time,
    expected_source,
):
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda: current_time)
    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    plan = ev_planner.ChargingPlan(
        vehicle_id=VIN,
        current_soc=60,
        target_soc=80,
        target_time="2026-11-01T03:00:00-05:00",
        energy_needed_kwh=10.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time="2026-11-01T01:00:00-04:00",
                end_time="2026-11-01T01:00:00-05:00",
                source="first",
                estimated_power_kw=7.36,
                estimated_energy_kwh=7.36,
                price_cents_kwh=10.0,
                reason="offpeak_rate",
            ),
            ev_planner.PlannedChargingWindow(
                start_time="2026-11-01T01:00:00-05:00",
                end_time="2026-11-01T02:00:00-05:00",
                source="second",
                estimated_power_kw=7.36,
                estimated_energy_kwh=2.64,
                price_cents_kwh=20.0,
                reason="offpeak_rate",
            ),
        ],
    )

    should_charge, _reason, source = asyncio.run(
        planner.should_charge_now(
            vehicle_id=VIN,
            plan=plan,
            current_surplus_kw=0.0,
            current_price_cents=50.0,
            battery_soc=80.0,
        )
    )

    assert should_charge is True
    assert source == expected_source


def test_cost_optimized_uses_ha_local_clock_for_window_filtering(monkeypatch):
    real_datetime = datetime
    brisbane_tz = timezone(timedelta(hours=10))

    class HostClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            wrong_host_now = real_datetime(2026, 7, 4, 8, 20)
            if tz is not None:
                return wrong_host_now.replace(tzinfo=timezone.utc).astimezone(tz)
            return wrong_host_now

        @classmethod
        def fromisoformat(cls, value):
            return real_datetime.fromisoformat(value)

    monkeypatch.setattr(ev_planner, "datetime", HostClockDatetime)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda *args, **kwargs: real_datetime(2026, 7, 3, 19, 20, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )

    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    price_forecast = [
        ev_planner.PriceForecast(
            hour="2026-07-03T20:00:00",
            import_cents=12.0,
            export_cents=5.0,
            period="offpeak",
        ),
        ev_planner.PriceForecast(
            hour="2026-07-03T21:00:00",
            import_cents=14.0,
            export_cents=5.0,
            period="offpeak",
        ),
    ]
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour=price.hour,
            solar_kw=0.0,
            load_kw=0.0,
            surplus_kw=0.0,
            confidence=0.8,
        )
        for price in price_forecast
    ]

    plan = asyncio.run(
        planner._plan_cost_optimized(
            vehicle_id=VIN,
            current_soc=50,
            target_soc=60,
            target_time=real_datetime(2026, 7, 4, 7, 30, tzinfo=brisbane_tz),
            energy_needed_kwh=8.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    assert plan.windows
    assert plan.estimated_grid_kwh > 0


def test_cost_optimized_converts_utc_forecast_rows_before_filtering(monkeypatch):
    brisbane_tz = timezone(timedelta(hours=10))
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 7, 3, 21, 19, tzinfo=brisbane_tz),
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "as_local",
        lambda value: value.astimezone(brisbane_tz),
        raising=False,
    )

    planner = ev_planner.ChargingPlanner(_FakeHass(), _FakeConfigEntry())
    price_forecast = [
        ev_planner.PriceForecast(
            hour="2026-07-03T12:00:00+00:00",
            import_cents=16.0,
            export_cents=5.0,
            period="offpeak",
        ),
        ev_planner.PriceForecast(
            hour="2026-07-03T13:00:00+00:00",
            import_cents=12.0,
            export_cents=5.0,
            period="offpeak",
        ),
    ]
    surplus_forecast = [
        ev_planner.SurplusForecast(
            hour=price.hour,
            solar_kw=0.0,
            load_kw=0.0,
            surplus_kw=0.0,
            confidence=0.8,
        )
        for price in price_forecast
    ]

    plan = asyncio.run(
        planner._plan_cost_optimized(
            vehicle_id=VIN,
            current_soc=68,
            target_soc=90,
            target_time=datetime(2026, 7, 4, 7, 30, tzinfo=brisbane_tz),
            energy_needed_kwh=10.0,
            charger_power_kw=7.36,
            surplus_forecast=surplus_forecast,
            price_forecast=price_forecast,
        )
    )

    assert len(plan.windows) == 2
    assert plan.windows[0].start_time == "2026-07-03T22:00:00"
    assert plan.windows[1].start_time == "2026-07-03T23:00:00"
    assert plan.windows[1].price_cents_kwh == 12.0
    assert plan.estimated_grid_kwh > 0


class _FakeConfigEntry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _FakeHass:
    def __init__(
        self,
        enabled: bool = True,
        price_settings: dict | None = None,
        states: dict[str, str] | None = None,
        entries: list | None = None,
    ) -> None:
        settings = {"enabled": enabled}
        if price_settings:
            settings.update(price_settings)

        self.data = {
            "power_sync": {
                "entry-1": {
                    "automation_store": SimpleNamespace(
                        _data={"price_level_charging": settings}
                    )
                }
            }
        }
        self.entity_registry = SimpleNamespace(entities={})
        self.device_registry = SimpleNamespace(devices={})
        self.states = _FakeStates(states)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain=None: entries or []
        )
        self.services = SimpleNamespace(async_call=AsyncMock())


class _FakeStates:
    def __init__(self, states: dict[str, str] | None = None) -> None:
        self._states = {
            entity_id: SimpleNamespace(entity_id=entity_id, state=state, attributes={})
            for entity_id, state in (states or {}).items()
        }

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_all(self):
        return list(self._states.values())

    def async_entity_ids(self, domain: str):
        prefix = f"{domain}."
        return [entity_id for entity_id in self._states if entity_id.startswith(prefix)]


class _FakeTeslaResponse:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeTeslaSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _FakeTeslaResponse(
            payload={
                "response": {
                    "backup_reserve_percent": 42,
                    "components": {"customer_preferred_export_rule": "pv_only"},
                }
            }
        )

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeTeslaResponse()


@pytest.fixture
def fake_actions(monkeypatch):
    actions = types.ModuleType("power_sync.automations.actions")
    actions.DEFAULT_VEHICLE_ID = "_default"
    actions._dynamic_ev_state = {}

    async def resolve_max_grid_import_kw(hass, config_entry, params=None):
        explicit = (params or {}).get("max_grid_import_kw")
        if explicit:
            return explicit

        entry_data = hass.data["power_sync"][config_entry.entry_id]
        coordinator = entry_data.get("tesla_coordinator")
        site_info = getattr(coordinator, "_site_info_cache", None) if coordinator else None
        if isinstance(site_info, dict) and site_info.get("max_site_meter_power_ac"):
            return float(site_info["max_site_meter_power_ac"])

        settings = entry_data.get("automation_store")._data.get("home_power_settings", {})
        amps = int(float(settings.get("max_grid_import_amps") or 0))
        if amps <= 0:
            return None
        phases = 3 if settings.get("phase_type") == "three" else 1
        voltage = int(float(settings.get("default_voltage") or 240))
        return round(amps * voltage * phases / 1000.0, 3)

    actions._resolve_max_grid_import_kw = resolve_max_grid_import_kw
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    return actions


async def _one_vehicle(*args, **kwargs):
    return [{"vin": VIN, "name": "Model 3"}]


async def _no_vehicles(*args, **kwargs):
    return []


def test_auto_schedule_tesla_helpers_use_powersync_proxy_base(monkeypatch):
    const = importlib.import_module("power_sync.const")
    fake_session = _FakeTeslaSession()
    hass = _FakeHass()
    hass.session = fake_session

    class PowerSyncEntry(_FakeConfigEntry):
        data = {
            const.CONF_TESLA_ENERGY_SITE_ID: "site-1",
            const.CONF_FLEET_API_BASE_URL: "https://fleet.example.test",
        }

    monkeypatch.setattr(
        sys.modules["power_sync"],
        "get_tesla_api_token",
        lambda hass, entry: ("psync_test", const.TESLA_PROVIDER_POWERSYNC),
        raising=False,
    )
    monkeypatch.setattr(
        _ha_aiohttp_client,
        "async_get_clientsession",
        lambda hass: hass.session,
        raising=False,
    )
    monkeypatch.setattr(
        ev_planner.aiohttp,
        "ClientTimeout",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )

    async def run_helpers():
        executor = ev_planner.AutoScheduleExecutor(
            hass,
            PowerSyncEntry(),
            planner=SimpleNamespace(),
        )
        reserve = await executor._get_tesla_backup_reserve()
        reserve_set = await executor._set_tesla_backup_reserve(35)
        export_rule = await executor._get_current_export_rule()
        export_set = await executor._set_export_rule("battery_ok")
        return reserve, reserve_set, export_rule, export_set

    assert asyncio.run(run_helpers()) == (42, True, "pv_only", True)
    assert [call[0] for call in fake_session.calls] == ["GET", "POST", "GET", "POST"]
    assert all(
        url.startswith(f"{const.POWERSYNC_API_BASE_URL}/api/1/energy_sites/site-1/")
        for _method, url, _kwargs in fake_session.calls
    )
    assert not any("fleet.example.test" in url for _method, url, _kwargs in fake_session.calls)
    assert all(
        kwargs["headers"]["Authorization"] == "Bearer psync_test"
        for _method, _url, kwargs in fake_session.calls
    )


def test_price_level_leaves_solar_surplus_owned_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {
        "entry-1": {
            VIN: {
                "active": True,
                "params": {"dynamic_mode": "solar_surplus"},
            }
        }
    }

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not probe or stop another owned session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "solar_surplus mode owns" in state.last_decision_reason


def test_price_level_leaves_manual_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {
        "entry-1": {
            VIN: {
                "active": True,
                "params": {"dynamic_mode": "manual"},
            }
        }
    }

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not stop a manual session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "manual mode owns" in state.last_decision_reason


def test_stop_guard_blocks_other_owner_family():
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="manual")

    allowed = ev_planner._can_stop_owned_loadpoint(
        hass,
        entry,
        VIN,
        expected_owner_mode="price_level_recovery",
    )

    assert allowed is False
    last_command = ev_ownership.get_ev_last_commands(hass, entry)[VIN]
    assert last_command["command"] == "stop"
    assert last_command["success"] is False
    assert "manual" in last_command["reason"]


def test_stop_guard_allows_same_owner_family():
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="price_level_recovery")

    allowed = ev_planner._can_stop_owned_loadpoint(
        hass,
        entry,
        VIN,
        expected_owner_mode="price_level_opportunity",
    )

    assert allowed is True


def test_price_level_stop_allows_unowned_high_price_stop(fake_actions):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    result = asyncio.run(executor._stop_charging("Price above threshold", vehicle_vin=VIN))

    assert result is True
    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == VIN
    assert params["vehicle_vin"] == VIN
    assert params["stop_untracked"] is True
    assert params["stop_reason"] == "Price above threshold"


def test_price_level_start_uses_vehicle_charger_config(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": "generic_ev",
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
        "min_amps": 6,
        "max_amps": 24,
        "voltage": 240,
        "phases": 3,
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(
        executor._start_charging(
            "price_level_recovery",
            "Cheap price",
            vehicle_vin="generic_ev",
        )
    )

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["charger_amps_entity"] == "number.garage_ev_current"
    assert params["charger_status_entity"] == "sensor.garage_ev_status"
    assert params["max_charge_amps"] == 24
    assert params["phases"] == 3
    assert params["allow_ownership_takeover"] is True


def test_vehicle_charger_params_accept_app_charge_amp_aliases(fake_actions):
    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": VIN,
        "charger_type": "tesla",
        "min_charge_amps": 6,
        "max_charge_amps": 15,
        "voltage": 240,
        "phases": 1,
    }]

    params = ev_planner._get_vehicle_charger_params(
        hass,
        "power_sync",
        _FakeConfigEntry(),
        VIN,
    )

    assert params["min_charge_amps"] == 6
    assert params["max_charge_amps"] == 15
    assert params["voltage"] == 240
    assert params["phases"] == 1


def test_price_level_sigenergy_start_uses_zero_battery_target(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.10",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["optimization_coordinator"] = SimpleNamespace(
        _config=SimpleNamespace(max_charge_w=10500, max_discharge_w=9900)
    )

    executor = ev_planner.PriceLevelChargingExecutor(hass, SigenergyEntry())
    result = asyncio.run(
        executor._start_charging(
            "price_level_opportunity",
            "Cheap price",
            vehicle_vin="sigenergy_charger",
        )
    )

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "sigenergy_charger"
    assert params["vehicle_vin"] == "sigenergy_charger"
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_host"] == "192.0.2.10"
    assert params["target_battery_charge_kw"] == 0
    assert params["max_inverter_kw"] == 9.9
    assert params["max_battery_charge_rate_kw"] == 10.5


def test_generic_price_level_start_uses_generic_loadpoint_id(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class GenericEntry(_FakeConfigEntry):
        options = {
            "generic_charger_enabled": True,
            "generic_charger_switch_entity": "switch.garage_ev",
            "generic_charger_amps_entity": "number.garage_ev_current",
        }

    async def wants_charge(self, current_price_cents):
        return True, "Cheap price", "price_level_opportunity"

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _no_vehicles)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision",
        wants_charge,
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": VIN,
        "charger_type": "tesla",
        "min_amps": 5,
        "max_amps": 32,
        "voltage": 230,
        "phases": 1,
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, GenericEntry())
    results = asyncio.run(executor.evaluate_all_vehicles(5))

    assert results["generic_ev"] == (True, "Cheap price", "price_level_opportunity")
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["allow_ownership_takeover"] is True


def test_price_level_generic_stop_ignores_stale_tesla_vehicle_config(fake_actions):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    class GenericEntry(_FakeConfigEntry):
        options = {
            "generic_charger_enabled": True,
            "generic_charger_switch_entity": "switch.garage_ev",
            "generic_charger_amps_entity": "number.garage_ev_current",
        }

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": VIN,
        "charger_type": "tesla",
        "min_amps": 5,
        "max_amps": 32,
        "voltage": 230,
        "phases": 1,
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, GenericEntry())
    result = asyncio.run(executor._stop_charging("Price above threshold", "generic_ev"))

    assert result is True
    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"


def test_scheduled_generic_start_uses_generic_loadpoint_id(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class GenericEntry(_FakeConfigEntry):
        options = {
            "generic_charger_enabled": True,
            "generic_charger_switch_entity": "switch.garage_ev",
            "generic_charger_amps_entity": "number.garage_ev_current",
        }

    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), GenericEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["allow_ownership_takeover"] is True


def test_scheduled_ocpp_start_uses_ocpp_loadpoint_id(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class OcppEntry(_FakeConfigEntry):
        options = {
            "ocpp_enabled": True,
            "ocpp_charger_id": "evse_1",
        }

    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), OcppEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "ocpp_evse_1"
    assert params["vehicle_vin"] == "ocpp_evse_1"
    assert params["charger_type"] == "ocpp"
    assert params["ocpp_charger_id"] == "evse_1"
    assert params["allow_ownership_takeover"] is True


def test_scheduled_sigenergy_start_uses_sigenergy_charger_loadpoint(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.10",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": "LRWYHCEK3PC907290",
        "charger_type": "tesla",
        "min_amps": 5,
        "max_amps": 32,
        "voltage": 230,
        "phases": 1,
    }]

    executor = ev_planner.ScheduledChargingExecutor(hass, SigenergyEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "sigenergy_charger"
    assert params["vehicle_vin"] == "sigenergy_charger"
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_host"] == "192.0.2.10"
    assert params["sigenergy_charger_slave_id"] == 1
    assert params["allow_ownership_takeover"] is True


def test_solar_surplus_config_falls_back_to_sigenergy_entry_charger():
    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.10",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    configs = ev_planner.get_solar_surplus_vehicle_configs(
        _FakeHass(),
        SigenergyEntry(),
        {"solar_surplus_config": {"enabled": True}},
    )

    assert len(configs) == 1
    config = configs[0]
    assert config["vehicle_id"] == "sigenergy_charger"
    assert config["display_name"] == "Sigenergy charger"
    assert config["charger_type"] == "sigenergy"
    assert config["sigenergy_charger_host"] == "192.0.2.10"
    assert config["sigenergy_charger_port"] == 502
    assert config["sigenergy_charger_slave_id"] == 1
    assert config["sigenergy_charger_type"] == "evac"
    assert config["supports_rate_control"] is True
    assert config["control_strategy"] == "dynamic_rate"


def test_solar_surplus_config_marks_sigenergy_evdc_native_handoff():
    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.11",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        }

    configs = ev_planner.get_solar_surplus_vehicle_configs(
        _FakeHass(),
        SigenergyEntry(),
        {"solar_surplus_config": {"enabled": True}},
    )

    assert len(configs) == 1
    config = configs[0]
    assert config["sigenergy_charger_type"] == "evdc"
    assert config["supports_rate_control"] is False
    assert config["supports_restart_while_plugged"] is False
    assert config["control_strategy"] == "one_shot"
    assert config["solar_control_strategy"] == "native_handoff"


def test_scheduled_preserve_home_battery_sets_optimizer_intent(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {"preserve_home_battery": True}

    executor = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["mode"] == "no_discharge_charge_allowed"

    result = asyncio.run(executor._stop_charging("Outside schedule"))

    assert result is True
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is False


def test_scheduled_no_grid_import_passes_dynamic_start_param(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {"no_grid_import": True}

    executor = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["no_grid_import"] is True


def test_scheduled_tesla_start_targets_single_plugged_home_vehicle(
    monkeypatch,
    fake_actions,
):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def two_vehicles(*args, **kwargs):
        return [
            {"vin": first_vin, "name": "Tesla_Flinn"},
            {"vin": second_vin, "name": "Tesla_YF88"},
        ]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (first_vin, second_vin)
        return "home"

    async def plugged_in(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (first_vin, second_vin)
        return vehicle_vin == second_vin

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", two_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)

    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), _FakeConfigEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == second_vin
    assert params["vehicle_vin"] == second_vin


def test_scheduled_tesla_start_targets_all_plugged_home_vehicles(
    monkeypatch,
    fake_actions,
):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def two_vehicles(*args, **kwargs):
        return [
            {"vin": first_vin, "name": "Tesla_Flinn"},
            {"vin": second_vin, "name": "Tesla_YF88"},
        ]

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", two_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", AsyncMock(return_value="home"))
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", AsyncMock(return_value=False))

    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), _FakeConfigEntry())
    result = asyncio.run(executor._start_charging("Scheduled window"))

    assert result is True
    assert fake_actions._action_start_ev_charging_dynamic.await_count == 2
    started_vins = {
        call.args[2]["vehicle_vin"]
        for call in fake_actions._action_start_ev_charging_dynamic.await_args_list
    }
    assert started_vins == {first_vin, second_vin}


def test_scheduled_coordinator_starts_second_tesla_when_first_already_charging(
    monkeypatch,
    fake_actions,
):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 50,
    }

    async def two_vehicles(*args, **kwargs):
        return [
            {"vin": first_vin, "name": "Tesla_Flinn"},
            {"vin": second_vin, "name": "Tesla_YF88"},
        ]

    async def active_state(_hass, _entry, vehicle_vin=None):
        return vehicle_vin == first_vin

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", two_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", AsyncMock(return_value="home"))
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", active_state)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 11, 30, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())
    coordinator._is_charging = True

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 0))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == second_vin
    assert params["vehicle_vin"] == second_vin


def test_price_level_preserve_home_battery_sets_optimizer_intent(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass(price_settings={"preserve_home_battery": True})

    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(
        executor._start_charging(
            "price_level_opportunity",
            "Cheap price",
            vehicle_vin="generic_ev",
        )
    )

    assert result is True
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "price_level_charging"
    assert preserve_state["mode"] == "no_discharge_charge_allowed"

    result = asyncio.run(executor._stop_charging("Price above threshold", "generic_ev"))

    assert result is True
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is False
    assert preserve_state["source"] == "price_level_charging"


def test_scheduled_stops_external_charging_outside_window(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 35,
    }

    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def actively_charging(*args, **kwargs):
        return True

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", actively_charging)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 15, 8, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 33))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["stop_untracked"] is True
    assert params["stop_reason"] == "Outside schedule (11:00-14:00)"
    assert scheduled.get_state()["last_decision"] == "stopped"


def test_scheduled_stops_active_second_tesla_outside_window(monkeypatch, fake_actions):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 35,
    }

    async def two_vehicles(*args, **kwargs):
        return [
            {"vin": first_vin, "name": "Tesla_Flinn"},
            {"vin": second_vin, "name": "Tesla_YF88"},
        ]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (None, first_vin, second_vin)
        return "home"

    async def actively_charging(_hass, _entry, vehicle_vin=None):
        return vehicle_vin == second_vin

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", two_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", actively_charging)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 15, 8, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 33))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == second_vin
    assert params["vehicle_vin"] == second_vin
    assert params["stop_untracked"] is True
    assert params["stop_reason"] == "Outside schedule (11:00-14:00)"
    assert scheduled.get_state()["last_decision"] == "stopped"


def test_unspecified_ev_location_prefers_home_vehicle(monkeypatch):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    hass = _FakeHass(
        states={
            "device_tracker.tesla_flinn_location": "not_home",
            "binary_sensor.tesla_yf88_located_at_home": "on",
        }
    )
    hass.device_registry = SimpleNamespace(
        devices={
            "device-1": SimpleNamespace(
                id="device-1",
                identifiers={("tesla_fleet", first_vin)},
            ),
            "device-2": SimpleNamespace(
                id="device-2",
                identifiers={("tesla_fleet", second_vin)},
            ),
        }
    )
    hass.entity_registry = SimpleNamespace(
        entities={
            "device_tracker.tesla_flinn_location": SimpleNamespace(
                entity_id="device_tracker.tesla_flinn_location",
                device_id="device-1",
            ),
            "binary_sensor.tesla_yf88_located_at_home": SimpleNamespace(
                entity_id="binary_sensor.tesla_yf88_located_at_home",
                device_id="device-2",
            ),
        }
    )
    monkeypatch.setattr(_ha_dr, "async_get", lambda _hass: _hass.device_registry)

    assert asyncio.run(ev_planner.get_ev_location(hass, _FakeConfigEntry())) == "home"
    assert (
        asyncio.run(
            ev_planner.get_ev_location(hass, _FakeConfigEntry(), vehicle_vin=first_vin)
        )
        == "not_home"
    )


def test_unspecified_ev_plug_state_checks_later_home_vehicle(monkeypatch):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    hass = _FakeHass(
        states={
            "binary_sensor.tesla_flinn_charge_cable": "off",
            "binary_sensor.tesla_yf88_charge_cable": "on",
        }
    )
    hass.device_registry = SimpleNamespace(
        devices={
            "device-1": SimpleNamespace(
                id="device-1",
                identifiers={("tesla_fleet", first_vin)},
            ),
            "device-2": SimpleNamespace(
                id="device-2",
                identifiers={("tesla_fleet", second_vin)},
            ),
        }
    )
    hass.entity_registry = SimpleNamespace(
        entities={
            "binary_sensor.tesla_flinn_charge_cable": SimpleNamespace(
                entity_id="binary_sensor.tesla_flinn_charge_cable",
                device_id="device-1",
            ),
            "binary_sensor.tesla_yf88_charge_cable": SimpleNamespace(
                entity_id="binary_sensor.tesla_yf88_charge_cable",
                device_id="device-2",
            ),
        }
    )
    monkeypatch.setattr(_ha_dr, "async_get", lambda _hass: _hass.device_registry)

    assert asyncio.run(ev_planner.is_ev_plugged_in(hass, _FakeConfigEntry())) is True
    assert (
        asyncio.run(
            ev_planner.is_ev_plugged_in(hass, _FakeConfigEntry(), vehicle_vin=first_vin)
        )
        is False
    )
    assert (
        asyncio.run(
            ev_planner.is_ev_plugged_in(hass, _FakeConfigEntry(), vehicle_vin=second_vin)
        )
        is True
    )


def test_external_scheduled_guard_does_not_report_away_vehicle_when_home_vehicle_seen(
    monkeypatch,
    fake_actions,
):
    first_vin = "XP7YHCEL7TB811704"
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    hass = _FakeHass()

    async def two_vehicles(*args, **kwargs):
        return [
            {"vin": first_vin, "name": "Tesla_Flinn"},
            {"vin": second_vin, "name": "Tesla_YF88"},
        ]

    async def location(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (first_vin, second_vin)
        return "home" if vehicle_vin == second_vin else "not_home"

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", two_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", location)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", AsyncMock(return_value=False))

    vehicle_vin, can_stop, reason = asyncio.run(
        ev_planner._find_external_scheduled_charging_vehicle(hass, _FakeConfigEntry())
    )

    assert vehicle_vin is None
    assert can_stop is False
    assert reason == "no active external scheduled session"


def test_scheduled_external_stop_is_not_repeated_while_tesla_state_is_stale(
    monkeypatch,
    fake_actions,
):
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 35,
    }

    async def one_vehicle(*args, **kwargs):
        return [{"vin": second_vin, "name": "Tesla_YF88"}]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (None, second_vin)
        return "home"

    async def still_reports_charging(_hass, _entry, vehicle_vin=None):
        return vehicle_vin == second_vin

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", one_vehicle)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", still_reports_charging)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 15, 8, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 33))
        asyncio.run(coordinator.evaluate({}, 33))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()


def test_scheduled_leaves_solar_surplus_owned_session_alone(
    monkeypatch,
    fake_actions,
):
    fake_actions._dynamic_ev_state = {
        "entry-1": {
            VIN: {
                "active": True,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "owner_mode": "solar_surplus",
                },
            }
        }
    }
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 35,
    }

    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    active_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", active_probe)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 15, 8, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 33))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    active_probe.assert_not_awaited()
    fake_actions._action_stop_ev_charging_dynamic.assert_not_awaited()
    assert scheduled.get_state()["last_decision"] == "waiting"


def test_scheduled_does_not_stop_external_charging_when_vehicle_away(
    monkeypatch,
    fake_actions,
):
    fake_actions._dynamic_ev_state = {}
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "scheduled_charging"
    ] = {
        "enabled": True,
        "start_time": "11:00",
        "end_time": "14:00",
        "max_price_cents": 35,
    }

    async def away(*args, **kwargs):
        return "not_home"

    async def actively_charging(*args, **kwargs):
        return True

    active_probe = AsyncMock(side_effect=actively_charging)
    monkeypatch.setattr(ev_planner, "get_ev_location", away)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", active_probe)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 27, 15, 8, tzinfo=timezone.utc),
    )

    previous_executor = ev_planner.get_scheduled_charging_executor()
    previous_price_executor = ev_planner.get_price_level_executor()
    scheduled = ev_planner.ScheduledChargingExecutor(hass, _FakeConfigEntry())
    coordinator = ev_planner.EVChargingModeCoordinator(hass, _FakeConfigEntry())

    try:
        ev_planner.set_scheduled_charging_executor(scheduled)
        ev_planner.set_price_level_executor(None)

        asyncio.run(coordinator.evaluate({}, 33))
    finally:
        ev_planner.set_scheduled_charging_executor(previous_executor)
        ev_planner.set_price_level_executor(previous_price_executor)

    active_probe.assert_not_awaited()
    fake_actions._action_stop_ev_charging_dynamic.assert_not_awaited()
    assert scheduled.get_state()["last_decision"] == "waiting"


def test_scheduled_time_window_excludes_end_boundary(monkeypatch):
    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), _FakeConfigEntry())

    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 21, 15, 0, tzinfo=timezone.utc),
    )

    assert executor._is_in_time_window("11:00", "15:00") is False


def test_scheduled_overnight_time_window_excludes_end_boundary(monkeypatch):
    executor = ev_planner.ScheduledChargingExecutor(_FakeHass(), _FakeConfigEntry())

    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc),
    )

    assert executor._is_in_time_window("22:00", "06:00") is False


def test_auto_schedule_sigenergy_start_uses_modbus_backend(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.20",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        SigenergyEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="sigenergy_charger",
        display_name="Sigenergy EVAC",
        charger_type="tesla",
        max_charge_amps=30,
    )
    state = ev_planner.AutoScheduleState(vehicle_id="sigenergy_charger")

    asyncio.run(executor._start_charging("sigenergy_charger", settings, state, "grid_opportunistic"))

    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "sigenergy_charger"
    assert params["vehicle_vin"] == "sigenergy_charger"
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_host"] == "192.0.2.20"
    assert params["sigenergy_charger_slave_id"] == 1
    assert params["target_battery_charge_kw"] == 0


def test_auto_schedule_blank_charger_type_uses_configured_sigenergy(
    monkeypatch,
    fake_actions,
):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.21",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        SigenergyEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="sigenergy_charger",
        display_name="Sigenergy EVAC",
        charger_type="",
        max_charge_amps=30,
    )
    state = ev_planner.AutoScheduleState(vehicle_id="sigenergy_charger")

    asyncio.run(executor._start_charging("sigenergy_charger", settings, state, "grid_opportunistic"))

    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "sigenergy_charger"
    assert params["vehicle_vin"] == "sigenergy_charger"
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_host"] == "192.0.2.21"


def test_auto_schedule_rate_update_blank_charger_type_uses_configured_sigenergy(
    monkeypatch,
    fake_actions,
):
    set_amps_calls = []

    async def set_vehicle_amps(hass, entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps, params))
        return True

    fake_actions._set_vehicle_amps = set_vehicle_amps
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.22",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        }

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        SigenergyEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="sigenergy_charger",
        display_name="Sigenergy EVAC",
        charger_type="",
        min_charge_amps=5,
        max_charge_amps=32,
        voltage=230,
        phases=1,
    )

    assert asyncio.run(
        executor._set_vehicle_charge_rate("sigenergy_charger", 3680, settings)
    )

    assert len(set_amps_calls) == 1
    vehicle_id, amps, params = set_amps_calls[0]
    assert vehicle_id == "sigenergy_charger"
    assert amps == 16
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_host"] == "192.0.2.22"


def test_auto_schedule_rate_update_skips_sigenergy_evdc(
    monkeypatch,
    fake_actions,
):
    set_amps_calls = []

    async def set_vehicle_amps(hass, entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps, params))
        return True

    fake_actions._set_vehicle_amps = set_vehicle_amps
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.23",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        }

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        SigenergyEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="sigenergy_charger",
        display_name="Sigenergy EVDC",
        charger_type="",
        min_charge_amps=6,
        max_charge_amps=32,
        voltage=230,
        phases=1,
    )

    assert asyncio.run(
        executor._set_vehicle_charge_rate("sigenergy_charger", 3680, settings)
    )
    assert set_amps_calls == []


def test_auto_schedule_rate_update_uses_configured_sigenergy_evdc_rate_entity(
    monkeypatch,
    fake_actions,
):
    set_amps_calls = []

    async def set_vehicle_amps(hass, entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps, params))
        return True

    fake_actions._set_vehicle_amps = set_vehicle_amps
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    class SigenergyEntry(_FakeConfigEntry):
        options = {
            "sigenergy_charger_enabled": True,
            "sigenergy_charger_host": "192.0.2.24",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
            "sigenergy_charger_charge_power_limit_entity": "number.evdc_charge_limit",
        }

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        SigenergyEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="sigenergy_charger",
        display_name="Sigenergy EVDC",
        charger_type="",
        min_charge_amps=6,
        max_charge_amps=32,
        voltage=230,
        phases=1,
    )

    assert asyncio.run(
        executor._set_vehicle_charge_rate("sigenergy_charger", 3680, settings)
    )

    assert len(set_amps_calls) == 1
    vehicle_id, amps, params = set_amps_calls[0]
    assert vehicle_id == "sigenergy_charger"
    assert amps == 16
    assert params["charger_type"] == "sigenergy"
    assert params["sigenergy_charger_type"] == "evdc"
    assert params["supports_rate_control"] is True
    assert params["solar_control_strategy"] == "dynamic_rate"
    assert params["sigenergy_charger_charge_power_limit_entity"] == "number.evdc_charge_limit"


def test_price_level_ocpp_start_uses_detected_hacs_prefix(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def wants_charge(self, current_price_cents):
        return True, "Opportunity", "price_level_opportunity"

    class OcppEntry(_FakeConfigEntry):
        options = {"ocpp_enabled": True}

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _no_vehicles)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision",
        wants_charge,
    )

    hass = _FakeHass(
        states={
            "switch.charger_charge_control": "off",
            "sensor.charger_status_connector": "Finishing",
        }
    )
    executor = ev_planner.PriceLevelChargingExecutor(hass, OcppEntry())
    results = asyncio.run(executor.evaluate_all_vehicles(10))

    assert "ocpp_charger" in results
    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "ocpp_charger"
    assert params["vehicle_vin"] == "ocpp_charger"
    assert params["charger_type"] == "ocpp"
    assert params["ocpp_charger_id"] == "charger"


def test_auto_schedule_start_allows_solar_surplus_takeover(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
        charger_type="generic",
        charger_switch_entity="switch.garage_ev",
        charger_amps_entity="number.garage_ev_current",
        charger_status_entity="sensor.garage_ev_status",
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)

    asyncio.run(executor._start_charging(VIN, settings, state, "grid_offpeak"))

    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["owner_mode"] == "smart_schedule"
    assert params["dynamic_mode"] == "battery_target"
    assert params["allow_ownership_takeover"] is True


@pytest.mark.parametrize(
    ("preserve_home_battery", "expect_preserve"),
    [(False, False), (True, True)],
)
def test_auto_schedule_keeps_future_plan_when_vehicle_away(
    monkeypatch,
    fake_actions,
    preserve_home_battery,
    expect_preserve,
):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    now = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)
    start = now.replace(hour=23)
    end = start.replace(hour=23, minute=30)
    plan_calls = []

    class FuturePlanner:
        async def plan_charging(self, **kwargs):
            plan_calls.append(kwargs)
            return ev_planner.ChargingPlan(
                vehicle_id=kwargs["vehicle_id"],
                current_soc=kwargs["current_soc"],
                target_soc=kwargs["target_soc"],
                target_time=(
                    kwargs["target_time"].isoformat()
                    if kwargs["target_time"]
                    else None
                ),
                energy_needed_kwh=12.0,
                windows=[
                    ev_planner.PlannedChargingWindow(
                        start_time=start.isoformat(),
                        end_time=end.isoformat(),
                        source="grid_offpeak",
                        estimated_power_kw=7.0,
                        estimated_energy_kwh=3.5,
                        price_cents_kwh=10.0,
                        reason="target_deadline",
                    )
                ],
                estimated_grid_kwh=3.5,
            )

        async def should_charge_now(self, *args, **kwargs):
            raise AssertionError("away vehicle must not reach charge decision")

    async def away(*args, **kwargs):
        return "not_home"

    async def plugged_in(*args, **kwargs):
        raise AssertionError("away vehicle should return before plug check")

    async def vehicle_soc(self, vehicle_id):
        return 40

    monkeypatch.setattr(ev_planner, "get_ev_location", away)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_start_charging", AsyncMock())
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: now)

    hass = _FakeHass()
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=FuturePlanner(),
    )
    executor._settings[VIN] = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        display_name="Model 3",
        target_soc=80,
        departure_time="07:00",
        preserve_home_battery=preserve_home_battery,
    )

    asyncio.run(
        executor.evaluate(
            {
                "battery_soc": 75,
                "solar_power": 0,
                "load_power": 1000,
                "grid_power": 0,
            },
            current_price_cents=20,
        )
    )

    state = executor.get_state(VIN)
    assert state.last_decision == "away"
    assert state.current_plan is not None
    assert state.current_plan.energy_needed_kwh == 12.0
    assert plan_calls[0]["current_soc"] == 40
    fake_actions._action_start_ev_charging_dynamic.assert_not_awaited()

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state.get("active", False) is expect_preserve
    if expect_preserve:
        assert preserve_state["source"] == "smart_schedule"
        assert "future EV demand" in preserve_state["reason"]


def test_auto_schedule_clears_future_preserve_when_effective_setting_turns_off(
    monkeypatch,
):
    now = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: now)

    hass = _FakeHass()
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        preserve_home_battery=True,
    )
    executor._settings[VIN] = settings
    state = executor.get_state(VIN)
    state.last_decision = "away"
    state.current_plan = ev_planner.ChargingPlan(
        vehicle_id=VIN,
        current_soc=40,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=12.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time=now.replace(hour=23).isoformat(),
                end_time=now.replace(hour=23, minute=30).isoformat(),
                source="grid_offpeak",
                estimated_power_kw=7.0,
                estimated_energy_kwh=3.5,
                price_cents_kwh=10.0,
                reason="target_deadline",
            )
        ],
    )

    executor._sync_future_demand_preserve_intent()
    assert hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"][
        "active"
    ] is True

    settings.preserve_home_battery = False
    executor._sync_future_demand_preserve_intent()

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is False
    assert preserve_state["source"] == "smart_schedule"


def test_auto_schedule_stops_untracked_tesla_while_waiting(monkeypatch, fake_actions):
    now = datetime.now()
    ha_now = now.replace(tzinfo=timezone.utc)
    stop_reason = "Cheapest charging window starts later"
    physical_probe = AsyncMock(return_value=True)
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    async def vehicle_soc(self, vehicle_id):
        return 55

    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner, "get_ev_location", AsyncMock(return_value="home"))
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", physical_probe)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: ha_now)

    hass = _FakeHass()
    planner = SimpleNamespace(
        should_charge_now=AsyncMock(
            return_value=(False, stop_reason, "grid_offpeak")
        )
    )
    executor = ev_planner.AutoScheduleExecutor(hass, _FakeConfigEntry(), planner)
    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        target_soc=80,
    )
    executor._settings[VIN] = settings
    state = executor.get_state(VIN)
    state.current_plan = ev_planner.ChargingPlan(
        vehicle_id=VIN,
        current_soc=55,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=10.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time=(now + timedelta(hours=1)).isoformat(),
                end_time=(now + timedelta(hours=2)).isoformat(),
                source="grid_offpeak",
                estimated_power_kw=7.0,
                estimated_energy_kwh=7.0,
                price_cents_kwh=0.0,
                reason="cheapest_window",
            )
        ],
    )
    state.last_plan_update = now

    asyncio.run(
        executor.evaluate(
            {
                "battery_soc": 100,
                "solar_power": 0,
                "load_power": 10000,
                "grid_power": 10000,
            },
            current_price_cents=47.3,
        )
    )

    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == VIN
    assert params["vehicle_vin"] == VIN
    assert params["stop_untracked"] is True
    assert params["stop_reason"] == stop_reason
    assert state.last_decision == "stopped"
    physical_probe.assert_awaited_once_with(
        hass,
        executor.config_entry,
        vehicle_vin=VIN,
    )


@pytest.mark.parametrize(
    ("owner_mode", "physical_charging", "expect_stop", "expect_probe"),
    [
        ("smart_schedule_grid_offpeak", True, True, True),
        ("manual", True, False, False),
        ("solar_surplus", True, False, False),
        (None, False, False, True),
    ],
)
def test_auto_schedule_external_stop_respects_owner_and_physical_state(
    monkeypatch,
    fake_actions,
    owner_mode,
    physical_charging,
    expect_stop,
    expect_probe,
):
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    hass = _FakeHass()
    entry = _FakeConfigEntry()
    if owner_mode is not None:
        ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode=owner_mode)
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)
    physical_probe = AsyncMock(return_value=physical_charging)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", physical_probe)

    executor = ev_planner.AutoScheduleExecutor(hass, entry, SimpleNamespace())
    stopped = asyncio.run(
        executor._stop_external_charging_if_needed(
            VIN,
            ev_planner.AutoScheduleSettings(enabled=True, vehicle_id=VIN),
            executor.get_state(VIN),
            "Waiting for planned window",
        )
    )

    assert stopped is expect_stop
    assert physical_probe.await_count == int(expect_probe)
    assert fake_actions._action_stop_ev_charging_dynamic.await_count == int(expect_stop)
    if not expect_stop:
        assert executor._last_external_smart_schedule_stops == {}


def test_auto_schedule_external_stop_requires_specific_tesla_loadpoint(
    monkeypatch,
    fake_actions,
):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)
    physical_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", physical_probe)

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        _FakeConfigEntry(),
        SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id="_default",
        charger_type="generic",
    )

    stopped = asyncio.run(
        executor._stop_external_charging_if_needed(
            "_default",
            settings,
            executor.get_state("_default"),
            "Waiting for planned window",
        )
    )

    assert stopped is False
    physical_probe.assert_not_awaited()
    fake_actions._action_stop_ev_charging_dynamic.assert_not_awaited()


def test_auto_schedule_external_stop_suppression_is_per_vehicle(
    monkeypatch,
    fake_actions,
):
    second_vin = "LRWYHCEKXTC687964"
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)
    physical_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", physical_probe)

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        _FakeConfigEntry(),
        SimpleNamespace(),
    )
    reason = "Waiting for planned window"

    async def run_stops():
        for vehicle_id in (VIN, VIN, second_vin):
            await executor._stop_external_charging_if_needed(
                vehicle_id,
                ev_planner.AutoScheduleSettings(enabled=True, vehicle_id=vehicle_id),
                executor.get_state(vehicle_id),
                reason,
            )

    asyncio.run(run_stops())

    assert fake_actions._action_stop_ev_charging_dynamic.await_count == 2
    assert physical_probe.await_count == 2
    stopped_ids = [
        call.args[2]["vehicle_id"]
        for call in fake_actions._action_stop_ev_charging_dynamic.await_args_list
    ]
    assert stopped_ids == [VIN, second_vin]


def test_auto_schedule_failed_external_stop_is_not_suppressed(
    monkeypatch,
    fake_actions,
):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=False)
    physical_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", physical_probe)

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        _FakeConfigEntry(),
        SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(enabled=True, vehicle_id=VIN)
    state = executor.get_state(VIN)

    async def run_stops():
        return [
            await executor._stop_external_charging_if_needed(
                VIN,
                settings,
                state,
                "Waiting for planned window",
            )
            for _ in range(2)
        ]

    assert asyncio.run(run_stops()) == [False, False]
    assert fake_actions._action_stop_ev_charging_dynamic.await_count == 2
    assert physical_probe.await_count == 2
    assert executor._last_external_smart_schedule_stops == {}


def test_auto_schedule_clears_stale_plan_when_away_vehicle_reaches_target(
    monkeypatch,
    fake_actions,
):
    now = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)

    async def away(*args, **kwargs):
        return "not_home"

    async def vehicle_soc(self, vehicle_id):
        return 82

    monkeypatch.setattr(ev_planner, "get_ev_location", away)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: now)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"] = {
        "active": True,
        "mode": "no_discharge_charge_allowed",
        "source": "smart_schedule",
        "reason": "future EV demand while unavailable: Model 3",
    }
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    executor._future_demand_preserve_active = True
    executor._settings[VIN] = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        display_name="Model 3",
        target_soc=80,
        departure_time="07:00",
    )
    state = executor.get_state(VIN)
    state.current_plan = ev_planner.ChargingPlan(
        vehicle_id=VIN,
        current_soc=40,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=12.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time=now.replace(hour=23).isoformat(),
                end_time=now.replace(hour=23, minute=30).isoformat(),
                source="grid_offpeak",
                estimated_power_kw=7.0,
                estimated_energy_kwh=3.5,
                price_cents_kwh=10.0,
                reason="target_deadline",
            )
        ],
    )

    asyncio.run(
        executor.evaluate(
            {
                "battery_soc": 75,
                "solar_power": 0,
                "load_power": 1000,
                "grid_power": 0,
            },
            current_price_cents=20,
        )
    )

    assert state.last_decision == "away"
    assert state.current_plan is None
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is False
    assert preserve_state["source"] == "smart_schedule"


def test_auto_schedule_forecast_refresh_updates_plan_without_charger_commands(
    monkeypatch,
    fake_actions,
):
    now = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)
    plan_calls = []

    class ForecastPlanner:
        async def plan_charging(self, **kwargs):
            plan_calls.append(kwargs)
            return ev_planner.ChargingPlan(
                vehicle_id=kwargs["vehicle_id"],
                current_soc=kwargs["current_soc"],
                target_soc=kwargs["target_soc"],
                target_time=None,
                energy_needed_kwh=10.0,
                windows=[
                    ev_planner.PlannedChargingWindow(
                        start_time=now.replace(hour=23).isoformat(),
                        end_time=now.replace(hour=23, minute=30).isoformat(),
                        source="grid_offpeak",
                        estimated_power_kw=7.0,
                        estimated_energy_kwh=3.5,
                        price_cents_kwh=10.0,
                        reason="target_deadline",
                    )
                ],
                estimated_grid_kwh=3.5,
            )

    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def vehicle_soc(self, vehicle_id):
        return 45

    start_charging = AsyncMock()
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_start_charging", start_charging)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: now)

    hass = _FakeHass()
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=ForecastPlanner(),
    )
    executor._settings[VIN] = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        display_name="Model 3",
        target_soc=80,
        departure_time="07:00",
    )

    asyncio.run(executor.refresh_optimizer_forecast_plans(current_price_cents=20))

    state = executor.get_state(VIN)
    assert state.current_plan is not None
    assert state.last_decision == "forecast_ready"
    assert plan_calls[0]["current_soc"] == 45
    start_charging.assert_not_awaited()


def test_auto_schedule_preserve_does_not_overwrite_price_level_intent(
    monkeypatch,
    fake_actions,
):
    now = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *args, **kwargs: now)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"] = {
        "active": True,
        "mode": "no_discharge_charge_allowed",
        "source": "price_level_charging",
        "reason": "cheap price",
    }
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    state = executor.get_state(VIN)
    state.last_decision = "away"
    state.current_plan = ev_planner.ChargingPlan(
        vehicle_id=VIN,
        current_soc=40,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=12.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time=now.replace(hour=23).isoformat(),
                end_time=now.replace(hour=23, minute=30).isoformat(),
                source="grid_offpeak",
                estimated_power_kw=7.0,
                estimated_energy_kwh=3.5,
                price_cents_kwh=10.0,
                reason="target_deadline",
            )
        ],
    )

    executor._sync_future_demand_preserve_intent()
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "price_level_charging"
    assert preserve_state["reason"] == "cheap price"

    state.current_plan = None
    executor._sync_future_demand_preserve_intent()
    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "price_level_charging"


def test_auto_schedule_active_preserve_sets_optimizer_intent():
    hass = _FakeHass()
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        preserve_home_battery=True,
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN, is_charging=True)

    executor._sync_active_charging_preserve_intent(
        VIN,
        True,
        state,
        "Smart Schedule charging",
    )

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "smart_schedule"
    assert preserve_state["mode"] == "no_discharge_charge_allowed"
    assert preserve_state["reason"] == "Smart Schedule charging"


def test_auto_schedule_active_preserve_waits_for_all_vehicles_before_clear():
    hass = _FakeHass()
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(preserve_home_battery=True)
    first = ev_planner.AutoScheduleState(vehicle_id="ev-1", is_charging=True)
    second = ev_planner.AutoScheduleState(vehicle_id="ev-2", is_charging=True)

    executor._sync_active_charging_preserve_intent(
        "ev-1",
        True,
        first,
        "first vehicle charging",
    )
    executor._sync_active_charging_preserve_intent(
        "ev-2",
        True,
        second,
        "second vehicle charging",
    )

    first.is_charging = False
    executor._sync_active_charging_preserve_intent(
        "ev-1",
        True,
        first,
        "first vehicle stopped",
    )

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "smart_schedule"
    assert preserve_state["reason"] == "second vehicle charging"

    second.is_charging = False
    executor._sync_active_charging_preserve_intent(
        "ev-2",
        True,
        second,
        "all smart schedule charging stopped",
    )

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is False
    assert preserve_state["source"] == "smart_schedule"


def test_auto_schedule_active_preserve_does_not_overwrite_other_ev_mode():
    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"] = {
        "active": True,
        "mode": "no_discharge_charge_allowed",
        "source": "price_level_charging",
        "reason": "cheap price",
    }
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(preserve_home_battery=True)
    state = ev_planner.AutoScheduleState(vehicle_id=VIN, is_charging=True)

    executor._sync_active_charging_preserve_intent(
        VIN,
        True,
        state,
        "Smart Schedule charging",
    )

    preserve_state = hass.data["power_sync"]["entry-1"]["scheduled_ev_preserve_state"]
    assert preserve_state["active"] is True
    assert preserve_state["source"] == "price_level_charging"
    assert preserve_state["reason"] == "cheap price"


def test_auto_schedule_grid_start_uses_optimizer_battery_target(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data["home_power_settings"] = {
        "phase_type": "single",
        "max_grid_import_amps": 80,
        "default_voltage": 240,
    }
    hass.data["power_sync"]["entry-1"]["optimization_coordinator"] = SimpleNamespace(
        _config=SimpleNamespace(max_charge_w=14700, max_discharge_w=10000)
    )
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)

    asyncio.run(executor._start_charging(VIN, settings, state, "grid_offpeak"))

    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["dynamic_mode"] == "battery_target"
    assert params["target_battery_charge_kw"] == 14.7
    assert params["max_battery_charge_rate_kw"] == 14.7
    assert params["max_inverter_kw"] == 10.0
    assert params["max_grid_import_kw"] == 19.2


def test_auto_schedule_grid_start_prefers_tesla_site_meter_limit(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    fake_actions._resolve_max_grid_import_kw = AsyncMock(return_value=16.1)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data["home_power_settings"] = {
        "phase_type": "single",
        "max_grid_import_amps": 80,
        "default_voltage": 240,
    }
    hass.data["power_sync"]["entry-1"]["optimization_coordinator"] = SimpleNamespace(
        _config=SimpleNamespace(max_charge_w=14700, max_discharge_w=10000)
    )
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)

    asyncio.run(executor._start_charging(VIN, settings, state, "grid_offpeak"))

    fake_actions._resolve_max_grid_import_kw.assert_awaited_once_with(
        hass,
        executor.config_entry,
    )
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["max_grid_import_kw"] == 16.1


def test_auto_schedule_solar_uses_smart_schedule_battery_floor(monkeypatch, fake_actions):
    start_calls: list[str] = []

    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def vehicle_soc(self, vehicle_id):
        return 40

    async def start_charging(self, vehicle_id, settings, state, source, force_max_rate=False):
        start_calls.append(source)
        state.is_charging = True

    class SolarPlanner:
        async def should_charge_now(self, **kwargs):
            assert kwargs["min_battery_soc"] == 45
            return True, "solar surplus available", "solar_surplus"

    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_start_charging", start_charging)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data["solar_surplus_config"] = {}
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SolarPlanner(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
        target_soc=80,
        min_battery_to_start=45,
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)
    state.current_plan = SimpleNamespace(windows=[])
    state.last_plan_update = ev_planner.datetime.now()
    executor._state[VIN] = state

    asyncio.run(
        executor._evaluate_vehicle(
            VIN,
            settings,
            {
                "battery_soc": 50,
                "solar_power": 7000,
                "load_power": 1000,
                "grid_power": -1000,
            },
            current_price_cents=0,
        )
    )

    assert start_calls == ["solar_surplus"]
    assert state.last_decision == "started"


def test_auto_schedule_solar_allows_strict_surplus_below_battery_floor(monkeypatch, fake_actions):
    start_calls: list[str] = []

    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def vehicle_soc(self, vehicle_id):
        return 40

    async def start_charging(self, vehicle_id, settings, state, source, force_max_rate=False):
        start_calls.append(source)
        state.is_charging = True

    class SolarPlanner:
        async def should_charge_now(self, **kwargs):
            return True, "solar surplus available", "solar_surplus"

    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_start_charging", start_charging)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data["solar_surplus_config"] = {
        "allow_parallel_charging": True,
        "max_battery_charge_rate_kw": 3.0,
    }
    executor = ev_planner.AutoScheduleExecutor(
        hass,
        _FakeConfigEntry(),
        planner=SolarPlanner(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
        target_soc=80,
        min_battery_to_start=45,
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)
    state.current_plan = SimpleNamespace(windows=[])
    state.last_plan_update = ev_planner.datetime.now()
    executor._state[VIN] = state

    asyncio.run(
        executor._evaluate_vehicle(
            VIN,
            settings,
            {
                "battery_soc": 40,
                "solar_power": 10000,
                "load_power": 1000,
                "grid_power": -4000,
            },
            current_price_cents=0,
        )
    )

    assert start_calls == ["solar_surplus"]
    assert state.last_decision == "started"
    assert state.last_decision_reason == (
        "Strict solar surplus: total 9.0kW, battery reserve 3.0kW, EV gets 6.0kW"
    )


def test_auto_schedule_deadline_uses_vehicle_max_amps(monkeypatch, fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: SimpleNamespace(weekday=lambda: 0),
    )

    executor = ev_planner.AutoScheduleExecutor(
        _FakeHass(),
        _FakeConfigEntry(),
        planner=SimpleNamespace(),
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id=VIN,
        display_name="Model 3",
        max_charge_amps=24,
        min_charge_amps=5,
        limit_grid_import=False,
    )
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)

    asyncio.run(
        executor._start_charging(
            VIN,
            settings,
            state,
            "grid_deadline",
            force_max_rate=True,
        )
    )

    fake_actions._action_start_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["max_charge_amps"] == 24
    assert params["start_amps"] == 24
    assert params["fixed_charge_amps"] == 24
    assert params["allow_stale_entity_max_override"] is True


def test_price_level_stop_uses_vehicle_charger_config(fake_actions):
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["automation_store"]._data[
        "vehicle_charging_configs"
    ] = [{
        "vehicle_id": "generic_ev",
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    }]

    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    result = asyncio.run(
        executor._stop_charging("Price above threshold", vehicle_vin="generic_ev")
    )

    assert result is True
    fake_actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    _hass, _entry, params = fake_actions._action_stop_ev_charging_dynamic.await_args.args
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] == "generic_ev"
    assert params["charger_type"] == "generic"
    assert params["charger_switch_entity"] == "switch.garage_ev"
    assert params["charger_amps_entity"] == "number.garage_ev_current"
    assert params["charger_status_entity"] == "sensor.garage_ev_status"
    assert params["stop_untracked"] is True


def test_price_level_stop_blocks_manual_owned_dynamic_stop(fake_actions):
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    fake_actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.claim_ev_ownership(hass, entry, VIN, owner_mode="manual")

    executor = ev_planner.PriceLevelChargingExecutor(hass, entry)
    result = asyncio.run(executor._stop_charging("Price above threshold", vehicle_vin=VIN))

    assert result is False
    fake_actions._action_stop_ev_charging_dynamic.assert_not_awaited()


def test_price_level_leaves_ownership_lease_session_alone(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {}

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("price-level must not stop an owned session")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        VIN: {"owner": "powersync", "owner_mode": "manual"}
    }
    executor = ev_planner.PriceLevelChargingExecutor(hass, _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "waiting"
    assert "manual mode owns" in state.last_decision_reason


def test_price_level_zaptec_start_is_blocked_by_manual_owner(fake_actions):
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    class ZaptecEntry(_FakeConfigEntry):
        options = {
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
        }

    client = SimpleNamespace(resume_charging=AsyncMock(return_value=True))
    hass = _FakeHass()
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "ev_ownership": {
                "zaptec_standalone": {
                    "owner": "powersync",
                    "owner_mode": "manual",
                }
            },
        }
    )

    executor = ev_planner.PriceLevelChargingExecutor(hass, ZaptecEntry())

    result = asyncio.run(
        executor._start_charging(
            "price_level_recovery",
            "cheap price",
            vehicle_vin="zaptec_standalone",
        )
    )

    assert result is False
    client.resume_charging.assert_not_awaited()
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["zaptec_standalone"]
    assert last_command["command"] == "start_price_level_recovery"
    assert last_command["success"] is False
    assert "manual already owns" in last_command["reason"]


def test_price_level_start_respects_manual_stop_hold(fake_actions):
    ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    hass = _FakeHass()
    entry = _FakeConfigEntry()
    ev_ownership.record_manual_stop_hold(
        hass,
        entry,
        "generic_ev",
        reason="Manual stop from mobile",
    )

    executor = ev_planner.PriceLevelChargingExecutor(hass, entry)
    result = asyncio.run(
        executor._start_charging(
            "price_level_opportunity",
            "cheap price",
            vehicle_vin="generic_ev",
        )
    )

    assert result is False
    fake_actions._action_start_ev_charging_dynamic.assert_not_awaited()
    state = executor._get_or_create_vehicle_state("generic_ev")
    assert state.last_decision == "waiting"
    assert "Manual stop hold active" in state.last_decision_reason


def test_price_level_disabled_does_not_stop_unowned_charging(monkeypatch, fake_actions):
    fake_actions._dynamic_ev_state = {}

    async def disabled_decision(self, vehicle_vin, current_price_cents):
        return False, "Price-level charging is disabled", ""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("disabled price-level charging must be passive")

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", fail_if_called)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        disabled_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        fail_if_called,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(enabled=False), _FakeConfigEntry()
    )
    asyncio.run(executor.evaluate_all_vehicles(50))

    state = executor._get_or_create_vehicle_state(VIN)
    assert state.last_decision == "disabled"
    assert state.last_decision_reason == "Price-level charging is disabled"


def test_generic_plug_detection_allows_missing_status_entity():
    hass = _FakeHass()
    hass.states = _FakeStates()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "",
        },
    )

    assert asyncio.run(ev_planner.is_ev_plugged_in(hass, entry)) is True


def test_generic_plug_detection_uses_connector_fallback():
    hass = _FakeHass()
    hass.states = _FakeStates({
        "sensor.garage_ev_status": "Available",
        "sensor.evse_status_connector": "Preparing",
    })
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "sensor.garage_ev_status",
        },
    )

    assert asyncio.run(ev_planner.is_ev_plugged_in(hass, entry)) is True


def test_generic_plug_detection_blocks_available_without_connector():
    hass = _FakeHass()
    hass.states = _FakeStates({"sensor.garage_ev_status": "Available"})
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "sensor.garage_ev_status",
        },
    )

    assert asyncio.run(ev_planner.is_ev_plugged_in(hass, entry)) is False


def test_sigenergy_plug_detection_wins_before_ocpp_false(monkeypatch):
    hass = _FakeHass()
    hass.states = _FakeStates()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_enabled": True,
            "ocpp_enabled": True,
        },
    )

    async def sigenergy_plugged(config_entry, hass=None):
        assert config_entry is entry
        return True

    monkeypatch.setattr(
        ev_planner,
        "_read_sigenergy_charger_plugged_state",
        sigenergy_plugged,
    )

    assert asyncio.run(
        ev_planner.is_ev_plugged_in(hass, entry, vehicle_vin="sigenergy_charger")
    ) is True


def test_sigenergy_plug_detection_can_report_unplugged(monkeypatch):
    hass = _FakeHass()
    hass.states = _FakeStates()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"sigenergy_charger_enabled": True},
    )

    async def sigenergy_unplugged(config_entry, hass=None):
        return False

    monkeypatch.setattr(
        ev_planner,
        "_read_sigenergy_charger_plugged_state",
        sigenergy_unplugged,
    )

    assert asyncio.run(
        ev_planner.is_ev_plugged_in(hass, entry, vehicle_vin="sigenergy_charger")
    ) is False


def test_disconnected_sigenergy_does_not_block_any_vehicle_fallback(monkeypatch):
    hass = _FakeHass()
    hass.states = _FakeStates({
        "sensor.garage_ev_status": "Available",
        "sensor.evse_status_connector": "Preparing",
    })
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_enabled": True,
            "generic_charger_enabled": True,
            "generic_charger_status_entity": "sensor.garage_ev_status",
        },
    )

    async def sigenergy_unplugged(config_entry, hass=None):
        return False

    monkeypatch.setattr(
        ev_planner,
        "_read_sigenergy_charger_plugged_state",
        sigenergy_unplugged,
    )

    assert asyncio.run(ev_planner.is_ev_plugged_in(hass, entry)) is True


def test_tesla_ble_plug_detection_ignores_off_charger_switch():
    hass = _FakeHass()
    hass.states = _FakeStates({
        "binary_sensor.ble_slater_status": "off",
        "switch.ble_slater_charger": "off",
    })
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": "tesla_ble",
            "tesla_ble_entity_prefix": "ble_slater",
        },
    )

    assert asyncio.run(
        ev_planner.is_ev_plugged_in(hass, entry, vehicle_vin="ble_ble_slater")
    ) is False
    assert asyncio.run(
        ev_planner.get_ev_location(hass, entry, vehicle_vin="ble_ble_slater")
    ) == "unknown"


def test_tesla_ble_plug_detection_uses_charge_flap():
    hass = _FakeHass()
    hass.states = _FakeStates({
        "binary_sensor.ble_phoenix_status": "off",
        "switch.ble_phoenix_charger": "off",
        "binary_sensor.ble_phoenix_charge_flap": "on",
    })
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": "tesla_ble",
            "tesla_ble_entity_prefix": "ble_phoenix",
        },
    )

    assert asyncio.run(
        ev_planner.is_ev_plugged_in(hass, entry, vehicle_vin="ble_ble_phoenix")
    ) is True
    assert asyncio.run(
        ev_planner.get_ev_location(hass, entry, vehicle_vin="ble_ble_phoenix")
    ) == "home"


def test_enabled_price_level_still_stops_external_high_price_charging(
    monkeypatch, fake_actions
):
    fake_actions._dynamic_ev_state = {}

    async def high_price_decision(self, vehicle_vin, current_price_cents):
        return False, "Price above threshold", ""

    async def is_charging(*args, **kwargs):
        return True

    stop_charging = AsyncMock(return_value=True)

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", _one_vehicle)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", is_charging)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "get_charging_decision_for_vehicle",
        high_price_decision,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_stop_charging",
        stop_charging,
    )

    executor = ev_planner.PriceLevelChargingExecutor(_FakeHass(), _FakeConfigEntry())
    asyncio.run(executor.evaluate_all_vehicles(50))

    stop_charging.assert_awaited_once_with("Price above threshold", vehicle_vin=VIN)


def test_unknown_soc_uses_recovery_price_fallback(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def unknown_soc(self, vehicle_vin=None):
        return None

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        unknown_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(executor.get_charging_decision(25))

    assert should_charge is True
    assert mode == "price_level_recovery"
    assert "EV SOC unknown" in reason
    assert executor._state.last_decision == "wants_charge"


def test_full_soc_blocks_price_level_opportunity(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def full_soc(self, vehicle_vin=None):
        return 100

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        full_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(executor.get_charging_decision(1))

    assert should_charge is False
    assert mode == ""
    assert "already full" in reason
    assert executor._state.last_decision == "waiting"


def test_unknown_vehicle_soc_uses_recovery_price_fallback(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def unknown_soc(self, vehicle_vin=None):
        return None

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        unknown_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(
        executor.get_charging_decision_for_vehicle(VIN, 25)
    )

    state = executor._get_or_create_vehicle_state(VIN)
    assert should_charge is True
    assert mode == "price_level_recovery"
    assert "EV SOC unknown" in reason
    assert state.last_decision == "wants_charge"


def test_mixed_tesla_ble_unknown_soc_does_not_recovery_start_duplicate(
    monkeypatch,
    fake_actions,
):
    ble_vin = "ble_garage_garage_ble_gateway"
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def discovered_vehicles(*args, **kwargs):
        return [
            {"vin": VIN, "name": "TSL43", "source": "fleet_api"},
            {
                "vin": ble_vin,
                "name": "Tesla BLE (garage_garage_ble_gateway)",
                "source": "tesla_ble",
            },
        ]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return "home"

    async def plugged_in(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return True

    async def mixed_soc(self, vehicle_vin=None):
        if vehicle_vin == VIN:
            return 59
        if vehicle_vin == ble_vin:
            return None
        raise AssertionError(f"Unexpected vehicle_vin {vehicle_vin!r}")

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", discovered_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        mixed_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 30,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 5,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    results = asyncio.run(executor.evaluate_all_vehicles(7.8))

    assert results[VIN] == (False, "EV 59% >= 30%, price 7.8c > 5c", "")
    assert results[ble_vin] == (
        False,
        "Tesla BLE SOC unavailable while another Tesla vehicle is already "
        "discovered; waiting for a vehicle-specific SOC source",
        "",
    )
    fake_actions._action_start_ev_charging_dynamic.assert_not_awaited()


def test_mixed_tesla_ble_unknown_soc_uses_duplicate_guard_above_recovery_price(
    monkeypatch,
    fake_actions,
):
    ble_vin = "ble_garage_garage_ble_gateway"
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def discovered_vehicles(*args, **kwargs):
        return [
            {"vin": VIN, "name": "TSL43", "source": "fleet_api"},
            {
                "vin": ble_vin,
                "name": "Tesla BLE (garage_garage_ble_gateway)",
                "source": "tesla_ble",
            },
        ]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return "home"

    async def plugged_in(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return True

    async def mixed_soc(self, vehicle_vin=None):
        if vehicle_vin == VIN:
            return 49
        if vehicle_vin == ble_vin:
            return None
        raise AssertionError(f"Unexpected vehicle_vin {vehicle_vin!r}")

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", discovered_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        mixed_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 30,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 5,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    results = asyncio.run(executor.evaluate_all_vehicles(34.3))

    assert results[VIN] == (False, "EV 49% >= 30%, price 34.3c > 5c", "")
    assert results[ble_vin] == (
        False,
        "Tesla BLE SOC unavailable while another Tesla vehicle is already "
        "discovered; waiting for a vehicle-specific SOC source",
        "",
    )
    fake_actions._action_start_ev_charging_dynamic.assert_not_awaited()


def test_mixed_tesla_ble_unknown_soc_does_not_opportunity_start_duplicate(
    monkeypatch,
    fake_actions,
):
    ble_vin = "ble_garage_garage_ble_gateway"
    fake_actions._action_start_ev_charging_dynamic = AsyncMock(return_value=True)

    async def discovered_vehicles(*args, **kwargs):
        return [
            {"vin": VIN, "name": "TSL43", "source": "fleet_api"},
            {
                "vin": ble_vin,
                "name": "Tesla BLE (garage_garage_ble_gateway)",
                "source": "tesla_ble",
            },
        ]

    async def at_home(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return "home"

    async def plugged_in(_hass, _entry, vehicle_vin=None):
        assert vehicle_vin in (VIN, ble_vin)
        return True

    async def mixed_soc(self, vehicle_vin=None):
        if vehicle_vin == VIN:
            return 59
        if vehicle_vin == ble_vin:
            return None
        raise AssertionError(f"Unexpected vehicle_vin {vehicle_vin!r}")

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", discovered_vehicles)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(ev_planner, "is_ev_actively_charging", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        mixed_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 30,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 5,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    results = asyncio.run(executor.evaluate_all_vehicles(4.0))

    assert results[VIN] == (
        True,
        "Opportunity: EV 59%, price 4.0c <= 5c",
        "price_level_opportunity",
    )
    assert results[ble_vin] == (
        False,
        "Tesla BLE SOC unavailable while another Tesla vehicle is already "
        "discovered; waiting for a vehicle-specific SOC source",
        "",
    )
    assert fake_actions._action_start_ev_charging_dynamic.await_count == 1
    _hass, _entry, params = fake_actions._action_start_ev_charging_dynamic.await_args.args
    assert params["vehicle_vin"] == VIN


def test_full_vehicle_soc_blocks_price_level_opportunity(monkeypatch):
    async def at_home(*args, **kwargs):
        return "home"

    async def plugged_in(*args, **kwargs):
        return True

    async def full_soc(self, vehicle_vin=None):
        return 100

    async def no_home_battery_limit(self):
        return None

    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged_in)
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_ev_soc",
        full_soc,
    )
    monkeypatch.setattr(
        ev_planner.PriceLevelChargingExecutor,
        "_get_home_battery_soc",
        no_home_battery_limit,
    )

    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            price_settings={
                "recovery_soc": 40,
                "recovery_price_cents": 30,
                "opportunity_price_cents": 10,
                "home_battery_minimum": 0,
            }
        ),
        _FakeConfigEntry(),
    )

    should_charge, reason, mode = asyncio.run(
        executor.get_charging_decision_for_vehicle(VIN, 1)
    )

    state = executor._get_or_create_vehicle_state(VIN)
    assert should_charge is False
    assert mode == ""
    assert "already full" in reason
    assert state.last_decision == "waiting"


def test_price_level_generic_soc_uses_fallback_sensor():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_soc_entity": "sensor.primary_ev_soc",
            "generic_charger_soc_entity_2": "sensor.fallback_ev_soc",
        },
    )
    executor = ev_planner.PriceLevelChargingExecutor(
        _FakeHass(
            states={
                "sensor.primary_ev_soc": "unknown",
                "sensor.fallback_ev_soc": "68",
            },
            entries=[entry],
        ),
        entry,
    )

    assert asyncio.run(executor._get_ev_soc()) == 68
