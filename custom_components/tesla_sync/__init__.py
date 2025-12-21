"""The Tesla Sync integration."""
from __future__ import annotations

import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform, CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_send

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
    CONF_DEMAND_ARTIFICIAL_PRICE,
    CONF_DAILY_SUPPLY_CHARGE,
    CONF_MONTHLY_SUPPLY_CHARGE,
    CONF_SOLAR_CURTAILMENT_ENABLED,
    CONF_TESLA_API_PROVIDER,
    CONF_FLEET_API_ACCESS_TOKEN,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    SERVICE_SYNC_TOU,
    SERVICE_SYNC_NOW,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_FORCE_CHARGE,
    SERVICE_RESTORE_NORMAL,
    DISCHARGE_DURATIONS,
    DEFAULT_DISCHARGE_DURATION,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_AEMO_REGION,
    CONF_AEMO_SPIKE_THRESHOLD,
    AMBER_API_BASE_URL,
    # Flow Power configuration
    CONF_ELECTRICITY_PROVIDER,
    CONF_FLOW_POWER_STATE,
    CONF_FLOW_POWER_PRICE_SOURCE,
    CONF_AEMO_SENSOR_ENTITY,
    CONF_AEMO_SENSOR_5MIN,
    CONF_AEMO_SENSOR_30MIN,
    AEMO_SENSOR_5MIN_PATTERN,
    AEMO_SENSOR_30MIN_PATTERN,
    # Network Tariff configuration
    CONF_NETWORK_DISTRIBUTOR,
    CONF_NETWORK_TARIFF_CODE,
    CONF_NETWORK_USE_MANUAL_RATES,
    CONF_NETWORK_TARIFF_TYPE,
    CONF_NETWORK_FLAT_RATE,
    CONF_NETWORK_PEAK_RATE,
    CONF_NETWORK_SHOULDER_RATE,
    CONF_NETWORK_OFFPEAK_RATE,
    CONF_NETWORK_PEAK_START,
    CONF_NETWORK_PEAK_END,
    CONF_NETWORK_OFFPEAK_START,
    CONF_NETWORK_OFFPEAK_END,
    CONF_NETWORK_OTHER_FEES,
    CONF_NETWORK_INCLUDE_GST,
    # Flow Power PEA configuration
    CONF_PEA_ENABLED,
    CONF_FLOW_POWER_BASE_RATE,
    CONF_PEA_CUSTOM_VALUE,
    FLOW_POWER_DEFAULT_BASE_RATE,
    # Export price boost configuration
    CONF_EXPORT_BOOST_ENABLED,
    CONF_EXPORT_PRICE_OFFSET,
    CONF_EXPORT_MIN_PRICE,
    CONF_EXPORT_BOOST_START,
    CONF_EXPORT_BOOST_END,
    CONF_EXPORT_BOOST_THRESHOLD,
    # Spike protection configuration
    CONF_SPIKE_PROTECTION_ENABLED,
    # Settled prices only mode
    CONF_SETTLED_PRICES_ONLY,
    DEFAULT_EXPORT_BOOST_START,
    DEFAULT_EXPORT_BOOST_END,
    DEFAULT_EXPORT_BOOST_THRESHOLD,
)
from .coordinator import (
    AmberPriceCoordinator,
    TeslaEnergyCoordinator,
    DemandChargeCoordinator,
    AEMOSensorCoordinator,
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

        # Handle site IDs (alphanumeric, like Amber 01KAR0YMB7JQDVZ10SN1SGA0CV)
        text = re.sub(
            r'(site[_\s]?[iI][dD]["\']?[\s:=]+["\']?)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text
        )

        # Handle "for site {id}" pattern
        text = re.sub(
            r'(for site\s+)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle email addresses
        text = re.sub(
            r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            lambda m: self.obfuscate(m.group(1)),
            text
        )

        # Handle Tesla energy site IDs (numeric, 13-20 digits) - in URLs and JSON
        text = re.sub(
            r'(energy_site[s]?[/\s:=]+["\']?)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle standalone long numeric IDs (Tesla energy site IDs in various contexts)
        text = re.sub(
            r'(\bsite\s+)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers in JSON format ('vin': 'XXX' or "vin": "XXX")
        text = re.sub(
            r'(["\']vin["\']:\s*["\'])([A-HJ-NPR-Z0-9]{17})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers plain format
        text = re.sub(
            r'(\bvin[\s:=]+)([A-HJ-NPR-Z0-9]{17})\b',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle DIN numbers in JSON format
        text = re.sub(
            r'(["\']din["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle DIN numbers plain format
        text = re.sub(
            r'(\bdin[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers in JSON format
        text = re.sub(
            r'(["\']serial_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers plain format
        text = re.sub(
            r'(serial[\s_]?(?:number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs in JSON format
        text = re.sub(
            r'(["\']gateway_id["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs plain format
        text = re.sub(
            r'(gateway[\s_]?(?:id)?[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers in JSON format
        text = re.sub(
            r'(["\']warp_site_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers plain format
        text = re.sub(
            r'(warp[\s_]?(?:site)?(?:[\s_]?number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle asset_site_id (UUIDs)
        text = re.sub(
            r'(["\']asset_site_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle device_id (UUIDs)
        text = re.sub(
            r'(["\']device_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        return text

    def _obfuscate_arg(self, arg: Any) -> Any:
        """Obfuscate an argument only if it contains sensitive data, preserving type otherwise."""
        # Convert to string for pattern matching
        str_value = str(arg)
        obfuscated = self._obfuscate_string(str_value)

        # Only return string version if obfuscation actually changed something
        # This preserves numeric types for format specifiers like %d and %.3f
        if obfuscated != str_value:
            return obfuscated
        return arg

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        # Only convert args to strings if obfuscation patterns match
        # This preserves numeric types for format specifiers like %d and %.3f
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._obfuscate_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_arg(a) for a in record.args)

        return True


_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SensitiveDataFilter())

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]

# Storage version for persisting data across HA restarts
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.storage"


async def fetch_active_amber_site_id(hass: HomeAssistant, api_token: str) -> str | None:
    """
    Fetch the active Amber site ID from the API.

    Returns the first active site ID, or None if no sites found.
    This ensures we always use the current active site, not a stale/closed one.
    """
    try:
        session = async_get_clientsession(hass)
        headers = {"Authorization": f"Bearer {api_token}"}

        async with session.get(
            f"{AMBER_API_BASE_URL}/sites",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status == 200:
                sites = await response.json()
                if sites and len(sites) > 0:
                    # Filter for active sites (status == "active")
                    active_sites = [s for s in sites if s.get("status") == "active"]
                    if active_sites:
                        site_id = active_sites[0]["id"]
                        _LOGGER.info(f"🔍 Fetched active Amber site ID from API: {site_id}")
                        return site_id
                    # If no active sites, fall back to first site
                    site_id = sites[0]["id"]
                    _LOGGER.warning(f"No active Amber sites found, using first available: {site_id}")
                    return site_id
                _LOGGER.error("No Amber sites found in API response")
                return None
            else:
                _LOGGER.error(f"Failed to fetch Amber sites: HTTP {response.status}")
                return None
    except Exception as e:
        _LOGGER.error(f"Error fetching Amber site ID: {e}")
        return None


class SyncCoordinator:
    """
    Coordinates Tesla sync with smarter price-aware logic (async version for Home Assistant).

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
        self._websocket_event = asyncio.Event()
        self._websocket_data = None
        self._current_period = None  # Track which 5-min period we're in
        self._lock = asyncio.Lock()
        self._initial_sync_done = False  # Has initial forecast sync happened this period?
        self._last_synced_prices = {}  # {'general': price, 'feedIn': price}
        self._websocket_received = False  # Has WebSocket delivered this period?

    def _get_current_period(self):
        """Get the current 5-minute period timestamp."""
        from homeassistant.util import dt as dt_util
        now = dt_util.utcnow()
        current_period = now.replace(second=0, microsecond=0)
        return current_period.replace(minute=current_period.minute - (current_period.minute % 5))

    async def _reset_if_new_period(self):
        """Reset state if we've moved to a new 5-minute period."""
        current_period = self._get_current_period()
        if self._current_period != current_period:
            _LOGGER.info(f"🆕 New sync period: {current_period}")
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
        self._websocket_data = prices_data
        self._websocket_received = True
        self._websocket_event.set()
        _LOGGER.info("📡 WebSocket price update received, notifying sync coordinator")

    def get_websocket_data(self):
        """Get the current WebSocket data if available."""
        return self._websocket_data

    async def mark_initial_sync_done(self):
        """Mark that the initial forecast sync has been completed for this period."""
        async with self._lock:
            await self._reset_if_new_period()
            self._initial_sync_done = True
            _LOGGER.info("✅ Initial forecast sync marked as done for this period")

    async def should_do_initial_sync(self):
        """
        Check if we should do the initial forecast sync at start of period.

        Returns:
            bool: True if initial sync hasn't been done yet this period
        """
        async with self._lock:
            await self._reset_if_new_period()
            if self._initial_sync_done:
                _LOGGER.debug("⏭️  Initial sync already done this period")
                return False
            return True

    async def has_websocket_delivered(self):
        """Check if WebSocket has delivered price data this period."""
        async with self._lock:
            await self._reset_if_new_period()
            return self._websocket_received

    def record_synced_price(self, general_price, feedin_price):
        """
        Record the price that was synced.

        Args:
            general_price: The general (buy) price in c/kWh
            feedin_price: The feedIn (sell) price in c/kWh
        """
        self._last_synced_prices = {
            'general': general_price,
            'feedIn': feedin_price
        }
        _LOGGER.debug(f"Recorded synced price: general={general_price}c, feedIn={feedin_price}c")

    def should_resync_for_price(self, new_general_price, new_feedin_price):
        """
        Check if we should re-sync because the price has changed significantly.

        Args:
            new_general_price: The new general price from WebSocket/REST
            new_feedin_price: The new feedIn price from WebSocket/REST

        Returns:
            bool: True if price difference exceeds threshold
        """
        last_prices = self._last_synced_prices

        if not last_prices:
            # No previous sync - should sync
            _LOGGER.info("No previous price recorded, will sync")
            return True

        last_general = last_prices.get('general')
        last_feedin = last_prices.get('feedIn')

        # Check general price difference
        if last_general is not None and new_general_price is not None:
            general_diff = abs(new_general_price - last_general)
            if general_diff > self.PRICE_DIFF_THRESHOLD:
                _LOGGER.info(f"General price changed by {general_diff:.2f}c ({last_general:.2f}c → {new_general_price:.2f}c) - will re-sync")
                return True

        # Check feedIn price difference
        if last_feedin is not None and new_feedin_price is not None:
            feedin_diff = abs(new_feedin_price - last_feedin)
            if feedin_diff > self.PRICE_DIFF_THRESHOLD:
                _LOGGER.info(f"FeedIn price changed by {feedin_diff:.2f}c ({last_feedin:.2f}c → {new_feedin_price:.2f}c) - will re-sync")
                return True

        _LOGGER.debug(f"Price unchanged (general={new_general_price}c, feedIn={new_feedin_price}c) - skipping re-sync")
        return False

    # Legacy methods for backwards compatibility
    async def wait_for_websocket_or_timeout(self, timeout_seconds=15):
        """Wait for WebSocket data or timeout (legacy method)."""
        _LOGGER.info(f"⏱️  Waiting up to {timeout_seconds}s for WebSocket price update...")

        try:
            await asyncio.wait_for(self._websocket_event.wait(), timeout=timeout_seconds)

            async with self._lock:
                if self._websocket_data:
                    _LOGGER.info("✅ WebSocket data received, using real-time prices")
                    return self._websocket_data
                else:
                    _LOGGER.warning("⏰ WebSocket event set but no data available")
                    return None

        except asyncio.TimeoutError:
            _LOGGER.info(f"⏰ WebSocket timeout after {timeout_seconds}s, falling back to REST API")
            return None

    async def already_synced_this_period(self):
        """Legacy method - check if initial sync is done."""
        async with self._lock:
            await self._reset_if_new_period()
            return self._initial_sync_done

    async def should_sync_this_period(self):
        """Legacy method - now always returns True for initial sync check."""
        async with self._lock:
            await self._reset_if_new_period()
            return not self._initial_sync_done


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
        token_getter: callable = None,
    ):
        """Initialize the AEMO spike manager."""
        self.hass = hass
        self.entry = entry
        self.region = region
        self.threshold = threshold
        self.site_id = site_id
        self._api_token = api_token  # Fallback token
        self._token_getter = token_getter  # Callable to get fresh token
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

    def _get_current_token(self) -> tuple[str, str]:
        """Get the current API token, fetching fresh if token_getter is available.

        Returns:
            tuple: (token, provider)
        """
        if self._token_getter:
            try:
                token, provider = self._token_getter()
                if token:
                    self.api_provider = provider
                    return token, provider
            except Exception as e:
                _LOGGER.warning(f"Token getter failed, using fallback token: {e}")
        return self._api_token, self.api_provider

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
            # Get fresh token in case it was refreshed by tesla_fleet integration
            current_token, current_provider = self._get_current_token()
            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

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
                current_token,
                current_provider,
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
            # Get fresh token in case it was refreshed by tesla_fleet integration
            current_token, current_provider = self._get_current_token()
            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

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
                    current_token,
                    current_provider,
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
    api_provider: str = TESLA_PROVIDER_TESLEMETRY,
    max_retries: int = 3,
    timeout_seconds: int = 60,
) -> bool:
    """Send tariff data to Tesla via Teslemetry or Fleet API with retry logic.

    Args:
        hass: HomeAssistant instance
        site_id: Tesla energy site ID
        tariff_data: Tariff data to send
        api_token: API token (Teslemetry or Fleet API)
        api_provider: API provider (teslemetry or fleet_api)
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

    # Use correct API base URL based on provider
    api_base = TESLEMETRY_API_BASE_URL if api_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL
    url = f"{api_base}/api/1/energy_sites/{site_id}/time_of_use_settings"
    _LOGGER.debug("Sending TOU schedule via %s API", api_provider)
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


def get_tesla_api_token(hass: HomeAssistant, entry: ConfigEntry) -> tuple[str | None, str]:
    """
    Get the current Tesla API token, fetching fresh from tesla_fleet if available.

    The tesla_fleet integration handles token refresh internally and updates its
    config entry data. This function always fetches the latest token.

    Returns:
        tuple: (token, provider) where provider is 'fleet_api' or 'teslemetry'
    """
    # Check if Tesla Fleet integration is configured and available
    tesla_fleet_entries = hass.config_entries.async_entries("tesla_fleet")
    for tesla_entry in tesla_fleet_entries:
        if tesla_entry.state == ConfigEntryState.LOADED:
            try:
                if CONF_TOKEN in tesla_entry.data:
                    token_data = tesla_entry.data[CONF_TOKEN]
                    if CONF_ACCESS_TOKEN in token_data:
                        return token_data[CONF_ACCESS_TOKEN], TESLA_PROVIDER_FLEET_API
            except Exception as e:
                _LOGGER.warning(f"Failed to extract token from Tesla Fleet integration: {e}")

    # Fall back to Teslemetry
    if CONF_TESLEMETRY_API_TOKEN in entry.data:
        return entry.data[CONF_TESLEMETRY_API_TOKEN], TESLA_PROVIDER_TESLEMETRY

    return None, TESLA_PROVIDER_TESLEMETRY


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesla Sync from a config entry."""
    _LOGGER.info("Setting up Tesla Sync integration")

    # Check pricing source configuration
    has_amber = bool(entry.data.get(CONF_AMBER_API_TOKEN))
    aemo_spike_enabled = entry.options.get(
        CONF_AEMO_SPIKE_ENABLED,
        entry.data.get(CONF_AEMO_SPIKE_ENABLED, False)
    )

    # Check for Flow Power with AEMO sensor price source
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
    )
    flow_power_price_source = entry.options.get(
        CONF_FLOW_POWER_PRICE_SOURCE,
        entry.data.get(CONF_FLOW_POWER_PRICE_SOURCE, "amber")
    )
    has_flow_power_aemo = (
        electricity_provider == "flow_power" and
        flow_power_price_source in ("aemo_sensor", "aemo")
    )

    if has_amber:
        _LOGGER.info("Running in Amber TOU Sync mode (provider: %s)", electricity_provider)
    elif has_flow_power_aemo:
        _LOGGER.info("Running in Flow Power mode with AEMO API pricing")
    elif aemo_spike_enabled:
        _LOGGER.info("Running in AEMO Spike Detection only mode (Globird)")
    else:
        _LOGGER.error("No pricing source configured")
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

        # Fetch the active Amber site ID from API (don't rely on stored/stale ID)
        stored_site_id = entry.data.get("amber_site_id")
        amber_site_id = await fetch_active_amber_site_id(hass, entry.data[CONF_AMBER_API_TOKEN])

        if amber_site_id:
            if stored_site_id and stored_site_id != amber_site_id:
                _LOGGER.warning(
                    f"⚠️ Stored Amber site ID ({stored_site_id}) differs from active site ({amber_site_id}). "
                    f"Using active site ID."
                )
        else:
            # Fall back to stored ID if API fetch fails
            amber_site_id = stored_site_id
            _LOGGER.warning(f"Could not fetch active Amber site, using stored ID: {amber_site_id}")

        try:
            from .websocket_client import AmberWebSocketClient

            _LOGGER.info(f"🔌 Initializing WebSocket client with site_id: {amber_site_id}")

            ws_client = AmberWebSocketClient(
                api_token=entry.data[CONF_AMBER_API_TOKEN],
                site_id=amber_site_id,
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
            amber_site_id,  # Use the active site ID
            ws_client=ws_client,  # Pass WebSocket client to coordinator
        )

    # Get initial Tesla API token and provider
    # Use get_tesla_api_token() which fetches fresh from tesla_fleet if available
    tesla_api_token, tesla_api_provider = get_tesla_api_token(hass, entry)

    if not tesla_api_token:
        _LOGGER.error("No Tesla API credentials available (neither Fleet API nor Teslemetry)")
        raise ConfigEntryNotReady("No Tesla API credentials configured")

    if tesla_api_provider == TESLA_PROVIDER_FLEET_API:
        _LOGGER.info(
            "Detected Tesla Fleet integration - using Fleet API tokens for site %s",
            entry.data[CONF_TESLA_ENERGY_SITE_ID]
        )
    else:
        _LOGGER.info("Using Teslemetry API for site %s", entry.data[CONF_TESLA_ENERGY_SITE_ID])

    # Create token getter that always fetches fresh token (handles token refresh)
    # This is called before each API request to ensure we use the latest token
    def token_getter():
        return get_tesla_api_token(hass, entry)

    tesla_coordinator = TeslaEnergyCoordinator(
        hass,
        entry.data[CONF_TESLA_ENERGY_SITE_ID],
        tesla_api_token,
        api_provider=tesla_api_provider,
        token_getter=token_getter,
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

    # Initialize AEMO Spike Manager if enabled (for Globird users)
    aemo_spike_manager = None
    if aemo_spike_enabled:
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
                token_getter=token_getter,
            )
            _LOGGER.info(
                "AEMO Spike Manager initialized: region=%s, threshold=$%.0f/MWh",
                aemo_region,
                aemo_threshold,
            )
        else:
            _LOGGER.warning("AEMO spike detection enabled but no region configured")

    # Initialize AEMO Price Coordinator for Flow Power AEMO mode
    # Now fetches directly from AEMO API - no external integration required
    aemo_sensor_coordinator = None  # Keep variable name for compatibility
    # flow_power_price_source already defined at top of function
    flow_power_state = entry.options.get(
        CONF_FLOW_POWER_STATE,
        entry.data.get(CONF_FLOW_POWER_STATE, "NSW1")
    )

    # Check for "aemo_sensor" (legacy) or "aemo" (new) price source
    # Both now use the direct AEMO API
    use_aemo_pricing = flow_power_price_source in ("aemo_sensor", "aemo")

    if use_aemo_pricing and flow_power_state:
        from .coordinator import AEMOPriceCoordinator

        # Get aiohttp session from Home Assistant
        session = async_get_clientsession(hass)

        aemo_sensor_coordinator = AEMOPriceCoordinator(
            hass,
            flow_power_state,  # Region code (NSW1, QLD1, VIC1, SA1, TAS1)
            session,
        )
        try:
            await aemo_sensor_coordinator.async_config_entry_first_refresh()
            _LOGGER.info(
                "AEMO Price Coordinator initialized for region %s (direct API)",
                flow_power_state,
            )
        except Exception as e:
            _LOGGER.error("Failed to initialize AEMO price coordinator: %s", e)
            aemo_sensor_coordinator = None
    elif use_aemo_pricing and not flow_power_state:
        _LOGGER.warning("AEMO price source selected but no region configured")

    # Initialize persistent storage for data that survives HA restarts
    # (like Teslemetry's RestoreEntity pattern for export rule state)
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    stored_data = await store.async_load() or {}
    cached_export_rule = stored_data.get("cached_export_rule")
    if cached_export_rule:
        _LOGGER.info(f"Restored cached_export_rule='{cached_export_rule}' from persistent storage")

    # Store coordinators and WebSocket client in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "amber_coordinator": amber_coordinator,
        "tesla_coordinator": tesla_coordinator,
        "demand_charge_coordinator": demand_charge_coordinator,
        "aemo_spike_manager": aemo_spike_manager,
        "aemo_sensor_coordinator": aemo_sensor_coordinator,  # For Flow Power AEMO-only mode
        "ws_client": ws_client,  # Store for cleanup on unload
        "entry": entry,
        "auto_sync_cancel": None,  # Will store the timer cancel function
        "aemo_spike_cancel": None,  # Will store the AEMO spike check cancel function
        "demand_charging_cancel": None,  # Will store the demand period grid charging cancel function
        "grid_charging_disabled_for_demand": False,  # Track if grid charging is disabled for demand period
        "cached_export_rule": cached_export_rule,  # Restored from persistent storage
        "store": store,  # Reference to Store for saving updates
        "token_getter": token_getter,  # Function to get fresh Tesla API token
    }

    # Helper function to update and persist cached export rule
    async def update_cached_export_rule(new_rule: str) -> None:
        """Update the cached export rule in memory and persist to storage."""
        hass.data[DOMAIN][entry.entry_id]["cached_export_rule"] = new_rule
        store = hass.data[DOMAIN][entry.entry_id]["store"]
        await store.async_save({"cached_export_rule": new_rule})
        _LOGGER.debug(f"Persisted cached_export_rule='{new_rule}' to storage")
        # Signal sensor to update
        async_dispatcher_send(hass, f"tesla_sync_curtailment_updated_{entry.entry_id}")

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_sync_initial_forecast() -> None:
        """
        STAGE 1 (0s): Sync immediately at start of 5-min period using forecast price.

        This gets the predicted price to Tesla ASAP at the start of each period.
        Later stages will re-sync if the actual price differs from forecast.
        """
        # Skip if no price coordinator available (AEMO spike-only mode without pricing)
        if not amber_coordinator and not aemo_sensor_coordinator:
            _LOGGER.debug("TOU sync skipped - no price coordinator available (AEMO spike-only mode)")
            return

        if not await coordinator.should_do_initial_sync():
            _LOGGER.info("⏭️  Initial forecast sync already done this period")
            return

        _LOGGER.info("🚀 Stage 1: Initial forecast sync at start of period")
        await _handle_sync_tou_internal(None, sync_mode='initial_forecast')
        await coordinator.mark_initial_sync_done()

    async def handle_sync_tou_with_websocket_data(websocket_data) -> None:
        """
        STAGE 2 (WebSocket): Re-sync only if price differs from what we synced.

        Called by WebSocket callback when new price data arrives.
        Compares with last synced price and only re-syncs if difference > threshold.
        """
        _LOGGER.info("📡 Stage 2: WebSocket price received - checking if re-sync needed")
        await _handle_sync_tou_internal(websocket_data, sync_mode='websocket_update')

    async def handle_sync_rest_api_check(check_name="fallback") -> None:
        """
        STAGE 3/4 (35s/60s): Check REST API and re-sync if price differs.

        Called at 35s and 60s as fallback if WebSocket hasn't delivered.
        Fetches current price from REST API and compares with last synced price.

        Args:
            check_name: Label for logging (e.g., "35s check", "60s final")
        """
        # Skip if no price coordinator available (AEMO spike-only mode without pricing)
        if not amber_coordinator and not aemo_sensor_coordinator:
            _LOGGER.debug("TOU sync skipped - no price coordinator available (AEMO spike-only mode)")
            return

        if await coordinator.has_websocket_delivered():
            _LOGGER.info(f"⏭️  REST API {check_name}: WebSocket already delivered this period, skipping")
            return

        _LOGGER.info(f"⏰ Stage 3/4: REST API {check_name} - checking if re-sync needed")
        await _handle_sync_tou_internal(None, sync_mode='rest_api_check')

    async def handle_sync_tou(call: ServiceCall) -> None:
        """
        LEGACY: Cron fallback sync (now calls handle_sync_rest_api_check).
        Kept for backwards compatibility and service call.
        """
        await handle_sync_rest_api_check(check_name="legacy fallback")

    async def _handle_sync_tou_internal(websocket_data, sync_mode='initial_forecast') -> None:
        """
        Internal sync logic with smart price-aware re-sync.

        Args:
            websocket_data: Price data from WebSocket (or None to fetch from REST API)
            sync_mode: One of:
                - 'initial_forecast': Always sync, record the price (Stage 1)
                - 'websocket_update': Re-sync only if price differs (Stage 2)
                - 'rest_api_check': Check REST API and re-sync if differs (Stage 3/4)
        """
        # Skip TOU sync if force discharge is active - don't overwrite the discharge tariff
        if force_discharge_state.get("active"):
            expires_at = force_discharge_state.get("expires_at")
            if expires_at:
                from homeassistant.util import dt as dt_util
                remaining = (expires_at - dt_util.utcnow()).total_seconds() / 60
                _LOGGER.info(f"⏭️  TOU sync skipped - Force discharge active ({remaining:.1f} min remaining)")
            else:
                _LOGGER.info("⏭️  TOU sync skipped - Force discharge active")
            return

        # Skip TOU sync if force charge is active - don't overwrite the charge tariff
        if force_charge_state.get("active"):
            expires_at = force_charge_state.get("expires_at")
            if expires_at:
                from homeassistant.util import dt as dt_util
                remaining = (expires_at - dt_util.utcnow()).total_seconds() / 60
                _LOGGER.info(f"⏭️  TOU sync skipped - Force charge active ({remaining:.1f} min remaining)")
            else:
                _LOGGER.info("⏭️  TOU sync skipped - Force charge active")
            return

        _LOGGER.info("=== Starting TOU sync ===")

        # Import tariff converter from existing code
        from .tariff_converter import (
            convert_amber_to_tesla_tariff,
            extract_most_recent_actual_interval,
        )

        # Determine price source: AEMO API or Amber
        # Support both "aemo_sensor" (legacy) and "aemo" (new) price source names
        use_aemo_sensor = (
            aemo_sensor_coordinator is not None and
            flow_power_price_source in ("aemo_sensor", "aemo")
        )

        if use_aemo_sensor:
            _LOGGER.info("📊 Using AEMO API for pricing data")
        else:
            _LOGGER.info("🟠 Using Amber for pricing data")

        # Get current interval price from WebSocket (real-time) or REST API fallback
        # WebSocket is PRIMARY source for current price, REST API is fallback if timeout
        # Note: AEMO mode doesn't have WebSocket - uses direct AEMO API
        current_actual_interval = None

        # Track prices for comparison
        general_price = None
        feedin_price = None

        if use_aemo_sensor:
            # AEMO mode: Refresh AEMO coordinator
            await aemo_sensor_coordinator.async_request_refresh()

            if not aemo_sensor_coordinator.data:
                _LOGGER.error("No AEMO API data available")
                return

            # Current price from AEMO API data
            current_prices = aemo_sensor_coordinator.data.get("current", [])
            if current_prices:
                current_actual_interval = {'general': None, 'feedIn': None}
                for price in current_prices:
                    channel = price.get('channelType')
                    if channel in ['general', 'feedIn']:
                        current_actual_interval[channel] = price
                general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
                _LOGGER.info(f"📊 Using AEMO API price for current interval: general={general_price:.2f}¢/kWh")
        elif websocket_data:
            # WebSocket data received within 60s - use it directly as primary source
            current_actual_interval = websocket_data
            general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
            feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
            _LOGGER.info(f"✅ Using WebSocket price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
        else:
            # WebSocket timeout - fallback to REST API for current price
            _LOGGER.info(f"⏰ Fetching current price from REST API")

            # Refresh coordinator to get REST API current prices
            await amber_coordinator.async_request_refresh()

            if amber_coordinator.data:
                # Extract most recent CurrentInterval/ActualInterval from 5-min forecast data
                forecast_5min = amber_coordinator.data.get("forecast_5min", [])
                current_actual_interval = extract_most_recent_actual_interval(forecast_5min)

                if current_actual_interval:
                    general_price = current_actual_interval.get('general', {}).get('perKwh') if current_actual_interval.get('general') else None
                    feedin_price = current_actual_interval.get('feedIn', {}).get('perKwh') if current_actual_interval.get('feedIn') else None
                    _LOGGER.info(f"📡 Using REST API price for current interval: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
                else:
                    _LOGGER.warning("No current price data available, proceeding with 30-min forecast only")
            else:
                _LOGGER.error("No Amber price data available from REST API")

        # SMART SYNC: For non-initial syncs, check if price has changed enough to warrant re-sync
        if sync_mode != 'initial_forecast':
            if general_price is not None or feedin_price is not None:
                if not coordinator.should_resync_for_price(general_price, feedin_price):
                    _LOGGER.info(f"⏭️  Price unchanged - skipping re-sync")
                    return
                _LOGGER.info(f"🔄 Price changed - proceeding with re-sync")

        # Get forecast data from appropriate coordinator
        if use_aemo_sensor:
            # AEMO coordinator already refreshed above
            forecast_data = aemo_sensor_coordinator.data.get("forecast", [])
            if not forecast_data:
                _LOGGER.error("No AEMO forecast data available from API")
                return
            _LOGGER.info(f"Using AEMO API forecast: {len(forecast_data) // 2} periods")
        else:
            # Refresh Amber coordinator to get latest forecast data (regardless of WebSocket status)
            await amber_coordinator.async_request_refresh()

            if not amber_coordinator.data:
                _LOGGER.error("No Amber forecast data available")
                return
            forecast_data = amber_coordinator.data.get("forecast", [])

        # Get forecast type from options (if set) or data (from initial config)
        forecast_type = entry.options.get(
            CONF_AMBER_FORECAST_TYPE,
            entry.data.get(CONF_AMBER_FORECAST_TYPE, "predicted")
        )
        _LOGGER.info(f"Using forecast type: {forecast_type}")

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
        demand_charge_days = entry.options.get(
            CONF_DEMAND_CHARGE_DAYS,
            entry.data.get(CONF_DEMAND_CHARGE_DAYS, "All Days")
        )
        demand_artificial_price_enabled = entry.options.get(
            CONF_DEMAND_ARTIFICIAL_PRICE,
            entry.data.get(CONF_DEMAND_ARTIFICIAL_PRICE, False)
        )

        if demand_charge_enabled:
            _LOGGER.info(
                "Demand charge schedule configured: $%.2f/kW window %s to %s (applied to: %s)",
                demand_charge_rate,
                demand_charge_start_time,
                demand_charge_end_time,
                demand_charge_apply_to,
            )

        # Get electricity provider for tariff naming
        electricity_provider = entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
        )

        # Get spike protection setting (Amber only, opt-in feature)
        spike_protection_enabled = entry.options.get(
            CONF_SPIKE_PROTECTION_ENABLED,
            entry.data.get(CONF_SPIKE_PROTECTION_ENABLED, False)
        )

        # Get export boost settings for spike protection calculation
        export_boost_enabled = entry.options.get(CONF_EXPORT_BOOST_ENABLED, False) if electricity_provider == "amber" else False
        export_price_offset = entry.options.get(CONF_EXPORT_PRICE_OFFSET, 0) or 0 if export_boost_enabled else 0
        export_min_price = entry.options.get(CONF_EXPORT_MIN_PRICE, 0) or 0 if export_boost_enabled else 0

        # Convert prices to Tesla tariff format
        # forecast_data comes from either AEMO sensor or Amber coordinator (set above)
        tariff = convert_amber_to_tesla_tariff(
            forecast_data,
            tesla_energy_site_id=entry.data[CONF_TESLA_ENERGY_SITE_ID],
            forecast_type=forecast_type,
            powerwall_timezone=powerwall_timezone,
            current_actual_interval=current_actual_interval,
            demand_charge_enabled=demand_charge_enabled,
            demand_charge_rate=demand_charge_rate,
            demand_charge_start_time=demand_charge_start_time,
            demand_charge_end_time=demand_charge_end_time,
            demand_charge_apply_to=demand_charge_apply_to,
            demand_charge_days=demand_charge_days,
            demand_artificial_price_enabled=demand_artificial_price_enabled,
            electricity_provider=electricity_provider,
            spike_protection_enabled=spike_protection_enabled,
            export_boost_enabled=export_boost_enabled,
            export_price_offset=export_price_offset,
            export_min_price=export_min_price,
        )

        if not tariff:
            _LOGGER.error("Failed to convert prices to Tesla tariff")
            return

        # Apply Flow Power export rates and network tariff if configured
        flow_power_state = entry.options.get(
            CONF_FLOW_POWER_STATE,
            entry.data.get(CONF_FLOW_POWER_STATE, "")
        )
        flow_power_price_source = entry.options.get(
            CONF_FLOW_POWER_PRICE_SOURCE,
            entry.data.get(CONF_FLOW_POWER_PRICE_SOURCE, "amber")
        )

        # Apply Flow Power PEA pricing (works with both AEMO and Amber price sources)
        if electricity_provider == "flow_power":
            # Check if PEA (Price Efficiency Adjustment) is enabled
            pea_enabled = entry.options.get(CONF_PEA_ENABLED, True)  # Default True for Flow Power

            if pea_enabled:
                # Use Flow Power PEA pricing model: Base Rate + PEA
                # Works with both AEMO (raw wholesale) and Amber (wholesaleKWHPrice forecast)
                from .tariff_converter import apply_flow_power_pea, get_wholesale_lookup

                base_rate = entry.options.get(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE)
                custom_pea = entry.options.get(CONF_PEA_CUSTOM_VALUE)

                # Build wholesale price lookup from forecast data
                # get_wholesale_lookup() handles both AEMO and Amber data formats
                wholesale_prices = get_wholesale_lookup(forecast_data)

                _LOGGER.info(
                    "Applying Flow Power PEA (%s): base_rate=%.1fc, custom_pea=%s",
                    flow_power_price_source,
                    base_rate,
                    f"{custom_pea:.1f}c" if custom_pea is not None else "auto"
                )
                tariff = apply_flow_power_pea(tariff, wholesale_prices, base_rate, custom_pea)
            elif flow_power_price_source in ("aemo_sensor", "aemo"):
                # PEA disabled + AEMO: fall back to network tariff calculation
                # (Amber prices already include network fees, no fallback needed)
                from .tariff_converter import apply_network_tariff
                _LOGGER.info("Applying network tariff to AEMO wholesale prices (PEA disabled)")

                # Get network tariff config from options
                # Primary: aemo_to_tariff library with distributor + tariff code
                # Fallback: Manual rates when use_manual_rates=True or library unavailable
                tariff = apply_network_tariff(
                    tariff,
                    # Library-based pricing (primary)
                    distributor=entry.options.get(CONF_NETWORK_DISTRIBUTOR),
                    tariff_code=entry.options.get(CONF_NETWORK_TARIFF_CODE),
                    use_manual_rates=entry.options.get(CONF_NETWORK_USE_MANUAL_RATES, False),
                    # Manual pricing (fallback)
                    tariff_type=entry.options.get(CONF_NETWORK_TARIFF_TYPE, "flat"),
                    flat_rate=entry.options.get(CONF_NETWORK_FLAT_RATE, 8.0),
                    peak_rate=entry.options.get(CONF_NETWORK_PEAK_RATE, 15.0),
                    shoulder_rate=entry.options.get(CONF_NETWORK_SHOULDER_RATE, 5.0),
                    offpeak_rate=entry.options.get(CONF_NETWORK_OFFPEAK_RATE, 2.0),
                    peak_start=entry.options.get(CONF_NETWORK_PEAK_START, "16:00"),
                    peak_end=entry.options.get(CONF_NETWORK_PEAK_END, "21:00"),
                    offpeak_start=entry.options.get(CONF_NETWORK_OFFPEAK_START, "10:00"),
                    offpeak_end=entry.options.get(CONF_NETWORK_OFFPEAK_END, "15:00"),
                    other_fees=entry.options.get(CONF_NETWORK_OTHER_FEES, 1.5),
                    include_gst=entry.options.get(CONF_NETWORK_INCLUDE_GST, True),
                )

        if electricity_provider == "flow_power" and flow_power_state:
            from .tariff_converter import apply_flow_power_export
            _LOGGER.info("Applying Flow Power export rates for state: %s", flow_power_state)
            tariff = apply_flow_power_export(tariff, flow_power_state)

        # Apply export price boost for Amber users (if enabled)
        if electricity_provider == "amber":
            export_boost_enabled = entry.options.get(CONF_EXPORT_BOOST_ENABLED, False)
            if export_boost_enabled:
                from .tariff_converter import apply_export_boost
                offset = entry.options.get(CONF_EXPORT_PRICE_OFFSET, 0) or 0
                min_price = entry.options.get(CONF_EXPORT_MIN_PRICE, 0) or 0
                boost_start = entry.options.get(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START)
                boost_end = entry.options.get(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END)
                threshold = entry.options.get(CONF_EXPORT_BOOST_THRESHOLD, DEFAULT_EXPORT_BOOST_THRESHOLD)
                _LOGGER.info(
                    "Applying export boost: offset=%.1fc, min=%.1fc, threshold=%.1fc, window=%s-%s",
                    offset, min_price, threshold, boost_start, boost_end
                )
                tariff = apply_export_boost(tariff, offset, min_price, boost_start, boost_end, threshold)

        # Store tariff schedule in hass.data for the sensor to read
        from datetime import datetime as dt
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        # Buy prices are at top level, sell prices are under sell_tariff
        buy_prices = tariff.get("energy_charges", {}).get("Summer", {}).get("rates", {})
        sell_prices = tariff.get("sell_tariff", {}).get("energy_charges", {}).get("Summer", {}).get("rates", {})

        hass.data[DOMAIN][entry.entry_id]["tariff_schedule"] = {
            "buy_prices": buy_prices,
            "sell_prices": sell_prices,
            "last_sync": dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Log price summary for debugging dashboard display issues
        if buy_prices:
            buy_values = list(buy_prices.values())
            sell_values = list(sell_prices.values()) if sell_prices else [0]
            _LOGGER.info(
                "Tariff schedule stored: %d periods, buy $%.4f-$%.4f (avg $%.4f), sell $%.4f-$%.4f",
                len(buy_prices),
                min(buy_values), max(buy_values), sum(buy_values)/len(buy_values),
                min(sell_values), max(sell_values)
            )
            # Log a sample period for verification
            sample_period = "PERIOD_18_00"  # 6pm
            if sample_period in buy_prices:
                _LOGGER.info(
                    "Sample %s: buy=$%.4f (%.1fc), sell=$%.4f (%.1fc)",
                    sample_period,
                    buy_prices[sample_period], buy_prices[sample_period] * 100,
                    sell_prices.get(sample_period, 0), sell_prices.get(sample_period, 0) * 100
                )
        else:
            _LOGGER.warning("No buy prices in tariff schedule!")

        # Signal the tariff schedule sensor to update
        async_dispatcher_send(hass, f"tesla_sync_tariff_updated_{entry.entry_id}")

        # Send tariff to Tesla via Teslemetry or Fleet API
        # Get fresh token in case it was refreshed by tesla_fleet integration
        current_token, current_provider = token_getter()
        if not current_token:
            _LOGGER.error("No Tesla API token available for TOU sync")
            return

        success = await send_tariff_to_tesla(
            hass,
            entry.data[CONF_TESLA_ENERGY_SITE_ID],
            tariff,
            current_token,
            current_provider,
        )

        if success:
            _LOGGER.info(f"TOU schedule synced successfully ({sync_mode})")

            # Record the synced price for smart price-change detection
            if general_price is not None or feedin_price is not None:
                coordinator.record_synced_price(general_price, feedin_price)

            # Enforce grid charging setting after TOU sync (counteracts VPP overrides)
            entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
            dc_coordinator = entry_data.get("demand_charge_coordinator")
            if dc_coordinator and dc_coordinator.enabled:
                from homeassistant.util import dt as dt_util
                current_time = dt_util.now()
                in_peak = dc_coordinator._is_in_peak_period(current_time)

                if in_peak:
                    # Force disable grid charging during peak (even if we think it's already disabled)
                    _LOGGER.info("⚡ Peak period - forcing grid charging OFF after TOU sync")
                    gc_success = await tesla_coordinator.set_grid_charging_enabled(False)
                    if gc_success:
                        hass.data[DOMAIN][entry.entry_id]["grid_charging_disabled_for_demand"] = True
                        _LOGGER.info("🔋 Grid charging enforcement after TOU sync: disabled_for_peak")
                    else:
                        _LOGGER.warning("⚠️ Grid charging enforcement failed after TOU sync")
                else:
                    # Outside peak - ensure grid charging is enabled if we had disabled it
                    if entry_data.get("grid_charging_disabled_for_demand", False):
                        _LOGGER.info("⚡ Outside peak period - re-enabling grid charging after TOU sync")
                        gc_success = await tesla_coordinator.set_grid_charging_enabled(True)
                        if gc_success:
                            hass.data[DOMAIN][entry.entry_id]["grid_charging_disabled_for_demand"] = False
                            _LOGGER.info("🔋 Grid charging enforcement after TOU sync: enabled_outside_peak")
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

            # Amber returns feed-in prices as NEGATIVE when you're paid to export
            # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
            # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
            # So we want to curtail when feedin_price > 0 (user would pay to export)
            export_earnings = -feedin_price  # Convert to positive = earnings per kWh
            _LOGGER.info(f"Current feed-in price from Amber: {feedin_price}c/kWh (export earnings: {export_earnings}c/kWh)")

            # Get current grid export settings from Tesla
            # Get fresh token in case it was refreshed by tesla_fleet integration
            current_token, current_provider = token_getter()
            if not current_token:
                _LOGGER.error("No Tesla API token available for curtailment check")
                return

            session = async_get_clientsession(hass)
            api_base_url = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }

            # Get current export rule from site_info (grid_import_export only supports POST)
            try:
                async with session.get(
                    f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        _LOGGER.error(f"Failed to get site_info: {response.status} - {error_text}")
                        return

                    data = await response.json()
                    site_info = data.get("response", {})
                    # Fields can be at top level OR inside 'components' depending on API/firmware
                    components = site_info.get("components", {})
                    current_export_rule = components.get("customer_preferred_export_rule") or site_info.get("customer_preferred_export_rule")

                    # Handle VPP users where export rule is derived from non_export_configured
                    if current_export_rule is None:
                        non_export = components.get("non_export_configured") or site_info.get("components_non_export_configured")
                        if non_export is not None:
                            current_export_rule = "never" if non_export else "battery_ok"
                            _LOGGER.info(f"VPP user: derived export_rule='{current_export_rule}' from components_non_export_configured={non_export}")

                    # If still None, fall back to cached value (but mark as unverified)
                    using_cached_rule = False
                    if current_export_rule is None:
                        cached_rule = hass.data[DOMAIN][entry.entry_id].get("cached_export_rule")
                        if cached_rule:
                            current_export_rule = cached_rule
                            using_cached_rule = True
                            _LOGGER.info(f"Using cached export_rule='{current_export_rule}' (API returned None - will verify by applying)")

                    _LOGGER.info(f"Current export rule: {current_export_rule}")

            except Exception as err:
                _LOGGER.error(f"Error fetching site_info: {err}")
                return

            # CURTAILMENT LOGIC: Curtail when export earnings < 1c/kWh
            # (i.e., when feedin_price > -1, meaning you earn less than 1c or pay to export)
            if export_earnings < 1:
                _LOGGER.info(f"🚫 CURTAILMENT TRIGGERED: Export earnings {export_earnings:.2f}c/kWh (<1c)")

                # If already curtailed AND verified from API, no action needed
                # If using cache, always apply curtailment to be safe (cache may be stale)
                if current_export_rule == "never" and not using_cached_rule:
                    _LOGGER.info(f"✅ Already curtailed (export='never', verified from API) - no action needed")
                    _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")
                else:
                    # Apply curtailment (either not 'never' or using unverified cache)
                    if using_cached_rule:
                        _LOGGER.info(f"Applying curtailment (cache says '{current_export_rule}' but unverified) → 'never'")
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

                            # Check response body for actual result
                            response_data = await response.json()
                            _LOGGER.debug(f"Set grid export rule response: {response_data}")
                            if isinstance(response_data, dict) and 'response' in response_data:
                                result_data = response_data['response']
                                if isinstance(result_data, dict) and 'result' in result_data:
                                    if not result_data['result']:
                                        reason = result_data.get('reason', 'Unknown reason')
                                        _LOGGER.error(f"❌ Set grid export rule failed: {reason}")
                                        _LOGGER.error(f"Full response: {response_data}")
                                        return

                        # Verify the change actually took effect by reading back
                        async with session.get(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as verify_response:
                            if verify_response.status == 200:
                                verify_data = await verify_response.json()
                                verify_info = verify_data.get("response", {})
                                # Fields can be at top level OR inside 'components' depending on API/firmware
                                verify_components = verify_info.get("components", {})
                                verified_rule = verify_components.get("customer_preferred_export_rule") or verify_info.get("customer_preferred_export_rule")
                                # Also check non_export_configured for VPP users
                                if verified_rule is None:
                                    non_export = verify_components.get("non_export_configured") or verify_info.get("components_non_export_configured")
                                    if non_export is not None:
                                        verified_rule = "never" if non_export else "battery_ok"
                                if verified_rule is None:
                                    # API doesn't return this field - can't verify but not a failure
                                    _LOGGER.info(f"ℹ️ Cannot verify curtailment (API returns None for export_rule) - operation reported success")
                                elif verified_rule != "never":
                                    _LOGGER.warning(f"⚠️ CURTAILMENT VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'never')")
                                    _LOGGER.warning(f"Full verification response: {verify_info}")
                                else:
                                    _LOGGER.info(f"✓ Curtailment verified via read-back: export_rule='{verified_rule}'")

                        _LOGGER.info(f"✅ CURTAILMENT APPLIED: Export rule changed '{current_export_rule}' → 'never'")
                        await update_cached_export_rule("never")
                        _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c)")

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

                            # Check response body for actual result
                            response_data = await response.json()
                            _LOGGER.debug(f"Set grid export rule response: {response_data}")
                            if isinstance(response_data, dict) and 'response' in response_data:
                                result_data = response_data['response']
                                if isinstance(result_data, dict) and 'result' in result_data:
                                    if not result_data['result']:
                                        reason = result_data.get('reason', 'Unknown reason')
                                        _LOGGER.error(f"❌ Set grid export rule failed: {reason}")
                                        _LOGGER.error(f"Full response: {response_data}")
                                        return

                        # Verify the change actually took effect by reading back
                        async with session.get(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as verify_response:
                            if verify_response.status == 200:
                                verify_data = await verify_response.json()
                                verify_info = verify_data.get("response", {})
                                # Fields can be at top level OR inside 'components' depending on API/firmware
                                verify_components = verify_info.get("components", {})
                                verified_rule = verify_components.get("customer_preferred_export_rule") or verify_info.get("customer_preferred_export_rule")
                                # Also check non_export_configured for VPP users
                                if verified_rule is None:
                                    non_export = verify_components.get("non_export_configured") or verify_info.get("components_non_export_configured")
                                    if non_export is not None:
                                        verified_rule = "never" if non_export else "battery_ok"
                                if verified_rule is None:
                                    # API doesn't return this field - can't verify but not a failure
                                    _LOGGER.info(f"ℹ️ Cannot verify restore (API returns None for export_rule) - operation reported success")
                                elif verified_rule != "battery_ok":
                                    _LOGGER.warning(f"⚠️ RESTORE VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'battery_ok')")
                                    _LOGGER.warning(f"Full verification response: {verify_info}")
                                else:
                                    _LOGGER.info(f"✓ Restore verified via read-back: export_rule='{verified_rule}'")

                        _LOGGER.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → 'battery_ok'")
                        await update_cached_export_rule("battery_ok")
                        _LOGGER.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")
                    _LOGGER.info(f"📊 Action summary: No change needed (earnings: {export_earnings:.2f}c/kWh, export: '{current_export_rule}')")

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

            # Amber returns feed-in prices as NEGATIVE when you're paid to export
            # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
            # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
            # So we want to curtail when feedin_price > 0 (user would pay to export)
            export_earnings = -feedin_price  # Convert to positive = earnings per kWh
            _LOGGER.info(f"Current feed-in price (WebSocket): {feedin_price}c/kWh (export earnings: {export_earnings:.2f}c/kWh)")

            # Get current grid export settings from Tesla
            # Get fresh token in case it was refreshed by tesla_fleet integration
            current_token, current_provider = token_getter()
            if not current_token:
                _LOGGER.error("No Tesla API token available for curtailment check")
                return

            session = async_get_clientsession(hass)
            api_base_url = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }

            # Get current export rule from site_info (grid_import_export only supports POST)
            try:
                async with session.get(
                    f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        _LOGGER.error(f"Failed to get site_info: {response.status} - {error_text}")
                        return

                    data = await response.json()
                    site_info = data.get("response", {})
                    # Fields can be at top level OR inside 'components' depending on API/firmware
                    components = site_info.get("components", {})
                    current_export_rule = components.get("customer_preferred_export_rule") or site_info.get("customer_preferred_export_rule")

                    # Handle VPP users where export rule is derived from non_export_configured
                    if current_export_rule is None:
                        non_export = components.get("non_export_configured") or site_info.get("components_non_export_configured")
                        if non_export is not None:
                            current_export_rule = "never" if non_export else "battery_ok"
                            _LOGGER.info(f"VPP user: derived export_rule='{current_export_rule}' from components_non_export_configured={non_export}")

                    # If still None, fall back to cached value (but mark as unverified)
                    using_cached_rule = False
                    if current_export_rule is None:
                        cached_rule = hass.data[DOMAIN][entry.entry_id].get("cached_export_rule")
                        if cached_rule:
                            current_export_rule = cached_rule
                            using_cached_rule = True
                            _LOGGER.info(f"Using cached export_rule='{current_export_rule}' (API returned None - will verify by applying)")

                    _LOGGER.info(f"Current export rule: {current_export_rule}")

            except Exception as err:
                _LOGGER.error(f"Error fetching site_info: {err}")
                return

            # CURTAILMENT LOGIC: Curtail when export earnings < 1c/kWh
            # (i.e., when feedin_price > -1, meaning you earn less than 1c or pay to export)
            if export_earnings < 1:
                _LOGGER.info(f"🚫 CURTAILMENT TRIGGERED: Export earnings {export_earnings:.2f}c/kWh (<1c)")

                # If already curtailed AND verified from API, no action needed
                # If using cache, always apply curtailment to be safe (cache may be stale)
                if current_export_rule == "never" and not using_cached_rule:
                    _LOGGER.info(f"✅ Already curtailed (export='never', verified from API) - no action needed")
                    _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")
                else:
                    # Apply curtailment (either not 'never' or using unverified cache)
                    if using_cached_rule:
                        _LOGGER.info(f"Applying curtailment (cache says '{current_export_rule}' but unverified) → 'never'")
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

                            # Check response body for actual result
                            response_data = await response.json()
                            _LOGGER.debug(f"Set grid export rule response: {response_data}")
                            if isinstance(response_data, dict) and 'response' in response_data:
                                result_data = response_data['response']
                                if isinstance(result_data, dict) and 'result' in result_data:
                                    if not result_data['result']:
                                        reason = result_data.get('reason', 'Unknown reason')
                                        _LOGGER.error(f"❌ Set grid export rule failed: {reason}")
                                        _LOGGER.error(f"Full response: {response_data}")
                                        return

                        # Verify the change actually took effect by reading back
                        async with session.get(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as verify_response:
                            if verify_response.status == 200:
                                verify_data = await verify_response.json()
                                verify_info = verify_data.get("response", {})
                                # Fields can be at top level OR inside 'components' depending on API/firmware
                                verify_components = verify_info.get("components", {})
                                verified_rule = verify_components.get("customer_preferred_export_rule") or verify_info.get("customer_preferred_export_rule")
                                # Also check non_export_configured for VPP users
                                if verified_rule is None:
                                    non_export = verify_components.get("non_export_configured") or verify_info.get("components_non_export_configured")
                                    if non_export is not None:
                                        verified_rule = "never" if non_export else "battery_ok"
                                if verified_rule is None:
                                    # API doesn't return this field - can't verify but not a failure
                                    _LOGGER.info(f"ℹ️ Cannot verify curtailment (API returns None for export_rule) - operation reported success")
                                elif verified_rule != "never":
                                    _LOGGER.warning(f"⚠️ CURTAILMENT VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'never')")
                                    _LOGGER.warning(f"Full verification response: {verify_info}")
                                else:
                                    _LOGGER.info(f"✓ Curtailment verified via read-back: export_rule='{verified_rule}'")

                        _LOGGER.info(f"✅ CURTAILMENT APPLIED: Export rule changed '{current_export_rule}' → 'never'")
                        await update_cached_export_rule("never")
                        _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c)")

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

                            # Check response body for actual result
                            response_data = await response.json()
                            _LOGGER.debug(f"Set grid export rule response: {response_data}")
                            if isinstance(response_data, dict) and 'response' in response_data:
                                result_data = response_data['response']
                                if isinstance(result_data, dict) and 'result' in result_data:
                                    if not result_data['result']:
                                        reason = result_data.get('reason', 'Unknown reason')
                                        _LOGGER.error(f"❌ Set grid export rule failed: {reason}")
                                        _LOGGER.error(f"Full response: {response_data}")
                                        return

                        # Verify the change actually took effect by reading back
                        async with session.get(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/site_info",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as verify_response:
                            if verify_response.status == 200:
                                verify_data = await verify_response.json()
                                verify_info = verify_data.get("response", {})
                                # Fields can be at top level OR inside 'components' depending on API/firmware
                                verify_components = verify_info.get("components", {})
                                verified_rule = verify_components.get("customer_preferred_export_rule") or verify_info.get("customer_preferred_export_rule")
                                # Also check non_export_configured for VPP users
                                if verified_rule is None:
                                    non_export = verify_components.get("non_export_configured") or verify_info.get("components_non_export_configured")
                                    if non_export is not None:
                                        verified_rule = "never" if non_export else "battery_ok"
                                if verified_rule is None:
                                    # API doesn't return this field - can't verify but not a failure
                                    _LOGGER.info(f"ℹ️ Cannot verify restore (API returns None for export_rule) - operation reported success")
                                elif verified_rule != "battery_ok":
                                    _LOGGER.warning(f"⚠️ RESTORE VERIFICATION FAILED: Set returned success but read-back shows '{verified_rule}' (expected 'battery_ok')")
                                    _LOGGER.warning(f"Full verification response: {verify_info}")
                                else:
                                    _LOGGER.info(f"✓ Restore verified via read-back: export_rule='{verified_rule}'")

                        _LOGGER.info(f"✅ CURTAILMENT REMOVED: Export restored 'never' → 'battery_ok'")
                        await update_cached_export_rule("battery_ok")
                        _LOGGER.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")
                    _LOGGER.info(f"📊 Action summary: No change needed (earnings: {export_earnings:.2f}c/kWh, export: '{current_export_rule}')")

        except Exception as e:
            _LOGGER.error(f"❌ Unexpected error in solar curtailment check: {e}", exc_info=True)

        _LOGGER.info("=== Solar curtailment check complete ===")

    hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU, handle_sync_tou)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)

    # ======================================================================
    # FORCE DISCHARGE AND RESTORE NORMAL SERVICES
    # ======================================================================

    # Storage for saved tariff and operation mode during force discharge
    force_discharge_state = {
        "active": False,
        "saved_tariff": None,
        "saved_operation_mode": None,
        "expires_at": None,
        "cancel_expiry_timer": None,
    }

    # Storage for saved tariff and operation mode during force charge
    force_charge_state = {
        "active": False,
        "saved_tariff": None,
        "saved_operation_mode": None,
        "expires_at": None,
        "cancel_expiry_timer": None,
    }

    async def handle_force_discharge(call: ServiceCall) -> None:
        """Force discharge mode - switches to autonomous with high export tariff."""
        from homeassistant.util import dt as dt_util

        duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        if duration not in DISCHARGE_DURATIONS:
            duration = DEFAULT_DISCHARGE_DURATION

        _LOGGER.info(f"🔋 FORCE DISCHARGE: Activating for {duration} minutes")

        try:
            # Get current token and provider
            provider = entry.options.get(
                CONF_TESLA_API_PROVIDER,
                entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            )
            if provider == TESLA_PROVIDER_TESLEMETRY:
                current_token = entry.options.get(
                    CONF_TESLEMETRY_API_TOKEN,
                    entry.data.get(CONF_TESLEMETRY_API_TOKEN)
                )
            else:
                current_token = entry.options.get(
                    CONF_FLEET_API_ACCESS_TOKEN,
                    entry.data.get(CONF_FLEET_API_ACCESS_TOKEN)
                )

            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for force discharge")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Step 1: Save current tariff (if not already in discharge mode)
            if not force_discharge_state["active"]:
                _LOGGER.info("Saving current tariff before force discharge...")
                async with session.get(
                    f"{api_base}/api/1/energy_sites/{site_id}/tariff_rate",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        force_discharge_state["saved_tariff"] = data.get("response", {}).get("tariff_content_v2")
                        _LOGGER.info("Saved current tariff for restoration after discharge")
                    else:
                        _LOGGER.warning("Could not save current tariff: %s", response.status)

                # Step 2: Get and save current operation mode
                async with session.get(
                    f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        force_discharge_state["saved_operation_mode"] = data.get("response", {}).get("default_real_mode")
                        _LOGGER.info("Saved operation mode: %s", force_discharge_state["saved_operation_mode"])

            # Step 3: Switch to autonomous mode for best export behavior
            if force_discharge_state.get("saved_operation_mode") != "autonomous":
                _LOGGER.info("Switching to autonomous mode for optimal export...")
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "autonomous"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info("Switched to autonomous mode")
                    else:
                        _LOGGER.warning("Could not switch operation mode: %s", response.status)

            # Step 4: Create and upload discharge tariff (high export rates)
            discharge_tariff = _create_discharge_tariff(duration)
            success = await send_tariff_to_tesla(
                hass,
                site_id,
                discharge_tariff,
                current_token,
                provider,
            )

            if success:
                force_discharge_state["active"] = True
                force_discharge_state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)
                _LOGGER.info(f"✅ FORCE DISCHARGE ACTIVE: Tariff uploaded for {duration} min")

                # Dispatch event for switch entity
                async_dispatcher_send(hass, f"{DOMAIN}_force_discharge_state", {
                    "active": True,
                    "expires_at": force_discharge_state["expires_at"].isoformat(),
                    "duration": duration,
                })

                # Schedule auto-restore
                if force_discharge_state["cancel_expiry_timer"]:
                    force_discharge_state["cancel_expiry_timer"]()

                async def auto_restore(_now):
                    """Auto-restore normal operation when discharge expires."""
                    if force_discharge_state["active"]:
                        _LOGGER.info("⏰ Force discharge expired, auto-restoring normal operation")
                        await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                force_discharge_state["cancel_expiry_timer"] = async_track_utc_time_change(
                    hass,
                    auto_restore,
                    hour=force_discharge_state["expires_at"].hour,
                    minute=force_discharge_state["expires_at"].minute,
                    second=force_discharge_state["expires_at"].second,
                )
            else:
                _LOGGER.error("Failed to upload discharge tariff")

        except Exception as e:
            _LOGGER.error(f"Error in force discharge: {e}", exc_info=True)

    def _create_discharge_tariff(duration_minutes: int) -> dict:
        """Create a Tesla tariff optimized for exporting (force discharge).

        Uses the same tariff structure as the working Flask implementation.
        """
        from homeassistant.util import dt as dt_util

        # Very high sell rate to encourage Powerwall to export all energy
        sell_rate_discharge = 10.00  # $10/kWh - huge incentive to discharge
        sell_rate_normal = 0.08      # 8c/kWh normal feed-in

        # Buy rate to discourage import during discharge
        buy_rate = 0.30  # 30c/kWh

        _LOGGER.info(f"Creating discharge tariff: sell=${sell_rate_discharge}/kWh, buy=${buy_rate}/kWh for {duration_minutes} min")

        # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
        buy_rates = {}
        sell_rates = {}
        tou_periods = {}

        # Get current time to determine discharge window
        now = dt_util.now()
        current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

        # Calculate how many 30-min periods the discharge covers
        discharge_periods = (duration_minutes + 29) // 30  # Round up
        discharge_start = current_period_index
        discharge_end = (current_period_index + discharge_periods) % 48

        _LOGGER.info(f"Discharge window: periods {discharge_start} to {discharge_end} (current time: {now.hour:02d}:{now.minute:02d})")

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

        # Create Tesla tariff structure (matching Flask implementation)
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

        _LOGGER.info(f"Created discharge tariff: buy=${buy_rate}/kWh, sell=${sell_rate_discharge}/kWh for {discharge_periods} periods")

        return tariff

    async def handle_force_charge(call: ServiceCall) -> None:
        """Force charge mode - switches to autonomous with free import tariff."""
        from homeassistant.util import dt as dt_util

        duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        if duration not in DISCHARGE_DURATIONS:
            duration = DEFAULT_DISCHARGE_DURATION

        _LOGGER.info(f"🔌 FORCE CHARGE: Activating for {duration} minutes")

        try:
            # Get current token and provider
            provider = entry.options.get(
                CONF_TESLA_API_PROVIDER,
                entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            )
            if provider == TESLA_PROVIDER_TESLEMETRY:
                current_token = entry.options.get(
                    CONF_TESLEMETRY_API_TOKEN,
                    entry.data.get(CONF_TESLEMETRY_API_TOKEN)
                )
            else:
                current_token = entry.options.get(
                    CONF_FLEET_API_ACCESS_TOKEN,
                    entry.data.get(CONF_FLEET_API_ACCESS_TOKEN)
                )

            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for force charge")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Cancel active discharge mode if switching to charge
            if force_discharge_state["active"]:
                _LOGGER.info("Canceling active discharge mode to enable charge mode")
                if force_discharge_state.get("cancel_expiry_timer"):
                    force_discharge_state["cancel_expiry_timer"]()
                    force_discharge_state["cancel_expiry_timer"] = None
                force_discharge_state["active"] = False
                force_discharge_state["expires_at"] = None

            # Step 1: Save current tariff (if not already in charge mode)
            if not force_charge_state["active"]:
                _LOGGER.info("Saving current tariff before force charge...")
                async with session.get(
                    f"{api_base}/api/1/energy_sites/{site_id}/tariff_rate",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        force_charge_state["saved_tariff"] = data.get("response", {}).get("tariff_content_v2")
                        _LOGGER.info("Saved current tariff for restoration after charge")
                    else:
                        _LOGGER.warning("Could not save current tariff: %s", response.status)

                # Step 2: Get and save current operation mode
                async with session.get(
                    f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        force_charge_state["saved_operation_mode"] = data.get("response", {}).get("default_real_mode")
                        _LOGGER.info("Saved operation mode: %s", force_charge_state["saved_operation_mode"])

            # Step 3: Switch to autonomous mode for best charging behavior
            if force_charge_state.get("saved_operation_mode") != "autonomous":
                _LOGGER.info("Switching to autonomous mode for optimal charging...")
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "autonomous"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info("Switched to autonomous mode")
                    else:
                        _LOGGER.warning("Could not switch operation mode: %s", response.status)

            # Step 4: Create and upload charge tariff (free import, no export incentive)
            charge_tariff = _create_charge_tariff(duration)
            success = await send_tariff_to_tesla(
                hass,
                site_id,
                charge_tariff,
                current_token,
                provider,
            )

            if success:
                force_charge_state["active"] = True
                force_charge_state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)
                _LOGGER.info(f"✅ FORCE CHARGE ACTIVE: Tariff uploaded for {duration} min")

                # Dispatch event for UI
                async_dispatcher_send(hass, f"{DOMAIN}_force_charge_state", {
                    "active": True,
                    "expires_at": force_charge_state["expires_at"].isoformat(),
                    "duration": duration,
                })

                # Schedule auto-restore
                if force_charge_state["cancel_expiry_timer"]:
                    force_charge_state["cancel_expiry_timer"]()

                async def auto_restore_charge(_now):
                    """Auto-restore normal operation when charge expires."""
                    if force_charge_state["active"]:
                        _LOGGER.info("⏰ Force charge expired, auto-restoring normal operation")
                        await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                force_charge_state["cancel_expiry_timer"] = async_track_utc_time_change(
                    hass,
                    auto_restore_charge,
                    hour=force_charge_state["expires_at"].hour,
                    minute=force_charge_state["expires_at"].minute,
                    second=force_charge_state["expires_at"].second,
                )
            else:
                _LOGGER.error("Failed to upload charge tariff")

        except Exception as e:
            _LOGGER.error(f"Error in force charge: {e}", exc_info=True)

    def _create_charge_tariff(duration_minutes: int) -> dict:
        """Create a Tesla tariff optimized for charging from grid (force charge).

        Uses the same tariff structure as the working Flask implementation.
        """
        from homeassistant.util import dt as dt_util

        # Rates during charge window - free to buy, no sell incentive
        buy_rate_charge = 0.00    # $0/kWh - maximum incentive to charge
        sell_rate_charge = 0.00   # $0/kWh - no incentive to export

        # Rates outside charge window - expensive to buy, no sell
        buy_rate_normal = 10.00   # $10/kWh - huge disincentive to charge
        sell_rate_normal = 0.00   # $0/kWh - no incentive to export

        _LOGGER.info(f"Creating charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {duration_minutes} min")

        # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
        buy_rates = {}
        sell_rates = {}
        tou_periods = {}

        # Get current time to determine charge window
        now = dt_util.now()
        current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)

        # Calculate how many 30-min periods the charge covers
        charge_periods = (duration_minutes + 29) // 30  # Round up
        charge_start = current_period_index
        charge_end = (current_period_index + charge_periods) % 48

        _LOGGER.info(f"Charge window: periods {charge_start} to {charge_end} (current time: {now.hour:02d}:{now.minute:02d})")

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

        # Create Tesla tariff structure (matching Flask implementation)
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

        _LOGGER.info(f"Created charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {charge_periods} periods")

        return tariff

    async def handle_restore_normal(call: ServiceCall) -> None:
        """Restore normal operation - restore saved tariff or trigger Amber sync."""
        _LOGGER.info("🔄 RESTORE NORMAL: Restoring normal operation")

        # Cancel any pending expiry timers (discharge and charge)
        if force_discharge_state.get("cancel_expiry_timer"):
            force_discharge_state["cancel_expiry_timer"]()
            force_discharge_state["cancel_expiry_timer"] = None
        if force_charge_state.get("cancel_expiry_timer"):
            force_charge_state["cancel_expiry_timer"]()
            force_charge_state["cancel_expiry_timer"] = None

        try:
            # Get current token and provider
            provider = entry.options.get(
                CONF_TESLA_API_PROVIDER,
                entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            )
            if provider == TESLA_PROVIDER_TESLEMETRY:
                current_token = entry.options.get(
                    CONF_TESLEMETRY_API_TOKEN,
                    entry.data.get(CONF_TESLEMETRY_API_TOKEN)
                )
            else:
                current_token = entry.options.get(
                    CONF_FLEET_API_ACCESS_TOKEN,
                    entry.data.get(CONF_FLEET_API_ACCESS_TOKEN)
                )

            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for restore normal")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Check if user is using Amber (restore via sync instead of saved tariff)
            electricity_provider = entry.options.get(
                CONF_ELECTRICITY_PROVIDER,
                entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
            )

            # Find saved tariff (prefer discharge, then charge)
            saved_tariff = force_discharge_state.get("saved_tariff") or force_charge_state.get("saved_tariff")

            if electricity_provider == "amber":
                # Amber users - trigger a fresh sync to get current prices
                _LOGGER.info("Amber user - triggering sync to restore normal operation")
                await handle_sync_tou(ServiceCall(DOMAIN, SERVICE_SYNC_TOU, {}))
            elif saved_tariff:
                # Non-Amber users - restore the saved tariff
                _LOGGER.info("Restoring saved tariff...")
                success = await send_tariff_to_tesla(
                    hass,
                    site_id,
                    saved_tariff,
                    current_token,
                    provider,
                )
                if success:
                    _LOGGER.info("Restored saved tariff successfully")
                else:
                    _LOGGER.error("Failed to restore saved tariff")
            else:
                _LOGGER.warning("No saved tariff to restore, triggering sync")
                await handle_sync_tou(ServiceCall(DOMAIN, SERVICE_SYNC_TOU, {}))

            # Restore operation mode (prefer discharge saved mode, then charge)
            restore_mode = (
                force_discharge_state.get("saved_operation_mode") or
                force_charge_state.get("saved_operation_mode") or
                "autonomous"
            )
            _LOGGER.info(f"Restoring operation mode to: {restore_mode}")
            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/operation",
                headers=headers,
                json={"default_real_mode": restore_mode},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"Restored operation mode to {restore_mode}")
                else:
                    _LOGGER.warning(f"Could not restore operation mode: {response.status}")

            # Clear discharge state
            force_discharge_state["active"] = False
            force_discharge_state["saved_tariff"] = None
            force_discharge_state["saved_operation_mode"] = None
            force_discharge_state["expires_at"] = None

            # Clear charge state
            force_charge_state["active"] = False
            force_charge_state["saved_tariff"] = None
            force_charge_state["saved_operation_mode"] = None
            force_charge_state["expires_at"] = None

            _LOGGER.info("✅ NORMAL OPERATION RESTORED")

            # Dispatch events for UI
            async_dispatcher_send(hass, f"{DOMAIN}_force_discharge_state", {
                "active": False,
                "expires_at": None,
                "duration": 0,
            })
            async_dispatcher_send(hass, f"{DOMAIN}_force_charge_state", {
                "active": False,
                "expires_at": None,
                "duration": 0,
            })

        except Exception as e:
            _LOGGER.error(f"Error in restore normal: {e}", exc_info=True)

    # Register force discharge, force charge, and restore normal services
    hass.services.async_register(DOMAIN, SERVICE_FORCE_DISCHARGE, handle_force_discharge)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_CHARGE, handle_force_charge)
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_NORMAL, handle_restore_normal)

    _LOGGER.info("🔋 Force discharge, force charge, and restore services registered")

    # Wire up WebSocket sync callback now that handlers are defined
    if ws_client:
        def websocket_sync_callback(prices_data):
            """
            STAGE 2: WebSocket price arrival triggers re-sync IF price differs.

            Smart sync flow:
            - Stage 1 (0s): Initial forecast already synced
            - Stage 2 (WebSocket): Re-sync only if price differs from forecast
            - Stage 3 (35s): REST API fallback if no WebSocket
            - Stage 4 (60s): Final REST API check

            NOTE: This callback is called from a background WebSocket thread,
            so we must use call_soon_threadsafe to schedule work on the HA event loop.
            """
            # Notify coordinator that WebSocket delivered (for REST API fallback checks)
            coordinator.notify_websocket_update(prices_data)

            # Trigger sync with price comparison (handle_sync_tou_with_websocket_data does comparison)
            async def trigger_sync():
                # Check if auto-sync is enabled (respect user's preference)
                auto_sync_enabled = entry.options.get(
                    CONF_AUTO_SYNC_ENABLED,
                    entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
                )

                # Check if solar curtailment is enabled
                solar_curtailment_enabled = entry.options.get(
                    CONF_SOLAR_CURTAILMENT_ENABLED,
                    entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
                )

                # Skip if neither feature is enabled
                if not auto_sync_enabled and not solar_curtailment_enabled:
                    _LOGGER.debug("⏭️  WebSocket price received but auto-sync and curtailment both disabled, skipping")
                    return

                _LOGGER.info("📡 Stage 2: WebSocket price received - checking if re-sync needed")

                try:
                    # 1. Re-sync TOU to Tesla if price changed (handles comparison internally)
                    if auto_sync_enabled:
                        await handle_sync_tou_with_websocket_data(prices_data)
                    else:
                        _LOGGER.debug("⏭️  Skipping TOU sync (auto-sync disabled)")

                    # 2. Check solar curtailment with WebSocket price (only if curtailment enabled)
                    if solar_curtailment_enabled:
                        await handle_solar_curtailment_with_websocket_data(prices_data)
                    else:
                        _LOGGER.debug("⏭️  Skipping solar curtailment check (curtailment disabled)")

                    _LOGGER.info("✅ Stage 2 WebSocket sync completed")
                except Exception as e:
                    _LOGGER.error(f"❌ Error in Stage 2 WebSocket sync: {e}", exc_info=True)

            # Schedule the async sync using thread-safe method
            # This callback runs in a background WebSocket thread, not the HA event loop
            hass.loop.call_soon_threadsafe(
                lambda: hass.async_create_task(trigger_sync())
            )

        # Assign callback to WebSocket client
        ws_client._sync_callback = websocket_sync_callback
        _LOGGER.info("🔗 WebSocket sync callback configured for smart price-aware sync")

    # Set up SMART SYNC with 4-stage approach
    # Stage 1 (0s): Initial forecast sync at start of period
    async def auto_sync_initial_forecast(now):
        """Stage 1: Initial forecast sync at start of 5-min period."""
        # Ensure WebSocket thread is alive (restart if it died)
        if ws_client:
            await ws_client.ensure_running()

        # Check if auto-sync is enabled in the config entry options
        auto_sync_enabled = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
        )

        # Check if settled prices only mode is enabled (skip forecast sync)
        settled_prices_only = entry.options.get(
            CONF_SETTLED_PRICES_ONLY,
            entry.data.get(CONF_SETTLED_PRICES_ONLY, False)
        )

        if not auto_sync_enabled:
            _LOGGER.debug("Auto-sync disabled, skipping initial forecast sync")
        elif settled_prices_only:
            _LOGGER.info("⏭️ Settled prices only mode - skipping initial forecast sync (waiting for actual prices at :35/:60)")
        else:
            await handle_sync_initial_forecast()

    # Stage 3 (35s): REST API fallback check if no WebSocket
    async def auto_sync_rest_api_35s(now):
        """Stage 3: REST API check at 35s if WebSocket hasn't delivered."""
        auto_sync_enabled = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
        )

        if auto_sync_enabled:
            await handle_sync_rest_api_check(check_name="35s check")
        else:
            _LOGGER.debug("Auto-sync disabled, skipping REST API 35s check")

    # Stage 4 (60s): Final REST API check
    async def auto_sync_rest_api_60s(now):
        """Stage 4: Final REST API check at 60s."""
        auto_sync_enabled = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
        )

        if auto_sync_enabled:
            await handle_sync_rest_api_check(check_name="60s final")
        else:
            _LOGGER.debug("Auto-sync disabled, skipping REST API 60s check")

    # Perform initial TOU sync if auto-sync is enabled (only in Amber mode)
    auto_sync_enabled = entry.options.get(
        CONF_AUTO_SYNC_ENABLED,
        entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
    )
    settled_prices_only = entry.options.get(
        CONF_SETTLED_PRICES_ONLY,
        entry.data.get(CONF_SETTLED_PRICES_ONLY, False)
    )

    if not auto_sync_enabled:
        _LOGGER.info("Skipping initial TOU sync - auto-sync disabled")
    elif settled_prices_only:
        _LOGGER.info("Skipping initial TOU sync - settled prices only mode (will sync at :35/:60)")
    elif amber_coordinator or aemo_sensor_coordinator:
        _LOGGER.info("Performing initial TOU sync")
        await handle_sync_initial_forecast()
    elif not amber_coordinator and not aemo_sensor_coordinator:
        _LOGGER.info("Skipping initial TOU sync - AEMO spike-only mode (no pricing data)")

    # STAGE 1: Initial forecast sync at start of each 5-min period (0s)
    cancel_timer_stage1 = async_track_utc_time_change(
        hass,
        auto_sync_initial_forecast,
        minute=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
        second=0,  # Start of each 5-min period
    )

    # STAGE 2: WebSocket-triggered sync (handled by callback, not scheduler)

    # STAGE 3: REST API fallback check at 35s if WebSocket hasn't delivered
    cancel_timer_stage3 = async_track_utc_time_change(
        hass,
        auto_sync_rest_api_35s,
        minute=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
        second=35,  # 35s into each period
    )

    # STAGE 4: Final REST API check at 60s (1 minute into period)
    cancel_timer_stage4 = async_track_utc_time_change(
        hass,
        auto_sync_rest_api_60s,
        minute=[1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 51, 56],
        second=0,  # 60s after period start
    )

    # Store the cancel functions so we can clean them up later
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel"] = cancel_timer_stage1
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel_35s"] = cancel_timer_stage3
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel_60s"] = cancel_timer_stage4
    _LOGGER.info("✅ Smart sync scheduled with 4-stage approach:")
    _LOGGER.info("  - Stage 1 (0s): Initial forecast sync at :00, :05, :10, etc.")
    _LOGGER.info("  - Stage 2 (WebSocket): Re-sync on price change (event-driven)")
    _LOGGER.info("  - Stage 3 (35s): REST API fallback if no WebSocket")
    _LOGGER.info("  - Stage 4 (60s): Final REST API check at :01, :06, :11, etc.")

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

    # Set up automatic demand period grid charging check if demand charges enabled
    if demand_charge_coordinator:
        async def auto_demand_charging_check(now):
            """Automatically check demand period and toggle grid charging."""
            from homeassistant.util import dt as dt_util
            try:
                entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
                dc_coordinator = entry_data.get("demand_charge_coordinator")
                ts_coordinator = entry_data.get("tesla_coordinator")

                if not dc_coordinator or not ts_coordinator:
                    return

                # Check if we're in peak period using the coordinator's method
                current_time = dt_util.now()
                in_peak = dc_coordinator._is_in_peak_period(current_time)
                currently_disabled = entry_data.get("grid_charging_disabled_for_demand", False)

                if in_peak:
                    # In peak period - force disable grid charging (even if we think it's already disabled)
                    # This counteracts VPP overrides that may re-enable grid charging
                    if not currently_disabled:
                        _LOGGER.info("⚡ Entering demand peak period - disabling grid charging")
                    else:
                        _LOGGER.debug("⚡ Peak period - forcing grid charging OFF (VPP override protection)")
                    success = await ts_coordinator.set_grid_charging_enabled(False)
                    if success:
                        hass.data[DOMAIN][entry.entry_id]["grid_charging_disabled_for_demand"] = True
                        if not currently_disabled:
                            _LOGGER.info("✅ Grid charging DISABLED for demand period")
                    else:
                        _LOGGER.error("❌ Failed to disable grid charging for demand period")

                elif not in_peak and currently_disabled:
                    # Exiting peak period - re-enable grid charging
                    _LOGGER.info("Exiting demand peak period - re-enabling grid charging")
                    success = await ts_coordinator.set_grid_charging_enabled(True)
                    if success:
                        hass.data[DOMAIN][entry.entry_id]["grid_charging_disabled_for_demand"] = False
                        _LOGGER.info("Grid charging re-enabled after demand period")
                    else:
                        _LOGGER.error("Failed to re-enable grid charging after demand period")

            except Exception as err:
                _LOGGER.error("Error in demand period grid charging check: %s", err)

        # Check every minute at :45 seconds (offset from AEMO check at :35)
        demand_charging_cancel_timer = async_track_utc_time_change(
            hass,
            auto_demand_charging_check,
            second=45,  # Every minute at :45 seconds
        )

        # Store the demand charging cancel function
        hass.data[DOMAIN][entry.entry_id]["demand_charging_cancel"] = demand_charging_cancel_timer
        _LOGGER.info(
            "Demand period grid charging check scheduled every minute (peak=%s to %s, days=%s)",
            demand_charge_coordinator.start_time,
            demand_charge_coordinator.end_time,
            demand_charge_coordinator.days,
        )

        # Perform initial demand period check
        _LOGGER.info("Performing initial demand period grid charging check")
        from homeassistant.util import dt as dt_util
        await auto_demand_charging_check(dt_util.now())

    _LOGGER.info("Tesla Sync integration setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Tesla Sync integration")

    # Cancel the auto-sync timers if they exist (4-stage smart sync)
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if cancel_timer := entry_data.get("auto_sync_cancel"):
        cancel_timer()
        _LOGGER.debug("Cancelled auto-sync timer (Stage 1)")
    if cancel_timer_35s := entry_data.get("auto_sync_cancel_35s"):
        cancel_timer_35s()
        _LOGGER.debug("Cancelled auto-sync timer (Stage 3 - 35s)")
    if cancel_timer_60s := entry_data.get("auto_sync_cancel_60s"):
        cancel_timer_60s()
        _LOGGER.debug("Cancelled auto-sync timer (Stage 4 - 60s)")

    # Cancel the curtailment timer if it exists
    if curtailment_cancel := entry_data.get("curtailment_cancel"):
        curtailment_cancel()
        _LOGGER.debug("Cancelled curtailment timer")

    # Cancel the AEMO spike timer if it exists
    if aemo_spike_cancel := entry_data.get("aemo_spike_cancel"):
        aemo_spike_cancel()
        _LOGGER.debug("Cancelled AEMO spike timer")

    # Cancel the demand period grid charging timer if it exists
    if demand_charging_cancel := entry_data.get("demand_charging_cancel"):
        demand_charging_cancel()
        _LOGGER.debug("Cancelled demand period grid charging timer")

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
