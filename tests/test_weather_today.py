"""Today's weather summary behind the dashboard Right now weather chip."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "power_sync" / "weather_today.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ps_weather_today_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wt = _load()
TZ = timezone(timedelta(hours=10))  # Brisbane, no DST
DAY = date(2026, 11, 11)


def _t(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


SUN = {"dawn": _t(4, 28), "sunrise": _t(4, 52), "sunset": _t(18, 12), "dusk": _t(18, 37)}


def _hourly(start, end, cloud=lambda h: 10 * (h % 10), temp=lambda h: 18 + h / 2, day=DAY):
    return [
        {"datetime": _t(h, day=day).astimezone(timezone.utc).isoformat(),
         "cloud_coverage": cloud(h), "temperature": temp(h)}
        for h in range(start, end)
    ]


def test_cloud_average_covers_only_dawn_to_dusk_hours():
    hourly = _hourly(0, 24, cloud=lambda h: 100 if h < 4 or h > 18 else 20)
    seen = wt.merge_hourly({}, hourly, DAY, TZ)
    out = wt.summarize_weather_today(
        day=DAY, tz=TZ, daily=[], hourly_seen=seen, sun=SUN,
        current={"source": "weather.home", "condition": "partlycloudy", "temperature": 27},
    )
    # 04:00 (contains dawn) through 18:00 (dusk 18:37): 15 hours, all 20%.
    assert [h["time"][11:16] for h in out["cloud_cover_hours"]][:1] == ["04:00"]
    assert len(out["cloud_cover_hours"]) == 15
    assert out["cloud_cover_avg"] == 20
    assert out["cloud_cover_complete"] is True
    assert out["daylight_minutes"] == 14 * 60 + 9
    assert out["dawn"].startswith("2026-11-11T04:28")
    assert out["source"] == "weather.home"
    assert out["temperature"] == 27


def test_daily_high_low_wins_and_hourly_fills_a_dropped_low():
    daily = [
        {"datetime": _t(0).astimezone(timezone.utc).isoformat(), "temperature": 29.4, "templow": 19.1},
        {"datetime": _t(0, day=DAY + timedelta(days=1)).isoformat(), "temperature": 33, "templow": 22},
    ]
    seen = wt.merge_hourly({}, _hourly(10, 20), DAY, TZ)
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=daily, hourly_seen=seen, sun=SUN)
    assert (out["high"], out["low"], out["high_low_source"]) == (29.4, 19.1, "daily")

    daily[0].pop("templow")
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=daily, hourly_seen=seen, sun=SUN)
    assert out["high"] == 29.4
    assert out["low"] == min(18 + h / 2 for h in range(10, 20))


def test_hourly_temps_are_used_when_daily_has_no_row_for_today():
    tomorrow_only = [{"datetime": _t(12, day=DAY + timedelta(days=1)).isoformat(), "temperature": 33}]
    seen = wt.merge_hourly({}, _hourly(5, 21), DAY, TZ)
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=tomorrow_only, hourly_seen=seen, sun=SUN)
    assert out["high_low_source"] == "hourly"
    assert out["high"] == 18 + 20 / 2 and out["low"] == 18 + 5 / 2


def test_merge_keeps_past_hours_and_ignores_other_days():
    morning = wt.merge_hourly({}, _hourly(4, 12, cloud=lambda h: 80), DAY, TZ)
    # A later fetch starts at noon and runs into tomorrow.
    later = _hourly(12, 24, cloud=lambda h: 10) + _hourly(0, 6, cloud=lambda h: 99, day=DAY + timedelta(days=1))
    merged = wt.merge_hourly(morning, later, DAY, TZ)
    assert len(merged) == 20
    assert merged[_t(5).isoformat()]["cloud"] == 80
    assert merged[_t(13).isoformat()]["cloud"] == 10
    assert all(k.startswith("2026-11-11") for k in merged)
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=[], hourly_seen=merged, sun=SUN)
    assert out["cloud_cover_complete"] is True


def test_cloud_average_is_marked_partial_when_forecast_starts_after_dawn():
    seen = wt.merge_hourly({}, _hourly(11, 24, cloud=lambda h: 40), DAY, TZ)
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=[], hourly_seen=seen, sun=SUN)
    assert out["cloud_cover_avg"] == 40
    assert out["cloud_cover_complete"] is False
    assert out["cloud_cover_from"].startswith("2026-11-11T11:00")


def test_missing_sun_and_forecast_degrade_to_nulls():
    out = wt.summarize_weather_today(
        day=DAY, tz=TZ, daily=None, hourly_seen={},
        sun={"dawn": None, "sunrise": None, "sunset": None, "dusk": None},
    )
    assert out["success"] is True
    for key in ("high", "low", "cloud_cover_avg", "daylight_minutes", "dawn", "dusk", "temperature"):
        assert out[key] is None
    assert out["cloud_cover_hours"] == [] and out["cloud_cover_complete"] is False


class _State:
    def __init__(self, entity_id, state, attributes):
        self.entity_id, self.state, self.attributes = entity_id, state, attributes


class _Hass:
    def __init__(self, states):
        self._states = {s.entity_id: s for s in states}
        self.data = {}
        self.config = types.SimpleNamespace(time_zone="Australia/Brisbane")
        self.calls = []
        hass = self

        class _Services:
            async def async_call(self, domain, service, data, blocking, return_response):
                hass.calls.append(data["type"])
                eid = data["entity_id"]
                if data["type"] == "daily":
                    return {eid: {"forecast": [{"datetime": _t(0).isoformat(), "temperature": 29, "templow": 19}]}}
                return {eid: {"forecast": _hourly(4, 20, cloud=lambda h: 30)}}

        self.services = _Services()

    @property
    def states(self):
        outer = self

        class _S:
            def get(self, eid):
                return outer._states.get(eid)

            def async_all(self, domain):
                return [s for k, s in outer._states.items() if k.startswith(domain + ".")]

        return _S()


@pytest.fixture
def ha_stubs(monkeypatch):
    sun_mod = types.ModuleType("homeassistant.helpers.sun")
    sun_mod.get_astral_event_date = lambda hass, event, day: SUN[event]
    dt_mod = types.ModuleType("homeassistant.util.dt")
    dt_mod.DEFAULT_TIME_ZONE = TZ
    dt_mod.get_time_zone = lambda name: TZ
    dt_mod.now = lambda tz=None: _t(11, 35)
    dt_mod.utcnow = lambda: _t(11, 35).astimezone(timezone.utc)
    util_mod = types.ModuleType("homeassistant.util")
    util_mod.dt = dt_mod
    helpers_mod = types.ModuleType("homeassistant.helpers")
    helpers_mod.sun = sun_mod
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    for name, mod in {
        "homeassistant": ha, "homeassistant.helpers": helpers_mod,
        "homeassistant.helpers.sun": sun_mod, "homeassistant.util": util_mod,
        "homeassistant.util.dt": dt_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_fetch_prefers_configured_entity_and_caches(ha_stubs):
    hass = _Hass([
        _State("weather.first", "rainy", {"temperature": 15, "supported_features": 3}),
        _State("weather.chosen", "partlycloudy", {"temperature": 27, "temperature_unit": "°C", "supported_features": 3}),
        _State("sun.sun", "above_horizon", {}),
    ])
    out = asyncio.run(wt.async_get_weather_today(hass, weather_entity="weather.chosen", domain="power_sync"))
    assert out["source"] == "weather.chosen" and out["condition"] == "partlycloudy"
    assert (out["high"], out["low"], out["cloud_cover_avg"]) == (29, 19, 30)
    assert out["fetched_at"]
    assert sorted(hass.calls) == ["daily", "hourly"]

    asyncio.run(wt.async_get_weather_today(hass, weather_entity="weather.chosen", domain="power_sync"))
    assert len(hass.calls) == 2  # served from cache
    asyncio.run(wt.async_get_weather_today(hass, weather_entity="weather.chosen", domain="power_sync", force=True))
    assert len(hass.calls) == 4


def test_fetch_without_weather_entity_still_reports_daylight(ha_stubs):
    hass = _Hass([_State("sun.sun", "above_horizon", {})])
    out = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain="power_sync"))
    assert out["source"] == "sun.sun"
    assert out["daylight_minutes"] == 14 * 60 + 9
    assert out["high"] is None and out["cloud_cover_avg"] is None
    assert hass.calls == []


@pytest.mark.parametrize("missing", [4, 10, 18])
def test_cloud_coverage_is_partial_when_any_daylight_hour_is_missing(missing):
    seen = wt.merge_hourly({}, [row for row in _hourly(4, 19) if row["datetime"] != _t(missing).astimezone(timezone.utc).isoformat()], DAY, TZ)
    out = wt.summarize_weather_today(day=DAY, tz=TZ, daily=[], hourly_seen=seen, sun=SUN)
    assert out["cloud_cover_avg"] is not None
    assert out["cloud_cover_complete"] is False


def test_transient_provider_failure_is_marked_and_retried_soon(ha_stubs, monkeypatch):
    hass = _Hass([_State("weather.home", "sunny", {"supported_features": 3})])
    original = hass.services.async_call
    failing = True
    now = [1000.0]
    monkeypatch.setattr(wt.time, "monotonic", lambda: now[0])

    async def service(*args, **kwargs):
        if failing:
            raise RuntimeError("temporary provider failure")
        return await original(*args, **kwargs)

    hass.services.async_call = service
    first = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain="power_sync"))
    assert first["forecast_stale"] is True
    failing = False
    now[0] += 61
    second = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain="power_sync"))
    assert second["forecast_stale"] is False
    assert second["high"] == 29
    assert second["cloud_cover_avg"] == 30


def test_simultaneous_clients_share_one_forecast_refresh(ha_stubs):
    hass = _Hass([_State("weather.home", "sunny", {"supported_features": 3})])
    original = hass.services.async_call

    async def service(*args, **kwargs):
        await asyncio.sleep(0)
        return await original(*args, **kwargs)

    hass.services.async_call = service

    async def clients():
        return await asyncio.gather(*[
            wt.async_get_weather_today(hass, weather_entity=None, domain="power_sync")
            for _ in range(3)
        ])

    results = asyncio.run(clients())
    assert hass.calls == ["daily", "hourly"]
    assert all(result == results[0] for result in results)


def test_repeated_dst_hour_is_not_deduplicated():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('America/New_York')
    day = date(2026, 11, 1)
    # Both 01:00 hours are separate forecast instants.
    hourly = [{'datetime': f'2026-11-01T{h:02}:00:00+00:00', 'cloud_coverage': 20, 'temperature': 0} for h in range(4, 10)]
    seen = wt.merge_hourly({}, hourly, day, tz)
    assert len(seen) == 6
    sun = {'dawn': datetime(2026,11,1,0,30,tzinfo=tz), 'dusk': datetime(2026,11,1,4,30,tzinfo=tz)}
    out = wt.summarize_weather_today(day=day, tz=tz, daily=[], hourly_seen=seen, sun=sun)
    assert out['cloud_cover_complete'] is True
    assert len(out['cloud_cover_hours']) == 6
    assert out['daylight_minutes'] == 300
    assert out['low'] == 0


def test_cache_does_not_mix_units_and_invalidates_at_midnight(ha_stubs, monkeypatch):
    state = _State('weather.home','sunny',{'supported_features':3, 'temperature_unit':'°C'})
    hass = _Hass([state])
    first = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain='power_sync'))
    assert first['timezone'] == 'Australia/Brisbane'
    hass.data['power_sync'][wt.CACHE_KEY]['hourly'][_t(1).isoformat()] = {'temp':-100,'cloud':0}
    state.attributes['temperature_unit'] = '°F'
    second = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain='power_sync'))
    assert second['temperature_unit'] == '°F'
    assert _t(1).isoformat() not in hass.data['power_sync'][wt.CACHE_KEY]['hourly']
    monkeypatch.setattr(sys.modules['homeassistant.util.dt'], 'now', lambda tz=None: _t(0,day=DAY+timedelta(days=1)))
    third = asyncio.run(wt.async_get_weather_today(hass, weather_entity=None, domain='power_sync'))
    assert third['date'] == '2026-11-12'
    assert third['high'] is None
    assert len(hass.calls) == 6
