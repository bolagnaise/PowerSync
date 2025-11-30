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
    CONF_SOLAR_CURTAILMENT_ENABLED,
    CONF_TESLA_API_PROVIDER,
    CONF_FLEET_API_ACCESS_TOKEN,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    SERVICE_SYNC_TOU,
    SERVICE_SYNC_NOW,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_AEMO_REGION,
    CONF_AEMO_SPIKE_THRESHOLD,
)
from .coordinator import (
    AmberPriceCoordinator,
    TeslaEnergyCoordinator,
    DemandChargeCoordinator,
)
import re


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that obfuscates sensitive data like API keys and tokens.
    Shows first 4 and last 4 characters with asterisks in between.
    """

    @staticmethod
    def obfuscate(value: str, show_chars: int = 4) -> str:
        """Obfuscate a string showing only first and last N characters."""
        if len(value) <= show_chars * 2:
            return '*' * len(value)
        return f"{value[:show_chars]}{'*' * (len(value) - show_chars * 2)}{value[-show_chars:]}"

    def _obfuscate_string(self, text: str) -> str:
        """Apply all obfuscation patterns to a string."""
        if not text:
            return text

        # Handle Bearer tokens
        text = re.sub(
            r'(Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle psk_ tokens (Amber API keys)
        text = re.sub(
            r'(psk_)([a-zA-Z0-9]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle authorization headers in websocket/API logs
        text = re.sub(
            r'(authorization:\s*Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        return text

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._obfuscate_string(str(v)) if isinstance(v, str) else v
                              for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_string(str(a)) if isinstance(a, str) else a
                                   for a in record.args)

        return True


_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SensitiveDataFilter())

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


class SyncCoordinator:
    """
    Coordinates Tesla sync between WebSocket and REST API (async version for Home Assistant).

    - WebSocket: If price data arrives, sync immediately (event-driven)
    - Cron fallback: At 60s into each 5-minute period (e.g., :01, :06, :11...),
      fetch directly from REST API (no wait needed - prices are fresh at 60s offset)

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

    async def wait_for_websocket_or_timeout(self, timeout_seconds=15):
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

    async def already_synced_this_period(self):
        """
        Check if we already synced for the current 5-minute period (read-only).
        Used by cron fallback to determine if WebSocket already handled this period.

        Returns:
            bool: True if already synced this period, False if not synced yet
        """
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        # Calculate current 5-minute period
        current_period = now.replace(second=0, microsecond=0)
        current_period = current_period.replace(minute=current_period.minute - (current_period.minute % 5))

        async with self._lock:
            return self._current_period == current_period

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


class AEMOSpikeManager:
    """
    Manages AEMO price spike detection and Tesla tariff modifications.

    When a price spike is detected:
    1. Save the current Tesla tariff
    2. Switch to autonomous mode
    3. Upload a spike tariff optimized for export
    4. Wait for price to normalize
    5. Restore the saved tariff and operation mode
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        region: str,
        threshold: float,
        site_id: str,
        api_token: str,
        api_provider: str = TESLA_PROVIDER_TESLEMETRY,
    ):
        """Initialize the AEMO spike manager."""
        self.hass = hass
        self.entry = entry
        self.region = region
        self.threshold = threshold
        self.site_id = site_id
        self.api_token = api_token
        self.api_provider = api_provider

        # State tracking
        self._in_spike_mode = False
        self._spike_start_time: datetime | None = None
        self._saved_tariff: dict | None = None
        self._saved_operation_mode: str | None = None
        self._last_price: float | None = None
        self._last_check: datetime | None = None

        # Create AEMO client
        from .aemo_client import AEMOAPIClient
        session = async_get_clientsession(hass)
        self._aemo_client = AEMOAPIClient(session)

        _LOGGER.info(
            "AEMO Spike Manager initialized: region=%s, threshold=$%.0f/MWh",
            region,
            threshold,
        )

    @property
    def in_spike_mode(self) -> bool:
        """Return whether currently in spike mode."""
        return self._in_spike_mode

    @property
    def last_price(self) -> float | None:
        """Return the last observed AEMO price."""
        return self._last_price

    @property
    def spike_start_time(self) -> datetime | None:
        """Return when the current spike started."""
        return self._spike_start_time

    async def check_and_handle_spike(self) -> None:
        """Check AEMO prices and handle spike mode transitions."""
        from homeassistant.util import dt as dt_util

        self._last_check = dt_util.utcnow()

        # Check for spike
        is_spike, current_price, price_data = await self._aemo_client.check_price_spike(
            self.region, self.threshold
        )

        if current_price is not None:
            self._last_price = current_price

        if current_price is None:
            _LOGGER.warning("Could not fetch AEMO price - skipping spike check")
            return

        # SPIKE DETECTED - Enter spike mode
        if is_spike and not self._in_spike_mode:
            await self._enter_spike_mode(current_price)

        # NO SPIKE - Exit spike mode if currently in it
        elif not is_spike and self._in_spike_mode:
            await self._exit_spike_mode(current_price)

        # Still in spike mode - maybe update tariff if price changed significantly
        elif is_spike and self._in_spike_mode:
            _LOGGER.debug(
                "Still in spike mode: $%.2f/MWh (threshold: $%.0f/MWh)",
                current_price,
                self.threshold,
            )

    async def _enter_spike_mode(self, current_price: float) -> None:
        """Enter spike mode: save tariff, switch to autonomous, upload spike tariff."""
        from homeassistant.util import dt as dt_util

        _LOGGER.warning(
            "SPIKE DETECTED: $%.2f/MWh >= $%.0f/MWh threshold - entering spike mode",
            current_price,
            self.threshold,
        )

        try:
            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if self.api_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Step 1: Save current tariff
            _LOGGER.info("Saving current tariff before spike mode...")
            async with session.get(
                f"{api_base}/api/1/energy_sites/{self.site_id}/tariff_rate",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self._saved_tariff = data.get("response", {}).get("tariff_content_v2")
                    _LOGGER.info("Saved current tariff for restoration after spike")
                else:
                    _LOGGER.warning("Could not save current tariff: %s", response.status)

            # Step 2: Get and save current operation mode
            async with session.get(
                f"{api_base}/api/1/energy_sites/{self.site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self._saved_operation_mode = data.get("response", {}).get("default_real_mode")
                    _LOGGER.info("Saved operation mode: %s", self._saved_operation_mode)

            # Step 3: Switch to autonomous mode for best export behavior
            if self._saved_operation_mode != "autonomous":
                _LOGGER.info("Switching to autonomous mode for optimal export...")
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{self.site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "autonomous"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info("Switched to autonomous mode")
                    else:
                        _LOGGER.warning("Could not switch operation mode: %s", response.status)

            # Step 4: Create and upload spike tariff
            spike_tariff = self._create_spike_tariff(current_price)
            success = await send_tariff_to_tesla(
                self.hass,
                self.site_id,
                spike_tariff,
                self.api_token,
            )

            if success:
                self._in_spike_mode = True
                self._spike_start_time = dt_util.utcnow()
                _LOGGER.warning(
                    "SPIKE MODE ACTIVE: Tariff uploaded to maximize export at $%.2f/MWh",
                    current_price,
                )
            else:
                _LOGGER.error("Failed to upload spike tariff")

        except Exception as e:
            _LOGGER.error("Error entering spike mode: %s", e, exc_info=True)

    async def _exit_spike_mode(self, current_price: float) -> None:
        """Exit spike mode: restore saved tariff and operation mode."""
        _LOGGER.info(
            "Price normalized: $%.2f/MWh < $%.0f/MWh threshold - exiting spike mode",
            current_price,
            self.threshold,
        )

        try:
            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if self.api_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Step 1: Switch to self_consumption mode first (helps tariff apply)
            _LOGGER.info("Switching to self_consumption mode before tariff restore...")
            async with session.post(
                f"{api_base}/api/1/energy_sites/{self.site_id}/operation",
                headers=headers,
                json={"default_real_mode": "self_consumption"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info("Switched to self_consumption mode")

            # Step 2: Restore saved tariff
            if self._saved_tariff:
                _LOGGER.info("Restoring saved tariff...")
                success = await send_tariff_to_tesla(
                    self.hass,
                    self.site_id,
                    self._saved_tariff,
                    self.api_token,
                )
                if success:
                    _LOGGER.info("Restored saved tariff successfully")
                else:
                    _LOGGER.error("Failed to restore saved tariff")
            else:
                _LOGGER.warning("No saved tariff to restore")

            # Step 3: Wait for Tesla to process the tariff
            await asyncio.sleep(5)

            # Step 4: Restore original operation mode
            restore_mode = self._saved_operation_mode or "autonomous"
            _LOGGER.info("Restoring operation mode to: %s", restore_mode)
            async with session.post(
                f"{api_base}/api/1/energy_sites/{self.site_id}/operation",
                headers=headers,
                json={"default_real_mode": restore_mode},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info("Restored operation mode to %s", restore_mode)

            # Clear spike state
            self._in_spike_mode = False
            self._spike_start_time = None
            _LOGGER.info("SPIKE MODE ENDED: Normal operation restored")

        except Exception as e:
            _LOGGER.error("Error exiting spike mode: %s", e, exc_info=True)

    def _create_spike_tariff(self, current_aemo_price_mwh: float) -> dict:
        """
        Create a Tesla tariff optimized for exporting during price spikes.

        Uses very high sell rates to encourage Powerwall to export all energy.
        """
        from homeassistant.util import dt as dt_util

        # Convert $/MWh to $/kWh (divide by 1000) and apply 3x markup
        # This creates a HUGE sell incentive that Powerwall will respond to
        sell_rate_spike = (current_aemo_price_mwh / 1000.0) * 3.0

        # Normal rates for buy (make it unattractive to import)
        buy_rate = 0.50  # 50c/kWh - expensive to discourage import
        sell_rate_normal = 0.08  # 8c/kWh normal feed-in

        # Get current 30-minute period
        now = dt_util.now()
        current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

        # Create rate periods - spike for next 2 hours (4 periods)
        buy_rates = []
        sell_rates = []
        spike_window_periods = 4

        for i in range(48):
            buy_rates.append(buy_rate)

            # Apply spike sell rate for current period + next few periods
            periods_from_now = (i - current_period_index) % 48
            if periods_from_now < spike_window_periods:
                sell_rates.append(sell_rate_spike)
            else:
                sell_rates.append(sell_rate_normal)

        tariff = {
            "code": "AEMO-SPIKE",
            "utility": "AEMO Spike Response",
            "name": f"Spike Tariff (${current_aemo_price_mwh:.0f}/MWh)",
            "daily_charges": [{"name": "Grid Connection", "amount": 1.0}],
            "demand_charges": {
                "ALL": {"ALL": 0}
            },
            "energy_charges": {
                "ALL": {
                    "ALL": 0
                }
            },
            "seasons": {
                "Summer": {
                    "fromMonth": 1,
                    "fromDay": 1,
                    "toMonth": 12,
                    "toDay": 31,
                    "tou_periods": {
                        "SPIKE": {
                            "periods": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": 0, "toMinute": 0}],
                            "buy": buy_rate,
                            "sell": sell_rate_spike,
                        }
                    }
                }
            },
            "sell_tariff": {
                "name": "Spike Export",
                "utility": "AEMO",
                "daily_charges": [],
                "demand_charges": {},
                "energy_charges": {
                    "ALL": {"ALL": sell_rate_spike}
                }
            }
        }

        _LOGGER.info(
            "Created spike tariff: buy=$%.2f/kWh, sell=$%.2f/kWh (AEMO price: $%.0f/MWh)",
            buy_rate,
            sell_rate_spike,
            current_aemo_price_mwh,
        )

        return tariff

    def get_status(self) -> dict:
        """Get current spike manager status."""
        return {
            "enabled": True,
            "region": self.region,
            "threshold": self.threshold,
            "in_spike_mode": self._in_spike_mode,
            "last_price": self._last_price,
            "spike_start_time": self._spike_start_time.isoformat() if self._spike_start_time else None,
            "last_check": self._last_check.isoformat() if self._last_check else None,
        }


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

    # Check if this is AEMO-only mode (no Amber API token)
    has_amber = bool(entry.data.get(CONF_AMBER_API_TOKEN))
    aemo_enabled = entry.options.get(
        CONF_AEMO_SPIKE_ENABLED,
        entry.data.get(CONF_AEMO_SPIKE_ENABLED, False)
    )

    if has_amber:
        _LOGGER.info("Running in Amber TOU Sync mode")
    elif aemo_enabled:
        _LOGGER.info("Running in AEMO Spike Detection only mode (no Amber)")
    else:
        _LOGGER.error("No Amber API token and AEMO spike detection not enabled")
        raise ConfigEntryNotReady("No pricing source configured")

    # Initialize sync coordinator for wait-with-timeout pattern
    coordinator = SyncCoordinator()
    _LOGGER.info("🎯 Sync coordinator initialized")

    # Initialize WebSocket client for real-time Amber prices (only if Amber mode)
    ws_client = None
    amber_coordinator = None

    if has_amber:
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
    if amber_coordinator:
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

    # Initialize AEMO Spike Manager if enabled
    aemo_spike_manager = None
    if aemo_enabled:
        aemo_region = entry.options.get(
            CONF_AEMO_REGION,
            entry.data.get(CONF_AEMO_REGION)
        )
        aemo_threshold = entry.options.get(
            CONF_AEMO_SPIKE_THRESHOLD,
            entry.data.get(CONF_AEMO_SPIKE_THRESHOLD, 300.0)
        )

        if aemo_region:
            aemo_spike_manager = AEMOSpikeManager(
                hass=hass,
                entry=entry,
                region=aemo_region,
                threshold=aemo_threshold,
                site_id=entry.data[CONF_TESLA_ENERGY_SITE_ID],
                api_token=tesla_api_token,
                api_provider=tesla_api_provider,
            )
            _LOGGER.info(
                "AEMO Spike Manager initialized: region=%s, threshold=$%.0f/MWh",
                aemo_region,
                aemo_threshold,
            )
        else:
            _LOGGER.warning("AEMO spike detection enabled but no region configured")

    # Store coordinators and WebSocket client in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "amber_coordinator": amber_coordinator,
        "tesla_coordinator": tesla_coordinator,
        "demand_charge_coordinator": demand_charge_coordinator,
        "aemo_spike_manager": aemo_spike_manager,
        "ws_client": ws_client,  # Store for cleanup on unload
        "entry": entry,
        "auto_sync_cancel": None,  # Will store the timer cancel function
        "aemo_spike_cancel": None,  # Will store the AEMO spike check cancel function
    }

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_sync_tou_with_websocket_data(websocket_data) -> None:
        """
        EVENT-DRIVEN: Handle sync with pre-fetched WebSocket data (called by WebSocket callback).
        This is the fast path - data already arrived, no waiting.
        """
        await _handle_sync_tou_internal(websocket_data)

    async def handle_sync_tou(call: ServiceCall) -> None:
        """
        CRON FALLBACK: Handle sync only if WebSocket hasn't delivered yet.

        Runs at :01 (60s into each 5-min period) so Amber REST API prices are fresh.
        No wait needed - just fetch directly from REST API.
        """
        # Skip if no Amber coordinator (AEMO-only mode)
        if not amber_coordinator:
            _LOGGER.debug("TOU sync skipped - no Amber coordinator (AEMO-only mode)")
            return

        # FALLBACK CHECK: Has WebSocket already synced this period?
        if await coordinator.already_synced_this_period():
            _LOGGER.info("⏭️  Cron triggered but WebSocket already synced this period - skipping (fallback not needed)")
            return

        # No wait needed - at 60s into period, REST API prices are fresh
        _LOGGER.info("⏰ Cron fallback: fetching prices from REST API (60s into period)")
        await _handle_sync_tou_internal(None)  # None = use REST API

    async def _handle_sync_tou_internal(websocket_data) -> None:
        """Internal sync logic shared by both event-driven and cron-fallback paths."""

        _LOGGER.info("=== Starting TOU sync ===")

        # Import tariff converter from existing code
        from .tariff_converter import (
            convert_amber_to_tesla_tariff,
            extract_most_recent_actual_interval,
        )

        # Get current interval price from WebSocket (real-time) or REST API fallback
        # WebSocket is PRIMARY source for current price, REST API is fallback if timeout
        current_actual_interval = None

        if websocket_data:
            # WebSocket data received within 60s - use it directly as primary source
            current_actual_interval = websocket_data
            general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
            feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
            _LOGGER.info(f"✅ Using WebSocket price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
        else:
            # WebSocket timeout - fallback to REST API for current price
            _LOGGER.warning(f"⏰ WebSocket timeout - using REST API fallback for current price")

            # Refresh coordinator to get REST API current prices
            await amber_coordinator.async_request_refresh()

            if amber_coordinator.data:
                # Extract most recent CurrentInterval/ActualInterval from 5-min forecast data
                forecast_5min = amber_coordinator.data.get("forecast_5min", [])
                current_actual_interval = extract_most_recent_actual_interval(forecast_5min)

                if current_actual_interval:
                    general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                    _LOGGER.info(f"📡 Using REST API price for current interval: general={general_price}¢/kWh")
                else:
                    _LOGGER.warning("No current price data available, proceeding with 30-min forecast only")
            else:
                _LOGGER.error("No Amber price data available from REST API")

        # Refresh coordinator to get latest forecast data (regardless of WebSocket status)
        await amber_coordinator.async_request_refresh()

        if not amber_coordinator.data:
            _LOGGER.error("No Amber forecast data available")
            return

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
        if amber_coordinator:
            await amber_coordinator.async_request_refresh()
        await tesla_coordinator.async_request_refresh()

    async def handle_solar_curtailment_check(call: ServiceCall = None) -> None:
        """
        Check Amber export prices and curtail solar export when price is below 1c/kWh.

        Flow:
        1. Check if curtailment is enabled for this entry
        2. Get feed-in price from Amber coordinator
        3. If export price < 1c: Set grid export rule to 'never'
        4. If export price >= 1c: Restore normal export ('battery_ok')
        """
        # Check if curtailment is enabled
        curtailment_enabled = entry.options.get(
            CONF_SOLAR_CURTAILMENT_ENABLED,
            entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
        )

        if not curtailment_enabled:
            _LOGGER.debug("Solar curtailment is disabled, skipping check")
            return

        # Skip if no Amber coordinator (AEMO-only mode) - curtailment requires Amber prices
        if not amber_coordinator:
            _LOGGER.debug("Solar curtailment skipped - no Amber coordinator (AEMO-only mode)")
            return

        _LOGGER.info("=== Starting solar curtailment check ===")

        try:
            # Refresh Amber prices to get latest feed-in price
            await amber_coordinator.async_request_refresh()

            if not amber_coordinator.data:
                _LOGGER.error("No Amber price data available for curtailment check")
                return

            # Get feed-in (export) price from current prices
            current_prices = amber_coordinator.data.get("current", [])
            if not current_prices:
                _LOGGER.warning("No current price data available for curtailment check")
                return

            feedin_price = None
            for price_data in current_prices:
                if price_data.get("channelType") == "feedIn":
                    feedin_price = price_data.get("perKwh", 0)
                    break

            if feedin_price is None:
                _LOGGER.warning("No feed-in price found in Amber data")
                return

            _LOGGER.info(f"Current export price: {feedin_price}c/kWh")

            # Get current grid export settings from Tesla
            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {entry.data[CONF_TESLEMETRY_API_TOKEN]}",
                "Content-Type": "application/json",
            }

            # Determine API base URL
            api_base_url = TESLEMETRY_API_BASE_URL
            if tesla_api_provider == TESLA_PROVIDER_FLEET_API:
                api_base_url = FLEET_API_BASE_URL

            # Get current export rule
            try:
                async with session.get(
                    f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        _LOGGER.error(f"Failed to get grid export settings: {response.status} - {error_text}")
                        return

                    data = await response.json()
                    current_export_rule = data.get("response", {}).get("customer_preferred_export_rule")
                    _LOGGER.info(f"Current export rule: {current_export_rule}")

            except Exception as err:
                _LOGGER.error(f"Error fetching grid export settings: {err}")
                return

            # CURTAILMENT LOGIC: Export price is below 1c/kWh
            if feedin_price < 1:
                _LOGGER.warning(f"🚫 CURTAILMENT TRIGGERED: Export price is {feedin_price}c/kWh (<1c)")
                _LOGGER.info(f"Current state: export_rule='{current_export_rule}' → Target: export_rule='never'")

                # If already set to 'never', toggle to force apply (Tesla API bug workaround)
                if current_export_rule == "never":
                    _LOGGER.info("Already curtailed - toggling to force re-apply (Tesla API workaround)")

                    # Toggle to pv_only
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "pv_only"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"Failed to toggle export to 'pv_only': {response.status} - {error_text}")
                                return

                        # Wait for change to register
                        await asyncio.sleep(2)

                        # Toggle back to never
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "never"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"Failed to set export to 'never': {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT MAINTAINED: Toggled export rule to force re-apply")

                    except Exception as err:
                        _LOGGER.error(f"Error toggling export rule: {err}")
                        return

                else:
                    # Not already 'never', so just set it
                    _LOGGER.info(f"Applying curtailment: '{current_export_rule}' → 'never'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "never"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"❌ Failed to apply curtailment: {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT APPLIED: Export rule changed '{current_export_rule}' → 'never'")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

                _LOGGER.info(f"📊 Action summary: Curtailment active (price: {feedin_price}c/kWh, export: 'never')")

            # NORMAL MODE: Export price is positive
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export price is {feedin_price}c/kWh (>0)")

                # If currently curtailed, restore to battery_ok
                if current_export_rule == "never":
                    _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → 'battery_ok'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "battery_ok"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"❌ Failed to restore from curtailment: {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → 'battery_ok'")
                        _LOGGER.info(f"📊 Action summary: Restored to normal (price: {feedin_price}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")
                    _LOGGER.info(f"📊 Action summary: No change needed (price: {feedin_price}c/kWh, export: '{current_export_rule}')")

        except Exception as e:
            _LOGGER.error(f"❌ Unexpected error in solar curtailment check: {e}", exc_info=True)

        _LOGGER.info("=== Solar curtailment check complete ===")

    async def handle_solar_curtailment_with_websocket_data(websocket_data) -> None:
        """
        EVENT-DRIVEN: Check solar curtailment using WebSocket price data.
        Called by WebSocket callback - uses price data directly without REST API refresh.
        """
        # Check if curtailment is enabled
        curtailment_enabled = entry.options.get(
            CONF_SOLAR_CURTAILMENT_ENABLED,
            entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
        )

        if not curtailment_enabled:
            _LOGGER.debug("Solar curtailment is disabled, skipping check")
            return

        _LOGGER.info("=== Starting solar curtailment check (WebSocket event-driven) ===")

        try:
            # Extract feed-in price from WebSocket data
            feedin_data = websocket_data.get('feedIn', {}) if websocket_data else None
            if not feedin_data:
                _LOGGER.warning("No feed-in data in WebSocket price update")
                return

            feedin_price = feedin_data.get('perKwh')
            if feedin_price is None:
                _LOGGER.warning("No perKwh in WebSocket feed-in data")
                return

            _LOGGER.info(f"Current export price (WebSocket): {feedin_price}c/kWh")

            # Get current grid export settings from Tesla
            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {entry.data[CONF_TESLEMETRY_API_TOKEN]}",
                "Content-Type": "application/json",
            }

            # Determine API base URL
            api_base_url = TESLEMETRY_API_BASE_URL
            if tesla_api_provider == TESLA_PROVIDER_FLEET_API:
                api_base_url = FLEET_API_BASE_URL

            # Get current export rule
            try:
                async with session.get(
                    f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        _LOGGER.error(f"Failed to get grid export settings: {response.status} - {error_text}")
                        return

                    data = await response.json()
                    current_export_rule = data.get("response", {}).get("customer_preferred_export_rule")
                    _LOGGER.info(f"Current export rule: {current_export_rule}")

            except Exception as err:
                _LOGGER.error(f"Error fetching grid export settings: {err}")
                return

            # CURTAILMENT LOGIC: Export price is below 1c/kWh
            if feedin_price < 1:
                _LOGGER.warning(f"🚫 CURTAILMENT TRIGGERED: Export price is {feedin_price}c/kWh (<1c)")

                if current_export_rule == "never":
                    _LOGGER.info("Already curtailed - toggling to force re-apply (Tesla API workaround)")

                    # Toggle to pv_only
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "pv_only"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"Failed to toggle export to 'pv_only': {response.status} - {error_text}")
                                return

                        await asyncio.sleep(2)

                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "never"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"Failed to set export to 'never': {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT MAINTAINED: Toggled export rule to force re-apply")

                    except Exception as err:
                        _LOGGER.error(f"Error toggling export rule: {err}")
                        return

                else:
                    _LOGGER.info(f"Applying curtailment: '{current_export_rule}' → 'never'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "never"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"❌ Failed to apply curtailment: {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT APPLIED: Export rule changed '{current_export_rule}' → 'never'")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

                _LOGGER.info(f"📊 Action summary: Curtailment active (price: {feedin_price}c/kWh, export: 'never')")

            # NORMAL MODE: Export price is positive
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export price is {feedin_price}c/kWh (>0)")

                if current_export_rule == "never":
                    _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → 'battery_ok'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": "battery_ok"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as response:
                            if response.status != 200:
                                error_text = await response.text()
                                _LOGGER.error(f"❌ Failed to restore from curtailment: {response.status} - {error_text}")
                                return

                        _LOGGER.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → 'battery_ok'")
                        _LOGGER.info(f"📊 Action summary: Restored to normal (price: {feedin_price}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")
                    _LOGGER.info(f"📊 Action summary: No change needed (price: {feedin_price}c/kWh, export: '{current_export_rule}')")

        except Exception as e:
            _LOGGER.error(f"❌ Unexpected error in solar curtailment check: {e}", exc_info=True)

        _LOGGER.info("=== Solar curtailment check complete ===")

    hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU, handle_sync_tou)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)

    # Wire up WebSocket sync callback now that handlers are defined
    if ws_client:
        def websocket_sync_callback(prices_data):
            """
            EVENT-DRIVEN SYNC: WebSocket price arrival triggers immediate sync.
            This is the primary trigger - cron jobs are just fallback.
            """
            # Notify coordinator (for period deduplication)
            coordinator.notify_websocket_update(prices_data)

            # Check if we should sync this period (prevents duplicates)
            async def trigger_sync():
                if not await coordinator.should_sync_this_period():
                    _LOGGER.info("⏭️  WebSocket price received but already synced this period, skipping")
                    return

                # TRIGGER SYNC IMMEDIATELY with WebSocket data (event-driven!)
                _LOGGER.info("🚀 WebSocket price received - triggering immediate sync (event-driven)")

                try:
                    # 1. Sync TOU to Tesla with WebSocket price
                    await handle_sync_tou_with_websocket_data(prices_data)

                    # 2. Check solar curtailment with WebSocket price
                    await handle_solar_curtailment_with_websocket_data(prices_data)

                    _LOGGER.info("✅ Event-driven sync completed successfully")
                except Exception as e:
                    _LOGGER.error(f"❌ Error in event-driven sync: {e}", exc_info=True)

            # Schedule the async sync
            hass.async_create_task(trigger_sync())

        # Assign callback to WebSocket client
        ws_client._sync_callback = websocket_sync_callback
        _LOGGER.info("🔗 WebSocket sync callback configured to trigger immediate sync")

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

    # Perform initial TOU sync if auto-sync is enabled (only in Amber mode)
    auto_sync_enabled = entry.options.get(
        CONF_AUTO_SYNC_ENABLED,
        entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
    )

    if auto_sync_enabled and amber_coordinator:
        _LOGGER.info("Performing initial TOU sync")
        await handle_sync_tou(None)
    elif not amber_coordinator:
        _LOGGER.info("Skipping initial TOU sync - AEMO-only mode")

    # Start the automatic sync timer (every 5 minutes, 60s after Amber price updates)
    # Triggers at :01:00, :06:00, :11:00, :16:00, :21:00, :26:00, :31:00, :36:00, :41:00, :46:00, :51:00, :56:00
    # Running 60s after period start ensures Amber prices are fresh when using REST API fallback
    cancel_timer = async_track_utc_time_change(
        hass,
        auto_sync_tou,
        minute=[1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 51, 56],
        second=0,  # 60s after Amber price update, REST API prices are fresh
    )

    # Store the cancel function so we can clean it up later
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel"] = cancel_timer

    # Set up automatic curtailment check every 5 minutes (same timing as TOU sync)
    # Triggers at :01:00, :06:00, :11:00, etc. - 60s after Amber price updates
    async def auto_curtailment_check(now):
        """Automatically check curtailment if enabled."""
        await handle_solar_curtailment_check(None)

    curtailment_cancel_timer = async_track_utc_time_change(
        hass,
        auto_curtailment_check,
        minute=[1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 51, 56],
        second=0,  # Same timing as TOU sync - 60s after Amber price updates
    )

    # Store the curtailment cancel function
    hass.data[DOMAIN][entry.entry_id]["curtailment_cancel"] = curtailment_cancel_timer
    _LOGGER.info("Solar curtailment check scheduled every 5 minutes at :01 (same as TOU sync)")

    # Set up automatic AEMO spike check every minute if enabled
    if aemo_spike_manager:
        async def auto_aemo_spike_check(now):
            """Automatically check AEMO prices for spikes."""
            await aemo_spike_manager.check_and_handle_spike()

        # Check every minute at :35 seconds
        aemo_spike_cancel_timer = async_track_utc_time_change(
            hass,
            auto_aemo_spike_check,
            second=35,  # Every minute at :35 seconds
        )

        # Store the AEMO spike cancel function
        hass.data[DOMAIN][entry.entry_id]["aemo_spike_cancel"] = aemo_spike_cancel_timer
        _LOGGER.info(
            "AEMO spike check scheduled every minute (region=%s, threshold=$%.0f/MWh)",
            aemo_spike_manager.region,
            aemo_spike_manager.threshold,
        )

        # Perform initial AEMO spike check
        _LOGGER.info("Performing initial AEMO spike check")
        await aemo_spike_manager.check_and_handle_spike()

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

    # Cancel the curtailment timer if it exists
    if curtailment_cancel := entry_data.get("curtailment_cancel"):
        curtailment_cancel()
        _LOGGER.debug("Cancelled curtailment timer")

    # Cancel the AEMO spike timer if it exists
    if aemo_spike_cancel := entry_data.get("aemo_spike_cancel"):
        aemo_spike_cancel()
        _LOGGER.debug("Cancelled AEMO spike timer")

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
