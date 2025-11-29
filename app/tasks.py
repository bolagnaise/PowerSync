# app/tasks.py
"""Background tasks for automatic syncing"""
import logging
import threading
from datetime import datetime, timezone
from app.models import User, PriceRecord, EnergyRecord, SavedTOUProfile
from app.api_clients import get_amber_client, get_tesla_client, AEMOAPIClient
from app.tariff_converter import AmberTariffConverter
import json

logger = logging.getLogger(__name__)


class SyncCoordinator:
    """
    Coordinates Tesla sync with WebSocket wait-with-timeout pattern.

    At the start of each 5-minute period:
    1. Wait up to 60 seconds for WebSocket to deliver price data
    2. If WebSocket arrives → Use WebSocket data and sync immediately
    3. If timeout (60s) → Fallback to REST API sync

    Only ONE sync per 5-minute period.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._websocket_event = threading.Event()
        self._websocket_data = None
        self._current_period = None  # Track which 5-min period we're in

    def notify_websocket_update(self, prices_data):
        """Called by WebSocket when new price data arrives."""
        with self._lock:
            self._websocket_data = prices_data
            self._websocket_event.set()
            logger.info("📡 WebSocket price update received, notifying sync coordinator")

    def wait_for_websocket_or_timeout(self, timeout_seconds=60):
        """
        Wait for WebSocket data or timeout.

        Returns:
            dict: WebSocket price data if arrived within timeout, None if timeout
        """
        logger.info(f"⏱️  Waiting up to {timeout_seconds}s for WebSocket price update...")

        # Wait for event with timeout
        received = self._websocket_event.wait(timeout=timeout_seconds)

        with self._lock:
            if received and self._websocket_data:
                logger.info("✅ WebSocket data received, using real-time prices")
                data = self._websocket_data
                # Clear for next period
                self._websocket_event.clear()
                self._websocket_data = None
                return data
            else:
                logger.warning(f"⏰ WebSocket timeout after {timeout_seconds}s, falling back to REST API")
                # Clear for next period
                self._websocket_event.clear()
                self._websocket_data = None
                return None

    def should_sync_this_period(self):
        """
        Check if we should sync for the current 5-minute period.
        Prevents duplicate syncs within the same period.

        Returns:
            bool: True if this is a new period and we should sync
        """
        now = datetime.now(timezone.utc)
        # Calculate current 5-minute period (e.g., 17:00, 17:05, 17:10, etc.)
        current_period = now.replace(second=0, microsecond=0)
        current_period = current_period.replace(minute=current_period.minute - (current_period.minute % 5))

        with self._lock:
            if self._current_period == current_period:
                logger.info(f"⏭️  Already synced for period {current_period}, skipping")
                return False

            self._current_period = current_period
            logger.info(f"🆕 New sync period: {current_period}")
            return True

    def already_synced_this_period(self):
        """
        Check if we already synced for the current 5-minute period (read-only).
        Used by cron fallback to determine if WebSocket already handled this period.

        Returns:
            bool: True if already synced this period, False if not synced yet
        """
        now = datetime.now(timezone.utc)
        # Calculate current 5-minute period
        current_period = now.replace(second=0, microsecond=0)
        current_period = current_period.replace(minute=current_period.minute - (current_period.minute % 5))

        with self._lock:
            return self._current_period == current_period


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


def sync_all_users():
    """
    Automatically sync TOU schedules for all configured users.

    Uses wait-with-timeout pattern:
    1. Wait up to 60s for WebSocket price update
    2. If WebSocket arrives -> Use WebSocket data
    3. If timeout -> Fallback to REST API

    Only ONE sync per 5-minute period.
    """
    from app import db

    # Check if we should sync this period (prevents duplicates within same 5-min window)
    if not _sync_coordinator.should_sync_this_period():
        return

    # Wait for WebSocket or timeout (60s)
    websocket_data = _sync_coordinator.wait_for_websocket_or_timeout(timeout_seconds=60)

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

            # Skip users without required configuration
            if not user.amber_api_token_encrypted:
                logger.debug(f"Skipping user {user.email} - no Amber token")
                continue

            if not user.teslemetry_api_key_encrypted:
                logger.debug(f"Skipping user {user.email} - no Teslemetry token")
                continue

            if not user.tesla_energy_site_id:
                logger.debug(f"Skipping user {user.email} - no Tesla site ID")
                continue

            logger.info(f"Syncing schedule for user: {user.email}")

            # Get API clients
            amber_client = get_amber_client(user)
            tesla_client = get_tesla_client(user)

            if not amber_client or not tesla_client:
                logger.warning(f"Failed to get API clients for user {user.email}")
                error_count += 1
                continue

            # Step 1: Get current interval price from WebSocket (real-time) or REST API fallback
            # WebSocket is PRIMARY source for current price, REST API is fallback if timeout
            current_actual_interval = None

            if websocket_data:
                # WebSocket data received within 60s - use it directly as primary source
                current_actual_interval = websocket_data
                general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
                logger.info(f"✅ Using WebSocket price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
            else:
                # WebSocket timeout - fallback to REST API for current price
                logger.warning(f"⏰ WebSocket timeout - using REST API fallback for current price")
                current_prices = amber_client.get_current_prices()

                if current_prices:
                    current_actual_interval = {'general': None, 'feedIn': None}
                    for price in current_prices:
                        channel = price.get('channelType')
                        if channel in ['general', 'feedIn']:
                            current_actual_interval[channel] = price

                    general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                    logger.info(f"📡 Using REST API price for current interval: general={general_price}¢/kWh")
                else:
                    logger.warning(f"No current price data available for {user.email}, proceeding with 30-min forecast only")

            # Step 2: Fetch 48-hour forecast with 30-min resolution for TOU schedule building
            # (The Amber API doesn't provide 48 hours of 5-min data, so we must use 30-min)
            forecast_30min = amber_client.get_price_forecast(next_hours=48, resolution=30)
            if not forecast_30min:
                logger.error(f"Failed to fetch 30-min forecast for user {user.email}")
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

            logger.info(f"Applying tariff for {user.email} with {len(tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {}))} rate periods")

            # Apply tariff to Tesla
            result = tesla_client.set_tariff_rate(
                user.tesla_energy_site_id,
                tariff
            )

            if result:
                logger.info(f"✅ Successfully synced schedule for user {user.email}")

                # Update user's last_update timestamp
                user.last_update_time = datetime.now(timezone.utc)
                user.last_update_status = "Auto-sync successful"
                db.session.commit()

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


def save_price_history():
    """
    Automatically save current Amber prices to database for historical tracking
    This runs periodically in the background to ensure continuous price history
    """
    from app import db

    logger.info("=== Starting automatic price history collection ===")

    users = User.query.all()

    if not users:
        logger.info("No users found for price history collection")
        return

    success_count = 0
    error_count = 0

    for user in users:
        try:
            # Skip users without Amber configuration
            if not user.amber_api_token_encrypted:
                logger.debug(f"Skipping user {user.email} - no Amber token")
                continue

            logger.debug(f"Collecting price history for user: {user.email}")

            # Get Amber client
            amber_client = get_amber_client(user)
            if not amber_client:
                logger.warning(f"Failed to get Amber client for user {user.email}")
                error_count += 1
                continue

            # Get current prices from WebSocket (primary) or REST API (fallback)
            # Wait up to 60 seconds for WebSocket data (same logic as TOU sync)
            coordinator = get_sync_coordinator()
            websocket_data = coordinator.wait_for_websocket_or_timeout(timeout_seconds=60)

            prices = None
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
                logger.warning(f"⏰ WebSocket timeout - using REST API fallback for price history")
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


def solar_curtailment_check():
    """
    Monitor Amber export prices and curtail solar export when price is 0c or negative

    Flow:
    1. Check Amber feed-in price for users with solar curtailment enabled
    2. If export price <= 0c:
       - Set grid export rule to 'never' to prevent exporting at negative prices
       - Workaround for Tesla API bug: Toggle to 'pv_only' then back to 'never' to force apply
    3. If export price > 0c:
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

            logger.info(f"Current export price for {user.email}: {feedin_price}c/kWh")

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
            logger.info(f"Current export rule for {user.email}: {current_export_rule}")

            # CURTAILMENT LOGIC: Export price is 0c or negative
            if feedin_price <= 0:
                logger.warning(f"🚫 Export price is {feedin_price}c/kWh - applying curtailment for {user.email}")

                # If already set to 'never', toggle to force apply (workaround for Tesla API bug)
                if current_export_rule == 'never':
                    logger.info(f"Export already set to 'never' - toggling to force apply for {user.email}")

                    # Toggle to pv_only
                    logger.info(f"Step 1: Setting export to 'pv_only' for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'pv_only')
                    if not result:
                        logger.error(f"Failed to toggle export to 'pv_only' for {user.email}")
                        error_count += 1
                        continue

                    # Wait a moment for the change to register
                    import time
                    time.sleep(2)

                    # Toggle back to never
                    logger.info(f"Step 2: Setting export back to 'never' for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'never')
                    if not result:
                        logger.error(f"Failed to set export to 'never' for {user.email}")
                        error_count += 1
                        continue

                    logger.info(f"✅ Toggled export rule to force curtailment for {user.email}")

                else:
                    # Not already 'never', so just set it
                    logger.info(f"Setting export to 'never' for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'never')
                    if not result:
                        logger.error(f"Failed to set export to 'never' for {user.email}")
                        error_count += 1
                        continue

                    logger.info(f"✅ Applied solar curtailment (export=never) for {user.email}")

                success_count += 1

            # NORMAL MODE: Export price is positive
            else:
                logger.info(f"✅ Export price is positive ({feedin_price}c/kWh) - normal operation for {user.email}")

                # If currently curtailed, restore to battery_ok (allows both solar and battery export)
                if current_export_rule == 'never':
                    logger.info(f"Restoring export from curtailment for {user.email}")
                    result = tesla_client.set_grid_export_rule(user.tesla_energy_site_id, 'battery_ok')
                    if not result:
                        logger.error(f"Failed to restore export to 'battery_ok' for {user.email}")
                        error_count += 1
                        continue

                    logger.info(f"✅ Restored export to 'battery_ok' for {user.email}")
                    success_count += 1
                else:
                    logger.debug(f"Export rule is already '{current_export_rule}' - no action needed for {user.email}")
                    success_count += 1

        except Exception as e:
            logger.error(f"Error checking solar curtailment for {user.email}: {e}", exc_info=True)
            error_count += 1
            continue

    logger.info(f"=== Solar curtailment check complete: {success_count} successful, {error_count} errors ===")

