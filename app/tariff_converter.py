# app/tariff_converter.py
"""Convert Amber Electric pricing to Tesla tariff format"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict

# Import aemo_to_tariff library for automatic network tariff calculation
try:
    from aemo_to_tariff import spot_to_tariff, get_daily_fee
    AEMO_TARIFF_AVAILABLE = True
except ImportError:
    AEMO_TARIFF_AVAILABLE = False

logger = logging.getLogger(__name__)


class AmberTariffConverter:
    """Converts Amber Electric price forecasts to Tesla-compatible tariff structure"""

    def __init__(self):
        logger.info("AmberTariffConverter initialized")

    @staticmethod
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

    def convert_amber_to_tesla_tariff(self, forecast_data: List[Dict], user=None, powerwall_timezone=None, current_actual_interval: Dict = None) -> Dict:
        """
        Convert Amber price forecast to Tesla tariff format

        Implements rolling 24-hour window: periods that have passed today get tomorrow's prices,
        future periods get today's prices. This gives Tesla a full 24-hour lookahead.

        NEW: Optionally uses ActualInterval (5-min actual price) for the current 30-min period
        to capture short-term price spikes that would otherwise be averaged out.

        Args:
            forecast_data: List of price forecast points from Amber API (5-min or 30-min resolution)
            user: User object for demand charge settings (optional)
            powerwall_timezone: Powerwall timezone from site_info (optional)
                               If provided, uses this instead of auto-detecting from Amber data
            current_actual_interval: Dict with 'general' and 'feedIn' ActualInterval data (optional)
                                    If provided, uses this for the current 30-min period instead of averaging

        Returns:
            Tesla-compatible tariff structure
        """
        if not forecast_data:
            logger.warning("No forecast data provided")
            return None

        logger.info(f"Converting {len(forecast_data)} Amber forecast points to Tesla tariff")

        # Timezone handling:
        # 1. Prefer Powerwall timezone from site_info (most accurate)
        # 2. Fall back to auto-detection from Amber data
        detected_tz = None
        if powerwall_timezone:
            from zoneinfo import ZoneInfo
            try:
                detected_tz = ZoneInfo(powerwall_timezone)
                logger.info(f"Using Powerwall timezone from site_info: {powerwall_timezone}")
            except Exception as e:
                logger.warning(f"Invalid Powerwall timezone '{powerwall_timezone}': {e}, falling back to auto-detection")

        if not detected_tz:
            # Auto-detect timezone from first Amber timestamp
            # Amber timestamps include timezone info: "2025-11-11T16:05:00+10:00"
            for point in forecast_data:
                nem_time = point.get('nemTime', '')
                if nem_time:
                    try:
                        timestamp = datetime.fromisoformat(nem_time.replace('Z', '+00:00'))
                        detected_tz = timestamp.tzinfo
                        logger.info(f"Auto-detected timezone from Amber data: {detected_tz}")
                        break
                    except Exception:
                        continue

        # Build timestamp-indexed price lookup: (date, hour, minute) -> price
        general_lookup = {}  # (date_str, hour, minute) -> [prices]
        feedin_lookup = {}

        # Count interval types for logging
        interval_types = {}
        for point in forecast_data:
            interval_type = point.get('type', 'unknown')
            interval_types[interval_type] = interval_types.get(interval_type, 0) + 1
        logger.info(f"Forecast data contains: {interval_types}")

        # Debug: Log sample of forecast data to understand date keys
        if forecast_data:
            sample = forecast_data[:4] if len(forecast_data) >= 4 else forecast_data
            logger.debug(f"AEMO/Amber forecast sample: {[p.get('nemTime') for p in sample]}")

        for point in forecast_data:
            try:
                nem_time = point.get('nemTime', '')
                timestamp = datetime.fromisoformat(nem_time.replace('Z', '+00:00'))
                channel_type = point.get('channelType', '')
                interval_type = point.get('type', 'unknown')
                duration = point.get('duration', 30)  # Get actual interval duration (usually 5 or 30 minutes)

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
                # User can select: 'predicted' (default), 'low' (conservative), 'high' (optimistic)
                forecast_type = 'predicted'
                if user and hasattr(user, 'amber_forecast_type') and user.amber_forecast_type:
                    forecast_type = user.amber_forecast_type

                advanced_price = point.get('advancedPrice')

                # For ForecastInterval: Prefer advancedPrice, fall back to perKwh (for AEMO data)
                if interval_type == 'ForecastInterval':
                    if advanced_price:
                        # Handle dict format (standard: {predicted, low, high})
                        if isinstance(advanced_price, dict):
                            if forecast_type not in advanced_price:
                                available = list(advanced_price.keys())
                                error_msg = f"Forecast type '{forecast_type}' not found in advancedPrice. Available: {available}"
                                logger.error(f"{nem_time}: {error_msg}")
                                raise ValueError(error_msg)

                            per_kwh_cents = advanced_price[forecast_type]
                            logger.debug(f"{nem_time} [ForecastInterval]: advancedPrice.{forecast_type}={per_kwh_cents:.2f}c/kWh")

                        # Handle simple number format (legacy)
                        elif isinstance(advanced_price, (int, float)):
                            per_kwh_cents = advanced_price
                            logger.debug(f"{nem_time} [ForecastInterval]: advancedPrice={per_kwh_cents:.2f}c/kWh (numeric)")

                        else:
                            error_msg = f"Invalid advancedPrice format at {nem_time}: {type(advanced_price).__name__}"
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                    else:
                        # No advancedPrice - use perKwh directly (AEMO data or far-future Amber forecasts)
                        per_kwh_cents = point.get('perKwh', 0)
                        logger.debug(f"{nem_time} [ForecastInterval]: perKwh={per_kwh_cents:.2f}c/kWh (AEMO/wholesale)")

                # For CurrentInterval: Prefer advancedPrice (Amber retail forecast) over perKwh (AEMO wholesale)
                # For ActualInterval: Use perKwh (actual settled retail price)
                else:
                    if interval_type == 'CurrentInterval' and advanced_price:
                        # CurrentInterval has advancedPrice during first 25 mins - use it for Amber retail forecast
                        if isinstance(advanced_price, dict):
                            per_kwh_cents = advanced_price.get(forecast_type, advanced_price.get('predicted', 0))
                            logger.debug(f"{nem_time} [CurrentInterval]: advancedPrice.{forecast_type}={per_kwh_cents:.2f}c/kWh (Amber retail forecast)")
                        else:
                            per_kwh_cents = advanced_price
                            logger.debug(f"{nem_time} [CurrentInterval]: advancedPrice={per_kwh_cents:.2f}c/kWh (Amber retail forecast)")
                    else:
                        # ActualInterval or CurrentInterval without advancedPrice (last 5 mins of 30-min period)
                        per_kwh_cents = point.get('perKwh', 0)
                        if interval_type == 'ActualInterval':
                            logger.debug(f"{nem_time} [ActualInterval]: perKwh={per_kwh_cents:.2f}c/kWh (actual settled retail)")
                        else:
                            logger.debug(f"{nem_time} [CurrentInterval]: perKwh={per_kwh_cents:.2f}c/kWh (fallback - AEMO wholesale)")

                # Amber API convention: feedIn (sell) prices are negative when you get paid
                # Tesla convention: sell prices are positive when you get paid
                # So we need to NEGATE feedIn prices to convert to Tesla's convention
                if channel_type == 'feedIn':
                    per_kwh_cents = -per_kwh_cents

                per_kwh_dollars = self._round_price(per_kwh_cents / 100)

                # Use interval START time for bucketing
                # Amber's nemTime is the END of the interval, duration tells us the length
                # Calculate startTime = nemTime - duration
                # This gives us direct alignment with Tesla's PERIOD_XX_XX naming
                #
                # Example:
                #   nemTime=18:00, duration=30
                #   startTime=17:30
                #   Tesla PERIOD_17_30 (17:30-18:00) → looks up key (17, 30)
                #   Result: Direct match, no shifting needed!
                interval_start = timestamp - timedelta(minutes=duration)

                # CRITICAL: Convert to local Powerwall timezone to handle DST correctly
                # Amber may provide timestamps with fixed offsets (e.g., +10:00 during AEDT when it should be +11:00)
                # Converting to the Powerwall's timezone ensures we get the correct local time
                if detected_tz:
                    interval_start_local = interval_start.astimezone(detected_tz)
                else:
                    interval_start_local = interval_start

                # Round to nearest 30-minute interval using START time
                start_minute_bucket = 0 if interval_start_local.minute < 30 else 30

                # Key by date, hour, minute for lookup (using START time)
                date_str = interval_start_local.date().isoformat()
                lookup_key = (date_str, interval_start_local.hour, start_minute_bucket)

                if channel_type == 'general':
                    if lookup_key not in general_lookup:
                        general_lookup[lookup_key] = []
                    general_lookup[lookup_key].append(per_kwh_dollars)
                elif channel_type == 'feedIn':
                    if lookup_key not in feedin_lookup:
                        feedin_lookup[lookup_key] = []
                    # Keep the actual value - will handle Tesla restrictions in _build_rolling_24h_tariff
                    feedin_lookup[lookup_key].append(per_kwh_dollars)

            except Exception as e:
                logger.error(f"Error processing price point: {e}")
                continue

        # Debug: Log what dates are in the lookup tables
        if general_lookup:
            dates_in_lookup = sorted(set(k[0] for k in general_lookup.keys()))
            times_sample = sorted(list(general_lookup.keys())[:5])
            logger.debug(f"Lookup dates available: {dates_in_lookup}")
            logger.debug(f"Lookup keys sample: {times_sample}")

        # Now build the rolling 24-hour tariff
        general_prices, feedin_prices = self._build_rolling_24h_tariff(
            general_lookup, feedin_lookup, user, detected_tz, current_actual_interval
        )

        # If too many periods are missing, abort sync to preserve last good tariff
        if general_prices is None or feedin_prices is None:
            logger.error("Aborting tariff conversion - too many missing price periods")
            return None

        logger.info(f"Built rolling 24h tariff with {len(general_prices)} general and {len(feedin_prices)} feed-in periods")

        # Create the Tesla tariff structure
        tariff = self._build_tariff_structure(general_prices, feedin_prices, user)

        return tariff

    def _build_rolling_24h_tariff(self, general_lookup: Dict, feedin_lookup: Dict, user=None, detected_tz=None, current_actual_interval: Dict = None) -> tuple:
        """
        Build a rolling 24-hour tariff where past periods use tomorrow's prices

        NEW: Optionally injects ActualInterval (5-min actual price) for the current 30-min period
        to capture short-term price spikes.

        Example (current time is 4:37 PM, ActualInterval shows $14 spike at 4:30-4:35):
        - PERIOD_04_30 (4:30-5:00) → uses $14 ActualInterval (instead of averaging 4:30-5:00 forecast)
        - All other periods → use normal 30-min averaged forecast

        Args:
            general_lookup: Dict of (date, hour, minute) -> [prices] for buy prices
            feedin_lookup: Dict of (date, hour, minute) -> [prices] for sell prices
            user: User object with demand charge settings
            detected_tz: Timezone detected from Amber data timestamps
            current_actual_interval: Dict with 'general' and 'feedIn' ActualInterval data (optional)
                                    If provided, uses this for the current 30-min period

        Returns:
            (general_prices, feedin_prices) as dicts mapping PERIOD_XX_XX to price
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        # Use Powerwall timezone from site_info (if provided)
        # Otherwise fall back to auto-detection from Amber data
        # This ensures correct "past vs future" period detection aligned with Powerwall's location
        if detected_tz:
            amber_tz = detected_tz
            logger.info(f"Using timezone: {amber_tz}")
        else:
            amber_tz = ZoneInfo('Australia/Sydney')
            logger.warning("Timezone detection failed, falling back to Australia/Sydney")

        now = datetime.now(amber_tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        current_hour = now.hour
        current_minute = 0 if now.minute < 30 else 30

        # Calculate current period key for ActualInterval injection
        current_period_key = f"PERIOD_{current_hour:02d}_{current_minute:02d}"
        logger.info(f"Current 30-min period: {current_period_key}")

        general_prices = {}
        feedin_prices = {}

        # Track last valid prices for fallback when AEMO forecast doesn't extend far enough
        # AEMO pre-dispatch typically provides only ~20 hours of forecast (39-40 periods)
        # Early morning tomorrow (04:00-08:00) often won't have data
        # Using the last known price is better than failing the sync
        last_valid_buy_price = None
        last_valid_sell_price = None

        # Build all 48 half-hour periods in a day
        for hour in range(24):
            for minute in [0, 30]:
                period_key = f"PERIOD_{hour:02d}_{minute:02d}"

                # SPECIAL CASE: Use ActualInterval for current period if available
                # This captures short-term (5-min) price spikes that would otherwise be averaged out
                if period_key == current_period_key and current_actual_interval:
                    # Use live 5-min ActualInterval price for current period
                    if current_actual_interval.get('general'):
                        actual_price_cents = current_actual_interval['general'].get('perKwh', 0)
                        buy_price = self._round_price(actual_price_cents / 100)
                        buy_price = max(0, buy_price)  # Tesla restriction: no negatives
                        general_prices[period_key] = buy_price
                        logger.info(f"{period_key} (CURRENT): Using ActualInterval buy price: ${buy_price:.4f}/kWh")
                    else:
                        logger.warning(f"{period_key}: No general ActualInterval, falling back to forecast")
                        # Will fall through to normal lookup below
                        current_actual_interval = None  # Disable for this iteration to fall through

                    # Use live 5-min ActualInterval sell price for current period
                    if current_actual_interval and current_actual_interval.get('feedIn'):
                        actual_feedin_cents = current_actual_interval['feedIn'].get('perKwh', 0)
                        # Amber convention: feedIn is negative, Tesla convention: positive
                        sell_price = self._round_price(-actual_feedin_cents / 100)
                        sell_price = max(0, sell_price)  # No negatives

                        # Tesla restriction: sell cannot exceed buy
                        if period_key in general_prices and sell_price > general_prices[period_key]:
                            logger.debug(f"{period_key}: Sell price capped to buy price ({sell_price:.4f} -> {general_prices[period_key]:.4f})")
                            sell_price = general_prices[period_key]

                        feedin_prices[period_key] = sell_price
                        logger.info(f"{period_key} (CURRENT): Using ActualInterval sell price: ${sell_price:.4f}/kWh")

                        # Skip normal lookup logic for this period since we've set both prices
                        continue
                    else:
                        if current_actual_interval:
                            logger.warning(f"{period_key}: No feedIn ActualInterval, falling back to forecast")

                # NORMAL CASE: Use forecast data for all other periods
                # Determine if this period has already passed today
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
                    buy_price = self._round_price(sum(prices) / len(prices))

                    # Tesla restriction: No negative prices - clamp to 0
                    if buy_price < 0:
                        logger.debug(f"{period_key}: Buy price adjusted: {buy_price:.4f} -> 0.0000 (negative->zero)")
                        buy_price = 0

                    general_prices[period_key] = buy_price
                    last_valid_buy_price = buy_price  # Track for fallback (always update, even if clamped to 0)
                    logger.debug(f"{period_key} (using {hour:02d}:{minute:02d} price): ${buy_price:.4f}")
                else:
                    # No data found - use fallback price if available
                    # This commonly happens with AEMO forecast which only provides ~20 hours ahead
                    # Early morning tomorrow (04:00-08:00) typically won't have forecast data
                    tried_keys = [
                        (date_str, hour, minute),
                        (today.isoformat(), hour, minute),
                        (tomorrow.isoformat(), hour, minute)
                    ]
                    if last_valid_buy_price is not None:
                        general_prices[period_key] = last_valid_buy_price
                        logger.info(f"{period_key}: Using fallback buy price ${last_valid_buy_price:.4f} (AEMO forecast gap)")
                    else:
                        logger.warning(f"{period_key}: No price data available for ({hour:02d}:{minute:02d}) - tried keys: {tried_keys}")
                        general_prices[period_key] = None

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
                    sell_price = self._round_price(sum(prices) / len(prices))
                    original_sell = sell_price
                    adjustments = []

                    # Tesla restriction #1: No negative prices - clamp to 0
                    if sell_price < 0:
                        adjustments.append(f"negative({sell_price:.4f})->zero")
                        sell_price = 0

                    # Tesla restriction #2: Sell price cannot exceed buy price
                    # If necessary, adjust sell price downward to comply
                    if period_key in general_prices and general_prices[period_key] is not None:
                        buy_price = general_prices[period_key]
                        if sell_price > buy_price:
                            adjustments.append(f"exceeds_buy({sell_price:.4f}>{buy_price:.4f})->match_buy")
                            sell_price = buy_price

                    # Log all adjustments made for this period
                    if adjustments:
                        logger.debug(f"{period_key}: Sell price adjusted: {original_sell:.4f} -> {sell_price:.4f} ({', '.join(adjustments)})")

                    feedin_prices[period_key] = sell_price
                    last_valid_sell_price = sell_price  # Track for fallback
                    if not adjustments:
                        logger.debug(f"{period_key} (using {hour:02d}:{minute:02d} sell price): ${sell_price:.4f}")
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
                        logger.info(f"{period_key}: Using fallback sell price ${fallback_sell:.4f} (AEMO forecast gap)")
                    else:
                        logger.warning(f"{period_key}: No feedIn price data available for ({hour:02d}:{minute:02d})")
                        feedin_prices[period_key] = None

        # Count missing periods and abort if too many are missing
        # This prevents sending bad tariffs when API is unreachable
        missing_buy = sum(1 for v in general_prices.values() if v is None)
        missing_sell = sum(1 for v in feedin_prices.values() if v is None)
        total_missing = missing_buy + missing_sell

        if total_missing > 0:
            logger.warning(
                f"Missing price data: {missing_buy} buy periods, {missing_sell} sell periods (total: {total_missing}/96)"
            )

        # If more than 10 periods are missing, abort - keep using last good tariff
        MAX_MISSING_PERIODS = 10
        if total_missing > MAX_MISSING_PERIODS:
            logger.error(
                f"❌ Too many missing price periods ({total_missing} > {MAX_MISSING_PERIODS}) - "
                f"ABORTING sync to preserve last good tariff. This usually indicates Amber API is unreachable."
            )
            return None, None

        # Replace any remaining None values with 0 (shouldn't happen if we abort above)
        for key in general_prices:
            if general_prices[key] is None:
                general_prices[key] = 0
        for key in feedin_prices:
            if feedin_prices[key] is None:
                feedin_prices[key] = 0

        # Apply artificial price increase during demand periods if enabled (ALPHA feature)
        if user and getattr(user, 'demand_artificial_price_enabled', False) and getattr(user, 'enable_demand_charges', False):
            artificial_increase = 2.0  # $2/kWh increase during demand periods
            periods_modified = 0

            # Check if today is a valid day for demand charges
            weekday = now.weekday()  # 0=Monday, 6=Sunday
            peak_days = getattr(user, 'peak_days', 'weekdays') or 'weekdays'

            day_is_valid = True
            if peak_days == 'weekdays' and weekday >= 5:  # Saturday or Sunday
                day_is_valid = False
            elif peak_days == 'weekends' and weekday < 5:  # Monday-Friday
                day_is_valid = False
            # 'all' matches any day

            if day_is_valid:
                for period_key in general_prices.keys():
                    # Extract hour/minute from PERIOD_HH_MM
                    parts = period_key.split('_')
                    hour = int(parts[1])
                    minute = int(parts[2])

                    # Check if this period is in the demand peak window
                    peak_start_hour = getattr(user, 'peak_start_hour', 14)
                    peak_start_minute = getattr(user, 'peak_start_minute', 0)
                    peak_end_hour = getattr(user, 'peak_end_hour', 20)
                    peak_end_minute = getattr(user, 'peak_end_minute', 0)

                    if self._is_in_time_range(hour, minute,
                                               peak_start_hour, peak_start_minute,
                                               peak_end_hour, peak_end_minute):
                        original_price = general_prices[period_key]
                        general_prices[period_key] = original_price + artificial_increase
                        periods_modified += 1
                        logger.debug(f"{period_key}: Artificial price increase applied: ${original_price:.4f} -> ${general_prices[period_key]:.4f} (+${artificial_increase})")

                if periods_modified > 0:
                    logger.info(f"🔺 ALPHA: Artificial price increase (+${artificial_increase}/kWh) applied to {periods_modified} demand periods")
            else:
                logger.debug(f"Artificial price increase skipped - today ({weekday}) not in peak_days ({peak_days})")

        logger.info(f"Rolling 24h window: {len([k for k in general_prices.keys()])} periods from {today} and {tomorrow}")

        # Validate Tesla TOU restrictions before returning
        self._validate_tesla_restrictions(general_prices, feedin_prices)

        return general_prices, feedin_prices

    def _validate_tesla_restrictions(self, general_prices: Dict[str, float], feedin_prices: Dict[str, float]):
        """
        Validate that the tariff complies with Tesla's restrictions:
        1. No negative prices
        2. Buy price >= Sell price for every period
        3. No gaps or overlaps in periods

        Logs detailed warnings if any violations are found.
        """
        violations = []

        # Check for negative prices
        for period, price in general_prices.items():
            if price < 0:
                violations.append(f"{period}: Buy price is negative: {price:.4f}")

        for period, price in feedin_prices.items():
            if price < 0:
                violations.append(f"{period}: Sell price is negative: {price:.4f}")

        # Check that buy >= sell for every period
        for period in general_prices.keys():
            buy_price = general_prices[period]
            sell_price = feedin_prices.get(period, 0)

            if sell_price > buy_price:
                violations.append(f"{period}: Sell ({sell_price:.4f}) > Buy ({buy_price:.4f}) - TESLA WILL REJECT THIS")

        # Log validation results
        if violations:
            logger.error(f"Tesla TOU validation FAILED with {len(violations)} violations:")
            for violation in violations:
                logger.error(f"  - {violation}")
        else:
            logger.info("Tesla TOU validation PASSED - all restrictions met")

        # Log summary statistics
        buy_prices = [p for p in general_prices.values()]
        sell_prices = [p for p in feedin_prices.values()]

        logger.info(f"Buy prices: min=${min(buy_prices):.4f}, max=${max(buy_prices):.4f}, avg=${sum(buy_prices)/len(buy_prices):.4f}")
        logger.info(f"Sell prices: min=${min(sell_prices):.4f}, max=${max(sell_prices):.4f}, avg=${sum(sell_prices)/len(sell_prices):.4f}")

        # Calculate and log the margin (buy - sell) for each period
        margins = [general_prices[p] - feedin_prices.get(p, 0) for p in general_prices.keys()]
        avg_margin = sum(margins) / len(margins)
        logger.info(f"Price margins (buy-sell): min=${min(margins):.4f}, max=${max(margins):.4f}, avg=${avg_margin:.4f}")

    def _build_tariff_structure(self, general_prices: Dict[str, float],
                                feedin_prices: Dict[str, float],
                                user=None) -> Dict:
        """Build the complete Tesla tariff structure"""

        # Build TOU periods for Summer season (covers whole year for Amber)
        tou_periods = self._build_tou_periods(general_prices.keys())

        # Build demand charges if enabled
        demand_charges_summer = {}
        demand_charges_sell = {}
        if user and user.enable_demand_charges:
            # Ensure demand charges match the exact periods that exist in general_prices
            base_demand_charges = self._build_demand_charge_rates(user, general_prices.keys())

            # Determine where to apply demand charges based on user setting
            apply_to = getattr(user, 'demand_charge_apply_to', 'buy') or 'buy'
            apply_to_buy = apply_to in ['buy', 'both']
            apply_to_sell = apply_to in ['sell', 'both']

            if apply_to_buy:
                demand_charges_summer = base_demand_charges
            if apply_to_sell:
                demand_charges_sell = base_demand_charges

        # Set tariff metadata based on electricity provider
        # Note: Globird not included - users set tariff manually, only AEMO spike feature used
        provider_names = {
            "amber": "Amber Electric",
            "flow_power": "Flow Power",
        }
        electricity_provider = getattr(user, 'electricity_provider', 'amber') if user else 'amber'
        provider_name = provider_names.get(electricity_provider, "Amber Electric")

        code = f"TESLA_SYNC:{electricity_provider.upper()}"
        name = f"{provider_name} (Tesla Sync)"
        utility = provider_name

        # Build daily charges list
        daily_charges_list = []
        if user and user.daily_supply_charge and user.daily_supply_charge > 0:
            daily_charges_list.append({
                "name": "Daily Supply Charge",
                "amount": float(user.daily_supply_charge)
            })

        if not daily_charges_list:
            # Default empty charge for Tesla compatibility
            daily_charges_list.append({"name": "Charge"})

        tariff = {
            "version": 1,
            "code": code,
            "name": name,
            "utility": utility,
            "currency": "AUD",
            "daily_charges": daily_charges_list,
            "demand_charges": {
                "ALL": {
                    "rates": {
                        "ALL": 0
                    }
                },
                "Summer": {
                    "rates": demand_charges_summer
                } if demand_charges_summer else {},
                "Winter": {}
            },
            "energy_charges": {
                "ALL": {
                    "rates": {
                        "ALL": 0
                    }
                },
                "Summer": {
                    "rates": general_prices
                },
                "Winter": {}
            },
            "seasons": {
                "Summer": {
                    "fromMonth": 1,
                    "toMonth": 12,
                    "fromDay": 1,
                    "toDay": 31,
                    "tou_periods": tou_periods
                },
                "Winter": {
                    "fromDay": 0,
                    "toDay": 0,
                    "fromMonth": 0,
                    "toMonth": 0,
                    "tou_periods": {}
                }
            },
            "sell_tariff": {
                "name": f"{provider_name} (managed by Tesla Sync)",
                "utility": provider_name,
                "daily_charges": daily_charges_list,
                "demand_charges": {
                    "ALL": {
                        "rates": {
                            "ALL": 0
                        }
                    },
                    "Summer": {
                        "rates": demand_charges_sell
                    } if demand_charges_sell else {},
                    "Winter": {}
                },
                "energy_charges": {
                    "ALL": {
                        "rates": {
                            "ALL": 0
                        }
                    },
                    "Summer": {
                        "rates": feedin_prices
                    },
                    "Winter": {}
                },
                "seasons": {
                    "Summer": {
                        "fromMonth": 1,
                        "toMonth": 12,
                        "fromDay": 1,
                        "toDay": 31,
                        "tou_periods": tou_periods
                    },
                    "Winter": {
                        "fromDay": 0,
                        "toDay": 0,
                        "fromMonth": 0,
                        "toMonth": 0,
                        "tou_periods": {}
                    }
                }
            }
        }

        logger.info("Built Tesla tariff structure")
        return tariff

    def _build_tou_periods(self, period_keys) -> Dict:
        """
        Build TOU period definitions for all time slots
        Omits fields when they're 0 for cleaner output

        Args:
            period_keys: Set of period keys like "PERIOD_14_30"

        Returns:
            Dictionary mapping period keys to time slot definitions
        """
        tou_periods = {}

        for period_key in period_keys:
            # Extract hour and minute from period key
            # PERIOD_14_30 -> hour=14, minute=30
            try:
                parts = period_key.split('_')
                from_hour = int(parts[1])
                from_minute = int(parts[2])

                # Calculate end time (30 minutes later)
                to_hour = from_hour
                to_minute = from_minute + 30

                if to_minute >= 60:
                    to_minute = 0
                    to_hour += 1

                # Build period definition, omitting fields when they're 0
                period_def = {
                    "toDayOfWeek": 6  # Saturday (covers all days with implicit fromDayOfWeek=0)
                }

                # Only include fromHour if non-zero
                if from_hour > 0:
                    period_def["fromHour"] = from_hour

                # Only include fromMinute if non-zero
                if from_minute > 0:
                    period_def["fromMinute"] = from_minute

                # Only include toHour if it's not same as fromHour or if it's non-zero
                if to_hour != from_hour or to_hour > 0:
                    period_def["toHour"] = to_hour

                # Only include toMinute if non-zero
                if to_minute > 0:
                    period_def["toMinute"] = to_minute

                tou_periods[period_key] = {
                    "periods": [period_def]
                }

            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing period key {period_key}: {e}")
                continue

        logger.debug(f"Built {len(tou_periods)} TOU period definitions")
        return tou_periods

    def _build_demand_charge_rates(self, user, period_keys) -> Dict[str, float]:
        """
        Build demand charge rates based on user configuration

        Maps user's configured time periods to Tesla PERIOD_XX_XX format
        and assigns rates based on peak/shoulder/offpeak configuration

        Args:
            user: User object with demand charge configuration
            period_keys: Set/list of PERIOD_XX_XX keys to create rates for (must match TOU periods)

        Returns:
            Dictionary mapping PERIOD_XX_XX to demand rate ($/kW)
        """
        demand_rates = {}

        # Only create rates for periods that exist in the tariff (to match TOU periods exactly)
        for period_key in period_keys:
            # Extract hour and minute from period key (PERIOD_14_30 -> hour=14, minute=30)
            try:
                parts = period_key.split('_')
                hour = int(parts[1])
                minute = int(parts[2])
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing period key {period_key}: {e}")
                continue

            # Determine which rate applies to this period
            # Priority: Peak > Shoulder > Off-peak

            # Check if this period falls in peak time
            if self._is_in_time_range(hour, minute,
                                     user.peak_start_hour, user.peak_start_minute,
                                     user.peak_end_hour, user.peak_end_minute):
                demand_rates[period_key] = float(user.peak_demand_rate or 0)

            # Check if this period falls in shoulder time
            elif user.shoulder_demand_rate and user.shoulder_demand_rate > 0:
                if self._is_in_time_range(hour, minute,
                                         user.shoulder_start_hour, user.shoulder_start_minute,
                                         user.shoulder_end_hour, user.shoulder_end_minute):
                    demand_rates[period_key] = float(user.shoulder_demand_rate)
                else:
                    # Off-peak
                    demand_rates[period_key] = float(user.offpeak_demand_rate or 0)
            else:
                # Off-peak (no shoulder period configured)
                demand_rates[period_key] = float(user.offpeak_demand_rate or 0)

        logger.info(f"Built demand charge rates with {len(demand_rates)} periods matching TOU periods")
        return demand_rates

    def _is_in_time_range(self, hour: int, minute: int,
                          start_hour: int, start_minute: int,
                          end_hour: int, end_minute: int) -> bool:
        """
        Check if a time falls within a given range

        Args:
            hour, minute: Time to check
            start_hour, start_minute: Start of range
            end_hour, end_minute: End of range

        Returns:
            True if time is in range (inclusive of start, exclusive of end)
        """
        # Convert to minutes since midnight for easier comparison
        time_minutes = hour * 60 + minute
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute

        # Handle case where range crosses midnight
        if end_minutes <= start_minutes:
            # Range crosses midnight (e.g., 22:00 to 06:00)
            return time_minutes >= start_minutes or time_minutes < end_minutes
        else:
            # Normal range (e.g., 14:00 to 20:00)
            return start_minutes <= time_minutes < end_minutes


# Network Tariff Support
# For Flow Power + AEMO, apply user-configured network (DNSP) charges
# since AEMO wholesale prices don't include network fees

def _normalize_network_rate(rate, default, name="rate"):
    """
    Normalize network rate to cents/kWh.

    If value is < 0.1, assume it was entered in dollars and convert to cents.

    Threshold rationale:
    - Lowest legitimate DNSP rate is ~0.4c/kWh (NTC6900 off-peak: 0.476c)
    - Values like 0.08 (8c entered as $0.08) need conversion
    - Values like 0.476 (legitimate off-peak) should NOT be converted

    Using 0.1 as threshold catches most dollar-value mistakes while
    preserving very low but legitimate off-peak rates.
    """
    if rate is None:
        return default
    if rate < 0.1:
        # Very likely entered in dollars instead of cents - convert
        corrected = rate * 100
        logger.warning(f"Network {name} appears to be in dollars ({rate}), converting to cents: {corrected:.2f}c/kWh")
        return corrected
    return rate


def apply_network_tariff(tariff: Dict, user) -> Dict:
    """
    Apply network tariff (DNSP) charges to wholesale prices.

    AEMO wholesale prices only include energy costs. This function adds:
    - Network (DNSP) charges (via aemo_to_tariff library or manual rates)
    - Other fees (environmental, market fees)
    - GST (optional)

    Args:
        tariff: Tesla tariff structure with wholesale prices
        user: User object with network tariff configuration

    Returns:
        Modified tariff with network charges applied to buy prices
    """
    if not tariff:
        logger.warning("No tariff provided for network tariff adjustment")
        return tariff

    # Check if user wants manual rates or library-based calculation
    use_manual_rates = getattr(user, 'network_use_manual_rates', False)

    # If library is available and user doesn't want manual rates, use it
    if AEMO_TARIFF_AVAILABLE and not use_manual_rates:
        return _apply_network_tariff_library(tariff, user)

    # Otherwise fall back to manual rate entry
    if not AEMO_TARIFF_AVAILABLE and not use_manual_rates:
        logger.warning("aemo_to_tariff library not available, falling back to manual rates")

    return _apply_network_tariff_manual(tariff, user)


def _apply_network_tariff_library(tariff: Dict, user) -> Dict:
    """
    Apply network tariff using the aemo_to_tariff library.

    Uses distributor and tariff code to automatically calculate network charges.
    """
    distributor = getattr(user, 'network_distributor', 'energex') or 'energex'
    tariff_code = getattr(user, 'network_tariff_code', '6900') or '6900'

    # Strip common prefixes from tariff code (e.g., NTC6900 -> 6900, EA025 -> EA025)
    # The aemo_to_tariff library expects codes without the NTC prefix
    if tariff_code.upper().startswith('NTC'):
        tariff_code = tariff_code[3:]
        logger.info(f"Stripped NTC prefix from tariff code: {tariff_code}")

    # Map distributors to library module names
    # CitiPower and United Energy use the generic Victoria module
    library_distributor_map = {
        "citipower": "victoria",
        "united": "victoria",
    }
    library_distributor = library_distributor_map.get(distributor, distributor)

    logger.info(f"Applying network tariff via library: distributor={distributor} -> {library_distributor}, tariff_code={tariff_code}")

    # Apply to Summer season buy rates (energy_charges)
    for season in ['Summer']:
        if season not in tariff.get('energy_charges', {}):
            continue

        rates = tariff['energy_charges'][season].get('rates', {})
        modified_count = 0

        for period, price in list(rates.items()):
            # Extract hour and minute from PERIOD_HH_MM
            try:
                parts = period.split('_')
                hour = int(parts[1])
                minute = int(parts[2])

                # Create datetime for this period
                now = datetime.now()
                interval_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # Convert wholesale price from $/kWh to $/MWh for the library
                wholesale_mwh = price * 1000

                # Use aemo_to_tariff library to get total retail price
                # Returns price in c/kWh including network charges
                try:
                    retail_price_cents = spot_to_tariff(
                        interval_time=interval_time,
                        network=library_distributor,
                        tariff=tariff_code,
                        rrp=wholesale_mwh
                    )

                    # Convert from c/kWh to $/kWh
                    new_price = round(retail_price_cents / 100, 4)

                    # Tesla restriction: no negative prices
                    new_price = max(0, new_price)

                    if rates[period] != new_price:
                        modified_count += 1
                        logger.debug(f"{period}: ${price:.4f} wholesale -> ${new_price:.4f} retail (via library)")
                        rates[period] = new_price

                except Exception as e:
                    logger.warning(f"{period}: Library error for {library_distributor}/{tariff_code}: {e}, keeping wholesale price")

            except (IndexError, ValueError):
                continue

        total_periods = len(rates)
        if modified_count == 0 and total_periods > 0:
            logger.error(f"Network tariff library failed for ALL {total_periods} periods! Check distributor={library_distributor}, tariff={tariff_code}")
        else:
            logger.info(f"Network tariff (library) applied to {modified_count}/{total_periods} periods in {season}")

    return tariff


def _apply_network_tariff_manual(tariff: Dict, user) -> Dict:
    """
    Apply network tariff using user-entered manual rates.

    Falls back to this when library is unavailable or user prefers manual entry.
    """
    # Check if network tariff is configured
    tariff_type = getattr(user, 'network_tariff_type', 'flat') or 'flat'
    raw_other_fees = getattr(user, 'network_other_fees', None)
    other_fees = _normalize_network_rate(raw_other_fees, 1.5, "other_fees")
    include_gst = getattr(user, 'network_include_gst', True)

    logger.info(f"Applying network tariff (manual): type={tariff_type}, other_fees={other_fees}c/kWh, gst={include_gst}")

    # Get rate configuration
    if tariff_type == 'flat':
        raw_flat = getattr(user, 'network_flat_rate', None)
        flat_rate = _normalize_network_rate(raw_flat, 8.0, "flat_rate")
        logger.info(f"Network flat rate: {flat_rate}c/kWh")
    else:
        # TOU rates - normalize each one
        raw_peak = getattr(user, 'network_peak_rate', None)
        raw_shoulder = getattr(user, 'network_shoulder_rate', None)
        raw_offpeak = getattr(user, 'network_offpeak_rate', None)

        peak_rate = _normalize_network_rate(raw_peak, 15.0, "peak_rate")
        shoulder_rate = _normalize_network_rate(raw_shoulder, 5.0, "shoulder_rate")
        offpeak_rate = _normalize_network_rate(raw_offpeak, 2.0, "offpeak_rate")

        # Time periods
        peak_start = getattr(user, 'network_peak_start', '16:00') or '16:00'
        peak_end = getattr(user, 'network_peak_end', '21:00') or '21:00'
        offpeak_start = getattr(user, 'network_offpeak_start', '10:00') or '10:00'
        offpeak_end = getattr(user, 'network_offpeak_end', '15:00') or '15:00'

        # Parse times
        peak_start_hour, peak_start_min = map(int, peak_start.split(':'))
        peak_end_hour, peak_end_min = map(int, peak_end.split(':'))
        offpeak_start_hour, offpeak_start_min = map(int, offpeak_start.split(':'))
        offpeak_end_hour, offpeak_end_min = map(int, offpeak_end.split(':'))

        logger.info(f"Network TOU: peak={peak_rate}c ({peak_start}-{peak_end}), "
                   f"shoulder={shoulder_rate}c, offpeak={offpeak_rate}c ({offpeak_start}-{offpeak_end})")

    # Apply to Summer season buy rates (energy_charges)
    for season in ['Summer']:
        if season not in tariff.get('energy_charges', {}):
            continue

        rates = tariff['energy_charges'][season].get('rates', {})
        modified_count = 0

        for period, price in list(rates.items()):
            # Extract hour and minute from PERIOD_HH_MM
            try:
                parts = period.split('_')
                hour = int(parts[1])
                minute = int(parts[2])
            except (IndexError, ValueError):
                continue

            # Calculate network charge based on tariff type
            if tariff_type == 'flat':
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
                logger.debug(f"{period}: ${price:.4f} + {total_charge_cents:.2f}c network = ${new_price:.4f}")
                rates[period] = new_price

        logger.info(f"Network tariff applied to {modified_count} periods in {season}")

    return tariff


# Flow Power Electricity Provider Support
# Flow Power offers fixed export rates during "Happy Hour" (5:30pm-7:30pm)
# Outside Happy Hour, export rate is 0c/kWh

# Happy Hour export rates by NEM region (in $/kWh)
FLOW_POWER_EXPORT_RATES = {
    'NSW1': 0.45,   # 45c/kWh
    'QLD1': 0.45,   # 45c/kWh
    'SA1': 0.45,    # 45c/kWh
    'VIC1': 0.35,   # 35c/kWh
}

# Happy Hour periods (5:30pm to 7:30pm)
# Maps to Tesla PERIOD_XX_XX format for 30-minute intervals
FLOW_POWER_HAPPY_HOUR_PERIODS = [
    'PERIOD_17_30',  # 5:30pm - 6:00pm
    'PERIOD_18_00',  # 6:00pm - 6:30pm
    'PERIOD_18_30',  # 6:30pm - 7:00pm
    'PERIOD_19_00',  # 7:00pm - 7:30pm
]

# Flow Power PEA (Price Efficiency Adjustment) Constants
# PEA adjusts pricing based on wholesale market efficiency
# Formula: PEA = wholesale - market_avg - benchmark = wholesale - 9.7c
FLOW_POWER_MARKET_AVG = 8.0       # Market TWAP average (c/kWh)
FLOW_POWER_BENCHMARK = 1.7       # BPEA - benchmark customer performance (c/kWh)
FLOW_POWER_PEA_OFFSET = 9.7      # Combined: MARKET_AVG + BENCHMARK (c/kWh)
FLOW_POWER_DEFAULT_BASE_RATE = 34.0  # Default Flow Power base rate (c/kWh)


def apply_flow_power_export(tariff: Dict, state: str) -> Dict:
    """
    Apply Flow Power export rates to a tariff structure.

    Flow Power pricing:
    - Happy Hour (5:30pm-7:30pm): Fixed export rate (45c NSW/QLD/SA, 35c VIC)
    - All other times: 0c export

    Args:
        tariff: Tesla tariff structure (from AmberTariffConverter)
        state: NEM region code (NSW1, VIC1, QLD1, SA1)

    Returns:
        Modified tariff with Flow Power export rates applied
    """
    if not tariff:
        logger.warning("No tariff provided for Flow Power export adjustment")
        return tariff

    # Get the happy hour export rate for this state
    export_rate = FLOW_POWER_EXPORT_RATES.get(state, 0.45)  # Default to 45c if unknown state

    logger.info(f"Applying Flow Power export rates for {state}: {export_rate * 100:.0f}c during Happy Hour, 0c otherwise")

    # Apply to both Summer and Winter seasons in sell_tariff
    for season in ['Summer', 'Winter']:
        if season not in tariff.get('sell_tariff', {}).get('energy_charges', {}):
            continue

        rates = tariff['sell_tariff']['energy_charges'][season].get('rates', {})

        # Set ALL periods to 0c first
        for period in list(rates.keys()):
            rates[period] = 0.0

        # Then set Happy Hour periods to the fixed rate
        for period in FLOW_POWER_HAPPY_HOUR_PERIODS:
            if period in rates or len(rates) > 0:  # Only add if rates exist
                rates[period] = export_rate

    # Log summary of changes
    happy_hour_periods_count = len(FLOW_POWER_HAPPY_HOUR_PERIODS)
    logger.info(f"Flow Power export applied: {happy_hour_periods_count} periods at ${export_rate}/kWh, remaining periods at $0.00/kWh")

    return tariff


def apply_flow_power_pea(
    tariff: Dict,
    wholesale_prices: Dict[str, float],
    base_rate: float = FLOW_POWER_DEFAULT_BASE_RATE,
    custom_pea: float = None
) -> Dict:
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
        logger.warning("No tariff provided for Flow Power PEA adjustment")
        return tariff

    logger.info(f"Applying Flow Power PEA: base_rate={base_rate}c/kWh, custom_pea={custom_pea}")

    # Track statistics for logging
    pea_values = []
    final_prices = []

    # Apply to Summer season buy rates (energy_charges)
    for season in ['Summer']:
        if season not in tariff.get('energy_charges', {}):
            continue

        rates = tariff['energy_charges'][season].get('rates', {})
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
                logger.debug(f"{period}: base={base_rate}c + PEA={pea:.1f}c = {final_cents:.1f}c (${final_dollars:.4f}/kWh)")

            rates[period] = final_dollars

        logger.info(f"Flow Power PEA applied to {modified_count} periods in {season}")

    # Log summary statistics
    if pea_values:
        avg_pea = sum(pea_values) / len(pea_values)
        min_pea = min(pea_values)
        max_pea = max(pea_values)
        avg_final = sum(final_prices) / len(final_prices)

        logger.info(
            f"Flow Power PEA summary: avg_pea={avg_pea:.1f}c, "
            f"range=[{min_pea:.1f}c to {max_pea:.1f}c], "
            f"avg_final={avg_final:.1f}c/kWh"
        )

    return tariff


def get_wholesale_lookup(forecast_data: list) -> Dict[str, float]:
    """
    Extract wholesale prices from forecast data into a period lookup.

    Used by apply_flow_power_pea() to calculate PEA from wholesale prices.

    Args:
        forecast_data: List of price forecast points (5-min or 30-min resolution)

    Returns:
        Dict mapping PERIOD_HH_MM to wholesale price in $/kWh
    """
    wholesale_lookup = {}

    for point in forecast_data:
        try:
            nem_time = point.get('nemTime', '')
            timestamp = datetime.fromisoformat(nem_time.replace('Z', '+00:00'))
            channel_type = point.get('channelType', '')
            duration = point.get('duration', 30)

            # Only use general (import) prices
            if channel_type != 'general':
                continue

            # Get wholesale component - prefer wholesaleKWHPrice for raw wholesale
            # Fall back to perKwh for AEMO data (which is already wholesale)
            wholesale_cents = point.get('wholesaleKWHPrice')
            if wholesale_cents is None:
                # AEMO data - perKwh is the wholesale price
                wholesale_cents = point.get('perKwh', 0)

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

        except Exception as e:
            logger.debug(f"Error extracting wholesale price: {e}")
            continue

    # Average multiple values per period
    result = {}
    for period, prices in wholesale_lookup.items():
        result[period] = sum(prices) / len(prices)

    logger.debug(f"Built wholesale lookup with {len(result)} periods")
    return result


def apply_export_boost(
    tariff: Dict,
    offset_cents: float = 0.0,
    min_price_cents: float = 0.0,
    boost_start: str = "17:00",
    boost_end: str = "21:00",
    activation_threshold_cents: float = 0.0,
) -> Dict:
    """
    Apply export price boost to trigger Powerwall exports at lower price points.

    This artificially increases sell prices so the Powerwall sees higher export
    value and is more willing to discharge. Useful when actual export prices are
    in the 20-25c range where Tesla's algorithm may not trigger exports.

    The boost is only applied during the configured time window, and only if the
    actual price is at or above the activation threshold.

    Args:
        tariff: Tesla tariff structure with energy_charges containing sell_prices
        offset_cents: Fixed offset to add to all export prices (c/kWh)
        min_price_cents: Minimum export price floor (c/kWh)
        boost_start: Time to start applying boost (HH:MM format)
        boost_end: Time to stop applying boost (HH:MM format)
        activation_threshold_cents: Minimum actual price to activate boost (c/kWh)
                                   If actual price is below this, boost is skipped

    Returns:
        Modified tariff with boosted export prices

    Example:
        Amber says export = 18c
        With offset_cents=5, min_price_cents=20, activation_threshold_cents=10:
        → Tesla sees 23c (18 + 5 = 23, above min)

        Amber says export = 12c
        With offset_cents=5, min_price_cents=20, activation_threshold_cents=10:
        → Tesla sees 20c (12 + 5 = 17, below min, so use min)

        Amber says export = 5c
        With offset_cents=5, min_price_cents=20, activation_threshold_cents=10:
        → Tesla sees 5c (below threshold, boost skipped)
    """
    if offset_cents == 0 and min_price_cents == 0:
        logger.debug("Export boost disabled (offset=0, min=0)")
        return tariff

    # Parse time window
    try:
        start_parts = boost_start.split(":")
        start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
        end_parts = boost_end.split(":")
        end_hour, end_minute = int(end_parts[0]), int(end_parts[1])
    except (ValueError, IndexError) as err:
        logger.error(f"Invalid export boost time format: {err}")
        return tariff

    # Build list of periods within the time window
    boost_periods = set()
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

    logger.debug(
        f"Export boost active for {len(boost_periods)} periods ({boost_start} to {boost_end}): "
        f"offset={offset_cents:.1f}c, min={min_price_cents:.1f}c, threshold={activation_threshold_cents:.1f}c"
    )

    modified_count = 0
    skipped_count = 0
    boosted_prices = []

    # Process each season in the tariff
    for season, season_data in tariff.get("energy_charges", {}).items():
        sell_prices = season_data.get("sell_prices", {})
        buy_prices = season_data.get("rates", {})

        for period in boost_periods:
            if period not in sell_prices:
                continue

            original_dollars = sell_prices[period]
            original_cents = original_dollars * 100

            # Skip boost if actual price is below activation threshold
            if activation_threshold_cents > 0 and original_cents < activation_threshold_cents:
                skipped_count += 1
                logger.debug(
                    f"{period}: Boost skipped - actual price {original_cents:.2f}c below threshold {activation_threshold_cents:.1f}c"
                )
                continue

            # Apply offset
            boosted_cents = original_cents + offset_cents

            # Apply minimum floor
            boosted_cents = max(boosted_cents, min_price_cents)

            # Convert back to dollars
            boosted_dollars = round(boosted_cents / 100, 4)

            # Tesla restriction: sell cannot exceed buy
            if period in buy_prices and boosted_dollars > buy_prices[period]:
                logger.debug(
                    f"{period}: Boosted sell price capped to buy price ({boosted_dollars:.4f} -> {buy_prices[period]:.4f})"
                )
                boosted_dollars = buy_prices[period]

            if boosted_dollars != original_dollars:
                modified_count += 1
                boosted_prices.append(boosted_cents)
                logger.debug(
                    f"{period}: Export boost {original_cents:.2f}c → {boosted_dollars * 100:.2f}c "
                    f"(offset={offset_cents:.1f}c, min={min_price_cents:.1f}c)"
                )

            sell_prices[period] = boosted_dollars

    # Log summary
    if boosted_prices:
        avg_boost = sum(boosted_prices) / len(boosted_prices)
        skip_msg = f", {skipped_count} skipped (below threshold)" if skipped_count > 0 else ""
        logger.info(
            f"Export boost applied to {modified_count} periods{skip_msg}: avg={avg_boost:.1f}c/kWh, "
            f"range=[{min(boosted_prices):.1f}c to {max(boosted_prices):.1f}c]"
        )
    elif skipped_count > 0:
        logger.info(f"Export boost: {skipped_count} periods skipped (below threshold {activation_threshold_cents:.1f}c)")
    else:
        logger.info("Export boost: no periods modified (prices already meet criteria)")

    return tariff
