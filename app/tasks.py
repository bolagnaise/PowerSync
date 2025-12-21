# app/tasks.py
"""Background tasks for automatic syncing"""
import logging
import threading
import hashlib
from datetime import datetime, timezone
from app.models import User, PriceRecord, EnergyRecord, SavedTOUProfile
from app.api_clients import get_amber_client, get_tesla_client, AEMOAPIClient
from app.tariff_converter import AmberTariffConverter
import json

logger = logging.getLogger(__name__)


def get_tariff_hash(tariff_structure):
    """
    Generate MD5 hash of tariff structure for deduplication.

    This allows us to skip sending unchanged tariffs to Tesla,
    which prevents duplicate rate plan entries in the Tesla dashboard.
    """
    # Sort keys for consistent hashing
    tariff_json = json.dumps(tariff_structure, sort_keys=True)
    return hashlib.md5(tariff_json.encode()).hexdigest()


class SyncCoordinator:
    """
    Coordinates Tesla sync with smarter price-aware logic.

    Sync flow for each 5-minute period:
    1. At 0s: Sync immediately using forecast price (get price to Tesla ASAP)
    2. When WebSocket arrives: Re-sync only if price differs from forecast
    3. At 35s: If no WebSocket yet, check REST API and sync if price differs
    4. At 60s: Final REST API check if price still hasn't been confirmed

    This ensures:
    - Fast response: Price synced at start of period using forecast
    - Accuracy: Re-sync when actual price differs from forecast
    - Reliability: Multiple fallback checks if WebSocket fails
    """

    # Price difference threshold (in cents) to trigger re-sync
    PRICE_DIFF_THRESHOLD = 0.5  # Re-sync if price differs by more than 0.5c/kWh

    def __init__(self):
        self._lock = threading.Lock()
        self._websocket_event = threading.Event()
        self._websocket_data = None
        self._current_period = None  # Track which 5-min period we're in
        self._initial_sync_done = False  # Has initial forecast sync happened this period?
        self._last_synced_prices = {}  # {user_id: {'general': price, 'feedIn': price}}
        self._websocket_received = False  # Has WebSocket delivered this period?

    def _get_current_period(self):
        """Get the current 5-minute period timestamp."""
        now = datetime.now(timezone.utc)
        current_period = now.replace(second=0, microsecond=0)
        return current_period.replace(minute=current_period.minute - (current_period.minute % 5))

    def _reset_if_new_period(self):
        """Reset state if we've moved to a new 5-minute period."""
        current_period = self._get_current_period()
        if self._current_period != current_period:
            logger.info(f"🆕 New sync period: {current_period}")
            self._current_period = current_period
            self._initial_sync_done = False
            self._websocket_received = False
            self._last_synced_prices = {}
            self._websocket_event.clear()
            self._websocket_data = None
            return True
        return False

    def notify_websocket_update(self, prices_data):
        """Called by WebSocket when new price data arrives."""
        with self._lock:
            self._reset_if_new_period()
            self._websocket_data = prices_data
            self._websocket_received = True
            self._websocket_event.set()
            logger.info("📡 WebSocket price update received, notifying sync coordinator")

    def get_websocket_data(self):
        """Get the current WebSocket data if available."""
        with self._lock:
            return self._websocket_data

    def mark_initial_sync_done(self):
        """Mark that the initial forecast sync has been completed for this period."""
        with self._lock:
            self._reset_if_new_period()
            self._initial_sync_done = True
            logger.info("✅ Initial forecast sync marked as done for this period")

    def should_do_initial_sync(self):
        """
        Check if we should do the initial forecast sync at start of period.

        Returns:
            bool: True if initial sync hasn't been done yet this period
        """
        with self._lock:
            self._reset_if_new_period()
            if self._initial_sync_done:
                logger.debug("⏭️  Initial sync already done this period")
                return False
            return True

    def has_websocket_delivered(self):
        """Check if WebSocket has delivered price data this period."""
        with self._lock:
            self._reset_if_new_period()
            return self._websocket_received

    def record_synced_price(self, user_id, general_price, feedin_price):
        """
        Record the price that was synced for a user.

        Args:
            user_id: The user's ID
            general_price: The general (buy) price in c/kWh
            feedin_price: The feedIn (sell) price in c/kWh
        """
        with self._lock:
            self._last_synced_prices[user_id] = {
                'general': general_price,
                'feedIn': feedin_price
            }
            logger.debug(f"Recorded synced price for user {user_id}: general={general_price}c, feedIn={feedin_price}c")

    def should_resync_for_price(self, user_id, new_general_price, new_feedin_price):
        """
        Check if we should re-sync because the price has changed significantly.

        Args:
            user_id: The user's ID
            new_general_price: The new general price from WebSocket/REST
            new_feedin_price: The new feedIn price from WebSocket/REST

        Returns:
            bool: True if price difference exceeds threshold
        """
        with self._lock:
            last_prices = self._last_synced_prices.get(user_id)

            if not last_prices:
                # No previous sync - should sync
                logger.info(f"User {user_id}: No previous price recorded, will sync")
                return True

            last_general = last_prices.get('general')
            last_feedin = last_prices.get('feedIn')

            # Check general price difference
            if last_general is not None and new_general_price is not None:
                general_diff = abs(new_general_price - last_general)
                if general_diff > self.PRICE_DIFF_THRESHOLD:
                    logger.info(f"User {user_id}: General price changed by {general_diff:.2f}c ({last_general:.2f}c → {new_general_price:.2f}c) - will re-sync")
                    return True

            # Check feedIn price difference
            if last_feedin is not None and new_feedin_price is not None:
                feedin_diff = abs(new_feedin_price - last_feedin)
                if feedin_diff > self.PRICE_DIFF_THRESHOLD:
                    logger.info(f"User {user_id}: FeedIn price changed by {feedin_diff:.2f}c ({last_feedin:.2f}c → {new_feedin_price:.2f}c) - will re-sync")
                    return True

            logger.debug(f"User {user_id}: Price unchanged (general={new_general_price}c, feedIn={new_feedin_price}c) - skipping re-sync")
            return False

    # Legacy methods for backwards compatibility
    def wait_for_websocket_or_timeout(self, timeout_seconds=15):
        """Wait for WebSocket data or timeout (legacy method)."""
        logger.info(f"⏱️  Waiting up to {timeout_seconds}s for WebSocket price update...")
        received = self._websocket_event.wait(timeout=timeout_seconds)

        with self._lock:
            if received and self._websocket_data:
                logger.info("✅ WebSocket data received, using real-time prices")
                return self._websocket_data
            else:
                logger.info(f"⏰ WebSocket timeout after {timeout_seconds}s, falling back to REST API")
                return None

    def should_sync_this_period(self):
        """Legacy method - now always returns True for initial sync check."""
        with self._lock:
            self._reset_if_new_period()
            return not self._initial_sync_done

    def already_synced_this_period(self):
        """Legacy method - check if initial sync is done."""
        with self._lock:
            self._reset_if_new_period()
            return self._initial_sync_done


# Global sync coordinator
_sync_coordinator = SyncCoordinator()


def get_sync_coordinator():
    """Get the global sync coordinator instance."""
    return _sync_coordinator


def extract_most_recent_actual_interval(forecast_data, timezone_str=None):
    """
    Extract the most recent live pricing from 5-minute forecast data.

    Priority order (most recent first):
    1. CurrentInterval - Real-time price for the current ongoing 5-minute period
    2. ActualInterval - Settled price from the last completed 5-minute period

    This ensures we always use the most up-to-date pricing to catch spikes.

    Args:
        forecast_data: List of price intervals from Amber API (with resolution=5)
        timezone_str: IANA timezone string (e.g., 'Australia/Sydney') for logging

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
        logger.warning("No forecast data provided to extract pricing interval")
        return None

    # PRIORITY 1: Check for CurrentInterval (ongoing period with real-time price)
    current_intervals = [
        interval for interval in forecast_data
        if interval.get('type') == 'CurrentInterval' and interval.get('duration') == 5
    ]

    if current_intervals:
        # Extract prices by channel (general = buy, feedIn = sell)
        result = {'general': None, 'feedIn': None}

        for interval in current_intervals:
            channel = interval.get('channelType')
            if channel in ['general', 'feedIn'] and result[channel] is None:
                result[channel] = interval

            # Stop when we have both channels
            if result['general'] and result['feedIn']:
                break

        if result['general'] or result['feedIn']:
            latest_time = current_intervals[0].get('nemTime', 'unknown')
            general_price = result['general'].get('perKwh') if result['general'] else None
            feedin_price = result['feedIn'].get('perKwh') if result['feedIn'] else None

            logger.info(f"Using CurrentInterval (real-time price) at {latest_time}")
            if general_price is not None:
                logger.info(f"  - General (buy): {general_price:.2f}¢/kWh")
            if feedin_price is not None:
                logger.info(f"  - FeedIn (sell): {feedin_price:.2f}¢/kWh")

            return result

    # PRIORITY 2: Fall back to ActualInterval (last completed period)
    actual_intervals = [
        interval for interval in forecast_data
        if interval.get('type') == 'ActualInterval' and interval.get('duration') == 5
    ]

    if not actual_intervals:
        logger.warning("No 5-minute CurrentInterval or ActualInterval found - may be too early in period")
        return None

    # Sort by nemTime descending to get most recent
    try:
        actual_intervals.sort(
            key=lambda x: datetime.fromisoformat(x.get('nemTime', '').replace('Z', '+00:00')),
            reverse=True
        )
    except Exception as e:
        logger.error(f"Error sorting ActualIntervals by time: {e}")
        return None

    # Extract prices by channel (general = buy, feedIn = sell)
    result = {'general': None, 'feedIn': None}

    for interval in actual_intervals:
        channel = interval.get('channelType')
        if channel in ['general', 'feedIn'] and result[channel] is None:
            result[channel] = interval

        # Stop when we have both channels from the same timestamp
        if result['general'] and result['feedIn']:
            break

    # Log what we found
    if result['general'] or result['feedIn']:
        latest_time = actual_intervals[0].get('nemTime', 'unknown')
        general_price = result['general'].get('perKwh') if result['general'] else None
        feedin_price = result['feedIn'].get('perKwh') if result['feedIn'] else None

        logger.info(f"Using ActualInterval (last completed period) at {latest_time}")
        if general_price is not None:
            logger.info(f"  - General (buy): {general_price:.2f}¢/kWh")
        if feedin_price is not None:
            logger.info(f"  - FeedIn (sell): {feedin_price:.2f}¢/kWh")

        return result
    else:
        logger.warning("ActualIntervals found but no valid channel data")
        return None


def sync_initial_forecast():
    """
    STAGE 1 (0s): Sync immediately at start of 5-min period using forecast price.

    This gets the predicted price to Tesla ASAP at the start of each period.
    Later stages will re-sync if the actual price differs from forecast.
    """
    if not _sync_coordinator.should_do_initial_sync():
        logger.info("⏭️  Initial forecast sync already done this period")
        return

    logger.info("🚀 Stage 1: Initial forecast sync at start of period")
    _sync_all_users_internal(None, sync_mode='initial_forecast')
    _sync_coordinator.mark_initial_sync_done()


def sync_all_users_with_websocket_data(websocket_data):
    """
    STAGE 2 (WebSocket): Re-sync only if price differs from what we synced.

    Called by WebSocket callback when new price data arrives.
    Compares with last synced price and only re-syncs if difference > threshold.
    """
    logger.info("📡 Stage 2: WebSocket price received - checking if re-sync needed")
    _sync_all_users_internal(websocket_data, sync_mode='websocket_update')


def sync_rest_api_check(check_name="fallback"):
    """
    STAGE 3/4 (35s/60s): Check REST API and re-sync if price differs.

    Called at 35s and 60s as fallback if WebSocket hasn't delivered.
    Fetches current price from REST API and compares with last synced price.

    Args:
        check_name: Label for logging (e.g., "35s check", "60s final")
    """
    if _sync_coordinator.has_websocket_delivered():
        logger.info(f"⏭️  REST API {check_name}: WebSocket already delivered this period, skipping")
        return

    logger.info(f"⏰ Stage 3/4: REST API {check_name} - checking if re-sync needed")
    _sync_all_users_internal(None, sync_mode='rest_api_check')


def sync_all_users():
    """
    LEGACY: Cron fallback sync (now calls sync_rest_api_check).
    Kept for backwards compatibility.
    """
    sync_rest_api_check(check_name="legacy fallback")


def _sync_all_users_internal(websocket_data, sync_mode='initial_forecast'):
    """
    Internal sync logic with smart price-aware re-sync.

    Args:
        websocket_data: Price data from WebSocket (or None to fetch from REST API)
        sync_mode: One of:
            - 'initial_forecast': Always sync, record the price (Stage 1)
            - 'websocket_update': Re-sync only if price differs (Stage 2)
            - 'rest_api_check': Check REST API and re-sync if differs (Stage 3/4)
    """
    from app import db

    logger.info("=== Starting automatic TOU sync for all users ===")

    # Import here to avoid circular imports
    users = User.query.all()

    if not users:
        logger.info("No users found to sync")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Skip users who have disabled syncing
            if not user.sync_enabled:
                logger.debug(f"Skipping user {user.email} - syncing disabled")
                continue

            # Skip users who have force discharge active - don't overwrite the discharge tariff
            if getattr(user, 'manual_discharge_active', False):
                expires_at = getattr(user, 'manual_discharge_expires_at', None)
                if expires_at:
                    # Make expires_at timezone-aware if it's naive (SQLite stores naive datetimes)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    remaining = (expires_at - now).total_seconds() / 60
                    logger.info(f"⏭️  Skipping user {user.email} - Force discharge active ({remaining:.1f} min remaining)")
                else:
                    logger.info(f"⏭️  Skipping user {user.email} - Force discharge active")
                continue

            # Skip users who have force charge active - don't overwrite the charge tariff
            if getattr(user, 'manual_charge_active', False):
                expires_at = getattr(user, 'manual_charge_expires_at', None)
                if expires_at:
                    # Make expires_at timezone-aware if it's naive (SQLite stores naive datetimes)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    remaining = (expires_at - now).total_seconds() / 60
                    logger.info(f"⏭️  Skipping user {user.email} - Force charge active ({remaining:.1f} min remaining)")
                else:
                    logger.info(f"⏭️  Skipping user {user.email} - Force charge active")
                continue

            # Determine if user is using AEMO-only mode
            use_aemo = (
                user.electricity_provider == 'flow_power' and
                user.flow_power_price_source == 'aemo'
            )

            # Skip users without required configuration
            if not use_aemo and not user.amber_api_token_encrypted:
                logger.debug(f"Skipping user {user.email} - no Amber token (and not AEMO mode)")
                continue

            if not user.teslemetry_api_key_encrypted and not user.fleet_api_access_token_encrypted:
                logger.debug(f"Skipping user {user.email} - no Tesla API token")
                continue

            if not user.tesla_energy_site_id:
                logger.debug(f"Skipping user {user.email} - no Tesla site ID")
                continue

            if use_aemo and not user.flow_power_state:
                logger.debug(f"Skipping user {user.email} - AEMO mode but no region configured")
                continue

            # Settled Prices Only mode: Skip initial forecast sync (Stage 1)
            # Only sync when WebSocket or REST API delivers actual/settled prices (Stage 2/3/4)
            if sync_mode == 'initial_forecast' and getattr(user, 'settled_prices_only', False) and not use_aemo:
                logger.info(f"⏭️  Skipping initial forecast sync for {user.email} - settled prices only mode enabled")
                continue

            logger.info(f"Syncing schedule for user: {user.email} (price source: {'AEMO' if use_aemo else 'Amber'})")

            # Get API clients
            amber_client = get_amber_client(user) if not use_aemo else None
            tesla_client = get_tesla_client(user)

            if not use_aemo and not amber_client:
                logger.warning(f"Failed to get Amber client for user {user.email}")
                error_count += 1
                continue

            if not tesla_client:
                logger.warning(f"Failed to get Tesla client for user {user.email}")
                error_count += 1
                continue

            # Step 1: Get current interval price from WebSocket (real-time) or REST API fallback
            # WebSocket is PRIMARY source for current price, REST API is fallback if timeout
            # Note: AEMO mode doesn't have WebSocket - uses forecast data only
            current_actual_interval = None

            # Track prices for this user to compare later
            general_price = None
            feedin_price = None

            if use_aemo:
                # AEMO mode: No WebSocket, use AEMO API for forecast
                # Current price will be derived from the first forecast interval
                logger.info(f"📊 AEMO mode - fetching forecast from AEMO for region {user.flow_power_state}")
            elif websocket_data:
                # WebSocket data received within 60s - use it directly as primary source
                current_actual_interval = websocket_data
                general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
                logger.info(f"✅ Using WebSocket price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
            else:
                # WebSocket timeout - fallback to REST API for current price
                logger.info(f"⏰ Fetching current price from REST API")
                current_prices = amber_client.get_current_prices()

                if current_prices:
                    current_actual_interval = {'general': None, 'feedIn': None}
                    for price in current_prices:
                        channel = price.get('channelType')
                        if channel in ['general', 'feedIn']:
                            current_actual_interval[channel] = price

                    general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                    feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
                    logger.info(f"📡 Using REST API price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
                else:
                    logger.warning(f"No current price data available for {user.email}, proceeding with 30-min forecast only")

            # SMART SYNC: For non-initial syncs, check if price has changed enough to warrant re-sync
            if sync_mode != 'initial_forecast' and not use_aemo:
                if general_price is not None or feedin_price is not None:
                    if not _sync_coordinator.should_resync_for_price(user.id, general_price, feedin_price):
                        logger.info(f"⏭️  Price unchanged for {user.email} - skipping re-sync")
                        success_count += 1
                        continue
                    logger.info(f"🔄 Price changed for {user.email} - proceeding with re-sync")

            # Step 2: Fetch forecast for TOU schedule building
            # Request 96 periods (48 hours) for AEMO to ensure rolling 24h window is fully covered
            if use_aemo:
                # AEMO mode: Get forecast from AEMO API
                aemo_client = AEMOAPIClient()
                forecast_30min = aemo_client.get_price_forecast(user.flow_power_state, periods=96)
                if not forecast_30min:
                    logger.error(f"Failed to fetch AEMO forecast for user {user.email} (region: {user.flow_power_state})")
                    error_count += 1
                    continue
                logger.info(f"✅ AEMO forecast: {len(forecast_30min) // 2} periods for {user.flow_power_state}")
            else:
                # Amber mode: Get forecast from Amber API with 30-min resolution
                forecast_30min = amber_client.get_price_forecast(next_hours=48, resolution=30)
                if not forecast_30min:
                    logger.error(f"Failed to fetch Amber forecast for user {user.email}")
                    error_count += 1
                    continue

            # Fetch Powerwall timezone from site_info
            # This ensures time alignment with the Powerwall's actual location
            powerwall_tz = None
            site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
            if site_info:
                powerwall_tz = site_info.get('installation_time_zone')
                if powerwall_tz:
                    logger.info(f"Using Powerwall timezone: {powerwall_tz}")
                else:
                    logger.warning(f"No installation_time_zone in site_info for {user.email}")
            else:
                logger.warning(f"Failed to fetch site_info for {user.email}")

            # Convert Amber prices to Tesla tariff format using 30-min forecast
            # The current_actual_interval (from 5-min data) will be injected for the current period only
            converter = AmberTariffConverter()
            tariff = converter.convert_amber_to_tesla_tariff(
                forecast_30min,
                user=user,
                powerwall_timezone=powerwall_tz,
                current_actual_interval=current_actual_interval
            )

            if not tariff:
                logger.error(f"Failed to convert tariff for user {user.email}")
                error_count += 1
                continue

            # Apply Flow Power PEA pricing (works with both AEMO and Amber price sources)
            if user.electricity_provider == 'flow_power':
                # Check if PEA (Price Efficiency Adjustment) is enabled
                pea_enabled = getattr(user, 'pea_enabled', True)  # Default True for Flow Power

                if pea_enabled:
                    # Use Flow Power PEA pricing model: Base Rate + PEA
                    # Works with both AEMO (raw wholesale) and Amber (wholesaleKWHPrice forecast)
                    from app.tariff_converter import apply_flow_power_pea, get_wholesale_lookup

                    base_rate = getattr(user, 'flow_power_base_rate', 34.0) or 34.0
                    custom_pea = getattr(user, 'pea_custom_value', None)

                    # Build wholesale price lookup from forecast data
                    # get_wholesale_lookup() handles both AEMO and Amber data formats
                    wholesale_prices = get_wholesale_lookup(forecast_30min)

                    price_source = user.flow_power_price_source or 'amber'
                    logger.info(f"Applying Flow Power PEA for {user.email} ({price_source}): base_rate={base_rate}c, custom_pea={custom_pea}")
                    tariff = apply_flow_power_pea(tariff, wholesale_prices, base_rate, custom_pea)
                elif user.flow_power_price_source == 'aemo':
                    # PEA disabled + AEMO: fall back to network tariff calculation
                    # (Amber prices already include network fees, no fallback needed)
                    from app.tariff_converter import apply_network_tariff
                    logger.info(f"Applying network tariff to AEMO wholesale prices for {user.email} (PEA disabled)")
                    tariff = apply_network_tariff(tariff, user)

            # Apply Flow Power export rates if user is on Flow Power
            if user.electricity_provider == 'flow_power' and user.flow_power_state:
                from app.tariff_converter import apply_flow_power_export
                logger.info(f"Applying Flow Power export rates for {user.email} (state: {user.flow_power_state})")
                tariff = apply_flow_power_export(tariff, user.flow_power_state)

            # Apply export price boost for Amber users (if enabled)
            if user.electricity_provider == 'amber' and getattr(user, 'export_boost_enabled', False):
                from app.tariff_converter import apply_export_boost
                offset = getattr(user, 'export_price_offset', 0) or 0
                min_price = getattr(user, 'export_min_price', 0) or 0
                boost_start = getattr(user, 'export_boost_start', '17:00') or '17:00'
                boost_end = getattr(user, 'export_boost_end', '21:00') or '21:00'
                threshold = getattr(user, 'export_boost_threshold', 0) or 0
                logger.info(f"Applying export boost for {user.email}: offset={offset}c, min={min_price}c, threshold={threshold}c, window={boost_start}-{boost_end}")
                tariff = apply_export_boost(tariff, offset, min_price, boost_start, boost_end, threshold)

            logger.info(f"Applying tariff for {user.email} with {len(tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {}))} rate periods")

            # Deduplication: Check if tariff has changed since last sync
            tariff_hash = get_tariff_hash(tariff)
            if tariff_hash == user.last_tariff_hash:
                logger.info(f"⏭️  Tariff unchanged for {user.email} - skipping sync (prevents duplicate dashboard entries)")
                success_count += 1  # Count as success since current state is correct
                continue

            # Apply tariff to Tesla
            result = tesla_client.set_tariff_rate(
                user.tesla_energy_site_id,
                tariff
            )

            if result:
                logger.info(f"✅ Successfully synced schedule for user {user.email}")

                # Alpha: Force mode toggle for faster Powerwall response
                if getattr(user, 'force_tariff_mode_toggle', False):
                    logger.info(f"🔄 Force mode toggle enabled for {user.email} - switching modes")
                    force_tariff_refresh(tesla_client, user.tesla_energy_site_id, wait_seconds=5)

                # Update user's last_update timestamp and tariff hash
                user.last_update_time = datetime.now(timezone.utc)
                user.last_update_status = f"Auto-sync successful ({sync_mode})"
                user.last_tariff_hash = tariff_hash  # Save hash for deduplication
                db.session.commit()

                # Record the synced price for smart price-change detection
                if general_price is not None or feedin_price is not None:
                    _sync_coordinator.record_synced_price(user.id, general_price, feedin_price)

                # Enforce grid charging setting after TOU sync (counteracts VPP overrides)
                if user.enable_demand_charges:
                    gc_success, gc_action = enforce_grid_charging_for_user(
                        user, tesla_client, db,
                        force_apply=True  # Always force during TOU sync to fight VPP
                    )
                    if gc_success:
                        logger.info(f"🔋 Grid charging enforcement after TOU sync: {gc_action}")
                    else:
                        logger.warning(f"⚠️ Grid charging enforcement failed: {gc_action}")

                success_count += 1
            else:
                logger.error(f"Failed to apply schedule to Tesla for user {user.email}")
                error_count += 1

        except Exception as e:
            logger.error(f"Error syncing schedule for user {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            error_count += 1
            continue

    logger.info(f"=== Automatic sync completed: {success_count} successful, {error_count} errors ===")
    return success_count, error_count


def check_manual_discharge_expiry():
    """
    Check for expired manual discharge modes and auto-restore normal operation.

    This should run every minute to catch expired discharge timers.
    """
    from app import db
    from app.models import User, SavedTOUProfile
    from app.api_clients import get_tesla_client
    import json

    logger.debug("Checking for expired manual discharge modes...")

    # Use naive UTC for SQLite compatibility (SQLite stores naive datetimes)
    now = datetime.utcnow()

    # Find users with active discharge that has expired
    expired_users = User.query.filter(
        User.manual_discharge_active == True,
        User.manual_discharge_expires_at <= now
    ).all()

    for user in expired_users:
        logger.info(f"Manual discharge expired for {user.email} - auto-restoring normal operation")

        try:
            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"No Tesla client available for {user.email} - cannot auto-restore")
                continue

            # Check if user has Amber configured (should sync instead of restore static tariff)
            use_amber_sync = bool(user.amber_api_token_encrypted and user.auto_sync_enabled)

            if use_amber_sync:
                # For Amber users, trigger a sync to get fresh prices
                logger.info(f"Amber user {user.email} - triggering price sync for restore")
                # The sync will happen on the next scheduled sync
            else:
                # Restore saved tariff
                backup_profile = None
                if user.manual_discharge_saved_tariff_id:
                    backup_profile = SavedTOUProfile.query.get(user.manual_discharge_saved_tariff_id)
                else:
                    backup_profile = SavedTOUProfile.query.filter_by(
                        user_id=user.id,
                        is_default=True
                    ).first()

                if backup_profile:
                    tariff = json.loads(backup_profile.tariff_json)
                    result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, tariff)

                    if result:
                        force_tariff_refresh(tesla_client, user.tesla_energy_site_id)
                        logger.info(f"Restored tariff from profile {backup_profile.id} for {user.email}")
                    else:
                        logger.error(f"Failed to restore tariff for {user.email}")

            # Clear discharge state
            user.manual_discharge_active = False
            user.manual_discharge_expires_at = None
            db.session.commit()
            logger.info(f"Manual discharge cleared for {user.email}")

        except Exception as e:
            logger.error(f"Error auto-restoring discharge for {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    if expired_users:
        logger.info(f"Processed {len(expired_users)} expired discharge modes")


def check_manual_charge_expiry():
    """
    Check for expired manual charge modes and auto-restore normal operation.

    This should run every minute to catch expired charge timers.
    """
    from app import db
    from app.models import User, SavedTOUProfile
    from app.api_clients import get_tesla_client
    import json

    logger.debug("Checking for expired manual charge modes...")

    # Use naive UTC for SQLite compatibility (SQLite stores naive datetimes)
    now = datetime.utcnow()

    # Find users with active charge that has expired
    expired_users = User.query.filter(
        User.manual_charge_active == True,
        User.manual_charge_expires_at <= now
    ).all()

    for user in expired_users:
        logger.info(f"Manual charge expired for {user.email} - auto-restoring normal operation")

        try:
            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"No Tesla client available for {user.email} - cannot auto-restore")
                continue

            # Check if user has Amber configured (should sync instead of restore static tariff)
            use_amber_sync = bool(user.amber_api_token_encrypted and user.auto_sync_enabled)

            if use_amber_sync:
                # For Amber users, trigger a sync to get fresh prices
                logger.info(f"Amber user {user.email} - triggering price sync for restore")
                # The sync will happen on the next scheduled sync
            else:
                # Restore saved tariff
                backup_profile = None
                if user.manual_charge_saved_tariff_id:
                    backup_profile = SavedTOUProfile.query.get(user.manual_charge_saved_tariff_id)
                else:
                    backup_profile = SavedTOUProfile.query.filter_by(
                        user_id=user.id,
                        is_default=True
                    ).first()

                if backup_profile:
                    tariff = json.loads(backup_profile.tariff_json)
                    result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, tariff)

                    if result:
                        force_tariff_refresh(tesla_client, user.tesla_energy_site_id)
                        logger.info(f"Restored tariff from profile {backup_profile.id} for {user.email}")
                    else:
                        logger.error(f"Failed to restore tariff for {user.email}")

            # Clear charge state
            user.manual_charge_active = False
            user.manual_charge_expires_at = None
            db.session.commit()
            logger.info(f"Manual charge cleared for {user.email}")

        except Exception as e:
            logger.error(f"Error auto-restoring charge for {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    if expired_users:
        logger.info(f"Processed {len(expired_users)} expired charge modes")


def save_price_history_with_websocket_data(websocket_data):
    """
    EVENT-DRIVEN: Save price history with pre-fetched WebSocket data.
    This is the fast path - data already arrived, no waiting.
    """
    _save_price_history_internal(websocket_data)


def save_price_history():
    """
    CRON FALLBACK: Save price history only if WebSocket hasn't delivered yet.

    Runs at :35 seconds into each 5-min period so REST API prices are fresh.
    No wait needed - just fetch directly from REST API.
    """
    from app import db

    # FALLBACK CHECK: Has WebSocket already triggered collection this period?
    coordinator = get_sync_coordinator()
    if coordinator.already_synced_this_period():
        logger.info("⏭️  Cron triggered but WebSocket already saved price history this period - skipping")
        return

    # No wait needed - at 35s into period, REST API prices are fresh
    logger.info("⏰ Cron fallback for price history: fetching from REST API (35s into period)")
    _save_price_history_internal(None)  # None = use REST API


def _normalize_rate(rate, default, name="rate"):
    """
    Normalize network rate to cents/kWh.

    If value is < 0.1, assume it was entered in dollars and convert to cents.

    Threshold rationale:
    - Lowest legitimate DNSP rate is ~0.4c/kWh (NTC6900 off-peak: 0.476c)
    - Values like 0.08 (8c entered as $0.08) need conversion
    - Values like 0.476 (legitimate off-peak) should NOT be converted
    """
    if rate is None:
        return default
    if rate < 0.1:
        # Very likely entered in dollars instead of cents - convert
        corrected = rate * 100
        logger.warning(f"Network {name} appears to be in dollars ({rate}), converting to cents: {corrected:.2f}c/kWh")
        return corrected
    return rate


def _save_price_history_internal(websocket_data):
    """Internal price history logic shared by both event-driven and cron-fallback paths."""
    from app import db
    from app.api_clients import AEMOAPIClient

    logger.info("=== Starting automatic price history collection ===")

    users = User.query.all()

    if not users:
        logger.info("No users found for price history collection")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Check if user is using AEMO price source
            use_aemo = (
                user.electricity_provider == 'flow_power' and
                user.flow_power_price_source == 'aemo'
            )

            prices = None

            if use_aemo:
                # AEMO price source - fetch from AEMO API
                aemo_region = user.flow_power_state
                if not aemo_region:
                    logger.debug(f"Skipping user {user.email} - AEMO configured but no region set")
                    continue

                logger.debug(f"Collecting AEMO price history for user: {user.email} (region: {aemo_region})")

                aemo_client = AEMOAPIClient()
                price_data = aemo_client.get_region_price(aemo_region)

                if not price_data:
                    logger.warning(f"Failed to fetch AEMO price for user {user.email}")
                    error_count += 1
                    continue

                # Calculate network tariff with normalization
                # Values may be stored in dollars (0.08) instead of cents (8) - normalize them
                now = datetime.now()
                hour, minute = now.hour, now.minute

                network_tariff_type = user.network_tariff_type or 'flat'
                if network_tariff_type == 'flat':
                    network_charge_cents = _normalize_rate(user.network_flat_rate, 8.0, "flat_rate")
                else:
                    time_minutes = hour * 60 + minute
                    peak_start = user.network_peak_start or '16:00'
                    peak_end = user.network_peak_end or '21:00'
                    offpeak_start = user.network_offpeak_start or '10:00'
                    offpeak_end = user.network_offpeak_end or '15:00'

                    peak_start_mins = int(peak_start.split(':')[0]) * 60 + int(peak_start.split(':')[1])
                    peak_end_mins = int(peak_end.split(':')[0]) * 60 + int(peak_end.split(':')[1])
                    offpeak_start_mins = int(offpeak_start.split(':')[0]) * 60 + int(offpeak_start.split(':')[1])
                    offpeak_end_mins = int(offpeak_end.split(':')[0]) * 60 + int(offpeak_end.split(':')[1])

                    if peak_start_mins <= time_minutes < peak_end_mins:
                        network_charge_cents = _normalize_rate(user.network_peak_rate, 15.0, "peak_rate")
                    elif offpeak_start_mins <= time_minutes < offpeak_end_mins:
                        network_charge_cents = _normalize_rate(user.network_offpeak_rate, 2.0, "offpeak_rate")
                    else:
                        network_charge_cents = _normalize_rate(user.network_shoulder_rate, 5.0, "shoulder_rate")

                network_other_fees = _normalize_rate(user.network_other_fees, 1.5, "other_fees")
                network_include_gst = user.network_include_gst if user.network_include_gst is not None else True

                total_network_cents = network_charge_cents + network_other_fees
                if network_include_gst:
                    total_network_cents = total_network_cents * 1.10

                # AEMO price is in $/MWh, convert to c/kWh
                wholesale_cents = price_data.get('price', 0) / 10
                total_price_cents = wholesale_cents + total_network_cents

                # Calculate feed-in price (Happy Hour check)
                feedin_cents = 0
                if (17 * 60 + 30) <= (hour * 60 + minute) < (19 * 60 + 30):
                    from app.tariff_converter import FLOW_POWER_EXPORT_RATES
                    export_rate = FLOW_POWER_EXPORT_RATES.get(aemo_region, 0.45)
                    feedin_cents = export_rate * 100

                # Format as Amber-compatible price records
                nem_time = datetime.now(timezone.utc)
                prices = [
                    {
                        'channelType': 'general',
                        'perKwh': round(total_price_cents, 2),
                        'wholesaleKWHPrice': round(wholesale_cents, 4),
                        'networkKWHPrice': round(total_network_cents, 4),
                        'nemTime': nem_time.isoformat(),
                        'forecast': False,
                        'source': 'AEMO',
                    },
                    {
                        'channelType': 'feedIn',
                        'perKwh': round(feedin_cents, 2),
                        'nemTime': nem_time.isoformat(),
                        'forecast': False,
                        'source': 'AEMO',
                    }
                ]

                logger.info(f"✅ AEMO price for {user.email}: wholesale={wholesale_cents:.2f}c + network={total_network_cents:.2f}c = {total_price_cents:.2f}c/kWh")

            else:
                # Amber price source - use existing logic
                if not user.amber_api_token_encrypted:
                    logger.debug(f"Skipping user {user.email} - no Amber token and not using AEMO")
                    continue

                logger.debug(f"Collecting Amber price history for user: {user.email}")

                # Get Amber client
                amber_client = get_amber_client(user)
                if not amber_client:
                    logger.warning(f"Failed to get Amber client for user {user.email}")
                    error_count += 1
                    continue

                # Use provided WebSocket data (already fetched by caller)
                if websocket_data:
                    # WebSocket data received within 60s - convert to list format for price history
                    prices = []
                    if websocket_data.get('general'):
                        prices.append(websocket_data['general'])
                    if websocket_data.get('feedIn'):
                        prices.append(websocket_data['feedIn'])

                    general_price = websocket_data.get('general', {}).get('perKwh') if websocket_data.get('general') else None
                    feedin_price = websocket_data.get('feedIn', {}).get('perKwh') if websocket_data.get('feedIn') else None
                    logger.info(f"✅ Using WebSocket price for history: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
                else:
                    # WebSocket timeout - fallback to REST API
                    logger.info(f"⏰ WebSocket timeout - using REST API fallback for price history")
                    prices = amber_client.get_current_prices()

            if not prices:
                logger.warning(f"No current prices available for user {user.email}")
                error_count += 1
                continue

            # Save prices to database
            records_saved = 0
            for price_data in prices:
                try:
                    # Parse NEM time
                    nem_time = datetime.fromisoformat(price_data['nemTime'].replace('Z', '+00:00'))

                    # Check if we already have this exact price record (avoid duplicates)
                    existing = PriceRecord.query.filter_by(
                        user_id=user.id,
                        nem_time=nem_time,
                        channel_type=price_data.get('channelType')
                    ).first()

                    if existing:
                        logger.debug(f"Price record already exists for {user.email} at {nem_time}")
                        continue

                    # Create new price record
                    record = PriceRecord(
                        user_id=user.id,
                        per_kwh=price_data.get('perKwh'),
                        spot_per_kwh=price_data.get('spotPerKwh'),
                        wholesale_kwh_price=price_data.get('wholesaleKWHPrice'),
                        network_kwh_price=price_data.get('networkKWHPrice'),
                        market_kwh_price=price_data.get('marketKWHPrice'),
                        green_kwh_price=price_data.get('greenKWHPrice'),
                        channel_type=price_data.get('channelType'),
                        forecast=price_data.get('forecast', False),
                        nem_time=nem_time,
                        spike_status=price_data.get('spikeStatus'),
                        timestamp=datetime.now(timezone.utc)
                    )
                    db.session.add(record)
                    records_saved += 1

                except Exception as e:
                    logger.error(f"Error saving individual price record for {user.email}: {e}")
                    continue

            # Commit all records for this user
            if records_saved > 0:
                db.session.commit()
                logger.info(f"✅ Saved {records_saved} price records for user {user.email}")
                success_count += 1
            else:
                logger.debug(f"No new price records to save for user {user.email}")

        except Exception as e:
            logger.error(f"Error collecting price history for user {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            db.session.rollback()
            error_count += 1
            continue

    logger.info(f"=== Price history collection completed: {success_count} users successful, {error_count} errors ===")
    return success_count, error_count


def save_energy_usage():
    """
    Automatically save Tesla Powerwall energy usage data to database for historical tracking
    This runs periodically in the background to capture solar, grid, battery, and load power
    """
    from app import db

    logger.debug("=== Starting automatic energy usage collection ===")

    users = User.query.all()

    if not users:
        logger.debug("No users found for energy usage collection")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Skip users without Tesla configuration
            if not user.tesla_energy_site_id:
                logger.debug(f"Skipping user {user.email} - no Tesla site ID")
                continue

            logger.debug(f"Collecting energy usage for user: {user.email}")

            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.warning(f"Failed to get Tesla client for user {user.email}")
                error_count += 1
                continue

            # Get site status (contains power flow data)
            site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
            if not site_status:
                logger.warning(f"No site status available for user {user.email}")
                error_count += 1
                continue

            # Extract power data (in watts)
            solar_power = site_status.get('solar_power', 0.0)
            battery_power = site_status.get('battery_power', 0.0)
            grid_power = site_status.get('grid_power', 0.0)
            load_power = site_status.get('load_power', 0.0)
            battery_level = site_status.get('percentage_charged', 0.0)

            # Create energy record
            record = EnergyRecord(
                user_id=user.id,
                solar_power=solar_power,
                battery_power=battery_power,
                grid_power=grid_power,
                load_power=load_power,
                battery_level=battery_level,
                timestamp=datetime.now(timezone.utc)
            )

            db.session.add(record)
            db.session.commit()

            logger.debug(f"✅ Saved energy record for user {user.email}: Solar={solar_power}W Grid={grid_power}W Battery={battery_power}W Load={load_power}W")
            success_count += 1

        except Exception as e:
            logger.error(f"Error collecting energy usage for user {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            db.session.rollback()
            error_count += 1
            continue

    logger.debug(f"=== Energy usage collection completed: {success_count} users successful, {error_count} errors ===")
    return success_count, error_count


def monitor_aemo_prices():
    """
    Monitor AEMO NEM wholesale electricity prices and trigger spike mode when threshold exceeded

    Flow:
    1. Check AEMO price for user's region
    2. If price >= threshold AND not in spike mode:
       - Save current Tesla tariff as backup
       - Upload spike tariff (very high sell rates to encourage export)
       - Mark user as in_spike_mode
    3. If price < threshold AND in spike mode:
       - Restore saved tariff from backup
       - Mark user as not in_spike_mode
    """
    from app import db

    logger.info("=== Starting AEMO price monitoring ===")

    users = User.query.filter_by(aemo_spike_detection_enabled=True).all()

    if not users:
        logger.debug("No users with AEMO spike detection enabled")
        return

    # Initialize AEMO client (no auth required)
    aemo_client = AEMOAPIClient()

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Skip users with Amber auto sync enabled to avoid conflicts
            if user.sync_enabled:
                logger.debug(f"Skipping AEMO spike detection for {user.email} - Amber auto sync is enabled")
                continue

            # Validate user configuration
            if not user.aemo_region:
                logger.warning(f"User {user.email} has AEMO enabled but no region configured")
                continue

            if not user.tesla_energy_site_id or not user.teslemetry_api_key_encrypted:
                logger.warning(f"User {user.email} has AEMO enabled but missing Tesla configuration")
                continue

            logger.info(f"Checking AEMO prices for user: {user.email} (Region: {user.aemo_region})")

            # Check current price vs threshold
            is_spike, current_price, price_data = aemo_client.check_price_spike(
                user.aemo_region,
                user.aemo_spike_threshold or 300.0
            )

            if current_price is None:
                logger.error(f"Failed to fetch AEMO price for {user.email}")
                error_count += 1
                continue

            # Update user's last check data
            user.aemo_last_check = datetime.now(timezone.utc)
            user.aemo_last_price = current_price

            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"Failed to get Tesla client for {user.email}")
                error_count += 1
                continue

            # SPIKE DETECTED - Enter spike mode
            if is_spike and not user.aemo_in_spike_mode:
                logger.warning(f"🚨 SPIKE DETECTED for {user.email}: ${current_price}/MWh >= ${user.aemo_spike_threshold}/MWh")

                # Check if battery is already exporting - if so, don't interfere
                logger.info(f"Checking battery status to avoid disrupting existing export for {user.email}")
                site_status = tesla_client.get_site_status(user.tesla_energy_site_id)

                if site_status:
                    solar_power = site_status.get('solar_power', 0.0)
                    battery_power = site_status.get('battery_power', 0.0)
                    load_power = site_status.get('load_power', 0.0)
                    grid_power = site_status.get('grid_power', 0.0)

                    logger.info(f"Current power flow: Solar={solar_power}W, Battery={battery_power}W, Load={load_power}W, Grid={grid_power}W")

                    # Check if BATTERY is exporting to grid (not just solar)
                    # Battery exports when: battery_power > (load - solar)
                    # This accounts for solar already covering some/all of the load
                    net_load_after_solar = max(0, load_power - solar_power)
                    battery_export = battery_power - net_load_after_solar

                    logger.info(f"Net load after solar: {net_load_after_solar}W, Battery export: {battery_export}W")

                    # If battery is already exporting >100W to grid, skip spike tariff upload
                    if battery_export > 100:
                        logger.info(f"⚡ Battery already exporting {battery_export}W to grid - skipping spike tariff upload to avoid disruption")
                        logger.info(f"Powerwall is already optimizing correctly during spike event")

                        # Reference default tariff as restore point (in case tariff changes during spike)
                        default_profile = SavedTOUProfile.query.filter_by(
                            user_id=user.id,
                            is_default=True
                        ).first()

                        if default_profile:
                            user.aemo_saved_tariff_id = default_profile.id
                            logger.info(f"Referenced default tariff ID {default_profile.id} ({default_profile.name}) as restore point")
                        else:
                            logger.warning(f"No default tariff found for {user.email} - no restore point set")

                        # Still mark as in spike mode so we don't keep checking
                        user.aemo_in_spike_mode = True
                        user.aemo_spike_start_time = datetime.now(timezone.utc)
                        db.session.commit()
                        success_count += 1
                        continue  # Skip to next user

                # Step 1: Check for default tariff or save current tariff as backup
                # First check if a default tariff already exists
                default_profile = SavedTOUProfile.query.filter_by(
                    user_id=user.id,
                    is_default=True
                ).first()

                if default_profile:
                    # Use existing default tariff as backup reference
                    user.aemo_saved_tariff_id = default_profile.id
                    logger.info(f"✅ Using existing default tariff ID {default_profile.id} ({default_profile.name}) as backup reference")
                else:
                    # No default exists - save current tariff and mark as default
                    logger.info(f"No default tariff found - saving current Tesla tariff as default for {user.email}")
                    current_tariff = tesla_client.get_current_tariff(user.tesla_energy_site_id)

                    if current_tariff:
                        backup_profile = SavedTOUProfile(
                            user_id=user.id,
                            name=f"Default Tariff (Saved {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})",
                            description=f"Automatically saved as default before AEMO spike at ${current_price}/MWh",
                            source_type='tesla',
                            tariff_name=current_tariff.get('name', 'Unknown'),
                            utility=current_tariff.get('utility', 'Unknown'),
                            tariff_json=json.dumps(current_tariff),
                            created_at=datetime.now(timezone.utc),
                            fetched_from_tesla_at=datetime.now(timezone.utc),
                            is_default=True  # Mark as default
                        )
                        db.session.add(backup_profile)
                        db.session.flush()
                        user.aemo_saved_tariff_id = backup_profile.id
                        logger.info(f"✅ Saved current tariff as default with ID {backup_profile.id}")
                    else:
                        logger.error(f"Failed to fetch current tariff for backup - {user.email}")

                # Step 2: Save current operation mode and switch to autonomous
                logger.info(f"Getting current operation mode for {user.email}")
                current_mode = tesla_client.get_operation_mode(user.tesla_energy_site_id)

                if current_mode:
                    user.aemo_pre_spike_operation_mode = current_mode
                    logger.info(f"💾 Saved pre-spike operation mode: {current_mode}")

                    # Only switch to autonomous if not already in it
                    if current_mode != 'autonomous':
                        logger.info(f"Switching {user.email} to autonomous mode for spike")
                        mode_result = tesla_client.set_operation_mode(user.tesla_energy_site_id, 'autonomous')
                        if not mode_result:
                            logger.error(f"Failed to switch {user.email} to autonomous mode - continuing anyway")
                        else:
                            logger.info(f"✅ Switched to autonomous mode")
                    else:
                        logger.info(f"Already in autonomous mode, no switch needed")
                else:
                    logger.warning(f"Could not get current operation mode - will default to autonomous during restore")
                    user.aemo_pre_spike_operation_mode = None

                # Step 3: Create and upload spike tariff
                logger.info(f"Creating spike tariff for {user.email}")
                spike_tariff = create_spike_tariff(current_price)

                result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, spike_tariff)

                if result:
                    user.aemo_in_spike_mode = True
                    user.aemo_spike_start_time = datetime.now(timezone.utc)
                    logger.info(f"✅ Entered spike mode for {user.email} - uploaded spike tariff")

                    # Force Powerwall to immediately apply the new spike tariff
                    logger.info(f"Forcing Powerwall to apply spike tariff for {user.email}")
                    force_tariff_refresh(tesla_client, user.tesla_energy_site_id)

                    success_count += 1
                else:
                    logger.error(f"Failed to upload spike tariff for {user.email}")
                    error_count += 1

            # NO SPIKE - Exit spike mode if currently in it
            elif not is_spike and user.aemo_in_spike_mode:
                # Skip automatic restore during manual test mode
                if user.aemo_spike_test_mode:
                    logger.info(f"⏭️ Skipping automatic restore for {user.email} - in manual test mode")
                    success_count += 1
                    continue

                logger.info(f"✅ Price normalized for {user.email}: ${current_price}/MWh < ${user.aemo_spike_threshold}/MWh")

                # Restore saved tariff
                if user.aemo_saved_tariff_id:
                    logger.info(f"Restoring backup tariff ID {user.aemo_saved_tariff_id} for {user.email}")
                    backup_profile = SavedTOUProfile.query.get(user.aemo_saved_tariff_id)

                    if backup_profile:
                        tariff = json.loads(backup_profile.tariff_json)

                        # Step 1: Switch to self_consumption mode FIRST
                        logger.info(f"Automatic restore: Switching {user.email} to self_consumption mode before tariff upload")
                        mode_result = tesla_client.set_operation_mode(user.tesla_energy_site_id, 'self_consumption')
                        if not mode_result:
                            logger.error(f"Automatic restore: Failed to switch {user.email} to self_consumption mode")
                            error_count += 1
                            continue

                        # Step 2: Upload tariff while in self_consumption mode
                        logger.info(f"Automatic restore: Uploading tariff for {user.email} while in self_consumption mode")
                        result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, tariff)

                        if result:
                            user.aemo_in_spike_mode = False
                            user.aemo_spike_start_time = None
                            backup_profile.last_restored_at = datetime.now(timezone.utc)
                            logger.info(f"✅ Automatic restore: Tariff uploaded for {user.email}")

                            # Step 3: Wait 60 seconds for Tesla to process
                            import time
                            logger.info(f"Automatic restore: Waiting 60 seconds for {user.email} to process tariff change...")
                            time.sleep(60)

                            # Step 4: Restore original operation mode
                            restore_mode = user.aemo_pre_spike_operation_mode or 'autonomous'
                            logger.info(f"Automatic restore: Switching {user.email} back to {restore_mode} mode")
                            mode_restore_result = tesla_client.set_operation_mode(user.tesla_energy_site_id, restore_mode)

                            if mode_restore_result:
                                logger.info(f"✅ Automatic restore completed for {user.email} - Restored to {restore_mode} mode")
                                user.aemo_pre_spike_operation_mode = None  # Clear saved mode
                                success_count += 1
                            else:
                                logger.error(f"❌ Failed to switch {user.email} back to {restore_mode} mode")
                                error_count += 1
                        else:
                            logger.error(f"Failed to restore backup tariff for {user.email}")
                            error_count += 1
                    else:
                        logger.error(f"Backup tariff ID {user.aemo_saved_tariff_id} not found for {user.email}")
                        user.aemo_in_spike_mode = False  # Exit spike mode anyway
                        error_count += 1
                else:
                    logger.warning(f"No backup tariff saved for {user.email}, exiting spike mode anyway")
                    user.aemo_in_spike_mode = False
                    success_count += 1

            # ONGOING SPIKE or ONGOING NORMAL - No action needed
            else:
                if is_spike:
                    logger.debug(f"Price still spiking for {user.email}: ${current_price}/MWh (in spike mode)")
                else:
                    logger.debug(f"Price normal for {user.email}: ${current_price}/MWh (not in spike mode)")
                success_count += 1

            # Commit user updates
            db.session.commit()

        except Exception as e:
            logger.error(f"Error monitoring AEMO price for user {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            db.session.rollback()
            error_count += 1
            continue

    logger.info(f"=== AEMO monitoring completed: {success_count} users successful, {error_count} errors ===")
    return success_count, error_count


def force_tariff_refresh(tesla_client, site_id, wait_seconds=30):
    """
    Force Powerwall to immediately apply new tariff by toggling operation mode

    The Powerwall can take several minutes to recognize tariff changes.
    Switching to self_consumption then back to autonomous forces immediate recalculation.

    Args:
        tesla_client: TeslemetryAPIClient instance
        site_id: Energy site ID
        wait_seconds: Seconds to wait in self_consumption mode (default: 30)
                     Use 60 for restore operations, 30 for spike activation

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        import time

        logger.info(f"Forcing tariff refresh for site {site_id} by toggling operation mode")

        # Step 1: Switch to self_consumption mode
        logger.info("Switching to self_consumption mode...")
        result1 = tesla_client.set_operation_mode(site_id, 'self_consumption')

        if not result1:
            logger.warning("Failed to switch to self_consumption mode")
            return False

        # Step 2: Wait for Tesla to detect the mode change
        # Tesla needs time to recognize and process the mode change
        logger.info(f"Waiting {wait_seconds} seconds for Tesla to detect mode change...")
        time.sleep(wait_seconds)

        # Step 3: Switch back to autonomous mode (TOU optimization)
        logger.info("Switching back to autonomous mode...")
        result2 = tesla_client.set_operation_mode(site_id, 'autonomous')

        if not result2:
            logger.warning("Failed to switch back to autonomous mode")
            return False

        logger.info("✅ Successfully toggled operation mode - tariff should apply immediately")
        return True

    except Exception as e:
        logger.error(f"Error forcing tariff refresh: {e}")
        return False


def create_spike_tariff(current_aemo_price_mwh):
    """
    Create a Tesla tariff optimized for exporting during price spikes

    Args:
        current_aemo_price_mwh: Current AEMO price in $/MWh (e.g., 500)

    Returns:
        dict: Tesla tariff JSON with very high sell rates
    """
    # Convert $/MWh to $/kWh (divide by 1000)
    # Make sell rate EXTREMELY attractive (way higher than actual spike price)
    sell_rate_spike = (current_aemo_price_mwh / 1000.0) * 3.0  # 3x markup - very high!

    # Normal buy rate for non-spike periods (typical grid price)
    # Powerwall needs to know it can recharge cheaply later
    buy_rate_normal = 0.30  # 30c/kWh - typical Australian grid price

    # Sell rate for non-spike periods (typical feed-in)
    sell_rate_normal = 0.08  # 8c/kWh - typical feed-in tariff

    logger.info(f"Creating spike tariff: Spike sell=${sell_rate_spike}/kWh, Normal buy=${buy_rate_normal}/kWh, Normal sell=${sell_rate_normal}/kWh (based on ${current_aemo_price_mwh}/MWh)")

    # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
    buy_rates = {}
    sell_rates = {}
    tou_periods = {}

    # Get current time to determine spike window
    now = datetime.now()
    current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

    # Spike window: current period + next 2 hours (4 x 30-min periods)
    # Short window creates urgency for Powerwall to export NOW
    spike_window_periods = 4
    spike_start = current_period_index
    spike_end = (current_period_index + spike_window_periods) % 48

    logger.info(f"Spike window: periods {spike_start} to {spike_end} (current time: {now.hour:02d}:{now.minute:02d})")

    for i in range(48):
        hour = i // 2
        minute = 30 if i % 2 else 0
        period_name = f"{hour:02d}:{minute:02d}"

        # Check if this period is in the spike window
        is_spike_period = False
        if spike_start < spike_end:
            is_spike_period = spike_start <= i < spike_end
        else:  # Wrap around midnight
            is_spike_period = i >= spike_start or i < spike_end

        # Set rates based on whether we're in spike window
        if is_spike_period:
            # During spike: normal buy price, VERY HIGH sell price
            buy_rates[period_name] = buy_rate_normal
            sell_rates[period_name] = sell_rate_spike
        else:
            # After spike: normal buy and sell prices
            buy_rates[period_name] = buy_rate_normal
            sell_rates[period_name] = sell_rate_normal

        # Calculate end time (30 minutes later)
        if minute == 0:
            to_hour = hour
            to_minute = 30
        else:  # minute == 30
            to_hour = (hour + 1) % 24  # Wrap around at midnight
            to_minute = 0

        # TOU period definition for seasons
        # Each period needs a "periods" array wrapper
        tou_periods[period_name] = {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": hour,
                "fromMinute": minute,
                "toHour": to_hour,
                "toMinute": to_minute
            }]
        }

    # Create Tesla tariff structure with separate buy and sell tariffs
    tariff = {
        "name": f"AEMO Spike - ${current_aemo_price_mwh}/MWh",
        "utility": "AEMO",
        "code": f"SPIKE_{int(current_aemo_price_mwh)}",
        "currency": "AUD",
        "daily_charges": [{"name": "Supply Charge"}],
        "demand_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {},
            "Winter": {}
        },
        "energy_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {"rates": buy_rates},
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
            "name": f"AEMO Spike Feed-in - ${current_aemo_price_mwh}/MWh",
            "utility": "AEMO",
            "daily_charges": [{"name": "Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {},
                "Winter": {}
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": sell_rates},
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

    return tariff


def create_discharge_tariff(duration_minutes=30):
    """
    Create a Tesla tariff optimized for forced battery discharge.

    Uses very high sell rates ($10/kWh) to encourage maximum battery export
    and moderate buy rates (30c/kWh) to discourage import.

    Args:
        duration_minutes: Duration of discharge window in minutes

    Returns:
        dict: Tesla tariff JSON with high sell rates for discharge
    """
    # Very high sell rate to encourage Powerwall to export all energy
    sell_rate_discharge = 10.00  # $10/kWh - huge incentive to discharge
    sell_rate_normal = 0.08      # 8c/kWh normal feed-in

    # Buy rate to discourage import during discharge
    buy_rate = 0.30  # 30c/kWh

    logger.info(f"Creating discharge tariff: sell=${sell_rate_discharge}/kWh, buy=${buy_rate}/kWh for {duration_minutes} min")

    # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
    buy_rates = {}
    sell_rates = {}
    tou_periods = {}

    # Get current time to determine discharge window
    now = datetime.now()
    current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

    # Calculate how many 30-min periods the discharge covers
    discharge_periods = (duration_minutes + 29) // 30  # Round up
    discharge_start = current_period_index
    discharge_end = (current_period_index + discharge_periods) % 48

    logger.info(f"Discharge window: periods {discharge_start} to {discharge_end} (current time: {now.hour:02d}:{now.minute:02d})")

    for i in range(48):
        hour = i // 2
        minute = 30 if i % 2 else 0
        period_name = f"{hour:02d}:{minute:02d}"

        # Check if this period is in the discharge window
        is_discharge_period = False
        if discharge_start < discharge_end:
            is_discharge_period = discharge_start <= i < discharge_end
        else:  # Wrap around midnight
            is_discharge_period = i >= discharge_start or i < discharge_end

        # Set rates based on whether we're in discharge window
        if is_discharge_period:
            buy_rates[period_name] = buy_rate
            sell_rates[period_name] = sell_rate_discharge
        else:
            buy_rates[period_name] = buy_rate
            sell_rates[period_name] = sell_rate_normal

        # Calculate end time (30 minutes later)
        if minute == 0:
            to_hour = hour
            to_minute = 30
        else:  # minute == 30
            to_hour = (hour + 1) % 24  # Wrap around at midnight
            to_minute = 0

        # TOU period definition for seasons
        tou_periods[period_name] = {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": hour,
                "fromMinute": minute,
                "toHour": to_hour,
                "toMinute": to_minute
            }]
        }

    # Create Tesla tariff structure
    tariff = {
        "name": f"Force Discharge ({duration_minutes}min)",
        "utility": "Tesla Sync",
        "code": f"DISCHARGE_{duration_minutes}",
        "currency": "AUD",
        "daily_charges": [{"name": "Supply Charge"}],
        "demand_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {},
            "Winter": {}
        },
        "energy_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {"rates": buy_rates},
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
            "name": f"Force Discharge Export ({duration_minutes}min)",
            "utility": "Tesla Sync",
            "daily_charges": [{"name": "Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {},
                "Winter": {}
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": sell_rates},
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

    return tariff


def create_charge_tariff(duration_minutes=30):
    """
    Create a Tesla tariff optimized for forced battery charging from grid.

    Uses very low buy rates (0c/kWh) during charge window to encourage charging
    and zero sell rates to prevent export. Outside the window, uses very high
    buy rates ($10/kWh) to discourage charging.

    Args:
        duration_minutes: Duration of charge window in minutes

    Returns:
        dict: Tesla tariff JSON with low buy rates for charging
    """
    # Rates during charge window - free to buy, no sell incentive
    buy_rate_charge = 0.00    # $0/kWh - maximum incentive to charge
    sell_rate_charge = 0.00   # $0/kWh - no incentive to export

    # Rates outside charge window - expensive to buy, no sell
    buy_rate_normal = 10.00   # $10/kWh - huge disincentive to charge
    sell_rate_normal = 0.00   # $0/kWh - no incentive to export

    logger.info(f"Creating charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {duration_minutes} min")

    # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
    buy_rates = {}
    sell_rates = {}
    tou_periods = {}

    # Get current time to determine charge window
    now = datetime.now()
    current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

    # Calculate how many 30-min periods the charge covers
    charge_periods = (duration_minutes + 29) // 30  # Round up
    charge_start = current_period_index
    charge_end = (current_period_index + charge_periods) % 48

    logger.info(f"Charge window: periods {charge_start} to {charge_end} (current time: {now.hour:02d}:{now.minute:02d})")

    for i in range(48):
        hour = i // 2
        minute = 30 if i % 2 else 0
        period_name = f"{hour:02d}:{minute:02d}"

        # Check if this period is in the charge window
        is_charge_period = False
        if charge_start < charge_end:
            is_charge_period = charge_start <= i < charge_end
        else:  # Wrap around midnight
            is_charge_period = i >= charge_start or i < charge_end

        # Set rates based on whether we're in charge window
        if is_charge_period:
            buy_rates[period_name] = buy_rate_charge
            sell_rates[period_name] = sell_rate_charge
        else:
            buy_rates[period_name] = buy_rate_normal
            sell_rates[period_name] = sell_rate_normal

        # Calculate end time (30 minutes later)
        if minute == 0:
            to_hour = hour
            to_minute = 30
        else:  # minute == 30
            to_hour = (hour + 1) % 24  # Wrap around at midnight
            to_minute = 0

        # TOU period definition for seasons
        tou_periods[period_name] = {
            "periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": hour,
                "fromMinute": minute,
                "toHour": to_hour,
                "toMinute": to_minute
            }]
        }

    # Create Tesla tariff structure
    tariff = {
        "name": f"Force Charge ({duration_minutes}min)",
        "utility": "Tesla Sync",
        "code": f"CHARGE_{duration_minutes}",
        "currency": "AUD",
        "daily_charges": [{"name": "Supply Charge"}],
        "demand_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {},
            "Winter": {}
        },
        "energy_charges": {
            "ALL": {"rates": {"ALL": 0}},
            "Summer": {"rates": buy_rates},
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
            "name": f"Force Charge Export ({duration_minutes}min)",
            "utility": "Tesla Sync",
            "daily_charges": [{"name": "Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {},
                "Winter": {}
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": sell_rates},
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

    return tariff


def solar_curtailment_check():
    """
    Monitor Amber export prices and curtail solar export when price is below 1c/kWh

    Flow:
    1. Check Amber feed-in price for users with solar curtailment enabled
    2. If export price < 1c:
       - Set grid export rule to 'never' to prevent exporting at negative prices
       - Workaround for Tesla API bug: Toggle to 'pv_only' then back to 'never' to force apply
    3. If export price >= 1c:
       - Restore normal export (pv_only or battery_ok based on user preferences)
    """
    from app import db
    from app.api_clients import get_amber_client, get_tesla_client

    logger.info("=== Starting solar curtailment check ===")

    users = User.query.filter_by(solar_curtailment_enabled=True).all()

    if not users:
        logger.debug("No users with solar curtailment enabled")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Validate user configuration
            if not user.amber_api_token_encrypted:
                logger.warning(f"User {user.email} has curtailment enabled but no Amber API token")
                continue

            if not user.tesla_energy_site_id or not user.teslemetry_api_key_encrypted:
                logger.warning(f"User {user.email} has curtailment enabled but missing Tesla configuration")
                continue

            logger.info(f"Checking export price for user: {user.email}")

            # Get Amber client and fetch current prices
            amber_client = get_amber_client(user)
            if not amber_client:
                logger.error(f"Failed to get Amber client for {user.email}")
                error_count += 1
                continue

            current_prices = amber_client.get_current_prices()
            if not current_prices:
                logger.error(f"Failed to fetch Amber prices for {user.email}")
                error_count += 1
                continue

            # Get feed-in (export) price
            feedin_price = None
            for price_data in current_prices:
                if price_data.get('channelType') == 'feedIn':
                    feedin_price = price_data.get('perKwh', 0)
                    break

            if feedin_price is None:
                logger.warning(f"No feed-in price found for {user.email}")
                error_count += 1
                continue

            # Amber returns feed-in prices as NEGATIVE when you're paid to export
            # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
            # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
            # So we want to curtail when feedin_price > 0 (user would pay to export)
            export_earnings = -feedin_price  # Convert to positive = earnings per kWh
            logger.info(f"Current feed-in price for {user.email}: {feedin_price}c/kWh (export earnings: {export_earnings}c/kWh)")

            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"Failed to get Tesla client for {user.email}")
                error_count += 1
                continue

            # Get current grid export settings
            current_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
            if not current_settings:
                logger.error(f"Failed to get grid export settings for {user.email}")
                error_count += 1
                continue

            current_export_rule = current_settings.get('customer_preferred_export_rule')

            # Handle VPP users where export rule is derived from components_non_export_configured
            if current_export_rule is None:
                non_export = current_settings.get('components_non_export_configured')
                if non_export is not None:
                    current_export_rule = 'never' if non_export else 'battery_ok'
                    logger.info(f"VPP user {user.email}: derived export_rule='{current_export_rule}' from components_non_export_configured={non_export}")

            # If still None, fall back to cached value from database (but mark as unverified)
            using_cached_rule = False
            if current_export_rule is None and user.current_export_rule:
                current_export_rule = user.current_export_rule
                using_cached_rule = True
                logger.info(f"Using cached export_rule='{current_export_rule}' for {user.email} (API returned None - will verify by applying)")

            logger.info(f"Current export rule for {user.email}: {current_export_rule}")

            # CURTAILMENT LOGIC: Curtail when export earnings < 1c/kWh
            # (i.e., when feedin_price > -1, meaning you earn less than 1c or pay to export)
            if export_earnings < 1:
                logger.info(f"🚫 CURTAILMENT TRIGGERED: Export earnings {export_earnings:.2f}c/kWh (<1c) for {user.email}")

                # If already curtailed AND verified from API, no action needed
                # If using cache, always apply curtailment to be safe (cache may be stale)
                if current_export_rule == 'never' and not using_cached_rule:
                    logger.info(f"✅ Already curtailed (export='never', verified from API) - no action needed for {user.email}")
                else:
                    # Apply curtailment (either not 'never' or using unverified cache)
                    if using_cached_rule:
                        logger.info(f"Applying curtailment (cache says '{current_export_rule}' but unverified) → 'never' for {user.email}")
                    else:
                        logger.info(f"Applying curtailment: '{current_export_rule}' → 'never' for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'never')
                    if not result:
                        logger.error(f"❌ Failed to apply curtailment (set export to 'never') for {user.email}")
                        error_count += 1
                        continue

                    # Verify the change actually took effect by reading back
                    verify_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
                    verified_rule = verify_settings.get('customer_preferred_export_rule') if verify_settings else None
                    if verified_rule is None:
                        # API doesn't return this field - can't verify but not a failure
                        logger.info(f"ℹ️ Cannot verify curtailment (API returns None for export_rule) - operation reported success for {user.email}")
                    elif verified_rule != 'never':
                        logger.warning(f"⚠️ CURTAILMENT VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'never') for {user.email}")
                        logger.warning(f"Full verification response: {verify_settings}")
                    else:
                        logger.info(f"✓ Curtailment verified via read-back: export_rule='{verified_rule}'")

                    logger.info(f"✅ CURTAILMENT APPLIED: Export rule changed '{current_export_rule}' → 'never' for {user.email}")
                    user.current_export_rule = 'never'
                    user.current_export_rule_updated = datetime.utcnow()
                    db.session.commit()

                success_count += 1
                logger.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                logger.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c) for {user.email}")

                # If currently curtailed, restore to battery_ok (allows both solar and battery export)
                if current_export_rule == 'never':
                    logger.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → 'battery_ok' for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'battery_ok')
                    if not result:
                        logger.error(f"❌ Failed to restore from curtailment (set export to 'battery_ok') for {user.email}")
                        error_count += 1
                        continue

                    # Verify the change actually took effect by reading back
                    verify_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
                    verified_rule = verify_settings.get('customer_preferred_export_rule') if verify_settings else None
                    if verified_rule is None:
                        # API doesn't return this field - can't verify but not a failure
                        logger.info(f"ℹ️ Cannot verify restore (API returns None for export_rule) - operation reported success for {user.email}")
                    elif verified_rule != 'battery_ok':
                        logger.warning(f"⚠️ RESTORE VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'battery_ok') for {user.email}")
                        logger.warning(f"Full verification response: {verify_settings}")
                    else:
                        logger.info(f"✓ Restore verified via read-back: export_rule='{verified_rule}'")

                    logger.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → 'battery_ok' for {user.email}")
                    user.current_export_rule = 'battery_ok'
                    user.current_export_rule_updated = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: 'battery_ok')")
                    success_count += 1
                else:
                    logger.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed for {user.email}")
                    logger.info(f"📊 Action summary: No change needed (earnings: {export_earnings:.2f}c/kWh, export: '{current_export_rule}')")
                    success_count += 1

        except Exception as e:
            logger.error(f"❌ Unexpected error in solar curtailment check for {user.email}: {e}", exc_info=True)
            logger.error(f"Error context: Failed during curtailment check - user may have incomplete configuration")
            error_count += 1
            continue

    logger.info(f"=== ✅ Solar curtailment check complete: {success_count} users processed successfully, {error_count} errors ===")


def solar_curtailment_with_websocket_data(prices_data):
    """
    EVENT-DRIVEN: Check solar curtailment using WebSocket price data.

    This is the primary trigger - called immediately when WebSocket receives prices.
    The cron job is just a fallback.

    Args:
        prices_data: Dict with 'general' and/or 'feedIn' price data from WebSocket
    """
    from app import db
    from app.api_clients import get_tesla_client

    logger.info("=== 🌞 Solar curtailment check (WebSocket-triggered) ===")

    # Extract feed-in price from WebSocket data
    feedin_data = prices_data.get('feedIn', {})
    feedin_price = feedin_data.get('perKwh')

    if feedin_price is None:
        logger.warning("No feed-in price in WebSocket data, skipping curtailment check")
        return

    # Amber returns feed-in prices as NEGATIVE when you're paid to export
    # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
    # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
    # So we want to curtail when feedin_price > 0 (user would pay to export)
    export_earnings = -feedin_price  # Convert to positive = earnings per kWh
    logger.info(f"WebSocket feed-in price: {feedin_price}c/kWh (export earnings: {export_earnings}c/kWh)")

    users = User.query.filter_by(solar_curtailment_enabled=True).all()

    if not users:
        logger.debug("No users with solar curtailment enabled")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Validate user configuration
            if not user.tesla_energy_site_id or not user.teslemetry_api_key_encrypted:
                logger.warning(f"User {user.email} has curtailment enabled but missing Tesla configuration")
                continue

            logger.info(f"Checking curtailment for user: {user.email}")

            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"Failed to get Tesla client for {user.email}")
                error_count += 1
                continue

            # Get current grid export settings
            current_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
            if not current_settings:
                logger.error(f"Failed to get grid export settings for {user.email}")
                error_count += 1
                continue

            current_export_rule = current_settings.get('customer_preferred_export_rule')

            # Handle VPP users where export rule is derived from components_non_export_configured
            if current_export_rule is None:
                non_export = current_settings.get('components_non_export_configured')
                if non_export is not None:
                    current_export_rule = 'never' if non_export else 'battery_ok'
                    logger.info(f"VPP user {user.email}: derived export_rule='{current_export_rule}' from components_non_export_configured={non_export}")

            # If still None, fall back to cached value from database
            if current_export_rule is None and user.current_export_rule:
                current_export_rule = user.current_export_rule
                logger.info(f"Using cached export_rule='{current_export_rule}' for {user.email} (API returned None)")

            logger.info(f"Current export rule for {user.email}: {current_export_rule}")

            # CURTAILMENT LOGIC: Curtail when export earnings < 1c/kWh
            # (i.e., when feedin_price > -1, meaning you earn less than 1c or pay to export)
            if export_earnings < 1:
                logger.info(f"🚫 CURTAILMENT TRIGGERED: Export earnings {export_earnings:.2f}c/kWh (<1c) for {user.email}")

                if current_export_rule == 'never':
                    logger.info(f"✅ Already curtailed (export='never') - no action needed for {user.email}")
                    success_count += 1
                else:
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'never')
                    if result:
                        # Verify the change actually took effect by reading back
                        verify_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
                        verified_rule = verify_settings.get('customer_preferred_export_rule') if verify_settings else None
                        if verified_rule is None:
                            # API doesn't return this field - can't verify but not a failure
                            logger.info(f"ℹ️ Cannot verify curtailment (API returns None for export_rule) - operation reported success for {user.email}")
                        elif verified_rule != 'never':
                            logger.warning(f"⚠️ CURTAILMENT VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'never') for {user.email}")
                            logger.warning(f"Full verification response: {verify_settings}")
                        else:
                            logger.info(f"✓ Curtailment verified via read-back: export_rule='{verified_rule}'")

                        logger.info(f"✅ CURTAILMENT APPLIED: '{current_export_rule}' → 'never' for {user.email}")
                        user.current_export_rule = 'never'
                        user.current_export_rule_updated = datetime.utcnow()
                        db.session.commit()
                        success_count += 1
                    else:
                        logger.error(f"Failed to apply curtailment for {user.email}")
                        error_count += 1

            # NORMAL MODE: Export price is positive
            else:
                if current_export_rule == 'never':
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'battery_ok')
                    if result:
                        # Verify the change actually took effect by reading back
                        verify_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
                        verified_rule = verify_settings.get('customer_preferred_export_rule') if verify_settings else None
                        if verified_rule is None:
                            # API doesn't return this field - can't verify but not a failure
                            logger.info(f"ℹ️ Cannot verify restore (API returns None for export_rule) - operation reported success for {user.email}")
                        elif verified_rule != 'battery_ok':
                            logger.warning(f"⚠️ RESTORE VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'battery_ok') for {user.email}")
                            logger.warning(f"Full verification response: {verify_settings}")
                        else:
                            logger.info(f"✓ Restore verified via read-back: export_rule='{verified_rule}'")

                        logger.info(f"✅ CURTAILMENT REMOVED: 'never' → 'battery_ok' for {user.email}")
                        user.current_export_rule = 'battery_ok'
                        user.current_export_rule_updated = datetime.utcnow()
                        db.session.commit()
                        success_count += 1
                    else:
                        logger.error(f"Failed to restore from curtailment for {user.email}")
                        error_count += 1
                else:
                    logger.debug(f"Normal mode, no action needed for {user.email}")
                    success_count += 1

        except Exception as e:
            logger.error(f"Error in curtailment check for {user.email}: {e}", exc_info=True)
            error_count += 1

    logger.info(f"=== ✅ Solar curtailment (WebSocket) complete: {success_count} OK, {error_count} errors ===")


def is_in_peak_period(user):
    """
    Check if current time is within the user's configured peak demand period.

    Args:
        user: User model instance with peak period configuration

    Returns:
        bool: True if currently in peak period, False otherwise
    """
    from datetime import datetime
    import pytz

    # Get user's timezone or default to Sydney
    try:
        user_tz = pytz.timezone(user.timezone or 'Australia/Sydney')
    except Exception:
        user_tz = pytz.timezone('Australia/Sydney')

    now = datetime.now(user_tz)
    current_day = now.weekday()  # 0=Monday, 6=Sunday

    # Check day of week
    peak_days = user.peak_days or 'weekdays'
    if peak_days == 'weekdays' and current_day >= 5:  # Saturday (5) or Sunday (6)
        return False
    elif peak_days == 'weekends' and current_day < 5:  # Monday-Friday
        return False
    # 'all' matches any day

    # Get peak period times
    peak_start_hour = user.peak_start_hour if user.peak_start_hour is not None else 14
    peak_start_minute = user.peak_start_minute if user.peak_start_minute is not None else 0
    peak_end_hour = user.peak_end_hour if user.peak_end_hour is not None else 20
    peak_end_minute = user.peak_end_minute if user.peak_end_minute is not None else 0

    # Create time objects for comparison
    current_time = now.hour * 60 + now.minute  # Minutes since midnight
    peak_start = peak_start_hour * 60 + peak_start_minute
    peak_end = peak_end_hour * 60 + peak_end_minute

    # Handle overnight periods (e.g., 22:00-06:00)
    if peak_end <= peak_start:
        # Overnight period
        return current_time >= peak_start or current_time < peak_end
    else:
        # Normal period
        return peak_start <= current_time < peak_end


def enforce_grid_charging_for_user(user, tesla_client, db, force_apply=False):
    """
    Enforce grid charging settings for a single user based on demand period.

    This is called:
    1. After each successful TOU sync (to counteract VPP overrides)
    2. By the periodic demand_period_grid_charging_check

    Args:
        user: User model instance
        tesla_client: Tesla API client
        db: Database session
        force_apply: If True, always apply the setting even if we think it's already set
                    (useful when VPP might have overridden our setting)

    Returns:
        tuple: (success: bool, action: str describing what was done)
    """
    if not user.enable_demand_charges:
        return True, "demand_charges_disabled"

    in_peak = is_in_peak_period(user)
    currently_disabled = user.grid_charging_disabled_for_demand

    logger.debug(f"User {user.email}: in_peak={in_peak}, currently_disabled={currently_disabled}, force_apply={force_apply}")

    if in_peak:
        # In peak period - ensure grid charging is disabled
        if not currently_disabled or force_apply:
            logger.info(f"⚡ Peak period for {user.email} - {'forcing' if force_apply else 'setting'} grid charging OFF")

            result = tesla_client.set_grid_charging_enabled(user.tesla_energy_site_id, False)

            if result:
                user.grid_charging_disabled_for_demand = True
                db.session.commit()
                logger.info(f"✅ Grid charging DISABLED for {user.email} during peak period")
                return True, "disabled_for_peak"
            else:
                logger.error(f"❌ Failed to disable grid charging for {user.email}")
                return False, "failed_to_disable"
        else:
            return True, "already_disabled"
    else:
        # Outside peak period - ensure grid charging is enabled
        if currently_disabled:
            logger.info(f"⚡ Outside peak period for {user.email} - re-enabling grid charging")

            result = tesla_client.set_grid_charging_enabled(user.tesla_energy_site_id, True)

            if result:
                user.grid_charging_disabled_for_demand = False
                db.session.commit()
                logger.info(f"✅ Grid charging ENABLED for {user.email} (peak period ended)")
                return True, "enabled_outside_peak"
            else:
                logger.error(f"❌ Failed to re-enable grid charging for {user.email}")
                return False, "failed_to_enable"
        else:
            return True, "already_enabled"


def demand_period_grid_charging_check():
    """
    Check if we're in a demand period and toggle grid charging accordingly.
    Runs every minute via scheduler.

    When in peak demand period: Disable grid charging to prevent imports
    When outside peak period: Re-enable grid charging

    Note: This runs with force_apply=True during peak periods to counteract
    VPP overrides that may re-enable grid charging.
    """
    from app import create_app
    from app.models import User, db
    from app.api_clients import get_tesla_client

    logger.info("=== 🔋 Starting demand period grid charging check ===")

    app = create_app()
    with app.app_context():
        # Get all users with demand charges enabled
        users = User.query.filter_by(enable_demand_charges=True).all()

        if not users:
            logger.debug("No users with demand charges enabled")
            return

        success_count = 0
        error_count = 0

        for user in users:
            try:
                # Check if user has Tesla configured
                if not user.tesla_energy_site_id:
                    logger.debug(f"User {user.email} has no Tesla site configured")
                    continue

                tesla_client = get_tesla_client(user)
                if not tesla_client:
                    logger.debug(f"User {user.email} has no Tesla API configured")
                    continue

                # Use force_apply=True during peak periods to counteract VPP overrides
                in_peak = is_in_peak_period(user)
                success, action = enforce_grid_charging_for_user(
                    user, tesla_client, db,
                    force_apply=in_peak  # Force re-apply during peak to fight VPP overrides
                )

                if success:
                    success_count += 1
                else:
                    error_count += 1

            except Exception as e:
                logger.error(f"Error in demand period check for {user.email}: {e}", exc_info=True)
                error_count += 1

        logger.info(f"=== ✅ Demand period grid charging check complete: {success_count} OK, {error_count} errors ===")

