"""Shared EV charging price lookup helpers."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

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
    if getattr(coordinator, "last_update_success", None) is False:
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
        or len(timestamps) != len(prices)
        or len(timestamps) < 2
    ):
        return None

    # Treat any malformed/non-finite value in the aligned retail array as an
    # invalid snapshot.  A valid-looking current slot must not mask a broken
    # provider refresh elsewhere in the same schedule.
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

    try:
        price_dollars = float(prices[active_index])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(price_dollars):
        return None

    price_cents = price_dollars * 100.0
    return price_cents if math.isfinite(price_cents) else None


def get_current_retail_price(hass: Any, entry_id: str) -> float | None:
    """Resolve the current retail import price from the optimizer schedule.

    ``OptimizationCoordinator`` keeps the tariff in dollars/kWh, aligned to
    ``schedule.timestamps``. This intentionally reads only that timestamped
    retail schedule; provider wholesale arrays and positional fallbacks are not
    safe inputs for a scheduled EV max-price gate.
    """
    return _get_current_optimizer_schedule_price(hass, entry_id, "import_price")


def get_current_export_price(hass: Any, entry_id: str) -> float | None:
    """Resolve current export earnings without synthetic fallback values."""
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
