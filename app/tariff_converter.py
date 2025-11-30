# app/tariff_converter.py
"""Convert Amber Electric pricing to Tesla tariff format"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict

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

                # For ForecastInterval: REQUIRE advancedPrice (no fallback)
                if interval_type == 'ForecastInterval':
                    if not advanced_price:
                        # Expected for far-future forecasts (>36h) - Amber API doesn't provide advancedPrice
                        logger.debug(f"Skipping ForecastInterval at {nem_time} - no advancedPrice (expected for far-future forecasts)")
                        continue

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

        # Now build the rolling 24-hour tariff
        general_prices, feedin_prices = self._build_rolling_24h_tariff(
            general_lookup, feedin_lookup, user, detected_tz, current_actual_interval
        )

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
                if lookup_key in general_lookup:
                    prices = general_lookup[lookup_key]
                    buy_price = self._round_price(sum(prices) / len(prices))

                    # Tesla restriction: No negative prices - clamp to 0
                    if buy_price < 0:
                        logger.debug(f"{period_key}: Buy price adjusted: {buy_price:.4f} -> 0.0000 (negative->zero)")
                        general_prices[period_key] = 0
                    else:
                        general_prices[period_key] = buy_price
                        logger.debug(f"{period_key} (using {hour:02d}:{minute:02d} price): ${buy_price:.4f}")
                else:
                    # Fallback: Use today's data when tomorrow's not available
                    fallback_key = (today.isoformat(), hour, minute)
                    if fallback_key in general_lookup:
                        prices = general_lookup[fallback_key]
                        buy_price = max(0, self._round_price(sum(prices) / len(prices)))
                        general_prices[period_key] = buy_price
                    else:
                        # Last resort: use 0
                        logger.warning(f"{period_key}: No price data available for ({hour:02d}:{minute:02d}), using 0.00")
                        general_prices[period_key] = 0

                # Get feedin price (sell price)
                if lookup_key in feedin_lookup:
                    prices = feedin_lookup[lookup_key]
                    sell_price = self._round_price(sum(prices) / len(prices))
                    original_sell = sell_price
                    adjustments = []

                    # Tesla restriction #1: No negative prices - clamp to 0
                    if sell_price < 0:
                        adjustments.append(f"negative({sell_price:.4f})->zero")
                        sell_price = 0

                    # Tesla restriction #2: Sell price cannot exceed buy price
                    # If necessary, adjust sell price downward to comply
                    if period_key in general_prices:
                        buy_price = general_prices[period_key]
                        if sell_price > buy_price:
                            adjustments.append(f"exceeds_buy({sell_price:.4f}>{buy_price:.4f})->match_buy")
                            sell_price = buy_price

                    # Log all adjustments made for this period
                    if adjustments:
                        logger.debug(f"{period_key}: Sell price adjusted: {original_sell:.4f} -> {sell_price:.4f} ({', '.join(adjustments)})")

                    feedin_prices[period_key] = sell_price
                    if not adjustments:
                        logger.debug(f"{period_key} (using {hour:02d}:{minute:02d} sell price): ${sell_price:.4f}")
                else:
                    # Fallback: Use today's data when tomorrow's not available
                    fallback_key = (today.isoformat(), hour, minute)
                    if fallback_key in feedin_lookup:
                        prices = feedin_lookup[fallback_key]
                        sell_price = max(0, self._round_price(sum(prices) / len(prices)))
                        if period_key in general_prices and sell_price > general_prices[period_key]:
                            sell_price = general_prices[period_key]
                        feedin_prices[period_key] = sell_price
                    else:
                        # Last resort: use 0
                        logger.warning(f"{period_key}: No feedIn price data available (current or next slot), using 0.00")
                        feedin_prices[period_key] = 0

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

        # Set tariff metadata
        code = "TESLA_SYNC:AMBER:AMBER"
        name = "Amber Electric (Tesla Sync)"
        utility = "Amber Electric"

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
                "name": "Amber Electric (managed by Tesla Sync, do not edit)",
                "utility": "Amber Electric",
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
