"""Forecast Tesla-owned charge schedules without taking vehicle control.

Tesla runs these schedules independently of PowerSync. Their demand must be
fixed in the battery solve, rather than offered as a movable EV decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import logging
import math
import hashlib
import json
from typing import Any, Mapping, Sequence

_LOGGER = logging.getLogger(__name__)
REFRESH_SECONDS = 900
MAX_CACHE_SECONDS = 86400
MAX_CHARGE_STATE_AGE_SECONDS = 86400
CHARGE_EFFICIENCY = 0.9


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _section(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Accept both Fleet JSON and HA's flattened coordinator data."""
    nested = data.get(name)
    if isinstance(nested, dict):
        return dict(nested)
    prefix = name + "_"
    return {key[len(prefix):]: value for key, value in data.items()
            if key.startswith(prefix)}


def _timestamp(value: Any) -> float:
    result = _number(value)
    return result / 1000 if result > 1e11 else result


def _near_site(schedule: Mapping[str, Any], latitude: float, longitude: float) -> bool:
    lat = _number(schedule.get("latitude", schedule.get("lat")), math.nan)
    lon = _number(schedule.get("longitude", schedule.get("lon")), math.nan)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    # Tesla schedules are location-specific; work/public schedules must never
    # become demand at the home site. Permit a 300 m GPS/site-location margin.
    north = math.radians(lat - latitude)
    east = math.radians(lon - longitude) * math.cos(math.radians(latitude))
    return math.hypot(north, east) * 6371000 <= 300


def _scheduled_day(raw: Any, day: datetime) -> bool:
    # Tesla's SDK defines Sunday=1, Monday=2, ..., Saturday=64.
    if isinstance(raw, str) and not raw.isdigit():
        names = {value.strip().lower() for value in raw.split(",")}
        return ("all" in names or day.strftime("%A").lower() in names
                or ("weekdays" in names and day.weekday() < 5))
    mask = int(_number(raw))
    return bool(mask & (1 << ((day.weekday() + 1) % 7)))


@dataclass(frozen=True)
class TeslaScheduleForecast:
    vehicle_id: str
    power_w: tuple[float, ...]
    energy_needed_kwh: float


@dataclass(frozen=True)
class NativeChargeWindow:
    identity: str
    start: float
    end: float
    read_at: float


def active_native_window(*, data: Mapping[str, Any], now: datetime,
                         schedule_read_at: datetime, latitude: float,
                         longitude: float) -> NativeChargeWindow | None:
    """Actual explicit home window; synthetic forecast bounds grant no control.

    One-time, work-location, start-only and finish-by schedules remain
    forecast-only; a fresh cache read alone cannot prove one-time occurrence.
    """
    if now.tzinfo is None or schedule_read_at.tzinfo is None:
        return None
    if not 0 <= now.timestamp()-schedule_read_at.timestamp() <= REFRESH_SECONDS:
        return None
    schedules = _section(data, "charge_schedule_data").get("charge_schedules")
    if not isinstance(schedules, list):
        return None
    windows = []
    for schedule in schedules:
        if (not isinstance(schedule, dict) or schedule.get("enabled") is not True
                or schedule.get("start_enabled") is not True
                or schedule.get("end_enabled") is not True
                or schedule.get("one_time") is True
                or not _near_site(schedule, latitude, longitude)):
            continue
        start_minutes = _number(schedule.get("start_time"), math.nan)
        end_minutes = _number(schedule.get("end_time"), math.nan)
        if (not 0 <= start_minutes < 1440 or not 0 <= end_minutes < 1440
                or not start_minutes.is_integer() or not end_minutes.is_integer()
                or start_minutes == end_minutes):
            continue
        reference = now
        day = datetime.combine(reference.date()-timedelta(days=1), time(), now.tzinfo)
        for offset in range(2):
            date = day+timedelta(days=offset)
            if not _scheduled_day(schedule.get("days_of_week"), date):
                continue
            start = date+timedelta(minutes=start_minutes)
            end = date+timedelta(minutes=end_minutes)
            if end <= start:
                end += timedelta(days=1)
            if start.timestamp() <= now.timestamp() < end.timestamp():
                identity = hashlib.sha256(json.dumps(
                    [schedule, start.timestamp(), end.timestamp()], sort_keys=True).encode()).hexdigest()
                windows.append(NativeChargeWindow(identity, start.timestamp(), end.timestamp(),
                                                 schedule_read_at.timestamp()))
    # Do not extend one command lease across a change between overlapping rules.
    return min(windows, key=lambda window: (window.end, window.identity)) if windows else None


def forecast_vehicle(
    *, vehicle_id: str, data: Mapping[str, Any], timestamps: Sequence[datetime],
    interval_minutes: int, latitude: float, longitude: float,
    capacity_kwh: float, max_power_kw: float | None = None,
    native_solar_settings: Mapping[str, Any] | None = None,
    one_time_reference: datetime | None = None,
    nominal_voltage: float | None = None, nominal_phases: int | None = None,
) -> TeslaScheduleForecast | None:
    """Expand local recurring windows, union overlaps and cap energy once."""
    if not timestamps:
        return None
    schedules = _section(data, "charge_schedule_data").get("charge_schedules")
    charge = _section(data, "charge_state")
    soc = _number(charge.get("battery_level"), math.nan)
    limit = _number(charge.get("charge_limit_soc"), math.nan)
    if native_solar_settings and native_solar_settings.get("enabled") is True:
        # Native schedules replenish from any source only to the lower limit;
        # extra solar above it has no guaranteed fixed charging window.
        limit = min(limit, _number(native_solar_settings.get("lowerChargeLimit"), limit))
    if not (isinstance(schedules, list) and 0 <= soc <= 100 and 0 <= limit <= 100):
        return None
    energy = max(0.0, limit - soc) / 100 * capacity_kwh / CHARGE_EFFICIENCY
    # Use the requested current, not a measured/tapering charging power. Tesla
    # reports charger_phases=2 on some three-phase installations.
    amps = _number(charge.get("charge_current_request"))
    if amps <= 0:
        amps = _number(charge.get("charge_current_request_max"))
    supply_voltage = _number(nominal_voltage, 230.0)
    if not 90 <= supply_voltage <= 300:
        supply_voltage = 230.0
    # Unplugged vehicles can report a nonzero residual voltage (e.g. 2 V).
    # That is not the supply voltage of the next scheduled home charge.
    voltage = _number(charge.get("charger_voltage"))
    if not 90 <= voltage <= 300:
        voltage = supply_voltage
    phases = int(_number(nominal_phases or charge.get("charger_phases"), 1))
    phases = 3 if phases in (2, 3) else 1
    power = amps * voltage * phases / 1000
    if max_power_kw is not None and max_power_kw > 0:
        power = min(power, max_power_kw) if power > 0 else max_power_kw
    if energy <= 0 or not 0 < power <= 50 or not 1 <= capacity_kwh <= 250:
        return None

    step = timedelta(minutes=interval_minutes)
    dt_hours = interval_minutes / 60
    first = timestamps[0]
    horizon_end = timestamps[-1].astimezone(timezone.utc) + step
    intervals: list[tuple[datetime, datetime]] = []
    for schedule in schedules:
        if (not isinstance(schedule, dict) or schedule.get("enabled") is not True
                or not _near_site(schedule, latitude, longitude)):
            continue
        start_enabled = schedule.get("start_enabled") is True
        end_enabled = schedule.get("end_enabled") is True
        if not (start_enabled or end_enabled):
            continue
        start_minutes = _number(schedule.get("start_time"), math.nan)
        end_minutes = _number(schedule.get("end_time"), math.nan)
        if ((start_enabled and not 0 <= start_minutes <= 1440)
                or (end_enabled and not 0 <= end_minutes <= 1440)):
            continue
        one_time = schedule.get("one_time") is True
        reference = (one_time_reference or first).astimezone(first.tzinfo) if one_time else first
        day = datetime.combine(reference.date() - timedelta(days=1), time(), first.tzinfo)
        while day < horizon_end:
            if _scheduled_day(schedule.get("days_of_week"), day):
                if start_enabled:
                    start = day + timedelta(minutes=start_minutes)
                    end = (day + timedelta(minutes=end_minutes) if end_enabled
                           else start.astimezone(timezone.utc) + timedelta(hours=max(6.0, energy / power)))
                    if end_enabled and end <= start:
                        end += timedelta(days=1)
                else:
                    # End-only means finish-by: Tesla computes the start from
                    # the remaining energy, rather than charging all day.
                    end = day + timedelta(minutes=end_minutes)
                    start = end.astimezone(timezone.utc) - timedelta(hours=energy / power)
                if end > reference:
                    if end > first and start < horizon_end:
                        intervals.append((start.astimezone(timezone.utc), end.astimezone(timezone.utc)))
                    # Anchor a one-time schedule to its confirmed read. A
                    # rolling solve must not move yesterday's cached one-time
                    # event to tomorrow merely because its window has passed.
                    if one_time:
                        break
            day += timedelta(days=1)

    if not intervals:
        return None
    intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    values = []
    remaining = energy
    for index, timestamp in enumerate(timestamps):
        slot_start = timestamp.astimezone(timezone.utc)
        duration = sum(max(0.0, (min(slot_start + step, end)
                                    - max(slot_start, start)).total_seconds())
                       for start, end in merged)
        watts = min(power * 1000 * duration / step.total_seconds(),
                    remaining * 1000 / dt_hours)
        charge_timestamp = _timestamp(charge.get("timestamp"))
        if (index == 0 and abs(first.timestamp() - charge_timestamp) <= 90
                and str(charge.get("charging_state", "")).lower() == "disconnected"):
            watts = 0.0
        location = _section(data, "drive_state") or _section(data, "location_data")
        if (index == 0 and abs(first.timestamp() - _timestamp(location.get("timestamp"))) <= 90
                and math.isfinite(_number(location.get("latitude"), math.nan))
                and math.isfinite(_number(location.get("longitude"), math.nan))
                and not _near_site(location, latitude, longitude)):
            watts = 0.0
        values.append(max(0.0, watts))
        remaining = max(0.0, remaining - watts / 1000 * dt_hours)
    return TeslaScheduleForecast(vehicle_id.upper(), tuple(values), energy)


class TeslaScheduleReader:
    """Bounded reads through loaded HA Tesla providers; never wake or write."""

    def __init__(self) -> None:
        self.cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self.attempted: dict[str, datetime] = {}
        self.diagnostics: dict[str, str] = {}

    async def refresh(self, hass: Any, now: datetime, *, excluded_vins: frozenset[str] = frozenset()) -> None:
        seen: set[str] = set()
        # Prefer Teslemetry when the same VIN is exposed by both integrations.
        for domain in ("teslemetry", "tesla_fleet"):
            for entry in hass.config_entries.async_entries(domain):
                for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                    vin = str(getattr(vehicle, "vin", "")).upper()
                    coordinator = getattr(vehicle, "coordinator", None)
                    data = getattr(coordinator, "data", None)
                    if len(vin) != 17 or not vin.isalnum() or vin.isdigit() or vin in seen or not isinstance(data, dict):
                        continue
                    # A stale/failed provider must not win duplicate-VIN selection.
                    if getattr(coordinator, "last_update_success", True) is False:
                        continue
                    seen.add(vin)
                    cached = self.cache.get(vin)
                    if cached and (now - cached[0]).total_seconds() > MAX_CACHE_SECONDS:
                        self.cache.pop(vin, None)
                        cached = None
                    if vin in excluded_vins:
                        self.diagnostics[vin] = "Tesla schedule planning disabled for this vehicle"
                        continue
                    if self.diagnostics.get(vin) == "Tesla schedule planning disabled for this vehicle":
                        self.diagnostics[vin] = "Tesla schedule planning enabled; using bounded cache"
                    # HA providers may already hold schedules while the car is
                    # asleep, including immediately after a PowerSync restart.
                    # Their source timestamp bounds freshness; a metadata
                    # refresh or this reader's clock must not renew old data.
                    schedules = _section(data, "charge_schedule_data")
                    schedule_stamp = _timestamp(schedules.get("timestamp_seconds", schedules.get("timestamp")))
                    if (isinstance(schedules.get("charge_schedules"), list)
                            and 0 <= now.timestamp() - schedule_stamp <= MAX_CACHE_SECONDS
                            and (cached is None or schedule_stamp > cached[0].timestamp())):
                        snapshot = {"charge_schedule_data": schedules,
                                    "charge_state": _section(data, "charge_state")}
                        for section in ("drive_state", "location_data"):
                            snapshot[section] = _section(data, section)
                        cached = self.cache[vin] = (
                            datetime.fromtimestamp(schedule_stamp, timezone.utc), snapshot)
                    # Charge state comes from its own timestamp, never from a
                    # location/metadata update or the schedule refresh clock.
                    if cached:
                        charge = _section(data, "charge_state")
                        cached_charge = _section(cached[1], "charge_state")
                        if (_timestamp(charge.get("timestamp"))
                                > _timestamp(cached_charge.get("timestamp"))):
                            cached[1]["charge_state"] = charge
                        for section in ("drive_state", "location_data"):
                            location = _section(data, section)
                            if location:
                                cached[1][section] = location
                    previous = self.attempted.get(vin)
                    if previous and (now - previous).total_seconds() < REFRESH_SECONDS:
                        continue
                    if str(data.get("state", "")).lower() != "online":
                        self.diagnostics[vin] = ("Vehicle asleep/offline; using bounded schedule cache; no wake requested"
                            if cached else "Vehicle asleep/offline; no wake requested")
                        continue
                    self.attempted[vin] = now
                    try:
                        # Coordinator.api is the cloud client, including when
                        # the integration's command API is a BLE-first router.
                        async with asyncio.timeout(15):
                            if domain == "teslemetry":
                                # The shared client's vehicle_data() has no
                                # use_cache argument. Teslemetry otherwise
                                # defaults to cached data, which may never have
                                # fetched the schedule subset. Reuse its auth/
                                # transport while explicitly requesting a live
                                # read; do not use the wake-capable refresh API.
                                result = await coordinator.api._request(
                                    "GET", f"api/1/vehicles/{vin}/vehicle_data",
                                    {"endpoints": "charge_schedule_data;charge_state", "use_cache": "false"},
                                )
                            else:
                                result = await coordinator.api.vehicle_data(
                                    endpoints=["charge_schedule_data", "charge_state"]
                                )
                        response = result.get("response") if isinstance(result, dict) else None
                        schedule_data = _section(response, "charge_schedule_data") if isinstance(response, dict) else {}
                        if not isinstance(schedule_data.get("charge_schedules"), list):
                            raise ValueError("Schedule data unavailable from vehicle")
                        snapshot = dict(response)
                        for section in ("drive_state", "location_data"):
                            location = _section(data, section)
                            if location:
                                snapshot[section] = location
                        self.cache[vin] = (now, snapshot)
                        self.diagnostics[vin] = "Tesla schedule read confirmed"
                    except Exception as err:
                        # Never turn an unavailable read into an empty schedule.
                        self.diagnostics[vin] = "Tesla schedule read unavailable; retaining bounded cache"
                        _LOGGER.debug("Tesla schedule read unavailable (%s)", type(err).__name__)
        for vin in set(self.cache) - seen:
            self.cache.pop(vin, None)

    def forecasts(
        self, *, timestamps: Sequence[datetime], interval_minutes: int,
        latitude: float, longitude: float, capacities: Mapping[str, float],
        power_limits: Mapping[str, float], now: datetime,
        native_solar_settings: Mapping[str, Mapping[str, Any]] | None = None,
        excluded_vins: frozenset[str] = frozenset(),
        charging_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, TeslaScheduleForecast]:
        forecasts = {}
        for vin, (read_at, data) in self.cache.items():
            if vin in excluded_vins:
                continue
            if not 0 <= (now - read_at).total_seconds() <= MAX_CACHE_SECONDS:
                continue
            charge = _section(data, "charge_state")
            timestamp = _timestamp(charge.get("timestamp"))
            if not 0 <= now.timestamp() - timestamp <= MAX_CHARGE_STATE_AGE_SECONDS:
                self.diagnostics[vin] = "Vehicle charge state stale; forecast unavailable"
                continue
            if now.timestamp() - timestamp > 900:
                self.diagnostics[vin] = "Forecast estimated from last known charge state; vehicle not woken"
            forecast = forecast_vehicle(
                vehicle_id=vin, data=data, timestamps=timestamps,
                interval_minutes=interval_minutes,
                latitude=_number(latitude, math.nan), longitude=_number(longitude, math.nan),
                capacity_kwh=capacities.get(vin, 60.0), max_power_kw=power_limits.get(vin),
                native_solar_settings=(native_solar_settings or {}).get(vin),
                one_time_reference=read_at,
                nominal_voltage=(charging_parameters or {}).get(vin, {}).get("voltage"),
                nominal_phases=(charging_parameters or {}).get(vin, {}).get("phases"),
            )
            if forecast is not None:
                forecasts[vin] = forecast
        return forecasts


def merge_native_schedules(
    *, native: Mapping[str, TeslaScheduleForecast], smart: Mapping[str, Sequence[float]],
    price: Mapping[str, Sequence[float]], plans: Sequence[Any] | None,
    canonical: Mapping[str, str], interval_minutes: int,
) -> tuple[dict[str, list[float]], dict[str, Sequence[float]], list[Any] | None]:
    """Union same-car forecasts; retain LP co-optimization for other cars.

    A native-owned vehicle uses a fixed union of both controllers' windows.
    Giving that same demand to the LP as well would double it or move Tesla's
    independently executed charging. Remaining energy is counted once.
    """
    native_ids = {canonical.get(key, key) for key in native}
    merged = {key: list(values) for key, values in smart.items()
              if canonical.get(key, key) not in native_ids}
    prices = {key: values for key, values in price.items()
              if canonical.get(key, key) not in native_ids}
    remaining_plans = None if plans is None else [
        plan for plan in plans if canonical.get(plan.vehicle_id, plan.vehicle_id) not in native_ids
    ]
    for vin, forecast in native.items():
        physical = canonical.get(vin, vin)
        values = list(forecast.power_w)
        energy = forecast.energy_needed_kwh
        for source in (smart, price):
            for key, series in source.items():
                if canonical.get(key, key) == physical:
                    for index in range(min(len(values), len(series))):
                        values[index] = max(values[index], _number(series[index]))
        for plan in plans or ():
            if canonical.get(plan.vehicle_id, plan.vehicle_id) == physical:
                energy = max(energy, plan.energy_needed_kwh / plan.charge_efficiency)
        remaining = energy
        for index, watts in enumerate(values):
            values[index] = min(watts, remaining * 60000 / interval_minutes)
            remaining = max(0.0, remaining - values[index] * interval_minutes / 60000)
        merged[vin] = values
    return merged, prices, remaining_plans
