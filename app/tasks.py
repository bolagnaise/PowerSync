# app/tasks.py
"""Background tasks for automatic syncing"""
import logging
import threading
import hashlib
from datetime import datetime, timezone
from app.models import User, PriceRecord, EnergyRecord, SavedTOUProfile
from app.api_clients import get_amber_client, get_tesla_client, AEMOAPIClient
from app.sigenergy_client import get_sigenergy_client, convert_amber_prices_to_sigenergy
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
        self._baseline_operation_modes = {}  # {user_id: 'autonomous'|'self_consumption'|etc} - mode at interval start

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
            self._baseline_operation_modes = {}  # Clear baseline modes for new period
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

    def set_baseline_mode(self, user_id, mode):
        """
        Store the operation mode at the start of this interval for a user.

        This is used to determine if the user manually set self_consumption mode
        (detected at interval start) vs a failed toggle leaving it in self_consumption.

        Args:
            user_id: The user's ID
            mode: The operation mode ('autonomous', 'self_consumption', 'backup', etc.)
        """
        with self._lock:
            self._reset_if_new_period()
            self._baseline_operation_modes[user_id] = mode
            logger.debug(f"Stored baseline operation mode for user {user_id}: {mode}")

    def get_baseline_mode(self, user_id):
        """
        Get the operation mode that was recorded at the start of this interval.

        Args:
            user_id: The user's ID

        Returns:
            str: The baseline mode, or None if not recorded
        """
        with self._lock:
            self._reset_if_new_period()
            return self._baseline_operation_modes.get(user_id)


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

            # Determine battery system type (default to Tesla)
            battery_system = getattr(user, 'battery_system', 'tesla') or 'tesla'

            # Check for battery system credentials based on type
            if battery_system == 'sigenergy':
                # Sigenergy requires station_id
                if not user.sigenergy_station_id:
                    logger.debug(f"Skipping user {user.email} - no Sigenergy station ID")
                    continue
            else:
                # Tesla requires API token and site ID
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

            logger.info(f"Syncing schedule for user: {user.email} (price source: {'AEMO' if use_aemo else 'Amber'}, battery: {battery_system})")

            # Get API clients
            amber_client = get_amber_client(user) if not use_aemo else None

            # Get appropriate battery client based on battery system type
            if battery_system == 'sigenergy':
                battery_client = get_sigenergy_client(user)
                tesla_client = None  # Not used for Sigenergy
            else:
                tesla_client = get_tesla_client(user)
                battery_client = tesla_client  # Use same client reference

            if not use_aemo and not amber_client:
                logger.warning(f"Failed to get Amber client for user {user.email}")
                error_count += 1
                continue

            if battery_system == 'sigenergy' and not battery_client:
                logger.warning(f"Failed to get Sigenergy client for user {user.email}")
                error_count += 1
                continue
            elif battery_system != 'sigenergy' and not tesla_client:
                logger.warning(f"Failed to get Tesla client for user {user.email}")
                error_count += 1
                continue

            # Capture baseline operation mode at start of interval (Stage 1 only)
            # This is used to detect if user manually set self_consumption vs failed toggle
            # Note: Only applicable to Tesla, Sigenergy doesn't have this feature
            if battery_system != 'sigenergy' and sync_mode == 'initial_forecast' and getattr(user, 'force_tariff_mode_toggle', False):
                try:
                    baseline_mode = tesla_client.get_operation_mode(user.tesla_energy_site_id)
                    if baseline_mode:
                        _sync_coordinator.set_baseline_mode(user.id, baseline_mode)
                        logger.info(f"📍 Captured baseline operation mode for {user.email}: {baseline_mode}")
                except Exception as e:
                    logger.warning(f"Failed to capture baseline mode for {user.email}: {e}")

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

                # Check for firmware updates and notify if changed
                firmware_version = site_info.get('version')
                if firmware_version:
                    try:
                        from app.push_notifications import check_and_notify_firmware_change
                        check_and_notify_firmware_change(user, firmware_version)
                    except Exception as e:
                        logger.warning(f"Error checking firmware change: {e}")
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

            # Apply tariff to appropriate battery system
            if battery_system == 'sigenergy':
                # Convert forecast data to Sigenergy format (30-min time slots)
                buy_prices = convert_amber_prices_to_sigenergy(forecast_30min, price_type='buy')
                sell_prices = convert_amber_prices_to_sigenergy(forecast_30min, price_type='sell')

                result = battery_client.set_tariff_rate(
                    user.sigenergy_station_id,
                    buy_prices,
                    sell_prices,
                    plan_name="PowerSync"
                )
                result = result.get('success', False) if isinstance(result, dict) else bool(result)
            else:
                # Tesla: Apply tariff using Tesla client
                result = tesla_client.set_tariff_rate(
                    user.tesla_energy_site_id,
                    tariff
                )

            if result:
                logger.info(f"✅ Successfully synced schedule for user {user.email} ({battery_system})")

                # Alpha: Force mode toggle for faster Powerwall response (Tesla only)
                # Only toggle on settled prices, not forecast (reduces unnecessary toggles)
                if battery_system != 'sigenergy' and getattr(user, 'force_tariff_mode_toggle', False):
                    if sync_mode != 'initial_forecast':
                        # Check BASELINE mode (captured at interval start) to respect user's manual self_consumption
                        # This distinguishes user-set self_consumption from failed-toggle self_consumption
                        baseline_mode = _sync_coordinator.get_baseline_mode(user.id)

                        if baseline_mode == 'self_consumption':
                            # User had self_consumption at interval start - respect their manual setting
                            logger.info(f"⏭️  Skipping force toggle for {user.email} - baseline was self_consumption (respecting user setting)")
                        elif baseline_mode and baseline_mode != 'autonomous':
                            # Not in TOU mode at interval start (e.g., backup mode) - don't toggle
                            logger.info(f"⏭️  Skipping force toggle for {user.email} - baseline not TOU mode (was: {baseline_mode})")
                        else:
                            # Baseline was autonomous (TOU) or unknown - proceed with toggle
                            # Get current mode to verify we're still in a good state
                            current_mode = tesla_client.get_operation_mode(user.tesla_energy_site_id)

                            if current_mode and current_mode not in ['autonomous', 'self_consumption']:
                                # Currently in backup or other mode - don't toggle
                                logger.info(f"⏭️  Skipping force toggle for {user.email} - current mode is {current_mode}")
                            else:
                                # Check if already optimizing before toggling
                                site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
                                grid_power = site_status.get('grid_power', 0) if site_status else 0
                                battery_power = site_status.get('battery_power', 0) if site_status else 0

                                if grid_power < 0:
                                    # Negative grid_power means exporting - already doing what we want
                                    logger.info(f"⏭️  Skipping force toggle for {user.email} - already exporting ({abs(grid_power):.0f}W to grid)")
                                elif battery_power < 0:
                                    # Negative battery_power means charging - already doing what we want
                                    logger.info(f"⏭️  Skipping force toggle for {user.email} - battery already charging ({abs(battery_power):.0f}W)")
                                else:
                                    logger.info(f"🔄 Force mode toggle for {user.email} - grid: {grid_power:.0f}W, battery: {battery_power:.0f}W")
                                    force_tariff_refresh(tesla_client, user.tesla_energy_site_id, wait_seconds=5)
                    else:
                        logger.debug(f"Skipping force toggle on forecast sync for {user.email} (waiting for settled prices)")

                # Update user's last_update timestamp and tariff hash
                user.last_update_time = datetime.now(timezone.utc)
                user.last_update_status = f"Auto-sync successful ({sync_mode}, {battery_system})"
                user.last_tariff_hash = tariff_hash  # Save hash for deduplication
                db.session.commit()

                # Record the synced price for smart price-change detection
                if general_price is not None or feedin_price is not None:
                    _sync_coordinator.record_synced_price(user.id, general_price, feedin_price)

                # Enforce grid charging setting after TOU sync (Tesla only - counteracts VPP overrides)
                if battery_system != 'sigenergy' and user.enable_demand_charges:
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
                logger.error(f"Failed to apply schedule to {battery_system} for user {user.email}")
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
                # For Amber users, we'll trigger an immediate sync after clearing the state
                logger.info(f"Amber user {user.email} - will trigger immediate price sync")
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
        # Trigger immediate sync to update tariffs for all users (especially Amber users)
        logger.info("🔄 Triggering immediate sync after force discharge expiry")
        _sync_all_users_internal(None, sync_mode='rest_api_check')


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
                # For Amber users, we'll trigger an immediate sync after clearing the state
                logger.info(f"Amber user {user.email} - will trigger immediate price sync")
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

            # Restore saved backup reserve if it was saved during force charge
            saved_backup_reserve = getattr(user, 'manual_charge_saved_backup_reserve', None)
            if saved_backup_reserve is not None:
                logger.info(f"Restoring backup reserve to {saved_backup_reserve}% for {user.email}")
                backup_result = tesla_client.set_backup_reserve(user.tesla_energy_site_id, saved_backup_reserve)
                if backup_result:
                    logger.info(f"Restored backup reserve to {saved_backup_reserve}%")
                else:
                    logger.warning(f"Failed to restore backup reserve to {saved_backup_reserve}%")

            # Clear charge state
            user.manual_charge_active = False
            user.manual_charge_expires_at = None
            user.manual_charge_saved_backup_reserve = None
            db.session.commit()
            logger.info(f"Manual charge cleared for {user.email}")

        except Exception as e:
            logger.error(f"Error auto-restoring charge for {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    if expired_users:
        logger.info(f"Processed {len(expired_users)} expired charge modes")
        # Trigger immediate sync to update tariffs for all users (especially Amber users)
        logger.info("🔄 Triggering immediate sync after force charge expiry")
        _sync_all_users_internal(None, sync_mode='rest_api_check')


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
    Automatically save energy usage data to database for historical tracking.
    Supports both Tesla Powerwall and Sigenergy systems.
    This runs periodically in the background to capture solar, grid, battery, and load power.
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
            # Determine which battery system to collect from
            battery_system = user.battery_system or 'tesla'

            if battery_system == 'sigenergy':
                # Collect from Sigenergy via Modbus
                result = _collect_sigenergy_energy(user)
            else:
                # Collect from Tesla
                result = _collect_tesla_energy(user)

            if result:
                success_count += 1
            else:
                # Result is None means skipped (not configured), not an error
                pass

        except Exception as e:
            logger.error(f"Error collecting energy usage for user {user.email}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            db.session.rollback()
            error_count += 1
            continue

    logger.debug(f"=== Energy usage collection completed: {success_count} users successful, {error_count} errors ===")
    return success_count, error_count


def _collect_tesla_energy(user) -> bool:
    """Collect energy data from Tesla Powerwall.

    Returns:
        True if successful, False if error, None if not configured
    """
    from app import db

    if not user.tesla_energy_site_id:
        logger.debug(f"Skipping user {user.email} - no Tesla site ID")
        return None

    logger.debug(f"Collecting Tesla energy usage for user: {user.email}")

    # Get Tesla client
    tesla_client = get_tesla_client(user)
    if not tesla_client:
        logger.warning(f"Failed to get Tesla client for user {user.email}")
        return False

    # Get site status (contains power flow data)
    site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
    if not site_status:
        logger.warning(f"No site status available for user {user.email}")
        return False

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

    logger.debug(f"✅ Saved Tesla energy record for user {user.email}: Solar={solar_power}W Grid={grid_power}W Battery={battery_power}W Load={load_power}W")
    return True


def _collect_sigenergy_energy(user) -> bool:
    """Collect energy data from Sigenergy via Modbus TCP.

    Returns:
        True if successful, False if error, None if not configured
    """
    from app import db
    from app.sigenergy_modbus import get_sigenergy_modbus_client

    if not user.sigenergy_modbus_host:
        logger.debug(f"Skipping user {user.email} - no Sigenergy Modbus configured")
        return None

    logger.debug(f"Collecting Sigenergy energy usage for user: {user.email}")

    # Get Modbus client
    client = get_sigenergy_modbus_client(user)
    if not client:
        logger.warning(f"Failed to create Sigenergy Modbus client for user {user.email}")
        return False

    # Get live status
    status = client.get_live_status()
    if 'error' in status:
        logger.warning(f"Sigenergy status error for user {user.email}: {status['error']}")
        return False

    # Extract power data (in watts)
    solar_power = status.get('solar_power', 0.0)
    battery_power = status.get('battery_power', 0.0)
    grid_power = status.get('grid_power', 0.0)
    load_power = status.get('load_power', 0.0)
    battery_level = status.get('percentage_charged', 0.0)

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

    logger.debug(f"✅ Saved Sigenergy energy record for user {user.email}: Solar={solar_power}W Grid={grid_power}W Battery={battery_power}W Load={load_power}W")
    return True


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


def force_tariff_refresh(tesla_client, site_id, wait_seconds=30, max_retries=2):
    """
    Force Powerwall to immediately apply new tariff by toggling operation mode

    The Powerwall can take several minutes to recognize tariff changes.
    Switching to self_consumption then back to autonomous forces immediate recalculation.

    Args:
        tesla_client: TeslemetryAPIClient instance
        site_id: Energy site ID
        wait_seconds: Seconds to wait in self_consumption mode (default: 30)
                     Use 60 for restore operations, 30 for spike activation
        max_retries: Number of retry attempts if mode switch verification fails (default: 2)

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

        # Step 3: Switch back to autonomous mode with verification and retry
        for attempt in range(max_retries + 1):
            logger.info(f"Switching back to autonomous mode... (attempt {attempt + 1}/{max_retries + 1})")
            result2 = tesla_client.set_operation_mode(site_id, 'autonomous')

            if not result2:
                logger.warning(f"Failed to switch back to autonomous mode (attempt {attempt + 1})")
                if attempt < max_retries:
                    time.sleep(2)  # Short wait before retry
                continue

            # Step 4: Verify the mode actually changed
            time.sleep(2)  # Give Tesla a moment to process
            current_mode = tesla_client.get_operation_mode(site_id)

            if current_mode == 'autonomous':
                logger.info("✅ Successfully toggled operation mode - verified autonomous")
                return True
            else:
                logger.warning(f"⚠️ Mode verification failed: expected 'autonomous', got '{current_mode}' (attempt {attempt + 1})")
                if attempt < max_retries:
                    time.sleep(2)  # Short wait before retry

        # All retries exhausted
        logger.error(f"❌ Failed to switch back to autonomous mode after {max_retries + 1} attempts - PW may be stuck in self_consumption!")
        return False

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
        "utility": "PowerSync",
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
            "utility": "PowerSync",
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
        "utility": "PowerSync",
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
            "utility": "PowerSync",
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


def _check_ac_coupled_curtailment(user, import_price: float = None, export_earnings: float = None):
    """
    Smart curtailment logic for AC-coupled solar systems.

    For AC-coupled systems, we only curtail the inverter when:
    1. Import price is negative (cheaper to buy power than generate it), OR
    2. Actually exporting (grid_power < 0) AND export earnings are negative, OR
    3. Battery is at 100% (can't absorb more solar) AND export is unprofitable, OR
    4. Solar producing but battery NOT charging AND exporting at negative price

    Args:
        user: User model instance
        import_price: Current import price in c/kWh (negative = get paid to import)
        export_earnings: Current export earnings in c/kWh

    Returns:
        bool: True if we should curtail, False if we should allow production
    """
    from app.api_clients import get_tesla_client

    if not getattr(user, 'inverter_curtailment_enabled', False):
        return False

    # Check 1: If import price is negative, always curtail (get paid to import instead)
    if import_price is not None and import_price < 0:
        logger.info(f"🔌 AC-COUPLED: Import price negative ({import_price:.2f}c/kWh) - should curtail for {user.email}")
        return True

    # Get configurable restore SOC threshold (default 98%)
    restore_soc = getattr(user, 'inverter_restore_soc', 98) or 98

    # Get live site data for grid_power, battery_soc, solar_power, battery_power
    battery_soc = None
    grid_power = None
    solar_power = 0
    battery_power = 0
    load_power = 0
    battery_system = getattr(user, 'battery_system', 'tesla') or 'tesla'

    try:
        if battery_system == 'tesla':
            if user.tesla_energy_site_id and user.teslemetry_api_key_encrypted:
                tesla_client = get_tesla_client(user)
                if tesla_client:
                    site_status = tesla_client.get_live_site_data(user.tesla_energy_site_id)
                    if site_status:
                        battery_soc = site_status.get('percentage_charged', 0)
                        grid_power = site_status.get('grid_power', 0)  # Negative = exporting
                        solar_power = site_status.get('solar_power', 0) or 0
                        battery_power = site_status.get('battery_power', 0) or 0  # Negative = charging
                        load_power = site_status.get('load_power', 0) or 0
                        logger.debug(
                            f"AC-Coupled check: solar={solar_power:.0f}W, battery={battery_power:.0f}W (neg=charging), "
                            f"grid={grid_power}W (neg=export), load={load_power:.0f}W, SOC={battery_soc}% for {user.email}"
                        )

        elif battery_system == 'sigenergy':
            # For Sigenergy, we could get SOC via Modbus if needed
            # For now, assume we don't have it and be conservative (don't curtail)
            logger.debug(f"Sigenergy SOC check not implemented - skipping AC curtailment for {user.email}")
            return False

    except Exception as e:
        logger.warning(f"Failed to get live site data for AC curtailment check: {e}")
        # If we can't get data, be conservative and don't curtail
        return False

    if battery_soc is None:
        logger.debug(f"Could not get battery SOC - not curtailing AC solar for {user.email}")
        return False

    # Compute state flags
    battery_is_charging = battery_power < -50  # At least 50W charging

    # RESTORE CHECK: If battery SOC < restore threshold, allow inverter to run
    # This ensures battery stays topped up before evening peak, even during negative export prices
    if battery_soc < restore_soc:
        if battery_is_charging or battery_soc < 100:  # Battery can still absorb
            logger.info(
                f"🔋 AC-COUPLED: Battery SOC {battery_soc:.0f}% < restore threshold {restore_soc}% "
                f"- allowing inverter to run (topping up battery) for {user.email}"
            )
            return False

    # Check 2: If actually exporting (grid_power < 0) AND export earnings are negative
    # Only curtail when we're actually paying to export, not just when export price is negative
    if grid_power is not None and grid_power < 0:  # Negative = exporting
        if export_earnings is not None and export_earnings < 0:
            logger.info(f"🔌 AC-COUPLED: Exporting {abs(grid_power):.0f}W at negative price ({export_earnings:.2f}c/kWh) - should curtail for {user.email}")
            return True
        else:
            logger.debug(f"Exporting {abs(grid_power):.0f}W but price is OK ({export_earnings:.2f}c/kWh) - not curtailing for {user.email}")
    else:
        logger.debug(f"Not exporting (grid={grid_power}W) - no need to curtail for negative export for {user.email}")

    # Check 3: Battery full (100%) AND export is unprofitable (< 1c/kWh)
    if battery_soc >= 100:
        if export_earnings is not None and export_earnings < 1:
            logger.info(f"🔌 AC-COUPLED: Battery full ({battery_soc}%) AND export unprofitable ({export_earnings:.2f}c/kWh) - should curtail for {user.email}")
            return True
        else:
            logger.debug(f"Battery full ({battery_soc}%) but export still profitable ({export_earnings:.2f}c/kWh) - not curtailing for {user.email}")
            return False

    # Check 4: Solar producing but battery NOT absorbing it
    # battery_power < 0 means charging, >= 0 means discharging or idle
    if solar_power > 100:  # Meaningful solar production
        battery_is_charging = battery_power < -50  # At least 50W charging
        if not battery_is_charging:
            # Solar is producing but battery isn't absorbing - check if we're exporting at bad prices
            if grid_power is not None and grid_power < -100:  # Exporting more than 100W
                if export_earnings is not None and export_earnings < 0:
                    logger.info(
                        f"🔌 AC-COUPLED: Solar producing {solar_power:.0f}W but battery NOT charging "
                        f"(battery_power={battery_power:.0f}W), exporting {abs(grid_power):.0f}W at negative price "
                        f"({export_earnings:.2f}c/kWh) - should curtail for {user.email}"
                    )
                    return True
                else:
                    logger.debug(
                        f"Solar producing but battery not charging, however export price OK ({export_earnings:.2f}c/kWh) for {user.email}"
                    )
            else:
                logger.debug(
                    f"Solar producing {solar_power:.0f}W, battery not charging, but not exporting significantly (grid={grid_power}W) for {user.email}"
                )
        else:
            logger.debug(f"Battery is charging ({abs(battery_power):.0f}W) - solar being absorbed, not curtailing for {user.email}")
            return False
    else:
        logger.debug(f"Low/no solar production ({solar_power:.0f}W) - not curtailing for {user.email}")
        return False

    # Default: don't curtail
    logger.debug(f"No curtailment conditions met - allowing solar production for {user.email}")
    return False


def _apply_inverter_curtailment(user, curtail: bool = True):
    """
    Apply or remove inverter curtailment for AC-coupled solar systems.

    This is a non-blocking helper that runs inverter control in a separate thread
    to avoid blocking the main curtailment task.

    For Zeversolar inverters, uses load-following mode when curtailing:
    - Gets current home load from Tesla API
    - Limits inverter output to match home load (prevents export while powering home)

    Note: For smart curtailment logic (checking battery SOC and import price),
    use _check_ac_coupled_curtailment() first to determine if curtailment is needed.

    Args:
        user: User model instance with inverter configuration
        curtail: True to curtail (stop inverter), False to restore (start inverter)
    """
    import asyncio
    from datetime import datetime
    from app import db

    if not getattr(user, 'inverter_curtailment_enabled', False):
        return

    if not user.inverter_host or not user.inverter_brand:
        logger.warning(f"Inverter curtailment enabled but not configured for {user.email}")
        return

    action = "curtailing" if curtail else "restoring"
    logger.info(f"🔌 INVERTER: {action.upper()} inverter for {user.email} ({user.inverter_brand} at {user.inverter_host})")

    # For load-following curtailment (Zeversolar), get current home load + battery charge rate
    home_load_w = None
    if curtail and user.inverter_brand == 'zeversolar':
        try:
            tesla_client = get_tesla_client(user)
            if tesla_client and user.tesla_energy_site_id:
                site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
                if site_status:
                    home_load_w = int(site_status.get('load_power', 0))
                    # Add battery charge rate if battery is charging
                    # battery_power < 0 means charging (negative = consuming power from solar)
                    # battery_power > 0 means discharging (positive = providing power)
                    battery_power = site_status.get('battery_power', 0) or 0
                    # Negate to get positive charge rate (e.g., -2580W charging → 2580W)
                    battery_charge_w = max(0, -int(battery_power))
                    if battery_charge_w > 50:  # At least 50W charging
                        total_load_w = home_load_w + battery_charge_w
                        logger.info(f"🔌 LOAD-FOLLOWING: Home={home_load_w}W + Battery charging={battery_charge_w}W = {total_load_w}W for {user.email}")
                        home_load_w = total_load_w
                    else:
                        logger.info(f"🔌 LOAD-FOLLOWING: Home load is {home_load_w}W (battery not charging or <50W) for {user.email}")
        except Exception as e:
            logger.warning(f"Failed to get home load for load-following: {e}")
            # Fall back to full curtailment if we can't get home load
            home_load_w = None

    try:
        from app.inverters import get_inverter_controller_from_user

        controller = get_inverter_controller_from_user(user)
        if not controller:
            logger.error(f"Failed to create inverter controller for {user.email}")
            return

        # Run async function in sync context - create new event loop for thread
        async def run_inverter_action():
            try:
                if curtail:
                    # Pass home_load_w for load-following (Zeversolar/Sungrow)
                    # Other inverters ignore this parameter
                    if hasattr(controller.curtail, '__code__') and 'home_load_w' in controller.curtail.__code__.co_varnames:
                        result = await controller.curtail(home_load_w=home_load_w)
                    else:
                        result = await controller.curtail()
                else:
                    result = await controller.restore()
                await controller.disconnect()
                return result
            except Exception as e:
                logger.error(f"Error during inverter action: {e}")
                return False

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(run_inverter_action())
        finally:
            loop.close()

        if success:
            new_state = 'curtailed' if curtail else 'online'
            user.inverter_last_state = new_state
            user.inverter_last_state_updated = datetime.utcnow()
            if home_load_w is not None and curtail:
                user.inverter_power_limit_w = home_load_w
            elif not curtail:
                user.inverter_power_limit_w = None  # Clear when restored
            db.session.commit()
            if home_load_w is not None and curtail:
                logger.info(f"✅ INVERTER: Load-following curtailment to {home_load_w}W for {user.email} (state: {new_state})")
            else:
                logger.info(f"✅ INVERTER: Successfully {action} inverter for {user.email} (state: {new_state})")
        else:
            logger.error(f"❌ INVERTER: Failed to {action[:-3]} inverter for {user.email}")

    except Exception as e:
        logger.error(f"❌ INVERTER ERROR: Failed to {action[:-3]} inverter for {user.email}: {e}", exc_info=True)


def _apply_sigenergy_curtailment(user, export_earnings: float, import_price: float, db) -> bool:
    """
    Apply or remove Sigenergy curtailment via Modbus TCP with SMART logic.

    Smart curtailment logic (same as Tesla+AC-coupled):
    - Only curtail when battery is full (100%) OR import price is negative
    - If battery can still absorb solar, don't curtail (let it charge)
    - Use load-following: set export limit = home load (not 0kW)

    Returns True on success, False on error.
    """
    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            logger.error(f"Failed to create Sigenergy Modbus client for {user.email}")
            return False

        # Get current curtailment state from user record
        current_state = getattr(user, 'sigenergy_curtailment_state', None)

        # CURTAILMENT: export_earnings < 1c/kWh
        if export_earnings < 1:
            logger.info(f"🚫 SIGENERGY: Export earnings {export_earnings:.2f}c/kWh (<1c) for {user.email}")

            # Get live status to check battery SOC and home load
            live_status = client.get_live_status()
            if 'error' in live_status:
                logger.warning(f"Failed to get Sigenergy live status: {live_status.get('error')}")
                # Fall back to basic curtailment if we can't read status
                battery_soc = 100  # Assume full to be safe
                home_load_kw = 0
            else:
                battery_soc = live_status.get('percentage_charged', 0)
                home_load_w = live_status.get('load_power', 0)
                home_load_kw = max(0, home_load_w / 1000)  # Convert to kW
                solar_power_w = live_status.get('solar_power', 0)
                logger.info(f"📊 SIGENERGY STATUS: SOC={battery_soc}%, Load={home_load_w}W, Solar={solar_power_w}W")

            # SMART LOGIC: Should we actually curtail?
            # Only curtail if: battery is full (100%) OR import price is negative
            should_curtail = False

            if import_price is not None and import_price < 0:
                # Import price is negative - definitely curtail (cheaper to buy than generate)
                logger.info(f"🔌 SIGENERGY: Import price negative ({import_price:.2f}c/kWh) - curtailing")
                should_curtail = True
            elif battery_soc >= 100:
                # Battery is full - curtail (can't absorb more solar)
                logger.info(f"🔋 SIGENERGY: Battery full ({battery_soc}%) - curtailing with load-following")
                should_curtail = True
            else:
                # Battery can still absorb solar - don't curtail
                logger.info(f"🔋 SIGENERGY: Battery not full ({battery_soc}%) - letting solar charge battery")
                should_curtail = False

            if not should_curtail:
                # Battery can absorb solar - restore normal operation
                if current_state == 'curtailed':
                    success = client.restore_export_limit()
                    if success:
                        user.sigenergy_curtailment_state = 'normal'
                        user.sigenergy_curtailment_updated = datetime.utcnow()
                        db.session.commit()
                        logger.info(f"✅ SIGENERGY: Export restored (battery can absorb solar)")
                return True

            # LOAD-FOLLOWING CURTAILMENT: Set export limit = home load
            # This powers the home while preventing grid export
            export_limit_kw = max(0.1, home_load_kw)  # Minimum 0.1kW to avoid complete shutdown

            if current_state == 'curtailed':
                # Already curtailed - update limit if home load changed significantly
                current_limit = getattr(user, 'sigenergy_export_limit_kw', 0)
                if abs(current_limit - export_limit_kw) < 0.5:
                    logger.debug(f"Export limit unchanged ({export_limit_kw:.1f}kW) for {user.email}")
                    return True

            success = client.set_export_limit(export_limit_kw)
            if success:
                user.sigenergy_curtailment_state = 'curtailed'
                user.sigenergy_export_limit_kw = export_limit_kw
                user.sigenergy_curtailment_updated = datetime.utcnow()
                db.session.commit()
                logger.info(f"✅ SIGENERGY LOAD-FOLLOWING: Export limit set to {export_limit_kw:.1f}kW (home load) for {user.email}")
                return True
            else:
                logger.error(f"❌ Failed to apply Sigenergy curtailment for {user.email}")
                return False

        # RESTORE: export_earnings >= 1c/kWh
        else:
            logger.info(f"✅ SIGENERGY NORMAL: Export earnings {export_earnings:.2f}c/kWh (>=1c) for {user.email}")

            if current_state != 'curtailed':
                logger.debug(f"Already in normal mode - no action needed for {user.email}")
                return True

            # Restore unlimited export
            success = client.restore_export_limit()
            if success:
                user.sigenergy_curtailment_state = 'normal'
                user.sigenergy_export_limit_kw = None
                user.sigenergy_curtailment_updated = datetime.utcnow()
                db.session.commit()
                logger.info(f"✅ SIGENERGY: Export limit restored to unlimited for {user.email}")
                return True
            else:
                logger.error(f"❌ Failed to restore Sigenergy export limit for {user.email}")
                return False

    except Exception as e:
        logger.error(f"❌ SIGENERGY CURTAILMENT ERROR for {user.email}: {e}", exc_info=True)
        return False


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

            # Determine battery system type
            battery_system = getattr(user, 'battery_system', 'tesla') or 'tesla'

            # Validate battery-system-specific configuration
            if battery_system == 'sigenergy':
                if not user.sigenergy_modbus_host:
                    logger.warning(f"User {user.email} has Sigenergy curtailment enabled but no Modbus host configured")
                    continue
            else:
                # Tesla system
                if not user.tesla_energy_site_id or not user.teslemetry_api_key_encrypted:
                    logger.warning(f"User {user.email} has curtailment enabled but missing Tesla configuration")
                    continue

            logger.info(f"Checking export price for user: {user.email} (battery_system: {battery_system})")

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

            # Get feed-in (export) and general (import) prices
            feedin_price = None
            import_price = None
            for price_data in current_prices:
                if price_data.get('channelType') == 'feedIn':
                    feedin_price = price_data.get('perKwh', 0)
                elif price_data.get('channelType') == 'general':
                    import_price = price_data.get('perKwh', 0)

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
            if import_price is not None:
                logger.debug(f"Current import price for {user.email}: {import_price}c/kWh")

            # Handle Sigenergy systems via Modbus (smart curtailment with load-following)
            if battery_system == 'sigenergy':
                result = _apply_sigenergy_curtailment(user, export_earnings, import_price, db)
                if result:
                    success_count += 1
                else:
                    error_count += 1
                continue

            # Tesla system - get Tesla client
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

                # AC-coupled inverter curtailment uses smart logic:
                # Only curtail if battery is full (100%) OR import price is negative
                if getattr(user, 'inverter_curtailment_enabled', False):
                    should_curtail = _check_ac_coupled_curtailment(user, import_price, export_earnings)
                    if should_curtail:
                        _apply_inverter_curtailment(user, curtail=True)
                    else:
                        # Battery can still absorb solar - restore if previously curtailed
                        if getattr(user, 'inverter_last_state', None) == 'curtailed':
                            logger.info(f"🔌 AC-COUPLED: Battery absorbing solar - RESTORING inverter for {user.email}")
                            _apply_inverter_curtailment(user, curtail=False)
                        else:
                            logger.info(f"🔌 AC-COUPLED: Skipping inverter curtailment (battery can absorb solar) for {user.email}")

                success_count += 1
                logger.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                logger.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c) for {user.email}")

                # If currently curtailed, restore based on user preference
                if current_export_rule == 'never':
                    # Check if user has a manual override active
                    if getattr(user, 'manual_export_override', False):
                        # Respect user's manual selection (e.g., pv_only to prevent battery export)
                        restore_rule = getattr(user, 'manual_export_rule', 'battery_ok') or 'battery_ok'
                        logger.info(f"🔄 RESTORING FROM CURTAILMENT (manual override active): 'never' → '{restore_rule}' for {user.email}")
                    else:
                        # Default to battery_ok for auto mode
                        restore_rule = 'battery_ok'
                        logger.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → '{restore_rule}' for {user.email}")

                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, restore_rule)
                    if not result:
                        logger.error(f"❌ Failed to restore from curtailment (set export to '{restore_rule}') for {user.email}")
                        error_count += 1
                        continue

                    # Verify the change actually took effect by reading back
                    verify_settings = tesla_client.get_grid_import_export(user.tesla_energy_site_id)
                    verified_rule = verify_settings.get('customer_preferred_export_rule') if verify_settings else None
                    if verified_rule is None:
                        # API doesn't return this field - can't verify but not a failure
                        logger.info(f"ℹ️ Cannot verify restore (API returns None for export_rule) - operation reported success for {user.email}")
                    elif verified_rule != restore_rule:
                        logger.warning(f"⚠️ RESTORE VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected '{restore_rule}') for {user.email}")
                        logger.warning(f"Full verification response: {verify_settings}")
                    else:
                        logger.info(f"✓ Restore verified via read-back: export_rule='{verified_rule}'")

                    logger.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → '{restore_rule}' for {user.email}")
                    user.current_export_rule = restore_rule
                    user.current_export_rule_updated = datetime.utcnow()
                    db.session.commit()

                    # Restore inverter - export is now profitable, check if we should still curtail (import negative)
                    if getattr(user, 'inverter_curtailment_enabled', False):
                        should_curtail = _check_ac_coupled_curtailment(user, import_price, export_earnings)
                        if should_curtail:
                            # Rare case: export profitable but import is negative - keep inverter curtailed
                            logger.info(f"🔌 AC-COUPLED: Keeping inverter curtailed (negative import price) for {user.email}")
                        else:
                            _apply_inverter_curtailment(user, curtail=False)

                    logger.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: '{restore_rule}')")
                    success_count += 1
                else:
                    # Even if Tesla export rule unchanged, check if inverter needs restoring
                    if getattr(user, 'inverter_curtailment_enabled', False) and getattr(user, 'inverter_last_state', None) == 'curtailed':
                        should_curtail = _check_ac_coupled_curtailment(user, import_price, export_earnings)
                        if not should_curtail:
                            _apply_inverter_curtailment(user, curtail=False)

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
                    else:
                        logger.error(f"Failed to apply curtailment for {user.email}")
                        error_count += 1
                        continue

                # AC-coupled inverter curtailment (independent of Tesla state)
                if getattr(user, 'inverter_curtailment_enabled', False):
                    should_curtail = _check_ac_coupled_curtailment(user, import_price, export_earnings)
                    if should_curtail:
                        _apply_inverter_curtailment(user, curtail=True)
                    else:
                        # Battery can still absorb solar - restore if previously curtailed
                        if getattr(user, 'inverter_last_state', None) == 'curtailed':
                            logger.info(f"🔌 AC-COUPLED: Battery absorbing solar - RESTORING inverter for {user.email}")
                            _apply_inverter_curtailment(user, curtail=False)
                        else:
                            logger.info(f"🔌 AC-COUPLED: Skipping inverter curtailment (battery can absorb solar) for {user.email}")

                success_count += 1

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
                    else:
                        logger.error(f"Failed to restore from curtailment for {user.email}")
                        error_count += 1
                        continue
                else:
                    logger.debug(f"Normal mode, no action needed for {user.email}")

                # AC-coupled inverter restore (independent of Tesla state)
                if getattr(user, 'inverter_curtailment_enabled', False):
                    if getattr(user, 'inverter_last_state', None) == 'curtailed':
                        _apply_inverter_curtailment(user, curtail=False)

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

