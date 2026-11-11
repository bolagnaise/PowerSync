"""Today's weather summary for the dashboard's Right now card.

Display only: nothing here feeds the optimizer. The summary combines

* the HA weather entity's daily and hourly ``weather.get_forecasts`` data
  (high / low temperature, hourly cloud cover), and
* today's civil dawn, sunrise, sunset and dusk from HA's astral helper.

``sun.sun`` only exposes the *next* events, which roll over to tomorrow once
they pass, so the astral helper is used for today's date instead.

Hourly forecasts only start at the current hour. Hours already seen today are
kept in memory, so the dawn-to-dusk cloud average covers the whole day once HA
has run since dawn; otherwise the payload says which hour the average starts.

This module imports only the standard library at load time so the summary
logic can be unit-tested without Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone, tzinfo
import logging
import time
from typing import Any, Iterable, Optional

_LOGGER = logging.getLogger(__name__)

CACHE_KEY = "weather_today_cache"
CACHE_TTL_S = 30 * 60
RETRY_TTL_S = 60

# WeatherEntityFeature bits.
FORECAST_DAILY = 1
FORECAST_HOURLY = 2

_UNAVAILABLE = (None, "", "unknown", "unavailable")


def _parse_time(value: Any, tz: tzinfo) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def merge_hourly(
    seen: dict[str, dict[str, Optional[float]]],
    hourly: Iterable[dict[str, Any]],
    day: date,
    tz: tzinfo,
) -> dict[str, dict[str, Optional[float]]]:
    """Fold hourly forecast rows for ``day`` into ``seen`` (keyed by ISO hour).

    Newer rows replace older ones for the same hour; hours no longer in the
    forecast (already past) keep their last forecast value.
    """
    out = dict(seen)
    for row in hourly or ():
        if not isinstance(row, dict):
            continue
        t = _parse_time(row.get("datetime"), tz)
        if t is None or t.date() != day:
            continue
        hour = t.replace(minute=0, second=0, microsecond=0)
        cloud = _num(row.get("cloud_coverage"))
        if cloud is not None and not 0 <= cloud <= 100:
            cloud = None
        temp = _num(row.get("temperature"))
        if cloud is None and temp is None:
            continue
        prev = out.get(hour.isoformat(), {})
        out[hour.isoformat()] = {
            "cloud": cloud if cloud is not None else prev.get("cloud"),
            "temp": temp if temp is not None else prev.get("temp"),
        }
    return out


def _daily_today(daily: Iterable[dict[str, Any]], day: date, tz: tzinfo) -> Optional[dict]:
    """Return the daily forecast row whose local date is ``day``."""
    for row in daily or ():
        if not isinstance(row, dict):
            continue
        t = _parse_time(row.get("datetime"), tz)
        if t is not None and t.date() == day:
            return row
    return None


def summarize_weather_today(
    *,
    day: date,
    tz: tzinfo,
    daily: Iterable[dict[str, Any]] | None,
    hourly_seen: dict[str, dict[str, Optional[float]]],
    sun: dict[str, Optional[datetime]],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the dashboard payload from already-fetched inputs."""
    current = current or {}
    dawn, dusk = sun.get("dawn"), sun.get("dusk")

    # High / low: the daily forecast for today, else today's hourly temps.
    high = low = None
    hl_source = None
    row = _daily_today(daily or (), day, tz)
    if row is not None:
        high = _num(row.get("temperature"))
        low = _num(row.get("templow"))
        if high is not None or low is not None:
            hl_source = "daily"
    hours = sorted(
        ((_parse_time(k, tz), v) for k, v in hourly_seen.items() if _parse_time(k, tz)),
        key=lambda item: item[0].timestamp(),
    )
    temps = [v["temp"] for _, v in hours if v.get("temp") is not None]
    if hl_source is None and temps:
        high, low = max(temps), min(temps)
        hl_source = "hourly"
    elif hl_source == "daily" and temps:
        # Some providers drop today's low once it has passed.
        if high is None or low is None:
            hl_source = "mixed"
        if high is None:
            high = max(temps)
        if low is None:
            low = min(temps)

    # Cloud cover: mean of hourly values from the hour containing dawn to the
    # hour containing dusk.
    cloud_hours: list[dict[str, Any]] = []
    if dawn is not None and dusk is not None:
        start = dawn.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        for t, v in hours:
            if start.timestamp() <= t.timestamp() <= dusk.timestamp() and v.get("cloud") is not None:
                cloud_hours.append({"time": t.isoformat(), "cloud": round(v["cloud"])})
    cloud_avg = (
        round(sum(h["cloud"] for h in cloud_hours) / len(cloud_hours))
        if cloud_hours else None
    )
    cloud_from = cloud_hours[0]["time"] if cloud_hours else None
    # Every expected hour must be present, including the hour containing dusk.
    # Step in UTC so repeated/skipped civil hours retain their real identities.
    expected = set()
    if dawn is not None and dusk is not None and dusk.timestamp() > dawn.timestamp():
        hour = dawn.astimezone(tz).replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = dusk.astimezone(timezone.utc)
        while hour <= end:
            expected.add(hour.timestamp())
            hour += timedelta(hours=1)
    actual = {_parse_time(h["time"], tz).timestamp() for h in cloud_hours}
    cloud_complete = bool(expected and expected <= actual)

    daylight = (
        round((dusk.timestamp() - dawn.timestamp()) / 60)
        if dawn is not None and dusk is not None and dusk > dawn else None
    )

    def iso(v: Optional[datetime]) -> Optional[str]:
        return v.astimezone(tz).isoformat() if v is not None else None

    return {
        "success": True,
        "date": day.isoformat(),
        "source": current.get("source"),
        "condition": current.get("condition"),
        "temperature": _num(current.get("temperature")),
        "temperature_unit": current.get("temperature_unit") or "°C",
        "high": high,
        "low": low,
        "high_low_source": hl_source,
        "cloud_cover_avg": cloud_avg,
        "cloud_cover_start": datetime.fromtimestamp(min(expected), tz).isoformat() if expected else None,
        "cloud_cover_end": datetime.fromtimestamp(max(expected) + 3600, tz).isoformat() if expected else None,
        "cloud_cover_hours": cloud_hours,
        "cloud_cover_from": cloud_from,
        "cloud_cover_complete": cloud_complete,
        "dawn": iso(dawn),
        "sunrise": iso(sun.get("sunrise")),
        "sunset": iso(sun.get("sunset")),
        "dusk": iso(dusk),
        "daylight_minutes": daylight,
    }


def _pick_weather_entity(hass: Any, configured: Optional[str]) -> Optional[Any]:
    if configured:
        state = hass.states.get(configured)
        if state is not None and state.state not in _UNAVAILABLE:
            return state
    for state in hass.states.async_all("weather"):
        if state.state not in _UNAVAILABLE:
            return state
    return None


async def _get_forecast(hass: Any, entity_id: str, kind: str) -> list[dict[str, Any]] | None:
    try:
        async with asyncio.timeout(10):
            resp = await hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": entity_id, "type": kind},
                blocking=True, return_response=True,
            )
    except Exception as err:  # unsupported type or provider error
        _LOGGER.debug("weather.get_forecasts %s failed for %s: %s", kind, entity_id, err)
        return None
    forecast = (resp or {}).get(entity_id, {}).get("forecast")
    return forecast if isinstance(forecast, list) else []


async def async_get_weather_today(
    hass: Any, *, weather_entity: Optional[str], domain: str, force: bool = False
) -> dict[str, Any]:
    """Coalesce clients, then fetch today's display-only summary."""
    store = hass.data.setdefault(domain, {})
    lock = store.setdefault("weather_today_lock", asyncio.Lock())
    async with lock:
        return await _async_refresh(hass, weather_entity=weather_entity, domain=domain, force=force)


async def _async_refresh(
    hass: Any, *, weather_entity: Optional[str], domain: str, force: bool = False
) -> dict[str, Any]:
    from homeassistant.helpers.sun import get_astral_event_date
    from homeassistant.util import dt as dt_util

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE
    today = dt_util.now(tz).date()
    store = hass.data.setdefault(domain, {})
    cache = store.get(CACHE_KEY)
    context = (hass.config.time_zone, getattr(hass.config, "latitude", None), getattr(hass.config, "longitude", None))
    if not isinstance(cache, dict) or cache.get("date") != today.isoformat() or cache.get("context") != context:
        cache = {"date": today.isoformat(), "hourly": {}, "payload": None, "at": 0.0, "entity": None, "daily": [], "context": context}
        store[CACHE_KEY] = cache

    state = _pick_weather_entity(hass, weather_entity)
    entity_id = state.entity_id if state is not None else None
    unit = (state.attributes or {}).get("temperature_unit") if state is not None else None
    if cache.get("unit") != unit:
        cache.update(hourly={}, daily=[], payload=None)
    if (
        not force and cache["payload"] is not None and cache["entity"] == entity_id
        and time.monotonic() - cache["at"] < (RETRY_TTL_S if cache["payload"].get("forecast_stale") else CACHE_TTL_S)
    ):
        return cache["payload"]
    if cache["entity"] != entity_id:
        cache.update(hourly={}, daily=[], payload=None)

    daily: list[dict[str, Any]] = cache.get("daily", [])
    failed = False
    current: dict[str, Any] = {}
    if state is not None:
        attrs = state.attributes or {}
        supported = int(attrs.get("supported_features") or 0)
        if not supported or supported & FORECAST_DAILY:
            result = await _get_forecast(hass, entity_id, "daily")
            failed |= result is None
            if result is not None:
                daily = result
        if not supported or supported & FORECAST_HOURLY:
            hourly = await _get_forecast(hass, entity_id, "hourly")
            failed |= hourly is None
            if hourly is not None:
                cache["hourly"] = merge_hourly(cache["hourly"], hourly, today, tz)
        current = {
            "source": entity_id,
            "condition": state.state,
            "temperature": attrs.get("temperature"),
            "temperature_unit": attrs.get("temperature_unit"),
        }
    else:
        current = {"source": "sun.sun" if hass.states.get("sun.sun") else None}

    sun: dict[str, Optional[datetime]] = {}
    for event in ("dawn", "sunrise", "sunset", "dusk"):
        try:
            sun[event] = get_astral_event_date(hass, event, today)
        except Exception as err:  # polar day/night or no location
            _LOGGER.debug("No %s for %s: %s", event, today, err)
            sun[event] = None

    payload = summarize_weather_today(
        day=today, tz=tz, daily=daily, hourly_seen=cache["hourly"], sun=sun, current=current,
    )
    payload["timezone"] = hass.config.time_zone
    payload["forecast_stale"] = failed
    payload["fetched_at"] = (cache.get("payload") or {}).get("fetched_at") if failed else dt_util.utcnow().isoformat()
    cache.update(payload=payload, at=time.monotonic(), entity=entity_id, daily=daily, unit=unit)
    return payload
