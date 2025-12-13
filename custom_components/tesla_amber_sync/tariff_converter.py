"""Convert Amber Electric pricing to Tesla tariff format."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _round_price(price: float) -> float:
    """
    Round price to 4 decimal places, removing trailing zeros.

    Examples:
    - 0.2014191 → 0.2014 (4 decimals)
    - 0.1990000 → 0.199 (3 decimals, trailing zeros removed)
    - 0.1234500 → 0.1235 (4 decimals, rounded)

    Args:
        price: Price in dollars per kWh

    Returns:
        Price rounded to max 4 decimal places with trailing zeros removed
    """
    # Round to 4 decimal places
    rounded = round(price, 4)
    # Python's float naturally drops trailing zeros in JSON serialization
    return rounded


def extract_most_recent_actual_interval(
    forecast_data: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """
    Extract the most recent live pricing from 5-minute forecast data.

    Priority order (most recent first):
    1. CurrentInterval - Real-time price for the current ongoing 5-minute period
    2. ActualInterval - Settled price from the last completed 5-minute period

    This ensures we always use the most up-to-date pricing to catch spikes.

    Args:
        forecast_data: List of price intervals from Amber API (with resolution=5)

    Returns:
        Dict with 'general' and 'feedIn' keys containing the interval data,
        or None if no suitable interval found.

    Example return:
        {
            'general': {'type': 'CurrentInterval', 'perKwh': 36.19, 'duration': 5, ...},
            'feedIn': {'type': 'CurrentInterval', 'perKwh': -10.44, 'duration': 5, ...}
        }
    """
    if not forecast_data:
        _LOGGER.warning("No forecast data provided to extract pricing interval")
        return None

    # PRIORITY 1: Check for CurrentInterval (ongoing period with real-time price)
    current_intervals = [
        interval
        for interval in forecast_data
        if interval.get("type") == "CurrentInterval" and interval.get("duration") == 5
    ]

    if current_intervals:
        # Extract prices by channel (general = buy, feedIn = sell)
        result: dict[str, Any] = {"general": None, "feedIn": None}

        for interval in current_intervals:
            channel = interval.get("channelType")
            if channel in ["general", "feedIn"] and result[channel] is None:
                result[channel] = interval

            # Stop when we have both channels
            if result["general"] and result["feedIn"]:
                break

        if result["general"] or result["feedIn"]:
            latest_time = current_intervals[0].get("nemTime", "unknown")
            general_price = result["general"].get("perKwh") if result["general"] else None
            feedin_price = result["feedIn"].get("perKwh") if result["feedIn"] else None

            _LOGGER.info("Using CurrentInterval (real-time price) at %s", latest_time)
            if general_price is not None:
                _LOGGER.info("  - General (buy): %.2f¢/kWh", general_price)
            if feedin_price is not None:
                _LOGGER.info("  - FeedIn (sell): %.2f¢/kWh", feedin_price)

            return result

    # PRIORITY 2: Fall back to ActualInterval (last completed period)
    actual_intervals = [
        interval
        for interval in forecast_data
        if interval.get("type") == "ActualInterval" and interval.get("duration") == 5
    ]

    if not actual_intervals:
        _LOGGER.warning(
            "No 5-minute CurrentInterval or ActualInterval found - may be too early in period"
        )
        return None

    # Sort by nemTime descending to get most recent
    try:
        actual_intervals.sort(
            key=lambda x: datetime.fromisoformat(
                x.get("nemTime", "").replace("Z", "+00:00")
            ),
            reverse=True,
        )
    except Exception as err:
        _LOGGER.error("Error sorting ActualIntervals by time: %s", err)
        return None

    # Extract prices by channel (general = buy, feedIn = sell)
    result: dict[str, Any] = {"general": None, "feedIn": None}

    for interval in actual_intervals:
        channel = interval.get("channelType")
        if channel in ["general", "feedIn"] and result[channel] is None:
            result[channel] = interval

        # Stop when we have both channels from the same timestamp
        if result["general"] and result["feedIn"]:
            break

    # Log what we found
    if result["general"] or result["feedIn"]:
        latest_time = actual_intervals[0].get("nemTime", "unknown")
        general_price = result["general"].get("perKwh") if result["general"] else None
        feedin_price = result["feedIn"].get("perKwh") if result["feedIn"] else None

        _LOGGER.info("Using ActualInterval (last completed period) at %s", latest_time)
        if general_price is not None:
            _LOGGER.info("  - General (buy): %.2f¢/kWh", general_price)
        if feedin_price is not None:
            _LOGGER.info("  - FeedIn (sell): %.2f¢/kWh", feedin_price)

        return result

    _LOGGER.warning("ActualIntervals found but no valid channel data")
    return None


def _build_demand_charge_rates(
    start_time: str,
    end_time: str,
    rate: float,
) -> dict[str, float]:
    """
    Build demand charge rates for Tesla tariff format.

    Creates a dict mapping PERIOD_XX_XX keys to demand charge rates.
    Only periods within the peak time range get the demand charge rate,
    all other periods get 0.

    Args:
        start_time: Peak period start time in HH:MM format (e.g., "14:00")
        end_time: Peak period end time in HH:MM format (e.g., "20:00")
        rate: Demand charge rate in $/kW

    Returns:
        Dict mapping PERIOD_XX_XX to demand charge rate

    Example:
        _build_demand_charge_rates("14:00", "20:00", 10.5)
        Returns: {"PERIOD_14_00": 10.5, "PERIOD_14_30": 10.5, ..., "PERIOD_19_30": 10.5}
    """
    try:
        # Parse start and end times
        start_hour, start_minute = map(int, start_time.split(":"))
        end_hour, end_minute = map(int, end_time.split(":"))
    except (ValueError, AttributeError) as err:
        _LOGGER.error("Invalid time format for demand charges: %s", err)
        return {}

    demand_rates: dict[str, float] = {}

    # Build all 48 half-hour periods
    for hour in range(24):
        for minute in [0, 30]:
            period_key = f"PERIOD_{hour:02d}_{minute:02d}"

            # Check if this period falls within peak time range
            period_minutes = hour * 60 + minute
            start_minutes = start_hour * 60 + start_minute
            end_minutes = end_hour * 60 + end_minute

            # Handle overnight periods (e.g., 22:00 to 06:00)
            if end_minutes <= start_minutes:
                # Peak period wraps around midnight
                is_peak = period_minutes >= start_minutes or period_minutes < end_minutes
            else:
                # Normal daytime peak period
                is_peak = start_minutes <= period_minutes < end_minutes

            # Apply rate for peak periods, 0 for off-peak
            demand_rates[period_key] = rate if is_peak else 0

    _LOGGER.info(
        "Built demand charge rates: %s to %s at $%.2f/kW",
        start_time,
        end_time,
        rate,
    )

    return demand_rates


def convert_amber_to_tesla_tariff(
    forecast_data: list[dict[str, Any]],
    tesla_energy_site_id: str,
    forecast_type: str = "predicted",
    powerwall_timezone: str | None = None,
    current_actual_interval: dict[str, Any] | None = None,
    demand_charge_enabled: bool = False,
    demand_charge_rate: float = 0.0,
    demand_charge_start_time: str = "14:00",
    demand_charge_end_time: str = "20:00",
    demand_charge_apply_to: str = "Buy Only",
    demand_charge_days: str = "All Days",
    demand_artificial_price_enabled: bool = False,
) -> dict[str, Any] | None:
    """
    Convert Amber price forecast to Tesla tariff format.

    Implements rolling 24-hour window: periods that have passed today get tomorrow's prices,
    future periods get today's prices. This gives Tesla a full 24-hour lookahead.

    NEW: Optionally uses ActualInterval (5-min actual price) for the current 30-min period
    to capture short-term price spikes that would otherwise be averaged out.

    NEW: Supports demand charges with configurable peak period and rate.

    Args:
        forecast_data: List of price forecast points from Amber API (5-min or 30-min resolution)
        tesla_energy_site_id: Tesla energy site ID
        forecast_type: Amber forecast type to use ('predicted', 'low', or 'high')
        powerwall_timezone: Powerwall timezone from site_info (optional)
                           If provided, uses this instead of auto-detecting from Amber data
        current_actual_interval: Dict with 'general' and 'feedIn' ActualInterval data (optional)
                                If provided, uses this for the current 30-min period instead of averaging
        demand_charge_enabled: Enable demand charge tracking (default: False)
        demand_charge_rate: Demand charge rate in $/kW (default: 0.0)
        demand_charge_start_time: Peak period start time in HH:MM format (default: "14:00")
        demand_charge_end_time: Peak period end time in HH:MM format (default: "20:00")

    Returns:
        Tesla-compatible tariff structure or None if conversion fails
    """
    if not forecast_data:
        _LOGGER.warning("No forecast data provided")
        return None

    _LOGGER.info("Converting %d Amber forecast points to Tesla tariff", len(forecast_data))

    # Timezone handling:
    # 1. Prefer Powerwall timezone from site_info (most accurate)
    # 2. Fall back to auto-detection from Amber data
    detected_tz = None
    if powerwall_timezone:
        from zoneinfo import ZoneInfo
        try:
            detected_tz = ZoneInfo(powerwall_timezone)
            _LOGGER.info("✓ Using Powerwall timezone from site_info: %s", powerwall_timezone)
        except Exception as err:
            _LOGGER.warning(
                "Invalid Powerwall timezone '%s': %s, falling back to auto-detection",
                powerwall_timezone,
                err,
            )

    if not detected_tz:
        # Auto-detect timezone from first Amber timestamp
        # Amber timestamps include timezone info: "2025-11-11T16:05:00+10:00"
        for point in forecast_data:
            nem_time = point.get("nemTime", "")
            if nem_time:
                try:
                    timestamp = datetime.fromisoformat(nem_time.replace("Z", "+00:00"))
                    detected_tz = timestamp.tzinfo
                    _LOGGER.info("Auto-detected timezone from Amber data: %s", detected_tz)
                    break
                except Exception:
                    continue

    # Build timestamp-indexed price lookup: (date, hour, minute) -> price
    general_lookup: dict[tuple[str, int, int], list[float]] = {}
    feedin_lookup: dict[tuple[str, int, int], list[float]] = {}

    for point in forecast_data:
        try:
            nem_time = point.get("nemTime", "")
            timestamp = datetime.fromisoformat(nem_time.replace("Z", "+00:00"))
            channel_type = point.get("channelType", "")
            duration = point.get("duration", 30)  # Get actual interval duration (usually 5 or 30 minutes)

            # Price extraction logic:
            # - ActualInterval (past): Use perKwh (actual settled price)
            # - CurrentInterval (now): Use perKwh (current actual price)
            # - ForecastInterval (future): Use advancedPrice (forecast with user-selected type)
            #
            # advancedPrice includes complete forecast:
            # - Wholesale price forecast
            # - Network fees
            # - Market fees
            # - Renewable energy certificates
            #
            # User selects: 'predicted' (default), 'low' (conservative), 'high' (optimistic)
            advanced_price = point.get("advancedPrice")
            interval_type = point.get("type", "unknown")

            # For ForecastInterval: Prefer advancedPrice, fall back to perKwh (for AEMO data)
            if interval_type == "ForecastInterval":
                if advanced_price:
                    # Handle dict format (standard: {predicted, low, high})
                    if isinstance(advanced_price, dict):
                        if forecast_type not in advanced_price:
                            available = list(advanced_price.keys())
                            error_msg = f"Forecast type '{forecast_type}' not found in advancedPrice. Available: {available}"
                            _LOGGER.error("%s: %s", nem_time, error_msg)
                            raise ValueError(error_msg)

                        per_kwh_cents = advanced_price[forecast_type]
                        _LOGGER.debug("%s [ForecastInterval]: advancedPrice.%s=%.2fc/kWh", nem_time, forecast_type, per_kwh_cents)

                    # Handle simple number format (legacy)
                    elif isinstance(advanced_price, (int, float)):
                        per_kwh_cents = advanced_price
                        _LOGGER.debug("%s [ForecastInterval]: advancedPrice=%.2fc/kWh (numeric)", nem_time, per_kwh_cents)

                    else:
                        error_msg = f"Invalid advancedPrice format at {nem_time}: {type(advanced_price).__name__}"
                        _LOGGER.error(error_msg)
                        raise ValueError(error_msg)
                else:
                    # No advancedPrice - use perKwh directly (AEMO data or far-future Amber forecasts)
                    per_kwh_cents = point.get("perKwh", 0)
                    _LOGGER.debug("%s [ForecastInterval]: perKwh=%.2fc/kWh (AEMO/wholesale)", nem_time, per_kwh_cents)

            # For CurrentInterval: Prefer advancedPrice (Amber retail forecast) over perKwh (AEMO wholesale)
            # For ActualInterval: Use perKwh (actual settled retail price)
            else:
                if interval_type == "CurrentInterval" and advanced_price:
                    # CurrentInterval has advancedPrice during first 25 mins - use it for Amber retail forecast
                    if isinstance(advanced_price, dict):
                        per_kwh_cents = advanced_price.get(forecast_type, advanced_price.get("predicted", 0))
                        _LOGGER.debug("%s [CurrentInterval]: advancedPrice.%s=%.2fc/kWh (Amber retail forecast)", nem_time, forecast_type, per_kwh_cents)
                    else:
                        per_kwh_cents = advanced_price
                        _LOGGER.debug("%s [CurrentInterval]: advancedPrice=%.2fc/kWh (Amber retail forecast)", nem_time, per_kwh_cents)
                else:
                    # ActualInterval or CurrentInterval without advancedPrice (last 5 mins of 30-min period)
                    per_kwh_cents = point.get("perKwh", 0)
                    if interval_type == "ActualInterval":
                        _LOGGER.debug("%s [ActualInterval]: perKwh=%.2fc/kWh (actual settled retail)", nem_time, per_kwh_cents)
                    else:
                        _LOGGER.debug("%s [CurrentInterval]: perKwh=%.2fc/kWh (fallback - AEMO wholesale)", nem_time, per_kwh_cents)

            # Amber convention: feedIn prices are negative when you get paid
            # Tesla convention: sell prices are positive when you get paid
            # So we negate feedIn prices
            if channel_type == "feedIn":
                per_kwh_cents = -per_kwh_cents

            per_kwh_dollars = _round_price(per_kwh_cents / 100)

            # Use interval START time for bucketing
            # Amber's nemTime is the END of the interval, duration tells us the length
            # Calculate startTime = nemTime - duration
            # This gives us direct alignment with Tesla's PERIOD_XX_XX naming
            #
            # Example:
            #   nemTime=18:00, duration=30 → startTime=17:30 → bucket key (17, 30)
            #   Tesla PERIOD_17_30 → looks up key (17, 30) directly
            #   Result: Clean alignment with no shifting needed
            interval_start = timestamp - timedelta(minutes=duration)

            # CRITICAL: Convert to local Powerwall timezone to handle DST correctly
            # Amber may provide timestamps with fixed offsets (e.g., +10:00 during AEDT when it should be +11:00)
            # Converting to the Powerwall's timezone ensures we get the correct local time
            if detected_tz:
                interval_start_local = interval_start.astimezone(detected_tz)
            else:
                interval_start_local = interval_start

            # Round interval start time to nearest 30-minute bucket
            start_minute_bucket = 0 if interval_start_local.minute < 30 else 30

            date_str = interval_start_local.date().isoformat()
            lookup_key = (date_str, interval_start_local.hour, start_minute_bucket)

            if channel_type == "general":
                if lookup_key not in general_lookup:
                    general_lookup[lookup_key] = []
                general_lookup[lookup_key].append(per_kwh_dollars)
            elif channel_type == "feedIn":
                if lookup_key not in feedin_lookup:
                    feedin_lookup[lookup_key] = []
                feedin_lookup[lookup_key].append(per_kwh_dollars)

        except Exception as err:
            _LOGGER.error("Error processing price point: %s", err)
            continue

    # Build the rolling 24-hour tariff
    general_prices, feedin_prices = _build_rolling_24h_tariff(
        general_lookup, feedin_lookup, detected_tz, current_actual_interval
    )

    # If too many periods are missing, abort sync to preserve last good tariff
    if general_prices is None or feedin_prices is None:
        _LOGGER.error("Aborting tariff conversion - too many missing price periods")
        return None

    _LOGGER.info(
        "Built rolling 24h tariff with %d general and %d feed-in periods",
        len(general_prices),
        len(feedin_prices),
    )

    # Build demand charge rates if enabled
    demand_charge_rates: dict[str, float] = {}
    if demand_charge_enabled and demand_charge_rate > 0:
        demand_charge_rates = _build_demand_charge_rates(
            demand_charge_start_time,
            demand_charge_end_time,
            demand_charge_rate,
        )
        _LOGGER.info("Demand charge schedule: %d peak periods in tariff",
                     sum(1 for rate in demand_charge_rates.values() if rate > 0))

    # Apply artificial price increase during demand periods if enabled (ALPHA feature)
    if demand_artificial_price_enabled and demand_charge_enabled:
        artificial_increase = 2.0  # $2/kWh increase during demand periods
        periods_modified = 0

        # Check if today is a valid day for demand charges
        weekday = datetime.now().weekday()  # 0=Monday, 6=Sunday

        day_is_valid = True
        if demand_charge_days == "Weekdays Only" and weekday >= 5:  # Saturday or Sunday
            day_is_valid = False
        elif demand_charge_days == "Weekends Only" and weekday < 5:  # Monday-Friday
            day_is_valid = False
        # "All Days" matches any day

        if day_is_valid:
            # Parse demand period times
            start_parts = demand_charge_start_time.split(":")
            start_hour, start_minute = int(start_parts[0]), int(start_parts[1]) if len(start_parts) > 1 else 0
            end_parts = demand_charge_end_time.split(":")
            end_hour, end_minute = int(end_parts[0]), int(end_parts[1]) if len(end_parts) > 1 else 0

            for period_key in general_prices.keys():
                # Extract hour/minute from PERIOD_HH_MM
                parts = period_key.split("_")
                hour = int(parts[1])
                minute = int(parts[2])

                # Check if this period is in the demand peak window
                time_minutes = hour * 60 + minute
                start_minutes = start_hour * 60 + start_minute
                end_minutes = end_hour * 60 + end_minute

                # Handle overnight periods
                if end_minutes <= start_minutes:
                    in_peak = time_minutes >= start_minutes or time_minutes < end_minutes
                else:
                    in_peak = start_minutes <= time_minutes < end_minutes

                if in_peak:
                    original_price = general_prices[period_key]
                    general_prices[period_key] = original_price + artificial_increase
                    periods_modified += 1
                    _LOGGER.debug(
                        "%s: Artificial price increase applied: $%.4f -> $%.4f (+$%.2f)",
                        period_key, original_price, general_prices[period_key], artificial_increase
                    )

            if periods_modified > 0:
                _LOGGER.info(
                    "🔺 ALPHA: Artificial price increase (+$%.2f/kWh) applied to %d demand periods",
                    artificial_increase, periods_modified
                )
        else:
            _LOGGER.debug(
                "Artificial price increase skipped - today (%d) not in demand_charge_days (%s)",
                weekday, demand_charge_days
            )

    # Create the Tesla tariff structure
    tariff = _build_tariff_structure(
        general_prices,
        feedin_prices,
        demand_charge_rates,
        demand_charge_apply_to
    )

    return tariff


def _build_rolling_24h_tariff(
    general_lookup: dict[tuple[str, int, int], list[float]],
    feedin_lookup: dict[tuple[str, int, int], list[float]],
    detected_tz: Any = None,
    current_actual_interval: dict[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Build a rolling 24-hour tariff where past periods use tomorrow's prices.

    NEW: Optionally injects ActualInterval (5-min actual price) for the current 30-min period
    to capture short-term price spikes.

    Example (current time is 4:37 PM, ActualInterval shows $14 spike at 4:30-4:35):
    - PERIOD_04_30 (4:30-5:00) → uses $14 ActualInterval (instead of averaging 4:30-5:00 forecast)
    - All other periods → use normal 30-min averaged forecast

    Args:
        general_lookup: Dict of (date, hour, minute) -> [prices] for buy prices
        feedin_lookup: Dict of (date, hour, minute) -> [prices] for sell prices
        detected_tz: Timezone detected from Amber data timestamps
        current_actual_interval: Dict with 'general' and 'feedIn' ActualInterval data (optional)

    Returns:
        (general_prices, feedin_prices) as dicts mapping PERIOD_XX_XX to price
    """
    from zoneinfo import ZoneInfo

    # IMPORTANT: Use the timezone from Amber data (auto-detected from nemTime timestamps)
    # This ensures correct "past vs future" period detection for all Australian locations
    # Falls back to Sydney timezone if detection failed
    if detected_tz:
        aus_tz = detected_tz
        _LOGGER.info("Using auto-detected timezone: %s", aus_tz)
    else:
        aus_tz = ZoneInfo("Australia/Sydney")
        _LOGGER.warning("Timezone detection failed, falling back to Australia/Sydney")

    now = datetime.now(aus_tz)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30

    # Calculate current period key for ActualInterval injection
    current_period_key = f"PERIOD_{current_hour:02d}_{current_minute:02d}"
    _LOGGER.info("Current 30-min period: %s", current_period_key)

    general_prices: dict[str, float] = {}
    feedin_prices: dict[str, float] = {}

    # Build all 48 half-hour periods in a day
    for hour in range(24):
        for minute in [0, 30]:
            period_key = f"PERIOD_{hour:02d}_{minute:02d}"

            # SPECIAL CASE: Use ActualInterval for current period if available
            # This captures short-term (5-min) price spikes that would otherwise be averaged out
            if period_key == current_period_key and current_actual_interval:
                # Use live 5-min ActualInterval price for current period
                if current_actual_interval.get("general"):
                    actual_price_cents = current_actual_interval["general"].get("perKwh", 0)
                    buy_price = _round_price(actual_price_cents / 100)
                    buy_price = max(0, buy_price)  # Tesla restriction: no negatives
                    general_prices[period_key] = buy_price
                    _LOGGER.info(
                        "%s (CURRENT): Using ActualInterval buy price: $%.4f/kWh",
                        period_key,
                        buy_price,
                    )
                else:
                    _LOGGER.warning(
                        "%s: No general ActualInterval, falling back to forecast", period_key
                    )
                    # Will fall through to normal lookup below
                    current_actual_interval = None  # Disable for this iteration

                # Use live 5-min ActualInterval sell price for current period
                if current_actual_interval and current_actual_interval.get("feedIn"):
                    actual_feedin_cents = current_actual_interval["feedIn"].get("perKwh", 0)
                    # Amber convention: feedIn is negative, Tesla convention: positive
                    sell_price = _round_price(-actual_feedin_cents / 100)
                    sell_price = max(0, sell_price)  # No negatives

                    # Tesla restriction: sell cannot exceed buy
                    if period_key in general_prices and sell_price > general_prices[period_key]:
                        _LOGGER.debug(
                            "%s: Sell price capped to buy price (%.4f -> %.4f)",
                            period_key,
                            sell_price,
                            general_prices[period_key],
                        )
                        sell_price = general_prices[period_key]

                    feedin_prices[period_key] = sell_price
                    _LOGGER.info(
                        "%s (CURRENT): Using ActualInterval sell price: $%.4f/kWh",
                        period_key,
                        sell_price,
                    )

                    # Skip normal lookup logic for this period since we've set both prices
                    continue
                else:
                    if current_actual_interval:
                        _LOGGER.warning(
                            "%s: No feedIn ActualInterval, falling back to forecast", period_key
                        )

            # NORMAL CASE: Use forecast data for all other periods
            # Determine if this period has already passed
            if (hour < current_hour) or (hour == current_hour and minute < current_minute):
                # Past period - use tomorrow's price
                date_to_use = tomorrow
            else:
                # Future period - use today's price
                date_to_use = today

            # Direct lookup - no shifting needed with START time bucketing
            # Tesla PERIOD_17_30 (17:30-18:00) directly looks up bucket (17, 30)
            date_str = date_to_use.isoformat()
            lookup_key = (date_str, hour, minute)

            # Get general price (buy price)
            # Try primary lookup key first, then try both today and tomorrow as fallbacks
            # This handles AEMO data where all prices are keyed to forecast dates
            found_in_lookup = lookup_key in general_lookup
            if not found_in_lookup:
                # Try today's date first (for AEMO forecasts that start from now)
                today_key = (today.isoformat(), hour, minute)
                if today_key in general_lookup:
                    lookup_key = today_key
                    found_in_lookup = True
                else:
                    # Try tomorrow's date (for AEMO forecasts that extend past midnight)
                    tomorrow_key = (tomorrow.isoformat(), hour, minute)
                    if tomorrow_key in general_lookup:
                        lookup_key = tomorrow_key
                        found_in_lookup = True

            if found_in_lookup:
                prices = general_lookup[lookup_key]
                buy_price = _round_price(sum(prices) / len(prices))
                # Tesla restriction: No negative prices
                general_prices[period_key] = max(0, buy_price)
            else:
                # Mark as missing - will be counted below
                general_prices[period_key] = None
                _LOGGER.warning("%s: No price data available", period_key)

            # Get feedin price (sell price)
            # Use same flexible lookup approach for AEMO compatibility
            feedin_lookup_key = lookup_key  # Start with same key as general
            found_feedin = feedin_lookup_key in feedin_lookup
            if not found_feedin:
                today_key = (today.isoformat(), hour, minute)
                if today_key in feedin_lookup:
                    feedin_lookup_key = today_key
                    found_feedin = True
                else:
                    tomorrow_key = (tomorrow.isoformat(), hour, minute)
                    if tomorrow_key in feedin_lookup:
                        feedin_lookup_key = tomorrow_key
                        found_feedin = True

            if found_feedin:
                prices = feedin_lookup[feedin_lookup_key]
                sell_price = _round_price(sum(prices) / len(prices))

                # Tesla restriction #1: No negative prices
                sell_price = max(0, sell_price)

                # Tesla restriction #2: Sell price cannot exceed buy price
                if period_key in general_prices and general_prices[period_key] is not None:
                    sell_price = min(sell_price, general_prices[period_key])

                feedin_prices[period_key] = sell_price
            else:
                # Mark as missing - will be counted below
                feedin_prices[period_key] = None
                _LOGGER.warning("%s: No sell price data available", period_key)

    # Count missing periods and abort if too many are missing
    # This prevents sending bad tariffs when API is unreachable
    missing_buy = sum(1 for v in general_prices.values() if v is None)
    missing_sell = sum(1 for v in feedin_prices.values() if v is None)
    total_missing = missing_buy + missing_sell

    if total_missing > 0:
        _LOGGER.warning(
            "Missing price data: %d buy periods, %d sell periods (total: %d/96)",
            missing_buy, missing_sell, total_missing
        )

    # If more than 10 periods are missing, abort - keep using last good tariff
    MAX_MISSING_PERIODS = 10
    if total_missing > MAX_MISSING_PERIODS:
        _LOGGER.error(
            "❌ Too many missing price periods (%d > %d) - ABORTING sync to preserve last good tariff. "
            "This usually indicates Amber API is unreachable.",
            total_missing, MAX_MISSING_PERIODS
        )
        return None, None

    # Replace any remaining None values with 0 (shouldn't happen if we abort above)
    for key in general_prices:
        if general_prices[key] is None:
            general_prices[key] = 0
    for key in feedin_prices:
        if feedin_prices[key] is None:
            feedin_prices[key] = 0

    return general_prices, feedin_prices


def _build_tariff_structure(
    general_prices: dict[str, float],
    feedin_prices: dict[str, float],
    demand_charge_rates: dict[str, float] | None = None,
    demand_charge_apply_to: str = "Buy Only",
) -> dict[str, Any]:
    """
    Build the complete Tesla tariff structure.

    Args:
        general_prices: Buy prices for all 48 periods
        feedin_prices: Sell prices for all 48 periods
        demand_charge_rates: Demand charge rates for all 48 periods (optional)
        demand_charge_apply_to: Where to apply demand charges ("Buy Only", "Sell Only", "Both")

    Returns:
        Complete Tesla tariff structure
    """
    # Build TOU periods
    tou_periods = _build_tou_periods(general_prices.keys())

    # Conditionally apply demand charges based on demand_charge_apply_to setting
    apply_to_buy = demand_charge_apply_to in ["Buy Only", "Both"]
    apply_to_sell = demand_charge_apply_to in ["Sell Only", "Both"]

    buy_demand_charges = (
        {"rates": demand_charge_rates}
        if demand_charge_rates and apply_to_buy
        else {}
    )

    sell_demand_charges = (
        {"rates": demand_charge_rates}
        if demand_charge_rates and apply_to_sell
        else {}
    )

    tariff = {
        "version": 1,
        "code": "TESLA_SYNC:AMBER:AMBER",
        "name": "Amber Electric (Tesla Sync)",
        "utility": "Amber Electric",
        "currency": "AUD",
        "daily_charges": [{"name": "Charge"}],
        "demand_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": buy_demand_charges,
            "Winter": {},
        },
        "energy_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {"rates": general_prices},
            "Winter": {},
        },
        "seasons": {
            "Summer": {
                "fromMonth": 1,
                "toMonth": 12,
                "fromDay": 1,
                "toDay": 31,
                "tou_periods": tou_periods,
            },
            "Winter": {
                "fromDay": 0,
                "toDay": 0,
                "fromMonth": 0,
                "toMonth": 0,
                "tou_periods": {},
            },
        },
        "sell_tariff": {
            "name": "Amber Electric (managed by Tesla Sync)",
            "utility": "Amber Electric",
            "daily_charges": [{"name": "Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": sell_demand_charges,
                "Winter": {},
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": feedin_prices},
                "Winter": {},
            },
            "seasons": {
                "Summer": {
                    "fromMonth": 1,
                    "toMonth": 12,
                    "fromDay": 1,
                    "toDay": 31,
                    "tou_periods": tou_periods,
                },
                "Winter": {
                    "fromDay": 0,
                    "toDay": 0,
                    "fromMonth": 0,
                    "toMonth": 0,
                    "tou_periods": {},
                },
            },
        },
    }

    return tariff


def _build_tou_periods(period_keys: Any) -> dict[str, Any]:
    """Build TOU period definitions for all time slots."""
    tou_periods: dict[str, Any] = {}

    for period_key in period_keys:
        try:
            parts = period_key.split("_")
            from_hour = int(parts[1])
            from_minute = int(parts[2])

            # Calculate end time (30 minutes later)
            to_hour = from_hour
            to_minute = from_minute + 30

            if to_minute >= 60:
                to_minute = 0
                to_hour += 1

            # Build period definition
            period_def: dict[str, int] = {"toDayOfWeek": 6}

            if from_hour > 0:
                period_def["fromHour"] = from_hour
            if from_minute > 0:
                period_def["fromMinute"] = from_minute
            if to_hour != from_hour or to_hour > 0:
                period_def["toHour"] = to_hour
            if to_minute > 0:
                period_def["toMinute"] = to_minute

            tou_periods[period_key] = {"periods": [period_def]}

        except (IndexError, ValueError) as err:
            _LOGGER.error("Error parsing period key %s: %s", period_key, err)
            continue

    return tou_periods


# Flow Power Electricity Provider Support
# Flow Power offers fixed export rates during "Happy Hour" (5:30pm-7:30pm)
# Outside Happy Hour, export rate is 0c/kWh

# Happy Hour export rates by NEM region (in $/kWh)
FLOW_POWER_EXPORT_RATES = {
    "NSW1": 0.45,   # 45c/kWh
    "QLD1": 0.45,   # 45c/kWh
    "SA1": 0.45,    # 45c/kWh
    "VIC1": 0.35,   # 35c/kWh
}

# Happy Hour periods (5:30pm to 7:30pm)
# Maps to Tesla PERIOD_XX_XX format for 30-minute intervals
FLOW_POWER_HAPPY_HOUR_PERIODS = [
    "PERIOD_17_30",  # 5:30pm - 6:00pm
    "PERIOD_18_00",  # 6:00pm - 6:30pm
    "PERIOD_18_30",  # 6:30pm - 7:00pm
    "PERIOD_19_00",  # 7:00pm - 7:30pm
]


def apply_flow_power_export(
    tariff: dict[str, Any],
    state: str
) -> dict[str, Any]:
    """
    Apply Flow Power export rates to a tariff structure.

    Flow Power pricing:
    - Happy Hour (5:30pm-7:30pm): Fixed export rate (45c NSW/QLD/SA, 35c VIC)
    - All other times: 0c export

    Args:
        tariff: Tesla tariff structure (from convert_amber_to_tesla_tariff)
        state: NEM region code (NSW1, VIC1, QLD1, SA1)

    Returns:
        Modified tariff with Flow Power export rates applied
    """
    if not tariff:
        _LOGGER.warning("No tariff provided for Flow Power export adjustment")
        return tariff

    # Get the happy hour export rate for this state
    export_rate = FLOW_POWER_EXPORT_RATES.get(state, 0.45)  # Default to 45c if unknown state

    _LOGGER.info(
        "Applying Flow Power export rates for %s: %.0fc during Happy Hour, 0c otherwise",
        state,
        export_rate * 100,
    )

    # Apply to both Summer and Winter seasons in sell_tariff
    for season in ["Summer", "Winter"]:
        if season not in tariff.get("sell_tariff", {}).get("energy_charges", {}):
            continue

        rates = tariff["sell_tariff"]["energy_charges"][season].get("rates", {})

        # Set ALL periods to 0c first
        for period in list(rates.keys()):
            rates[period] = 0.0

        # Then set Happy Hour periods to the fixed rate
        for period in FLOW_POWER_HAPPY_HOUR_PERIODS:
            if period in rates or len(rates) > 0:
                rates[period] = export_rate

    # Log summary of changes
    happy_hour_periods_count = len(FLOW_POWER_HAPPY_HOUR_PERIODS)
    _LOGGER.info(
        "Flow Power export applied: %d periods at $%.2f/kWh, remaining periods at $0.00/kWh",
        happy_hour_periods_count,
        export_rate,
    )

    return tariff
