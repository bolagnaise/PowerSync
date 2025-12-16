"""Convert Amber Electric pricing to Tesla tariff format."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

# Import aemo_to_tariff library for automatic network tariff calculation
try:
    from aemo_to_tariff import spot_to_tariff, get_daily_fee
    AEMO_TARIFF_AVAILABLE = True
except ImportError:
    AEMO_TARIFF_AVAILABLE = False

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
    electricity_provider: str = "amber",
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
        demand_charge_apply_to,
        electricity_provider,
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

    # Track last valid prices for fallback when AEMO forecast doesn't extend far enough
    # AEMO pre-dispatch typically provides only ~20 hours of forecast (39-40 periods)
    # Early morning tomorrow (04:00-08:00) often won't have data
    # Using the last known price is better than failing the sync
    last_valid_buy_price: float | None = None
    last_valid_sell_price: float | None = None

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
                buy_price = max(0, buy_price)
                general_prices[period_key] = buy_price
                last_valid_buy_price = buy_price  # Track for fallback
            else:
                # No data found - use fallback price if available
                # This commonly happens with AEMO forecast which only provides ~20 hours ahead
                # Early morning tomorrow (04:00-08:00) typically won't have forecast data
                if last_valid_buy_price is not None:
                    general_prices[period_key] = last_valid_buy_price
                    _LOGGER.info(
                        "%s: Using fallback buy price $%.4f (AEMO forecast gap)",
                        period_key, last_valid_buy_price
                    )
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
                last_valid_sell_price = sell_price  # Track for fallback
            else:
                # No data found - use fallback price if available
                # This commonly happens with AEMO forecast which only provides ~20 hours ahead
                if last_valid_sell_price is not None:
                    # Ensure fallback sell price doesn't exceed current buy price
                    fallback_sell = last_valid_sell_price
                    if period_key in general_prices and general_prices[period_key] is not None:
                        if fallback_sell > general_prices[period_key]:
                            fallback_sell = general_prices[period_key]
                    feedin_prices[period_key] = fallback_sell
                    _LOGGER.info(
                        "%s: Using fallback sell price $%.4f (AEMO forecast gap)",
                        period_key, fallback_sell
                    )
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
    electricity_provider: str = "amber",
) -> dict[str, Any]:
    """
    Build the complete Tesla tariff structure.

    Args:
        general_prices: Buy prices for all 48 periods
        feedin_prices: Sell prices for all 48 periods
        demand_charge_rates: Demand charge rates for all 48 periods (optional)
        demand_charge_apply_to: Where to apply demand charges ("Buy Only", "Sell Only", "Both")
        electricity_provider: Provider code ("amber", "flow_power", "globird")

    Returns:
        Complete Tesla tariff structure
    """
    # Map provider codes to display names
    # Note: Globird not included - users set tariff manually, only AEMO spike feature used
    provider_names = {
        "amber": "Amber Electric",
        "flow_power": "Flow Power",
    }
    provider_name = provider_names.get(electricity_provider, "Amber Electric")
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
        "code": f"TESLA_SYNC:{electricity_provider.upper()}",
        "name": f"{provider_name} (Tesla Sync)",
        "utility": provider_name,
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
            "name": f"{provider_name} (managed by Tesla Sync)",
            "utility": provider_name,
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


def apply_network_tariff(
    tariff: dict[str, Any],
    distributor: str | None = None,
    tariff_code: str | None = None,
    use_manual_rates: bool = False,
    tariff_type: str = "flat",
    flat_rate: float = 8.0,
    peak_rate: float = 15.0,
    shoulder_rate: float = 5.0,
    offpeak_rate: float = 2.0,
    peak_start: str = "16:00",
    peak_end: str = "21:00",
    offpeak_start: str = "10:00",
    offpeak_end: str = "15:00",
    other_fees: float = 1.5,
    include_gst: bool = True,
) -> dict[str, Any]:
    """
    Apply network tariff (DNSP) charges to wholesale prices.

    AEMO wholesale prices only include energy costs. This function adds:
    - Network (DNSP) charges (via aemo_to_tariff library or manual rates)
    - Other fees (environmental, market fees) - only for manual rates
    - GST (optional) - only for manual rates

    Primary: Uses aemo_to_tariff library with distributor + tariff code
    Fallback: Manual rate entry when library unavailable or use_manual_rates=True

    Args:
        tariff: Tesla tariff structure with wholesale prices
        distributor: Network distributor code (e.g., "energex", "ausgrid")
        tariff_code: Tariff code from electricity bill (e.g., "NTC6900")
        use_manual_rates: Force use of manual rates instead of library
        tariff_type: "flat" or "tou" (for manual rates only)
        flat_rate: Flat network rate in c/kWh (used when tariff_type="flat")
        peak_rate: Peak network rate in c/kWh
        shoulder_rate: Shoulder network rate in c/kWh
        offpeak_rate: Off-peak network rate in c/kWh
        peak_start: Peak period start time (HH:MM)
        peak_end: Peak period end time (HH:MM)
        offpeak_start: Off-peak period start time (HH:MM)
        offpeak_end: Off-peak period end time (HH:MM)
        other_fees: Other fees in c/kWh (environmental, market)
        include_gst: Whether to add 10% GST

    Returns:
        Modified tariff with network charges applied to buy prices
    """
    if not tariff:
        _LOGGER.warning("No tariff provided for network tariff adjustment")
        return tariff

    # Determine whether to use library or manual rates
    if AEMO_TARIFF_AVAILABLE and not use_manual_rates and distributor and tariff_code:
        _LOGGER.info(
            "Using aemo_to_tariff library: distributor=%s, tariff=%s",
            distributor, tariff_code
        )
        return _apply_network_tariff_library(tariff, distributor, tariff_code)
    else:
        if use_manual_rates:
            _LOGGER.info("Using manual network rates (user preference)")
        elif not AEMO_TARIFF_AVAILABLE:
            _LOGGER.warning("aemo_to_tariff library not available, using manual rates")
        else:
            _LOGGER.info("Using manual network rates (no distributor/tariff configured)")
        return _apply_network_tariff_manual(
            tariff, tariff_type, flat_rate, peak_rate, shoulder_rate, offpeak_rate,
            peak_start, peak_end, offpeak_start, offpeak_end, other_fees, include_gst
        )


def _apply_network_tariff_library(
    tariff: dict[str, Any],
    distributor: str,
    tariff_code: str,
) -> dict[str, Any]:
    """
    Apply network tariff using the aemo_to_tariff library.

    The library calculates complete retail prices from AEMO wholesale:
    - Network (DNSP) charges based on distributor and tariff code
    - Market fees, environmental certificates, etc.
    - GST included

    Args:
        tariff: Tesla tariff structure with wholesale prices (in $/kWh)
        distributor: Network distributor code (e.g., "energex", "ausgrid")
        tariff_code: Tariff code from electricity bill (e.g., "NTC6900")

    Returns:
        Modified tariff with retail prices from library
    """
    from datetime import datetime, timezone, timedelta

    # Map distributors to library module names
    # CitiPower and United Energy use the generic Victoria module
    library_distributor_map = {
        "citipower": "victoria",
        "united": "victoria",
    }
    library_distributor = library_distributor_map.get(distributor, distributor)

    # Apply to Summer season buy rates (energy_charges)
    for season in ["Summer"]:
        if season not in tariff.get("energy_charges", {}):
            continue

        rates = tariff["energy_charges"][season].get("rates", {})
        modified_count = 0

        for period, price in list(rates.items()):
            # Extract hour and minute from PERIOD_HH_MM
            try:
                parts = period.split("_")
                hour = int(parts[1])
                minute = int(parts[2])
            except (IndexError, ValueError):
                continue

            # Build a datetime for this period (use today's date)
            # The library uses interval_time for TOU period detection
            now = datetime.now()
            interval_time = datetime(
                now.year, now.month, now.day, hour, minute,
                tzinfo=timezone(timedelta(hours=10))  # AEST
            )

            # Convert wholesale $/kWh to $/MWh for the library
            # Our tariff stores prices in $/kWh, library expects $/MWh
            wholesale_mwh = price * 1000

            try:
                # spot_to_tariff returns price in c/kWh including all fees + GST
                retail_cents = spot_to_tariff(
                    interval_time=interval_time,
                    network=library_distributor,
                    tariff=tariff_code,
                    rrp=wholesale_mwh  # RRP in $/MWh
                )

                # Convert c/kWh back to $/kWh
                new_price = round(retail_cents / 100, 4)

                # Tesla restriction: no negative prices
                new_price = max(0, new_price)

                if rates[period] != new_price:
                    modified_count += 1
                    _LOGGER.debug(
                        "%s: wholesale $%.4f -> retail $%.4f (%.2fc/kWh)",
                        period, price, new_price, retail_cents
                    )
                    rates[period] = new_price

            except Exception as err:
                _LOGGER.warning(
                    "%s: Library calculation failed, keeping wholesale: %s",
                    period, err
                )

        _LOGGER.info(
            "Network tariff (library) applied to %d periods in %s",
            modified_count, season
        )

    return tariff


def _apply_network_tariff_manual(
    tariff: dict[str, Any],
    tariff_type: str = "flat",
    flat_rate: float = 8.0,
    peak_rate: float = 15.0,
    shoulder_rate: float = 5.0,
    offpeak_rate: float = 2.0,
    peak_start: str = "16:00",
    peak_end: str = "21:00",
    offpeak_start: str = "10:00",
    offpeak_end: str = "15:00",
    other_fees: float = 1.5,
    include_gst: bool = True,
) -> dict[str, Any]:
    """
    Apply network tariff using manual rate entry.

    This is the fallback when aemo_to_tariff library is not available
    or when the user prefers manual rate entry.

    Args:
        tariff: Tesla tariff structure with wholesale prices
        tariff_type: "flat" or "tou"
        flat_rate: Flat network rate in c/kWh (used when tariff_type="flat")
        peak_rate: Peak network rate in c/kWh
        shoulder_rate: Shoulder network rate in c/kWh
        offpeak_rate: Off-peak network rate in c/kWh
        peak_start: Peak period start time (HH:MM)
        peak_end: Peak period end time (HH:MM)
        offpeak_start: Off-peak period start time (HH:MM)
        offpeak_end: Off-peak period end time (HH:MM)
        other_fees: Other fees in c/kWh (environmental, market)
        include_gst: Whether to add 10% GST

    Returns:
        Modified tariff with network charges applied to buy prices
    """
    _LOGGER.info(
        "Applying manual network tariff: type=%s, other_fees=%.1fc/kWh, gst=%s",
        tariff_type, other_fees, include_gst
    )

    # Parse TOU time periods
    if tariff_type == "tou":
        try:
            peak_start_hour, peak_start_min = map(int, peak_start.split(":"))
            peak_end_hour, peak_end_min = map(int, peak_end.split(":"))
            offpeak_start_hour, offpeak_start_min = map(int, offpeak_start.split(":"))
            offpeak_end_hour, offpeak_end_min = map(int, offpeak_end.split(":"))

            _LOGGER.info(
                "Network TOU: peak=%.1fc (%s-%s), shoulder=%.1fc, offpeak=%.1fc (%s-%s)",
                peak_rate, peak_start, peak_end,
                shoulder_rate,
                offpeak_rate, offpeak_start, offpeak_end
            )
        except (ValueError, AttributeError) as err:
            _LOGGER.error("Invalid time format for network tariff: %s", err)
            return tariff
    else:
        _LOGGER.info("Network flat rate: %.1fc/kWh", flat_rate)

    # Apply to Summer season buy rates (energy_charges)
    for season in ["Summer"]:
        if season not in tariff.get("energy_charges", {}):
            continue

        rates = tariff["energy_charges"][season].get("rates", {})
        modified_count = 0

        for period, price in list(rates.items()):
            # Extract hour and minute from PERIOD_HH_MM
            try:
                parts = period.split("_")
                hour = int(parts[1])
                minute = int(parts[2])
            except (IndexError, ValueError):
                continue

            # Calculate network charge based on tariff type
            if tariff_type == "flat":
                network_charge_cents = flat_rate
            else:
                # TOU - determine which rate applies
                time_minutes = hour * 60 + minute

                # Check peak period
                peak_start_mins = peak_start_hour * 60 + peak_start_min
                peak_end_mins = peak_end_hour * 60 + peak_end_min

                # Check off-peak period
                offpeak_start_mins = offpeak_start_hour * 60 + offpeak_start_min
                offpeak_end_mins = offpeak_end_hour * 60 + offpeak_end_min

                if peak_start_mins <= time_minutes < peak_end_mins:
                    network_charge_cents = peak_rate
                elif offpeak_start_mins <= time_minutes < offpeak_end_mins:
                    network_charge_cents = offpeak_rate
                else:
                    network_charge_cents = shoulder_rate

            # Add other fees
            total_charge_cents = network_charge_cents + other_fees

            # Apply GST (10%)
            if include_gst:
                total_charge_cents = total_charge_cents * 1.10

            # Convert wholesale price ($/kWh) to cents, add network charges, convert back
            wholesale_cents = price * 100
            total_cents = wholesale_cents + total_charge_cents
            new_price = round(total_cents / 100, 4)

            # Tesla restriction: no negative prices
            new_price = max(0, new_price)

            if rates[period] != new_price:
                modified_count += 1
                _LOGGER.debug(
                    "%s: $%.4f + %.2fc network = $%.4f",
                    period, price, total_charge_cents, new_price
                )
                rates[period] = new_price

        _LOGGER.info("Manual network tariff applied to %d periods in %s", modified_count, season)

    return tariff


# Flow Power PEA (Price Efficiency Adjustment) Constants
# These are also defined in const.py but duplicated here for tariff_converter independence
FLOW_POWER_PEA_OFFSET = 9.7      # Combined: MARKET_AVG + BENCHMARK (c/kWh)
FLOW_POWER_DEFAULT_BASE_RATE = 34.0  # Default Flow Power base rate (c/kWh)


def apply_flow_power_pea(
    tariff: dict[str, Any],
    wholesale_prices: dict[str, float],
    base_rate: float = FLOW_POWER_DEFAULT_BASE_RATE,
    custom_pea: float | None = None
) -> dict[str, Any]:
    """
    Apply Flow Power base rate + PEA (Price Efficiency Adjustment) pricing model.

    REPLACES network tariff calculation with Flow Power's actual billing model:
    Final Rate = Base Rate + PEA
               = Base Rate + (wholesale - 9.7)

    PEA adjusts your rate based on wholesale prices when you consume:
    - Cheap/negative wholesale → negative PEA → pay less than base rate
    - Average wholesale (8c) → PEA ≈ -1.7c → pay slightly less than base rate
    - Expensive wholesale → positive PEA → pay more than base rate

    Examples:
    - Wholesale -0.5c: Final = 34 + (-0.5 - 9.7) = 34 - 10.2 = 23.8c/kWh
    - Wholesale 8c:    Final = 34 + (8 - 9.7) = 34 - 1.7 = 32.3c/kWh
    - Wholesale 15c:   Final = 34 + (15 - 9.7) = 34 + 5.3 = 39.3c/kWh

    Args:
        tariff: Tesla tariff structure with wholesale prices in energy_charges
        wholesale_prices: Dict mapping PERIOD_HH_MM to wholesale price in $/kWh
        base_rate: Flow Power base rate in c/kWh (from your plan, default 34c)
        custom_pea: Optional fixed PEA override in c/kWh (from bills)

    Returns:
        Modified tariff with Flow Power pricing applied to buy prices
    """
    if not tariff:
        _LOGGER.warning("No tariff provided for Flow Power PEA adjustment")
        return tariff

    _LOGGER.info(
        "Applying Flow Power PEA: base_rate=%.1fc/kWh, custom_pea=%s",
        base_rate,
        f"{custom_pea:.1f}c" if custom_pea is not None else "auto"
    )

    # Track statistics for logging
    pea_values = []
    final_prices = []

    # Apply to Summer season buy rates (energy_charges)
    for season in ["Summer"]:
        if season not in tariff.get("energy_charges", {}):
            continue

        rates = tariff["energy_charges"][season].get("rates", {})
        modified_count = 0

        for period in list(rates.keys()):
            if custom_pea is not None:
                # User override - apply fixed PEA from bills
                pea = custom_pea
            else:
                # Calculate PEA from wholesale price
                # Get wholesale price for this period ($/kWh -> c/kWh)
                wholesale_dollars = wholesale_prices.get(period, 0.08)  # Default 8c if missing
                wholesale_cents = wholesale_dollars * 100
                pea = wholesale_cents - FLOW_POWER_PEA_OFFSET  # wholesale - 9.7

            pea_values.append(pea)

            # Final rate = base_rate + PEA (in c/kWh)
            final_cents = base_rate + pea

            # Convert to $/kWh and clamp to 0 (Tesla restriction: no negatives)
            final_dollars = max(0, final_cents / 100)
            final_dollars = round(final_dollars, 4)

            final_prices.append(final_cents)

            if rates[period] != final_dollars:
                modified_count += 1
                _LOGGER.debug(
                    "%s: base=%.1fc + PEA=%.1fc = %.1fc ($%.4f/kWh)",
                    period, base_rate, pea, final_cents, final_dollars
                )

            rates[period] = final_dollars

        _LOGGER.info("Flow Power PEA applied to %d periods in %s", modified_count, season)

    # Log summary statistics
    if pea_values:
        avg_pea = sum(pea_values) / len(pea_values)
        min_pea = min(pea_values)
        max_pea = max(pea_values)
        avg_final = sum(final_prices) / len(final_prices)

        _LOGGER.info(
            "Flow Power PEA summary: avg_pea=%.1fc, range=[%.1fc to %.1fc], avg_final=%.1fc/kWh",
            avg_pea, min_pea, max_pea, avg_final
        )

    return tariff


def get_wholesale_lookup(forecast_data: list[dict[str, Any]]) -> dict[str, float]:
    """
    Extract wholesale prices from forecast data into a period lookup.

    Used by apply_flow_power_pea() to calculate PEA from wholesale prices.

    Args:
        forecast_data: List of price forecast points (5-min or 30-min resolution)

    Returns:
        Dict mapping PERIOD_HH_MM to wholesale price in $/kWh
    """
    wholesale_lookup: dict[str, list[float]] = {}

    for point in forecast_data:
        try:
            nem_time = point.get("nemTime", "")
            timestamp = datetime.fromisoformat(nem_time.replace("Z", "+00:00"))
            channel_type = point.get("channelType", "")
            duration = point.get("duration", 30)

            # Only use general (import) prices
            if channel_type != "general":
                continue

            # Get wholesale component - prefer wholesaleKWHPrice for raw wholesale
            # Fall back to perKwh for AEMO data (which is already wholesale)
            wholesale_cents = point.get("wholesaleKWHPrice")
            if wholesale_cents is None:
                # AEMO data - perKwh is the wholesale price
                wholesale_cents = point.get("perKwh", 0)

            wholesale_dollars = wholesale_cents / 100

            # Use interval START time for bucketing (same as tariff converter)
            interval_start = timestamp - timedelta(minutes=duration)

            # Round to nearest 30-minute interval
            start_minute_bucket = 0 if interval_start.minute < 30 else 30
            period_key = f"PERIOD_{interval_start.hour:02d}_{start_minute_bucket:02d}"

            # Store in lookup (average if multiple intervals in same 30-min period)
            if period_key not in wholesale_lookup:
                wholesale_lookup[period_key] = []
            wholesale_lookup[period_key].append(wholesale_dollars)

        except Exception as err:
            _LOGGER.debug("Error extracting wholesale price: %s", err)
            continue

    # Average multiple values per period
    result: dict[str, float] = {}
    for period, prices in wholesale_lookup.items():
        result[period] = sum(prices) / len(prices)

    _LOGGER.debug("Built wholesale lookup with %d periods", len(result))
    return result


def apply_export_boost(
    tariff: dict[str, Any],
    offset_cents: float = 0.0,
    min_price_cents: float = 0.0,
    boost_start: str = "17:00",
    boost_end: str = "21:00",
) -> dict[str, Any]:
    """
    Apply export price boost to trigger Powerwall exports at lower price points.

    This artificially increases sell prices so the Powerwall sees higher export
    value and is more willing to discharge. Useful when actual export prices are
    in the 20-25c range where Tesla's algorithm may not trigger exports.

    The boost is only applied during the configured time window.

    Args:
        tariff: Tesla tariff structure with energy_charges containing sell_prices
        offset_cents: Fixed offset to add to all export prices (c/kWh)
        min_price_cents: Minimum export price floor (c/kWh)
        boost_start: Time to start applying boost (HH:MM format)
        boost_end: Time to stop applying boost (HH:MM format)

    Returns:
        Modified tariff with boosted export prices

    Example:
        Amber says export = 18c
        With offset_cents=5, min_price_cents=20:
        → Tesla sees 23c (18 + 5 = 23, above min)

        Amber says export = 12c
        With offset_cents=5, min_price_cents=20:
        → Tesla sees 20c (12 + 5 = 17, below min, so use min)
    """
    if offset_cents == 0 and min_price_cents == 0:
        _LOGGER.debug("Export boost disabled (offset=0, min=0)")
        return tariff

    # Parse time window
    try:
        start_parts = boost_start.split(":")
        start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
        end_parts = boost_end.split(":")
        end_hour, end_minute = int(end_parts[0]), int(end_parts[1])
    except (ValueError, IndexError) as err:
        _LOGGER.error("Invalid export boost time format: %s", err)
        return tariff

    # Build list of periods within the time window
    boost_periods: set[str] = set()
    for hour in range(24):
        for minute in [0, 30]:
            period_minutes = hour * 60 + minute
            start_minutes = start_hour * 60 + start_minute
            end_minutes = end_hour * 60 + end_minute

            # Handle overnight windows (e.g., 22:00 to 06:00)
            if end_minutes <= start_minutes:
                in_window = period_minutes >= start_minutes or period_minutes < end_minutes
            else:
                in_window = start_minutes <= period_minutes < end_minutes

            if in_window:
                boost_periods.add(f"PERIOD_{hour:02d}_{minute:02d}")

    _LOGGER.debug(
        "Export boost active for %d periods (%s to %s): offset=%.1fc, min=%.1fc",
        len(boost_periods), boost_start, boost_end, offset_cents, min_price_cents
    )

    modified_count = 0
    boosted_prices: list[float] = []

    # Process each season in the tariff
    for season, season_data in tariff.get("energy_charges", {}).items():
        sell_prices = season_data.get("sell_prices", {})
        buy_prices = season_data.get("rates", {})

        for period in boost_periods:
            if period not in sell_prices:
                continue

            original_dollars = sell_prices[period]
            original_cents = original_dollars * 100

            # Apply offset
            boosted_cents = original_cents + offset_cents

            # Apply minimum floor
            boosted_cents = max(boosted_cents, min_price_cents)

            # Convert back to dollars
            boosted_dollars = round(boosted_cents / 100, 4)

            # Tesla restriction: sell cannot exceed buy
            if period in buy_prices and boosted_dollars > buy_prices[period]:
                _LOGGER.debug(
                    "%s: Boosted sell price capped to buy price (%.4f -> %.4f)",
                    period, boosted_dollars, buy_prices[period]
                )
                boosted_dollars = buy_prices[period]

            if boosted_dollars != original_dollars:
                modified_count += 1
                boosted_prices.append(boosted_cents)
                _LOGGER.debug(
                    "%s: Export boost %.2fc → %.2fc (offset=%.1fc, min=%.1fc)",
                    period, original_cents, boosted_dollars * 100, offset_cents, min_price_cents
                )

            sell_prices[period] = boosted_dollars

    # Log summary
    if boosted_prices:
        avg_boost = sum(boosted_prices) / len(boosted_prices)
        _LOGGER.info(
            "Export boost applied to %d periods: avg=%.1fc/kWh, range=[%.1fc to %.1fc]",
            modified_count, avg_boost, min(boosted_prices), max(boosted_prices)
        )
    else:
        _LOGGER.info("Export boost: no periods modified (prices already meet criteria)")

    return tariff
