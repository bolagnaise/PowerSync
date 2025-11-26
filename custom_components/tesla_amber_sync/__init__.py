"""The Tesla Sync integration."""
from __future__ import annotations

import aiohttp
import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform, CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_utc_time_change

from .const import (
    DOMAIN,
    CONF_AMBER_API_TOKEN,
    CONF_AMBER_FORECAST_TYPE,
    CONF_AUTO_SYNC_ENABLED,
    CONF_TESLEMETRY_API_TOKEN,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_DEMAND_CHARGE_ENABLED,
    CONF_DEMAND_CHARGE_RATE,
    CONF_DEMAND_CHARGE_START_TIME,
    CONF_DEMAND_CHARGE_END_TIME,
    CONF_DEMAND_CHARGE_DAYS,
    CONF_DEMAND_CHARGE_BILLING_DAY,
    CONF_DEMAND_CHARGE_APPLY_TO,
    CONF_DAILY_SUPPLY_CHARGE,
    CONF_MONTHLY_SUPPLY_CHARGE,
    CONF_TESLA_API_PROVIDER,
    CONF_FLEET_API_ACCESS_TOKEN,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    SERVICE_SYNC_TOU,
    SERVICE_SYNC_NOW,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
)
from .coordinator import (
    AmberPriceCoordinator,
    TeslaEnergyCoordinator,
    DemandChargeCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


class SyncCoordinator:
    """
    Coordinates Tesla sync with WebSocket wait-with-timeout pattern (async version for Home Assistant).

    At the start of each 5-minute period:
    1. Wait up to 60 seconds for WebSocket to deliver price data
    2. If WebSocket arrives → Use WebSocket data and sync immediately
    3. If timeout (60s) → Fallback to REST API sync

    Only ONE sync per 5-minute period.
    """

    def __init__(self):
        self._websocket_event = asyncio.Event()
        self._websocket_data = None
        self._current_period = None  # Track which 5-min period we're in
        self._lock = asyncio.Lock()

    def notify_websocket_update(self, prices_data):
        """Called by WebSocket when new price data arrives."""
        self._websocket_data = prices_data
        self._websocket_event.set()
        _LOGGER.info("📡 WebSocket price update received, notifying sync coordinator")

    async def wait_for_websocket_or_timeout(self, timeout_seconds=60):
        """
        Wait for WebSocket data or timeout.

        Returns:
            dict: WebSocket price data if arrived within timeout, None if timeout
        """
        _LOGGER.info(f"⏱️  Waiting up to {timeout_seconds}s for WebSocket price update...")

        try:
            # Wait for event with timeout
            await asyncio.wait_for(self._websocket_event.wait(), timeout=timeout_seconds)

            async with self._lock:
                if self._websocket_data:
                    _LOGGER.info("✅ WebSocket data received, using real-time prices")
                    data = self._websocket_data
                    # Clear for next period
                    self._websocket_event.clear()
                    self._websocket_data = None
                    return data
                else:
                    _LOGGER.warning("⏰ WebSocket event set but no data available")
                    self._websocket_event.clear()
                    return None

        except asyncio.TimeoutError:
            _LOGGER.warning(f"⏰ WebSocket timeout after {timeout_seconds}s, falling back to REST API")
            # Clear for next period
            self._websocket_event.clear()
            async with self._lock:
                self._websocket_data = None
            return None

    async def should_sync_this_period(self):
        """
        Check if we should sync for the current 5-minute period.
        Prevents duplicate syncs within the same period.

        Returns:
            bool: True if this is a new period and we should sync
        """
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        # Calculate current 5-minute period (e.g., 17:00, 17:05, 17:10, etc.)
        current_period = now.replace(second=0, microsecond=0)
        current_period = current_period.replace(minute=current_period.minute - (current_period.minute % 5))

        async with self._lock:
            if self._current_period == current_period:
                _LOGGER.info(f"⏭️  Already synced for period {current_period}, skipping")
                return False

            self._current_period = current_period
            _LOGGER.info(f"🆕 New sync period: {current_period}")
            return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry to new format."""
    _LOGGER.info("Migrating Tesla Sync config entry from version %s", config_entry.version)

    if config_entry.version == 1:
        # Migrate from version 1 to version 2
        # Changes: tesla_site_id -> tesla_energy_site_id
        new_data = {**config_entry.data}

        if "tesla_site_id" in new_data:
            new_data["tesla_energy_site_id"] = new_data.pop("tesla_site_id")
            _LOGGER.info("Migrated tesla_site_id to tesla_energy_site_id")

        # Update the config entry with new data and version
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2
        )

        _LOGGER.info("Migration to version 2 complete")

    return True


async def send_tariff_to_tesla(
    hass: HomeAssistant,
    site_id: str,
    tariff_data: dict[str, Any],
    api_token: str,
    max_retries: int = 3,
    timeout_seconds: int = 60,
) -> bool:
    """Send tariff data to Tesla via Teslemetry API with retry logic.

    Args:
        hass: HomeAssistant instance
        site_id: Tesla energy site ID
        tariff_data: Tariff data to send
        api_token: Teslemetry API token
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Request timeout in seconds (default: 60)

    Returns:
        True if successful, False otherwise
    """
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "tou_settings": {
            "tariff_content_v2": tariff_data
        }
    }

    url = f"{TESLEMETRY_API_BASE_URL}/api/1/energy_sites/{site_id}/time_of_use_settings"
    last_error = None

    for attempt in range(max_retries):
        try:
            # Exponential backoff: 2^attempt seconds (1s, 2s, 4s)
            if attempt > 0:
                wait_time = 2 ** attempt
                _LOGGER.info(
                    "TOU sync retry attempt %d/%d after %ds delay",
                    attempt + 1,
                    max_retries,
                    wait_time
                )
                await asyncio.sleep(wait_time)

            _LOGGER.debug(
                "Sending TOU schedule to Teslemetry API for site %s (attempt %d/%d)",
                site_id,
                attempt + 1,
                max_retries
            )

            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    _LOGGER.info(
                        "Successfully synced TOU schedule to Tesla (attempt %d/%d)",
                        attempt + 1,
                        max_retries
                    )
                    _LOGGER.debug("Tesla API response: %s", result)
                    return True

                # Log error and potentially retry
                error_text = await response.text()

                if response.status >= 500:
                    # Server error - retry
                    _LOGGER.warning(
                        "Failed to sync TOU schedule: %s - %s (attempt %d/%d, will retry)",
                        response.status,
                        error_text[:200],
                        attempt + 1,
                        max_retries
                    )
                    last_error = f"Server error {response.status}"
                    continue  # Retry on 5xx errors
                else:
                    # Client error - don't retry
                    _LOGGER.error(
                        "Failed to sync TOU schedule: %s - %s (client error, not retrying)",
                        response.status,
                        error_text
                    )
                    return False

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Error communicating with Teslemetry API (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                err
            )
            last_error = f"Network error: {err}"
            continue  # Retry on network errors

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Teslemetry API timeout after %ds (attempt %d/%d)",
                timeout_seconds,
                attempt + 1,
                max_retries
            )
            last_error = f"Timeout after {timeout_seconds}s"
            continue  # Retry on timeout

        except Exception as err:
            _LOGGER.exception(
                "Unexpected error syncing TOU schedule (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                err
            )
            last_error = f"Unexpected error: {err}"
            # Don't continue - unexpected errors might indicate a bug
            return False

    # All retries failed
    _LOGGER.error(
        "Failed to sync TOU schedule after %d attempts. Last error: %s",
        max_retries,
        last_error
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesla Sync from a config entry."""
    _LOGGER.info("Setting up Tesla Sync integration")

    # Initialize sync coordinator for wait-with-timeout pattern
    coordinator = SyncCoordinator()
    _LOGGER.info("🎯 Sync coordinator initialized")

    # Initialize WebSocket client for real-time Amber prices
    ws_client = None

    # Create a placeholder for the sync callback that will be set up later
    # after coordinators are initialized
    websocket_sync_callback = None

    try:
        from .websocket_client import AmberWebSocketClient

        ws_client = AmberWebSocketClient(
            api_token=entry.data[CONF_AMBER_API_TOKEN],
            site_id=entry.data.get("amber_site_id"),
            sync_callback=None,  # Will be set up after coordinators are initialized
        )
        await ws_client.start()
        _LOGGER.info("🔌 Amber WebSocket client initialized and started")
    except Exception as e:
        _LOGGER.error(f"Failed to initialize WebSocket client: {e}", exc_info=True)
        _LOGGER.warning("WebSocket client not available - will use REST API fallback")
        ws_client = None

    # Initialize coordinators for data fetching
    amber_coordinator = AmberPriceCoordinator(
        hass,
        entry.data[CONF_AMBER_API_TOKEN],
        entry.data.get("amber_site_id"),
        ws_client=ws_client,  # Pass WebSocket client to coordinator
    )

    # Check if Tesla Fleet integration is configured and available
    tesla_api_token = None
    tesla_api_provider = TESLA_PROVIDER_TESLEMETRY  # Default to Teslemetry

    tesla_fleet_entries = hass.config_entries.async_entries("tesla_fleet")
    if tesla_fleet_entries:
        for tesla_entry in tesla_fleet_entries:
            if tesla_entry.state == ConfigEntryState.LOADED:
                # Tesla Fleet integration is loaded, try to use its tokens
                try:
                    if CONF_TOKEN in tesla_entry.data:
                        token_data = tesla_entry.data[CONF_TOKEN]
                        if CONF_ACCESS_TOKEN in token_data:
                            tesla_api_token = token_data[CONF_ACCESS_TOKEN]
                            tesla_api_provider = TESLA_PROVIDER_FLEET_API
                            _LOGGER.info(
                                "Detected Tesla Fleet integration - using Fleet API tokens for site %s",
                                entry.data[CONF_TESLA_ENERGY_SITE_ID]
                            )
                            break
                except Exception as e:
                    _LOGGER.warning(
                        "Failed to extract tokens from Tesla Fleet integration: %s",
                        e
                    )

    # Fall back to Teslemetry if Tesla Fleet not available or failed
    if not tesla_api_token:
        if CONF_TESLEMETRY_API_TOKEN in entry.data:
            tesla_api_token = entry.data[CONF_TESLEMETRY_API_TOKEN]
            tesla_api_provider = TESLA_PROVIDER_TESLEMETRY
            _LOGGER.info("Using Teslemetry API for site %s", entry.data[CONF_TESLA_ENERGY_SITE_ID])
        else:
            _LOGGER.error("No Tesla API credentials available (neither Fleet API nor Teslemetry)")
            raise ConfigEntryNotReady("No Tesla API credentials configured")

    tesla_coordinator = TeslaEnergyCoordinator(
        hass,
        entry.data[CONF_TESLA_ENERGY_SITE_ID],
        tesla_api_token,
        api_provider=tesla_api_provider,
    )

    # Fetch initial data
    await amber_coordinator.async_config_entry_first_refresh()
    await tesla_coordinator.async_config_entry_first_refresh()

    # Initialize demand charge coordinator if enabled
    demand_charge_coordinator = None
    demand_charge_enabled = entry.options.get(
        CONF_DEMAND_CHARGE_ENABLED,
        entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False)
    )
    if demand_charge_enabled:
        demand_charge_rate = entry.options.get(
            CONF_DEMAND_CHARGE_RATE,
            entry.data.get(CONF_DEMAND_CHARGE_RATE, 10.0)
        )
        demand_charge_start_time = entry.options.get(
            CONF_DEMAND_CHARGE_START_TIME,
            entry.data.get(CONF_DEMAND_CHARGE_START_TIME, "14:00")
        )
        demand_charge_end_time = entry.options.get(
            CONF_DEMAND_CHARGE_END_TIME,
            entry.data.get(CONF_DEMAND_CHARGE_END_TIME, "20:00")
        )
        demand_charge_days = entry.options.get(
            CONF_DEMAND_CHARGE_DAYS,
            entry.data.get(CONF_DEMAND_CHARGE_DAYS, "All Days")
        )
        demand_charge_billing_day = entry.options.get(
            CONF_DEMAND_CHARGE_BILLING_DAY,
            entry.data.get(CONF_DEMAND_CHARGE_BILLING_DAY, 1)
        )
        daily_supply_charge = entry.options.get(
            CONF_DAILY_SUPPLY_CHARGE,
            entry.data.get(CONF_DAILY_SUPPLY_CHARGE, 0.0)
        )
        monthly_supply_charge = entry.options.get(
            CONF_MONTHLY_SUPPLY_CHARGE,
            entry.data.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0)
        )

        demand_charge_coordinator = DemandChargeCoordinator(
            hass,
            tesla_coordinator,
            enabled=True,
            rate=demand_charge_rate,
            start_time=demand_charge_start_time,
            end_time=demand_charge_end_time,
            days=demand_charge_days,
            billing_day=demand_charge_billing_day,
            daily_supply_charge=daily_supply_charge,
            monthly_supply_charge=monthly_supply_charge,
        )
        await demand_charge_coordinator.async_config_entry_first_refresh()
        _LOGGER.info("Demand charge coordinator initialized")

    # Store coordinators and WebSocket client in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "amber_coordinator": amber_coordinator,
        "tesla_coordinator": tesla_coordinator,
        "demand_charge_coordinator": demand_charge_coordinator,
        "ws_client": ws_client,  # Store for cleanup on unload
        "entry": entry,
        "auto_sync_cancel": None,  # Will store the timer cancel function
    }

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_sync_tou(call: ServiceCall) -> None:
        """
        Handle the sync TOU schedule service call.

        Uses wait-with-timeout pattern:
        1. Check if we should sync this period (prevents duplicates within same 5-min window)
        2. Wait up to 60s for WebSocket price update
        3. If WebSocket arrives -> Use WebSocket data
        4. If timeout -> Fallback to REST API

        Only ONE sync per 5-minute period.
        """
        # Check if we should sync this period (prevents duplicates within same 5-min window)
        if not await coordinator.should_sync_this_period():
            return

        # Wait for WebSocket or timeout (60s)
        websocket_data = await coordinator.wait_for_websocket_or_timeout(timeout_seconds=60)

        _LOGGER.info("=== Starting TOU sync ===")

        # Get latest Amber prices
        await amber_coordinator.async_request_refresh()

        if not amber_coordinator.data:
            _LOGGER.error("No Amber price data available")
            return

        # Import tariff converter from existing code
        from .tariff_converter import (
            convert_amber_to_tesla_tariff,
            extract_most_recent_actual_interval,
        )

        # Extract most recent CurrentInterval/ActualInterval from 5-min forecast data
        # This captures short-term price spikes that would otherwise be averaged out
        forecast_5min = amber_coordinator.data.get("forecast_5min", [])
        current_actual_interval = extract_most_recent_actual_interval(forecast_5min)
        if current_actual_interval:
            _LOGGER.info("CurrentInterval/ActualInterval extracted for current period pricing")
        else:
            _LOGGER.info("No CurrentInterval/ActualInterval available, will use 30-min forecast averaging")

        # Get forecast type from options (if set) or data (from initial config)
        forecast_type = entry.options.get(
            CONF_AMBER_FORECAST_TYPE,
            entry.data.get(CONF_AMBER_FORECAST_TYPE, "predicted")
        )
        _LOGGER.info(f"Using Amber forecast type: {forecast_type}")

        # Fetch Powerwall timezone from site_info
        # This ensures correct timezone handling for TOU schedule alignment
        powerwall_timezone = None
        site_info = await tesla_coordinator.async_get_site_info()
        if site_info:
            powerwall_timezone = site_info.get("installation_time_zone")
            if powerwall_timezone:
                _LOGGER.info(f"Using Powerwall timezone: {powerwall_timezone}")
            else:
                _LOGGER.warning("No installation_time_zone in site_info, will auto-detect from Amber data")
        else:
            _LOGGER.warning("Failed to fetch site_info, will auto-detect timezone from Amber data")

        # Get demand charge configuration from options (if set) or data (from initial config)
        demand_charge_enabled = entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False)
        )
        demand_charge_rate = entry.options.get(
            CONF_DEMAND_CHARGE_RATE,
            entry.data.get(CONF_DEMAND_CHARGE_RATE, 0.0)
        )
        demand_charge_start_time = entry.options.get(
            CONF_DEMAND_CHARGE_START_TIME,
            entry.data.get(CONF_DEMAND_CHARGE_START_TIME, "14:00")
        )
        demand_charge_end_time = entry.options.get(
            CONF_DEMAND_CHARGE_END_TIME,
            entry.data.get(CONF_DEMAND_CHARGE_END_TIME, "20:00")
        )
        demand_charge_apply_to = entry.options.get(
            CONF_DEMAND_CHARGE_APPLY_TO,
            entry.data.get(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only")
        )

        if demand_charge_enabled:
            _LOGGER.info(
                "Demand charges enabled: $%.2f/kW from %s to %s (applied to: %s)",
                demand_charge_rate,
                demand_charge_start_time,
                demand_charge_end_time,
                demand_charge_apply_to,
            )

        # Convert prices to Tesla tariff format
        tariff = convert_amber_to_tesla_tariff(
            amber_coordinator.data.get("forecast", []),
            tesla_energy_site_id=entry.data[CONF_TESLA_ENERGY_SITE_ID],
            forecast_type=forecast_type,
            powerwall_timezone=powerwall_timezone,
            current_actual_interval=current_actual_interval,
            demand_charge_enabled=demand_charge_enabled,
            demand_charge_rate=demand_charge_rate,
            demand_charge_start_time=demand_charge_start_time,
            demand_charge_end_time=demand_charge_end_time,
            demand_charge_apply_to=demand_charge_apply_to,
        )

        if not tariff:
            _LOGGER.error("Failed to convert Amber prices to Tesla tariff")
            return

        # Send tariff to Tesla via Teslemetry API
        success = await send_tariff_to_tesla(
            hass,
            entry.data[CONF_TESLA_ENERGY_SITE_ID],
            tariff,
            entry.data[CONF_TESLEMETRY_API_TOKEN],
        )

        if success:
            _LOGGER.info("TOU schedule synced successfully")
        else:
            _LOGGER.error("Failed to sync TOU schedule")

    async def handle_sync_now(call: ServiceCall) -> None:
        """Handle the sync now service call."""
        _LOGGER.info("Immediate data refresh requested")
        await amber_coordinator.async_request_refresh()
        await tesla_coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU, handle_sync_tou)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)

    # Wire up WebSocket sync callback now that handlers are defined
    if ws_client:
        def websocket_sync_callback(prices_data):
            """Callback to notify sync coordinator when WebSocket receives price update."""
            coordinator.notify_websocket_update(prices_data)

        # Assign callback to WebSocket client
        ws_client._sync_callback = websocket_sync_callback
        _LOGGER.info("🔗 WebSocket sync callback configured to notify coordinator")

    # Set up automatic TOU sync every 5 minutes if auto-sync is enabled
    async def auto_sync_tou(now):
        """Automatically sync TOU schedule if enabled."""
        # Check if auto-sync is enabled in the config entry options
        auto_sync_enabled = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
        )

        _LOGGER.debug(
            "Auto-sync check: options=%s, data=%s, enabled=%s",
            entry.options.get(CONF_AUTO_SYNC_ENABLED),
            entry.data.get(CONF_AUTO_SYNC_ENABLED),
            auto_sync_enabled
        )

        if auto_sync_enabled:
            _LOGGER.debug("Auto-sync enabled, triggering TOU sync")
            await handle_sync_tou(None)
        else:
            _LOGGER.debug("Auto-sync disabled, skipping TOU sync")

    # Perform initial TOU sync if auto-sync is enabled
    auto_sync_enabled = entry.options.get(
        CONF_AUTO_SYNC_ENABLED,
        entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
    )

    if auto_sync_enabled:
        _LOGGER.info("Performing initial TOU sync")
        await handle_sync_tou(None)

    # Start the automatic sync timer (every 5 minutes, at start of period)
    # Triggers at :00:00, :05:00, :10:00, :15:00, :20:00, :25:00, :30:00, :35:00, :40:00, :45:00, :50:00, :55:00
    # At :00, waits up to 60s for WebSocket, then falls back to REST API if no WebSocket data
    cancel_timer = async_track_utc_time_change(
        hass,
        auto_sync_tou,
        minute=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
        second=0,  # Start of period, wait for WebSocket
    )

    # Store the cancel function so we can clean it up later
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel"] = cancel_timer

    _LOGGER.info("Tesla Sync integration setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Tesla Sync integration")

    # Cancel the auto-sync timer if it exists
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if cancel_timer := entry_data.get("auto_sync_cancel"):
        cancel_timer()
        _LOGGER.debug("Cancelled auto-sync timer")

    # Stop WebSocket client if it exists
    if ws_client := entry_data.get("ws_client"):
        try:
            await ws_client.stop()
            _LOGGER.info("🔌 WebSocket client stopped")
        except Exception as e:
            _LOGGER.error(f"Error stopping WebSocket client: {e}")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove services if this is the last entry
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_TOU)
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_NOW)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
