"""Shared EV charging price lookup helpers."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

try:
    from homeassistant.util import dt as dt_util
except ModuleNotFoundError:  # pragma: no cover - lightweight unit-test fallback
    class _DtUtil:
        @staticmethod
        def now() -> datetime:
            return datetime.now(timezone.utc)

    dt_util = _DtUtil()

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _parse_schedule_timestamp(value: Any, local_tz: Any) -> datetime | None:
    """Parse one optimizer timestamp, preserving an explicit offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz or timezone.utc)
    return parsed


def _get_current_optimizer_schedule_price(
    hass: Any,
    entry_id: str,
    price_key: str,
) -> float | None:
    """Resolve one timestamp-aligned optimizer schedule price in cents/kWh.

    Returns ``None`` when the schedule is missing, malformed, stale, or has no
    slot covering the HA-local current time.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
    coordinator = entry_data.get("optimization_coordinator")
    if (getattr(coordinator, "enabled", None) is False
            or getattr(coordinator, "last_update_success", None) is False):
        return None
    data = getattr(coordinator, "data", None)
    if isinstance(data, dict) and data.get("optimization_status") == "stale":
        return None
    schedule = data.get("schedule") if isinstance(data, dict) else None
    if not isinstance(schedule, dict):
        return None

    timestamps = schedule.get("timestamps")
    prices = schedule.get(price_key)
    if (
        not isinstance(timestamps, (list, tuple))
        or not isinstance(prices, (list, tuple))
        or len(timestamps) < 2
        or len(prices) < 2
        or len(prices) > len(timestamps)
    ):
        return None

    # A display schedule may intentionally omit synthetic future prices while
    # retaining optimizer timestamps for the full planning horizon.  Validate
    # every real price, then only select a current slot that has one.
    for raw_price in prices:
        if isinstance(raw_price, bool):
            return None
        try:
            if not math.isfinite(float(raw_price)):
                return None
        except (TypeError, ValueError, OverflowError):
            return None

    try:
        now = dt_util.now()
    except Exception:
        return None
    if not isinstance(now, datetime):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    parsed_timestamps: list[datetime] = []
    for raw_timestamp in timestamps:
        timestamp = _parse_schedule_timestamp(raw_timestamp, now.tzinfo)
        if timestamp is None:
            return None
        parsed_timestamps.append(timestamp)

    # A schedule must be strictly ordered.  This also prevents an accidental
    # first/padded-array selection when timestamps do not describe real slots.
    if any(
        parsed_timestamps[index] >= parsed_timestamps[index + 1]
        for index in range(len(parsed_timestamps) - 1)
    ):
        return None

    active_index: int | None = None
    for index, slot_start in enumerate(parsed_timestamps):
        if index + 1 < len(parsed_timestamps):
            slot_end = parsed_timestamps[index + 1]
        else:
            # The final slot has no following timestamp; use the preceding
            # interval and reject it once that interval has elapsed.
            slot_end = slot_start + (
                parsed_timestamps[index] - parsed_timestamps[index - 1]
            )
        if slot_start <= now < slot_end:
            active_index = index
            break

    if active_index is None:
        return None
    if active_index >= len(prices):
        return None

    try:
        price_dollars = float(prices[active_index])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(price_dollars):
        return None

    price_cents = price_dollars * 100.0
    return price_cents if math.isfinite(price_cents) else None


class RetailInterval(NamedTuple):
    start: datetime
    end: datetime
    cents: float


INTERVAL_RETAIL_PROVIDERS = ("epex", "localvolts", "octopus")


def _finite_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) else None


def _epex_sensor_intervals(hass, entity_id, region, now):
    """Use the selected retail sensor's real coverage, anchored to its own sample."""
    state = hass.states.get(entity_id)
    if state is None or str(state.state).lower() in ("unknown", "unavailable", "none", ""):
        return []
    attrs = state.attributes
    unit = str(attrs.get("unit_of_measurement") or attrs.get("price_unit")
               or attrs.get("minor_price_unit") or "ct/kWh").lower().replace(" ", "")
    if unit in ("eur/kwh", "€/kwh"):
        scale = 100.0
    elif unit in ("ct/kwh", "c/kwh", "cent/kwh", "cents/kwh", "€ct/kwh", "eurocent/kwh", "ct", "cent"):
        scale = 1.0
    else:
        return []

    values = attrs.get("price_values")
    if not isinstance(values, (list, dict)):
        values = {key: value for key, value in attrs.items()
                  if _parse_schedule_timestamp(key, now.tzinfo) is not None}
    if isinstance(values, dict) and values:
        rows = {}
        for key, value in values.items():
            start = _parse_schedule_timestamp(key, now.tzinfo)
            if start is not None:
                rows[start.astimezone(timezone.utc)] = _finite_price(value)
        starts = sorted(rows)
        # A missing interval must not extend an earlier cheap price. Use the
        # regional settlement period as the upper bound on timestamp-only rows.
        period = timedelta(minutes=15 if region == "BE" else 60)
        period = min([period] + [right - left for left, right in zip(starts, starts[1:])])
        intervals = []
        for index, start in enumerate(starts):
            end = start + period
            if index + 1 < len(starts):
                end = min(end, starts[index + 1])
            if rows[start] is not None:
                intervals.append(RetailInterval(start, end, rows[start] * scale))
        return intervals

    # The optimizer accepts five-minute positional arrays and a scalar
    # current rate. Advance from the sensor observation, never from each tick.
    if not values and attrs.get("forecast"):
        return []
    updated = getattr(state, "last_updated", None)
    if not isinstance(updated, datetime) or updated.tzinfo is None or updated > now:
        return []
    updated = updated.astimezone(timezone.utc)
    start = updated.replace(minute=updated.minute // 5 * 5, second=0, microsecond=0)
    intervals = []
    for raw in (values if isinstance(values, list) else [state.state]):
        end = start + timedelta(minutes=5)
        price = _finite_price(raw)
        if price is not None:
            intervals.append(RetailInterval(start, end, price * scale))
        start = end
    return intervals


def provider_retail_intervals(hass, entry, provider, now, channel="general"):
    """Read one selected retail channel, preserving signed cents and real coverage.

    None means no independent source exists. An empty result means the selected
    source has no valid coverage, so a lower-priority source cannot authorize use.
    """
    options = {**entry.data, **entry.options}
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator_key = f"{provider}_coordinator"
    coordinator = runtime.get(coordinator_key)
    if provider == "epex":
        key = "epex_import_price_entity" if channel == "general" else "epex_export_price_entity"
        source = options.get("epex_export_source")
        if channel == "general" or source in (None, "custom_entity"):
            entity_id = options.get(key)
            if entity_id:
                return _epex_sensor_intervals(hass, entity_id, options.get("epex_region"), now)
            if channel == "feedIn" and source == "custom_entity":
                return []
    if coordinator is None:
        return [] if coordinator_key in runtime else None
    data = getattr(coordinator, "data", None)
    if (getattr(coordinator, "last_update_success", None) is not True
            or not isinstance(data, dict) or not isinstance(now, datetime)):
        return []
    if channel == "feedIn" and (data.get("octopus") or {}).get("export_status") == "unknown":
        return []
    if provider == "localvolts":
        updated = data.get("last_update")
        if (not isinstance(updated, datetime) or updated.tzinfo is None or
                not timedelta(0) <= now - updated <= timedelta(minutes=15)):
            return []

    prices = {}
    for group in ("current", "forecast"):
        for row in data.get(group) or ():
            if not isinstance(row, dict) or row.get("channelType") != channel:
                continue
            start = _parse_schedule_timestamp(row.get("startTime") or row.get("valid_from"), now.tzinfo)
            end = _parse_schedule_timestamp(
                row.get("endTime") or row.get("valid_to") or row.get("nemTime"), now.tzinfo
            )
            if start is None or end is None or end <= start:
                continue
            bounds = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
            price = _finite_price(row.get("perKwh")) if row.get("price_available", True) else None
            if price is not None and channel == "feedIn":
                price = -price
            if bounds in prices and prices[bounds] != price:
                price = None
            prices[bounds] = price

    # A duplicate forecast row is fine, but overlapping incompatible bounds
    # cannot authorize a price-capped window. Suppress both disputed rows.
    intervals = []
    ordered = sorted(prices)
    latest_end = None
    for index, (start, end) in enumerate(ordered):
        overlaps_previous = latest_end is not None and start < latest_end
        latest_end = max(end, latest_end) if latest_end is not None else end
        overlaps_next = index + 1 < len(ordered) and ordered[index + 1][0] < end
        price = prices[start, end]
        if price is not None and not overlaps_previous and not overlaps_next:
            intervals.append(RetailInterval(start, end, price))
    return intervals


def _active_price(intervals, now):
    return next((row.cents for row in intervals if row.start <= now < row.end), None)


def get_current_retail_price(hass: Any, entry_id: str, entry=None) -> float | None:
    """Read the selected retail interval or the enabled optimizer's aligned tariff."""
    if entry is not None:
        provider = entry.options.get("electricity_provider", entry.data.get("electricity_provider"))
        if provider in INTERVAL_RETAIL_PROVIDERS:
            now = dt_util.now()
            intervals = provider_retail_intervals(hass, entry, provider, now)
            if intervals is not None:
                return _active_price(intervals, now)
    return _get_current_optimizer_schedule_price(hass, entry_id, "import_price")


def get_ev_control_import_price(hass, entry):
    """Resolve the live import price used by every EV mode in the periodic tick."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    provider = entry.options.get("electricity_provider", entry.data.get("electricity_provider", "amber"))
    if provider in INTERVAL_RETAIL_PROVIDERS:
        return get_current_retail_price(hass, entry.entry_id, entry)

    if provider in ("amber", "flow_power"):
        coordinator = data.get("amber_coordinator")
        if getattr(coordinator, "last_update_success", None) is not False:
            for row in (getattr(coordinator, "data", None) or {}).get("current", []):
                if row.get("channelType") == "general":
                    price = _finite_price(row.get("perKwh"))
                    if price is not None:
                        return price
        cached = _finite_price((data.get("amber_prices") or {}).get("import_cents"))
        if cached is not None:
            return cached

    tariff = data.get("tariff_schedule") or {}
    if tariff.get("tou_periods"):
        from .. import get_current_price_from_tariff_schedule

        buy_price, _, _ = get_current_price_from_tariff_schedule(tariff)
        return _finite_price(buy_price)
    buy_price = _finite_price(tariff.get("buy_price"))
    if buy_price is not None:
        return buy_price
    now = dt_util.now()
    if not isinstance(now, datetime):
        return None
    period = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
    period_price = _finite_price((tariff.get("buy_prices") or {}).get("PERIOD_" + period.replace(":", "_")))
    if period_price is not None:
        return period_price * 100
    for slot in (data.get("sigenergy_tariff") or {}).get("buy_prices", []):
        if slot.get("timeRange", "").startswith(period):
            return _finite_price(slot.get("price"))
    return None


def get_current_export_price(hass: Any, entry_id: str) -> float | None:
    """Resolve current export earnings without synthetic fallback values."""
    entry = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("entry")
    if entry is not None:
        provider = entry.options.get("electricity_provider", entry.data.get("electricity_provider"))
        if provider in INTERVAL_RETAIL_PROVIDERS:
            now = dt_util.now()
            intervals = provider_retail_intervals(hass, entry, provider, now, "feedIn")
            if intervals is not None:
                return _active_price(intervals, now)
    optimizer_price = _get_current_optimizer_schedule_price(
        hass,
        entry_id,
        "export_price",
    )
    if optimizer_price is not None:
        return optimizer_price

    entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
    for coordinator_key in (
        "amber_coordinator",
        "localvolts_coordinator",
        "octopus_coordinator",
        "epex_coordinator",
        "aemo_sensor_coordinator",
    ):
        coordinator = entry_data.get(coordinator_key)
        if getattr(coordinator, "last_update_success", True) is False:
            continue
        data = getattr(coordinator, "data", None)
        if not isinstance(data, dict):
            continue
        for price in data.get("current", []):
            if price.get("channelType") != "feedIn":
                continue
            raw = price.get("perKwh")
            if isinstance(raw, bool):
                return None
            try:
                feed_in = float(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            return -feed_in if math.isfinite(feed_in) else None

    tariff_schedule = entry_data.get("tariff_schedule")
    if tariff_schedule:
        try:
            from .. import get_current_price_from_tariff_schedule

            _import_price, export_price, _current_period = (
                get_current_price_from_tariff_schedule(tariff_schedule)
            )
            parsed = float(export_price)
            return parsed if math.isfinite(parsed) else None
        except Exception as err:
            _LOGGER.debug("Could not resolve strict EV export price: %s", err)

    sigenergy_tariff = entry_data.get("sigenergy_tariff")
    if sigenergy_tariff:
        sell_prices = sigenergy_tariff.get("sell_prices", [])
        now = dt_util.now()
        current_time = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
        for slot in sell_prices:
            if slot.get("timeRange", "").startswith(current_time):
                try:
                    parsed = float(slot.get("price"))
                except (TypeError, ValueError, OverflowError):
                    return None
                return parsed if math.isfinite(parsed) else None

    for key, field in (
        ("current_prices", "export_cents"),
        ("price_data", "export_price_cents"),
    ):
        price_data = entry_data.get(key)
        if not isinstance(price_data, dict) or field not in price_data:
            continue
        try:
            parsed = float(price_data[field])
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None

    return None


def _prices_from_current(data: dict[str, Any] | None) -> tuple[float, float] | None:
    """Extract import/export prices from Amber-shaped current price data."""
    if not data:
        return None

    current_prices = data.get("current", [])
    if not current_prices:
        return None

    import_price = None
    export_price = None
    for price in current_prices:
        if price.get("channelType") == "general":
            import_price = price.get("perKwh")
        elif price.get("channelType") == "feedIn":
            feed_in = price.get("perKwh")
            if feed_in is not None:
                export_price = abs(feed_in)

    if import_price is None:
        return None

    return (
        import_price,
        export_price if export_price is not None else 8.0,
    )


def get_current_ev_prices(hass: Any, entry_id: str) -> tuple[float, float]:
    """Get current import/export prices for EV charging session accounting.

    Returns prices in cents/kWh.
    """
    import_price = 30.0
    export_price = 8.0

    entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})

    # Dynamic provider coordinators expose Amber-shaped c/kWh current data.
    for coordinator_key in (
        "amber_coordinator",
        "localvolts_coordinator",
        "octopus_coordinator",
        "epex_coordinator",
        "aemo_sensor_coordinator",
    ):
        coordinator = entry_data.get(coordinator_key)
        prices = _prices_from_current(getattr(coordinator, "data", None))
        if prices is not None:
            return prices

    # Globird/AEMO VPP and other static TOU providers store a tariff schedule.
    # Resolve the live period, otherwise a free/cheap window can be costed with
    # an old cached or default price.
    tariff_schedule = entry_data.get("tariff_schedule")
    if tariff_schedule:
        try:
            from .. import get_current_price_from_tariff_schedule

            import_price, export_price, _current_period = (
                get_current_price_from_tariff_schedule(tariff_schedule)
            )
            return import_price, export_price
        except Exception as err:
            _LOGGER.debug("Could not resolve EV tariff schedule price: %s", err)
            import_price = tariff_schedule.get("buy_price", 30.0)
            export_price = tariff_schedule.get("sell_price", 8.0)
            return import_price, export_price

    # Sigenergy tariff slots are already in c/kWh.
    sigenergy_tariff = entry_data.get("sigenergy_tariff")
    if sigenergy_tariff:
        buy_prices = sigenergy_tariff.get("buy_prices", [])
        sell_prices = sigenergy_tariff.get("sell_prices", [])
        now = dt_util.now()
        current_time = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
        if buy_prices:
            for slot in buy_prices:
                if slot.get("timeRange", "").startswith(current_time):
                    import_price = slot.get("price", 30.0)
                    break
        if sell_prices:
            for slot in sell_prices:
                if slot.get("timeRange", "").startswith(current_time):
                    export_price = slot.get("price", 8.0)
                    break
        return import_price, export_price

    price_data = entry_data.get("current_prices", {})
    if price_data:
        import_price = price_data.get("import_cents", 30.0)
        export_price = price_data.get("export_cents", 8.0)

    return import_price, export_price
