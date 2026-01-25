"""The PowerSync integration."""
from __future__ import annotations

import aiohttp
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform, CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_utc_time_change, async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.components.http import HomeAssistantView
from aiohttp import web

from .const import (
    DOMAIN,
    CONF_AMBER_API_TOKEN,
    CONF_AMBER_SITE_ID,
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
    CONF_BATTERY_CURTAILMENT_ENABLED,
    CONF_TESLA_API_PROVIDER,
    CONF_FLEET_API_ACCESS_TOKEN,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    SERVICE_SYNC_TOU,
    SERVICE_SYNC_NOW,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_FORCE_CHARGE,
    SERVICE_RESTORE_NORMAL,
    SERVICE_GET_CALENDAR_HISTORY,
    SERVICE_SYNC_BATTERY_HEALTH,
    SERVICE_SET_BACKUP_RESERVE,
    SERVICE_SET_OPERATION_MODE,
    SERVICE_SET_GRID_EXPORT,
    SERVICE_SET_GRID_CHARGING,
    SERVICE_CURTAIL_INVERTER,
    SERVICE_RESTORE_INVERTER,
    DISCHARGE_DURATIONS,
    DEFAULT_DISCHARGE_DURATION,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_AEMO_REGION,
    CONF_AEMO_SPIKE_THRESHOLD,
    # Solcast solar forecasting
    CONF_SOLCAST_ENABLED,
    CONF_SOLCAST_API_KEY,
    CONF_SOLCAST_RESOURCE_ID,
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
    DEFAULT_EXPORT_BOOST_START,
    DEFAULT_EXPORT_BOOST_END,
    DEFAULT_EXPORT_BOOST_THRESHOLD,
    # Chip Mode configuration (inverse of export boost)
    CONF_CHIP_MODE_ENABLED,
    CONF_CHIP_MODE_START,
    CONF_CHIP_MODE_END,
    CONF_CHIP_MODE_THRESHOLD,
    DEFAULT_CHIP_MODE_START,
    DEFAULT_CHIP_MODE_END,
    DEFAULT_CHIP_MODE_THRESHOLD,
    # Spike protection configuration
    CONF_SPIKE_PROTECTION_ENABLED,
    # Settled prices only mode
    CONF_SETTLED_PRICES_ONLY,
    # Alpha: Force tariff mode toggle
    CONF_FORCE_TARIFF_MODE_TOGGLE,
    # AC-Coupled Inverter Curtailment configuration
    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_MODEL,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SLAVE_ID,
    CONF_INVERTER_TOKEN,
    CONF_INVERTER_RESTORE_SOC,
    CONF_FRONIUS_LOAD_FOLLOWING,
    # Enphase credentials for JWT token refresh
    CONF_ENPHASE_USERNAME,
    CONF_ENPHASE_PASSWORD,
    CONF_ENPHASE_SERIAL,
    CONF_ENPHASE_NORMAL_PROFILE,
    CONF_ENPHASE_ZERO_EXPORT_PROFILE,
    CONF_ENPHASE_IS_INSTALLER,
    DEFAULT_INVERTER_PORT,
    DEFAULT_INVERTER_SLAVE_ID,
    DEFAULT_INVERTER_RESTORE_SOC,
    # Sigenergy configuration
    CONF_SIGENERGY_STATION_ID,
    CONF_SIGENERGY_USERNAME,
    CONF_SIGENERGY_PASS_ENC,
    CONF_SIGENERGY_DEVICE_ID,
    CONF_SIGENERGY_MODBUS_HOST,
    CONF_SIGENERGY_MODBUS_PORT,
    CONF_SIGENERGY_MODBUS_SLAVE_ID,
    CONF_SIGENERGY_ACCESS_TOKEN,
    CONF_SIGENERGY_REFRESH_TOKEN,
    CONF_SIGENERGY_TOKEN_EXPIRES_AT,
    # Battery system selection
    CONF_BATTERY_SYSTEM,
    # OpenWeatherMap for automations weather triggers
    CONF_OPENWEATHERMAP_API_KEY,
    # EV BLE configuration
    CONF_EV_PROVIDER,
    EV_PROVIDER_FLEET_API,
    EV_PROVIDER_TESLA_BLE,
    EV_PROVIDER_BOTH,
    CONF_TESLA_BLE_ENTITY_PREFIX,
    DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    TESLA_BLE_SENSOR_CHARGE_LEVEL,
    TESLA_BLE_SENSOR_CHARGING_STATE,
    TESLA_BLE_SENSOR_CHARGE_LIMIT,
    TESLA_BLE_BINARY_ASLEEP,
    TESLA_BLE_BINARY_STATUS,
    TESLA_BLE_SWITCH_CHARGER,
    TESLA_BLE_NUMBER_CHARGING_AMPS,
    TESLA_BLE_NUMBER_CHARGING_LIMIT,
    TESLA_BLE_BUTTON_WAKE_UP,
)
from .inverters import get_inverter_controller
from .coordinator import (
    AmberPriceCoordinator,
    TeslaEnergyCoordinator,
    SigenergyEnergyCoordinator,
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

# Force DEBUG logging for power_sync and all submodules
_LOGGER.setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.coordinator").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.sensor").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.inverters").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.inverters.sungrow").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.inverters.zeversolar").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.inverters.sigenergy").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.websocket_client").setLevel(logging.DEBUG)
logging.getLogger("custom_components.power_sync.tariff_converter").setLevel(logging.DEBUG)

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
    _LOGGER.info("Migrating PowerSync config entry from version %s", config_entry.version)

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

    if config_entry.version == 2:
        # Migrate from version 2 to version 3
        # Changes:
        #   - solar_curtailment_enabled -> battery_curtailment_enabled
        #   - inverter_curtailment_enabled -> ac_inverter_curtailment_enabled
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}

        # Migrate data keys
        if "solar_curtailment_enabled" in new_data:
            new_data["battery_curtailment_enabled"] = new_data.pop("solar_curtailment_enabled")
            _LOGGER.info("Migrated solar_curtailment_enabled to battery_curtailment_enabled (data)")

        if "inverter_curtailment_enabled" in new_data:
            new_data["ac_inverter_curtailment_enabled"] = new_data.pop("inverter_curtailment_enabled")
            _LOGGER.info("Migrated inverter_curtailment_enabled to ac_inverter_curtailment_enabled (data)")

        # Migrate options keys
        if "solar_curtailment_enabled" in new_options:
            new_options["battery_curtailment_enabled"] = new_options.pop("solar_curtailment_enabled")
            _LOGGER.info("Migrated solar_curtailment_enabled to battery_curtailment_enabled (options)")

        if "inverter_curtailment_enabled" in new_options:
            new_options["ac_inverter_curtailment_enabled"] = new_options.pop("inverter_curtailment_enabled")
            _LOGGER.info("Migrated inverter_curtailment_enabled to ac_inverter_curtailment_enabled (options)")

        # Update the config entry with new data, options, and version
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=3
        )

        _LOGGER.info("Migration to version 3 complete")

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

    # DEBUG: Log the exact payload being sent to Tesla to diagnose flat pricing issues
    try:
        buy_prices = tariff_data.get("energy_charges", {}).get("Summer", {}).get("rates", {})
        sell_prices = tariff_data.get("sell_tariff", {}).get("energy_charges", {}).get("Summer", {}).get("rates", {})
        if buy_prices:
            buy_values = list(buy_prices.values())
            sell_values = list(sell_prices.values()) if sell_prices else [0]
            unique_buy = len(set(buy_values))
            unique_sell = len(set(sell_values))
            _LOGGER.info(
                "PAYLOAD DEBUG: Sending %d buy prices (min=$%.4f, max=$%.4f, avg=$%.4f, unique=%d)",
                len(buy_values), min(buy_values), max(buy_values),
                sum(buy_values)/len(buy_values), unique_buy
            )
            _LOGGER.info(
                "PAYLOAD DEBUG: Sending %d sell prices (min=$%.4f, max=$%.4f, unique=%d)",
                len(sell_values), min(sell_values), max(sell_values), unique_sell
            )
            # Log sample periods to verify variation
            sample_periods = ["PERIOD_00_00", "PERIOD_06_00", "PERIOD_12_00", "PERIOD_18_00"]
            for period in sample_periods:
                if period in buy_prices:
                    _LOGGER.info(
                        "PAYLOAD DEBUG: %s buy=$%.4f sell=$%.4f",
                        period, buy_prices[period], sell_prices.get(period, 0)
                    )
            # CRITICAL: If all prices are the same, log a warning
            if unique_buy == 1:
                _LOGGER.warning(
                    "PAYLOAD DEBUG: ALL BUY PRICES ARE IDENTICAL ($%.4f) - this will cause flat tariff!",
                    buy_values[0]
                )
            if unique_buy <= 2:
                _LOGGER.warning(
                    "PAYLOAD DEBUG: Only %d unique buy prices - tariff may appear flat",
                    unique_buy
                )
    except Exception as err:
        _LOGGER.warning("PAYLOAD DEBUG: Error logging payload details: %s", err)

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


class CalendarHistoryView(HomeAssistantView):
    """HTTP view to get calendar history for mobile app."""

    url = "/api/power_sync/calendar_history"
    name = "api:power_sync:calendar_history"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for calendar history."""
        # Get period from query params (default: day)
        period = request.query.get("period", "day")
        # Get end_date from query params (format: YYYY-MM-DD)
        end_date = request.query.get("end_date")

        # Validate period
        valid_periods = ["day", "week", "month", "year"]
        if period not in valid_periods:
            return web.json_response(
                {"success": False, "error": f"Invalid period. Must be one of: {valid_periods}"},
                status=400
            )

        _LOGGER.info(f"📊 Calendar history HTTP request for period: {period}, end_date: {end_date}")

        # Find the power_sync entry and coordinator
        tesla_coordinator = None
        is_sigenergy = False
        for entry_id, data in self._hass.data.get(DOMAIN, {}).items():
            if isinstance(data, dict):
                is_sigenergy = data.get("is_sigenergy", False)
                if "tesla_coordinator" in data:
                    tesla_coordinator = data["tesla_coordinator"]
                break

        # Check if this is a Sigenergy setup - calendar history not available
        if is_sigenergy:
            _LOGGER.info("Calendar history not available for Sigenergy battery systems")
            return web.json_response(
                {
                    "success": False,
                    "error": "Calendar history is not available for Sigenergy battery systems",
                    "reason": "sigenergy_not_supported"
                },
                status=200  # Return 200 with error in body so mobile app handles gracefully
            )

        if not tesla_coordinator:
            _LOGGER.error("Tesla coordinator not available for HTTP endpoint")
            return web.json_response(
                {"success": False, "error": "Tesla coordinator not available"},
                status=503
            )

        # Fetch calendar history
        try:
            history = await tesla_coordinator.async_get_calendar_history(period=period, end_date=end_date)
        except Exception as e:
            _LOGGER.error(f"Error fetching calendar history: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

        if not history:
            _LOGGER.error("Failed to fetch calendar history")
            return web.json_response(
                {"success": False, "error": "Failed to fetch calendar history from Tesla API"},
                status=500
            )

        # Transform time_series to match mobile app format
        # Include both normalized fields AND detailed Tesla breakdown fields
        time_series = []
        for entry_data in history.get("time_series", []):
            time_series.append({
                "timestamp": entry_data.get("timestamp", ""),
                # Normalized fields for compatibility
                "solar_generation": entry_data.get("solar_energy_exported", 0),
                "battery_discharge": entry_data.get("battery_energy_exported", 0),
                "battery_charge": entry_data.get("battery_energy_imported", 0),
                "grid_import": entry_data.get("grid_energy_imported", 0),
                "grid_export": entry_data.get("grid_energy_exported_from_solar", 0) + entry_data.get("grid_energy_exported_from_battery", 0),
                "home_consumption": entry_data.get("consumer_energy_imported_from_grid", 0) + entry_data.get("consumer_energy_imported_from_solar", 0) + entry_data.get("consumer_energy_imported_from_battery", 0),
                # Detailed breakdown fields from Tesla API (for detail screens)
                "solar_energy_exported": entry_data.get("solar_energy_exported", 0),
                "battery_energy_exported": entry_data.get("battery_energy_exported", 0),
                "battery_energy_imported_from_grid": entry_data.get("battery_energy_imported_from_grid", 0),
                "battery_energy_imported_from_solar": entry_data.get("battery_energy_imported_from_solar", 0),
                "consumer_energy_imported_from_grid": entry_data.get("consumer_energy_imported_from_grid", 0),
                "consumer_energy_imported_from_solar": entry_data.get("consumer_energy_imported_from_solar", 0),
                "consumer_energy_imported_from_battery": entry_data.get("consumer_energy_imported_from_battery", 0),
                "grid_energy_exported_from_solar": entry_data.get("grid_energy_exported_from_solar", 0),
                "grid_energy_exported_from_battery": entry_data.get("grid_energy_exported_from_battery", 0),
            })

        result = {
            "success": True,
            "period": period,
            "time_series": time_series,
            "serial_number": history.get("serial_number"),
            "installation_date": history.get("installation_date"),
        }

        _LOGGER.info(f"✅ Calendar history HTTP response: {len(time_series)} records for period '{period}'")
        return web.json_response(result)


class PowerwallSettingsView(HomeAssistantView):
    """HTTP view to get Powerwall settings for mobile app Controls."""

    url = "/api/power_sync/powerwall_settings"
    name = "api:power_sync:powerwall_settings"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for Powerwall settings."""
        _LOGGER.info("⚙️ Powerwall settings HTTP request")

        # Find the power_sync entry and get token/site_id
        entry = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        # Check if this is a Sigenergy setup - Powerwall settings not applicable
        is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))
        if is_sigenergy:
            _LOGGER.info("Powerwall settings not available for Sigenergy battery systems")
            return web.json_response(
                {
                    "success": False,
                    "error": "Powerwall settings are not available for Sigenergy battery systems",
                    "reason": "sigenergy_not_supported"
                },
                status=200
            )

        try:
            current_token, provider = get_tesla_api_token(self._hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                return web.json_response(
                    {"success": False, "error": "Missing Tesla site ID or token"},
                    status=503
                )

            session = async_get_clientsession(self._hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Fetch site info
            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Failed to get site info: {response.status} - {text}")
                    return web.json_response(
                        {"success": False, "error": f"Failed to get site info: {response.status}"},
                        status=500
                    )
                data = await response.json()
                site_info = data.get("response", {})

            # Extract settings from site_info
            backup_reserve = site_info.get("backup_reserve_percent", 20)
            operation_mode = site_info.get("default_real_mode", "autonomous")

            # Get grid settings from components
            components = site_info.get("components", {})
            # Try components first, then site_info
            api_export_rule = components.get("customer_preferred_export_rule") or site_info.get("customer_preferred_export_rule")
            disallow_charge = components.get("disallow_charge_from_grid_with_solar_installed", False)

            # For VPP users, the API doesn't return customer_preferred_export_rule
            # Use non_export_configured to derive the value, or default to battery_ok
            if api_export_rule is None:
                non_export = components.get("non_export_configured", False)
                api_export_rule = "never" if non_export else "battery_ok"

            # Check if solar curtailment is enabled - if so, use server's target rule
            # (more accurate than stale Tesla API values)
            solar_curtailment_enabled = entry.options.get(
                CONF_BATTERY_CURTAILMENT_ENABLED,
                entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
            )

            if solar_curtailment_enabled:
                # Use cached rule (what server is targeting) if available
                entry_data = self._hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                cached_rule = entry_data.get("cached_export_rule")
                if cached_rule:
                    grid_export_rule = cached_rule
                    _LOGGER.debug(f"Using server's target export rule '{cached_rule}' (API reported '{api_export_rule}')")
                else:
                    grid_export_rule = api_export_rule
            else:
                grid_export_rule = api_export_rule

            # Check if manual export override is active
            entry_data = self._hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            manual_export_override = entry_data.get("manual_export_override", False)

            result = {
                "success": True,
                "backup_reserve": backup_reserve,
                "operation_mode": operation_mode,
                "grid_export_rule": grid_export_rule,
                "grid_charging_enabled": not disallow_charge,
                "solar_curtailment_enabled": solar_curtailment_enabled,
                "manual_export_override": manual_export_override,
            }

            _LOGGER.info(f"✅ Powerwall settings: reserve={backup_reserve}%, mode={operation_mode}, export={grid_export_rule}, manual_override={manual_export_override}")
            return web.json_response(result)

        except Exception as e:
            _LOGGER.error(f"Error fetching Powerwall settings: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class PowerwallTypeView(HomeAssistantView):
    """HTTP view to get Powerwall type (PW2/PW3) for mobile app Settings."""

    url = "/api/power_sync/powerwall_type"
    name = "api:power_sync:powerwall_type"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for Powerwall type."""
        _LOGGER.info("🔋 Powerwall type HTTP request")

        # Find the power_sync entry and get token/site_id
        entry = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        try:
            current_token, provider = get_tesla_api_token(self._hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                return web.json_response(
                    {"success": False, "error": "Missing Tesla site ID or token"},
                    status=503
                )

            session = async_get_clientsession(self._hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Fetch site info
            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Failed to get site info: {response.status} - {text}")
                    return web.json_response(
                        {"success": False, "error": f"Failed to get site info: {response.status}"},
                        status=500
                    )
                data = await response.json()
                site_info = data.get("response", {})

            # Extract gateway info - gateways array contains part_name
            components = site_info.get("components", {})
            gateways = components.get("gateways", [])
            if not gateways:
                # Try top-level gateways
                gateways = site_info.get("gateways", [])

            powerwall_type = "unknown"
            part_name = None

            if gateways and len(gateways) > 0:
                gateway = gateways[0]  # Primary gateway
                part_name = gateway.get("part_name", "")

                # Detect type from part_name
                if "Powerwall 3" in part_name:
                    powerwall_type = "PW3"
                elif "Powerwall 2" in part_name or "Powerwall+" in part_name:
                    powerwall_type = "PW2"
                elif "Powerwall" in part_name:
                    # Generic Powerwall, try to determine from part_number
                    part_number = gateway.get("part_number", "")
                    if part_number.startswith("170"):  # PW3 part numbers start with 170
                        powerwall_type = "PW3"
                    else:
                        powerwall_type = "PW2"  # Default to PW2 for older units

            _LOGGER.info(f"✅ Powerwall type: {powerwall_type} (part_name: {part_name})")

            return web.json_response({
                "success": True,
                "powerwall_type": powerwall_type,
                "part_name": part_name,
                "gateway_count": len(gateways),
            })

        except Exception as e:
            _LOGGER.error(f"Error fetching Powerwall type: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class InverterStatusView(HomeAssistantView):
    """HTTP view to get AC-coupled inverter status for mobile app."""

    url = "/api/power_sync/inverter_status"
    name = "api:power_sync:inverter_status"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for inverter status."""
        _LOGGER.info("☀️ Inverter status HTTP request")

        # Find the power_sync entry
        entry = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        # Check if inverter curtailment is enabled
        inverter_enabled = entry.options.get(
            CONF_AC_INVERTER_CURTAILMENT_ENABLED,
            entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        )

        if not inverter_enabled:
            return web.json_response({
                "success": True,
                "enabled": False,
                "message": "Inverter curtailment not enabled"
            })

        # Get inverter configuration
        inverter_brand = entry.options.get(
            CONF_INVERTER_BRAND,
            entry.data.get(CONF_INVERTER_BRAND, "sungrow")
        )
        inverter_host = entry.options.get(
            CONF_INVERTER_HOST,
            entry.data.get(CONF_INVERTER_HOST, "")
        )
        inverter_port = entry.options.get(
            CONF_INVERTER_PORT,
            entry.data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
        )
        inverter_slave_id = entry.options.get(
            CONF_INVERTER_SLAVE_ID,
            entry.data.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)
        )
        inverter_model = entry.options.get(
            CONF_INVERTER_MODEL,
            entry.data.get(CONF_INVERTER_MODEL)
        )
        inverter_token = entry.options.get(
            CONF_INVERTER_TOKEN,
            entry.data.get(CONF_INVERTER_TOKEN)
        )
        fronius_load_following = entry.options.get(
            CONF_FRONIUS_LOAD_FOLLOWING,
            entry.data.get(CONF_FRONIUS_LOAD_FOLLOWING, False)
        )
        # Enphase Enlighten credentials for automatic JWT token refresh
        enphase_username = entry.options.get(
            CONF_ENPHASE_USERNAME,
            entry.data.get(CONF_ENPHASE_USERNAME)
        )
        enphase_password = entry.options.get(
            CONF_ENPHASE_PASSWORD,
            entry.data.get(CONF_ENPHASE_PASSWORD)
        )
        enphase_serial = entry.options.get(
            CONF_ENPHASE_SERIAL,
            entry.data.get(CONF_ENPHASE_SERIAL)
        )
        enphase_normal_profile = entry.options.get(
            CONF_ENPHASE_NORMAL_PROFILE,
            entry.data.get(CONF_ENPHASE_NORMAL_PROFILE)
        )
        enphase_zero_export_profile = entry.options.get(
            CONF_ENPHASE_ZERO_EXPORT_PROFILE,
            entry.data.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE)
        )
        enphase_is_installer = entry.options.get(
            CONF_ENPHASE_IS_INSTALLER,
            entry.data.get(CONF_ENPHASE_IS_INSTALLER, False)
        )

        if not inverter_host:
            return web.json_response({
                "success": True,
                "enabled": True,
                "error": "Inverter not configured (no host)"
            })

        try:
            controller = get_inverter_controller(
                brand=inverter_brand,
                host=inverter_host,
                port=inverter_port,
                slave_id=inverter_slave_id,
                model=inverter_model,
                token=inverter_token,
                load_following=fronius_load_following,
                enphase_username=enphase_username,
                enphase_password=enphase_password,
                enphase_serial=enphase_serial,
                enphase_normal_profile=enphase_normal_profile,
                enphase_zero_export_profile=enphase_zero_export_profile,
                enphase_is_installer=enphase_is_installer,
            )

            if not controller:
                return web.json_response({
                    "success": False,
                    "enabled": True,
                    "error": f"Unsupported inverter brand: {inverter_brand}"
                })

            # Get status from controller
            state = await controller.get_status()
            await controller.disconnect()

            # Convert state to dict
            state_dict = state.to_dict()

            # Use tracked inverter_last_state as source of truth for is_curtailed
            # This fixes Fronius simple mode where power_limit_enabled is False
            # but the inverter is actually curtailed using soft export limit
            entry_data = self._hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            inverter_last_state = entry_data.get("inverter_last_state")
            if inverter_last_state == "curtailed":
                state_dict["is_curtailed"] = True
                if state_dict.get("status") == "online":
                    state_dict["status"] = "curtailed"
            elif inverter_last_state in ("normal", "running"):
                state_dict["is_curtailed"] = False

            # Check if it's nighttime for sleep detection
            is_night = False
            try:
                sun_state = self._hass.states.get("sun.sun")
                if sun_state:
                    is_night = sun_state.state == "below_horizon"
                else:
                    # Fallback to hour-based check (6pm-6am)
                    from datetime import datetime
                    local_hour = datetime.now().hour
                    is_night = local_hour >= 18 or local_hour < 6
            except Exception:
                pass

            # Apply sleep detection at night if:
            # - Status is offline/error, OR
            # - Power output is very low (< 100W, e.g. Sungrow PID recovery mode)
            if is_night:
                power_output = state_dict.get('power_output_w', 0) or 0
                status = state_dict.get('status')
                if status in ('offline', 'error') or power_output < 100:
                    state_dict['status'] = 'sleep'
                    state_dict['error_message'] = 'Inverter in sleep mode (night)'

            result = {
                "success": True,
                "enabled": True,
                "brand": inverter_brand,
                "model": inverter_model,
                "host": inverter_host,
                **state_dict
            }

            _LOGGER.info(f"✅ Inverter status: {state_dict.get('status')}, curtailed: {state_dict.get('is_curtailed')}")
            return web.json_response(result)

        except Exception as e:
            _LOGGER.error(f"Error getting inverter status: {e}", exc_info=True)
            # Determine if it's likely nighttime (inverter sleep) vs actual offline
            # Use sun.sun entity if available for accurate sunrise/sunset
            is_night = False
            try:
                sun_state = self._hass.states.get("sun.sun")
                if sun_state:
                    is_night = sun_state.state == "below_horizon"
                else:
                    # Fallback to hour-based check (6pm-6am)
                    from datetime import datetime
                    local_hour = datetime.now().hour
                    is_night = local_hour >= 18 or local_hour < 6
            except Exception:
                pass

            status = "sleep" if is_night else "offline"
            description = "Inverter in sleep mode (night)" if is_night else "Cannot reach inverter"

            return web.json_response({
                "success": True,
                "enabled": True,
                "status": status,
                "is_curtailed": False,
                "power_output_w": None,
                "power_limit_percent": None,
                "brand": inverter_brand,
                "model": inverter_model,
                "host": inverter_host,
                "error_message": description
            })


class SigenergyTariffView(HomeAssistantView):
    """HTTP view to get current Sigenergy tariff schedule for mobile app."""

    url = "/api/power_sync/sigenergy_tariff"
    name = "api:power_sync:sigenergy_tariff"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for Sigenergy tariff schedule."""
        _LOGGER.debug("📊 Sigenergy tariff HTTP request")

        # Find the power_sync entry and data
        entry = None
        entry_data = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            entry_data = self._hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        # Check if this is a Sigenergy system
        battery_system = entry.data.get(CONF_BATTERY_SYSTEM, "tesla")
        if battery_system != "sigenergy":
            return web.json_response({
                "success": False,
                "error": "Not a Sigenergy system",
                "battery_system": battery_system
            })

        # Get stored tariff data
        tariff_data = entry_data.get("sigenergy_tariff")
        if not tariff_data:
            return web.json_response({
                "success": True,
                "message": "No tariff synced yet",
                "buy_prices": [],
                "sell_prices": [],
            })

        return web.json_response({
            "success": True,
            "buy_prices": tariff_data.get("buy_prices", []),
            "sell_prices": tariff_data.get("sell_prices", []),
            "synced_at": tariff_data.get("synced_at"),
            "sync_mode": tariff_data.get("sync_mode"),
        })


class ConfigView(HomeAssistantView):
    """HTTP view to get backend configuration for mobile app auto-detection."""

    url = "/api/power_sync/backend_config"
    name = "api:power_sync:backend_config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for backend configuration."""
        _LOGGER.info("📱 Config HTTP request (mobile app auto-detection)")

        # Find the power_sync entry
        entry = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        try:
            # Get battery system from config
            battery_system = entry.data.get(CONF_BATTERY_SYSTEM, "tesla")

            # Get electricity provider
            electricity_provider = entry.options.get(
                CONF_ELECTRICITY_PROVIDER,
                entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
            )

            # Build features dict based on configuration
            features = {
                "solar_curtailment": entry.options.get(
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
                ),
                "inverter_control": entry.options.get(
                    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
                    entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
                ),
                "spike_protection": entry.options.get(
                    CONF_SPIKE_PROTECTION_ENABLED,
                    entry.data.get(CONF_SPIKE_PROTECTION_ENABLED, False)
                ),
                "export_boost": entry.options.get(
                    CONF_EXPORT_BOOST_ENABLED,
                    entry.data.get(CONF_EXPORT_BOOST_ENABLED, False)
                ),
                "demand_charges": entry.options.get(
                    CONF_DEMAND_CHARGE_ENABLED,
                    entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False)
                ),
                "auto_sync": entry.options.get(
                    CONF_AUTO_SYNC_ENABLED,
                    entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
                ),
            }

            # Add Sigenergy-specific info if applicable
            sigenergy_config = None
            if battery_system == "sigenergy":
                sigenergy_config = {
                    "station_id": entry.data.get(CONF_SIGENERGY_STATION_ID),
                    "modbus_enabled": bool(entry.options.get(
                        CONF_SIGENERGY_MODBUS_HOST,
                        entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
                    )),
                }

            # Get EV provider configuration
            ev_provider = entry.options.get(
                CONF_EV_PROVIDER,
                entry.data.get(CONF_EV_PROVIDER)
            )

            result = {
                "success": True,
                "battery_system": battery_system,
                "electricity_provider": electricity_provider,
                "ev_provider": ev_provider,  # Tesla (fleet_api/tesla_ble/both) or None for OCPP-only
                "features": features,
                "sigenergy": sigenergy_config,
            }

            _LOGGER.info(f"✅ Config response: battery_system={battery_system}, provider={electricity_provider}")
            return web.json_response(result)

        except Exception as e:
            _LOGGER.error(f"Error fetching config: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class ConfigViewLegacy(HomeAssistantView):
    """Legacy HTTP view at old URL for backwards compatibility."""

    url = "/api/power_sync/config"
    name = "api:power_sync:config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, config_view: ConfigView):
        """Initialize the view."""
        self._config_view = config_view

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request - delegate to main ConfigView."""
        _LOGGER.info("📱 Config HTTP request (legacy URL)")
        return await self._config_view.get(request)


class TariffPriceView(HomeAssistantView):
    """HTTP view to get current electricity prices from Tesla tariff schedule.

    This endpoint is designed for Globird users who don't have an API like Amber.
    It calculates the current import/export prices based on the Tesla tariff
    that was manually configured in the Tesla app.
    """

    url = "/api/power_sync/tariff_price"
    name = "api:power_sync:tariff_price"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for current tariff-based prices."""
        _LOGGER.info("💰 Tariff price HTTP request")

        # Find the power_sync entry
        entry = None
        entry_id = None
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            entry = config_entry
            entry_id = config_entry.entry_id
            break

        if not entry:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503
            )

        try:
            # Always fetch fresh tariff data to get current TOU period
            _LOGGER.info("Fetching tariff from Tesla API")
            tariff_data = await self._fetch_tesla_tariff(entry)

            if not tariff_data:
                return web.json_response({
                    "success": False,
                    "error": "No tariff schedule available. Configure your rate plan in the Tesla app."
                }, status=404)

            # Get current prices (already in cents from _fetch_tesla_tariff)
            buy_price_cents = tariff_data.get("buy_price", 0)
            sell_price_cents = tariff_data.get("sell_price", 0)
            current_period = tariff_data.get("current_period", "UNKNOWN")

            result = {
                "success": True,
                "import": {
                    "perKwh": buy_price_cents,
                    "channelType": "general",
                    "type": "TariffInterval",
                    "duration": 30,
                    "spikeStatus": None,
                    "source": "tesla_tariff",
                },
                "feedIn": {
                    # Amber format: feedIn is negative when you get paid
                    # We negate to match Amber convention
                    "perKwh": -sell_price_cents,
                    "channelType": "feedIn",
                    "type": "TariffInterval",
                    "duration": 30,
                    "spikeStatus": None,
                    "source": "tesla_tariff",
                },
                "current_period": current_period,
                "utility": tariff_data.get("utility"),
                "plan_name": tariff_data.get("plan_name"),
                "last_sync": tariff_data.get("last_sync"),
            }

            _LOGGER.info(
                f"✅ Tariff price response: period={current_period}, buy={buy_price_cents:.1f}c, sell={sell_price_cents:.1f}c"
            )
            return web.json_response(result)

        except Exception as e:
            _LOGGER.error(f"Error fetching tariff price: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def _fetch_tesla_tariff(self, entry: ConfigEntry) -> dict | None:
        """Fetch tariff from Tesla site_info API and extract current prices."""
        try:
            current_token, provider = get_tesla_api_token(self._hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                _LOGGER.warning("Missing Tesla site ID or token")
                return None

            session = async_get_clientsession(self._hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Fetch site_info which contains tariff_content
            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Failed to get site_info: {response.status} - {text}")
                    return None

                data = await response.json()
                site_info = data.get("response", {})

            # Get tariff_content from site_info
            tariff = site_info.get("tariff_content", {})
            if not tariff:
                _LOGGER.warning("No tariff_content in Tesla site_info response")
                return None

            _LOGGER.debug(f"Tesla tariff_content utility: {tariff.get('utility')}, name: {tariff.get('name')}")

            # Determine current season and TOU period
            from datetime import datetime as dt
            from zoneinfo import ZoneInfo

            # Get timezone from site_info
            tz_name = site_info.get("installation_time_zone", "UTC")
            try:
                tz = ZoneInfo(tz_name)
            except:
                tz = ZoneInfo("UTC")
            now = dt.now(tz)
            current_hour = now.hour
            current_dow = now.weekday()  # 0=Monday, 6=Sunday

            # Find current season
            seasons = tariff.get("seasons", {})
            current_season = None
            for season_name, season_data in seasons.items():
                from_month = season_data.get("fromMonth", 0)
                to_month = season_data.get("toMonth", 0)
                if from_month and to_month:
                    if from_month <= now.month <= to_month:
                        current_season = season_name
                        break
            if not current_season:
                current_season = "Summer" if "Summer" in seasons else next(iter(seasons.keys()), None)

            _LOGGER.debug(f"Current season: {current_season}, hour: {current_hour}, dow: {current_dow}")

            # Find current TOU period
            tou_periods = seasons.get(current_season, {}).get("tou_periods", {})
            current_period = None
            for period_name, period_data in tou_periods.items():
                # Handle both list format and object format
                periods_list = period_data if isinstance(period_data, list) else []
                for period in periods_list:
                    from_dow = period.get("fromDayOfWeek", 0)
                    to_dow = period.get("toDayOfWeek", 6)
                    from_hour = period.get("fromHour", 0)
                    to_hour = period.get("toHour", 24)

                    # Check day of week (Tesla uses 0=Sunday, Python uses 0=Monday)
                    tesla_dow = (current_dow + 1) % 7  # Convert Python dow to Tesla dow
                    if from_dow <= tesla_dow <= to_dow:
                        # Check time - handle overnight periods (e.g., 21:00 to 10:00)
                        if from_hour <= to_hour:
                            # Normal period (e.g., 10:00 to 14:00)
                            if from_hour <= current_hour < to_hour:
                                current_period = period_name
                                break
                        else:
                            # Overnight period (e.g., 21:00 to 10:00)
                            if current_hour >= from_hour or current_hour < to_hour:
                                current_period = period_name
                                break
                if current_period:
                    break

            if not current_period:
                current_period = "ALL"
            _LOGGER.info(f"Current TOU period: {current_period}")

            # Get energy charges for current season
            # tariff_content format: energy_charges.Summer.ON_PEAK = 0.48 (no 'rates' key)
            energy_charges = tariff.get("energy_charges", {})
            season_charges = energy_charges.get(current_season, {})

            # Handle both formats: direct values or nested under 'rates'
            if "rates" in season_charges:
                buy_rates = season_charges.get("rates", {})
            else:
                buy_rates = {k: v for k, v in season_charges.items() if isinstance(v, (int, float))}

            # Get sell tariff
            sell_tariff = tariff.get("sell_tariff", {})
            sell_energy_charges = sell_tariff.get("energy_charges", {})
            sell_season_charges = sell_energy_charges.get(current_season, {})
            if "rates" in sell_season_charges:
                sell_rates = sell_season_charges.get("rates", {})
            else:
                sell_rates = {k: v for k, v in sell_season_charges.items() if isinstance(v, (int, float))}

            # Get current prices
            current_buy_price = buy_rates.get(current_period, buy_rates.get("ALL", 0))
            current_sell_price = sell_rates.get(current_period, sell_rates.get("ALL", 0))

            # Convert from $/kWh to c/kWh (multiply by 100)
            current_buy_cents = round(current_buy_price * 100, 2)
            current_sell_cents = round(current_sell_price * 100, 2)

            _LOGGER.info(f"Tesla tariff prices - Buy: {current_buy_cents}c/kWh, Sell: {current_sell_cents}c/kWh (period: {current_period})")

            tariff_result = {
                "current_period": current_period,
                "current_season": current_season,
                "buy_price": current_buy_cents,
                "sell_price": current_sell_cents,
                "buy_rates": buy_rates,
                "sell_rates": sell_rates,
                "utility": tariff.get("utility", "Unknown"),
                "plan_name": tariff.get("name", "Unknown"),
                "last_sync": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

            # Store for future use
            if DOMAIN in self._hass.data and entry.entry_id in self._hass.data[DOMAIN]:
                self._hass.data[DOMAIN][entry.entry_id]["tariff_schedule"] = tariff_result

            return tariff_result

        except Exception as e:
            _LOGGER.error(f"Error fetching Tesla tariff: {e}", exc_info=True)
            return None


class AutomationsView(HomeAssistantView):
    """HTTP view to manage automations for mobile app."""

    url = "/api/power_sync/automations"
    name = "api:power_sync:automations"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_store(self):
        """Get the automation store from hass.data."""
        if DOMAIN not in self._hass.data:
            return None
        return self._hass.data[DOMAIN].get("automation_store")

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request - list all automations."""
        _LOGGER.info("📱 Automations HTTP GET request")

        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            automations = store.get_all()
            return web.json_response({
                "success": True,
                "automations": automations
            })
        except Exception as e:
            _LOGGER.error(f"Error fetching automations: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def post(self, request: web.Request) -> web.Response:
        """Handle POST request - create new automation."""
        _LOGGER.info("📱 Automations HTTP POST request")

        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            data = await request.json()
            _LOGGER.debug(f"📱 Creating automation with data: name={data.get('name')}, actions={data.get('actions')}")
            automation = store.create(data)
            await store.async_save()
            return web.json_response({
                "success": True,
                "automation": automation
            })
        except Exception as e:
            _LOGGER.error(f"Error creating automation: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class AutomationDetailView(HomeAssistantView):
    """HTTP view to manage a single automation."""

    url = "/api/power_sync/automations/{automation_id}"
    name = "api:power_sync:automation_detail"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_store(self):
        """Get the automation store from hass.data."""
        if DOMAIN not in self._hass.data:
            return None
        return self._hass.data[DOMAIN].get("automation_store")

    async def get(self, request: web.Request, automation_id: str) -> web.Response:
        """Handle GET request - get single automation."""
        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            automation = store.get(int(automation_id))
            if not automation:
                return web.json_response(
                    {"success": False, "error": "Automation not found"},
                    status=404
                )
            return web.json_response({
                "success": True,
                "automation": automation
            })
        except Exception as e:
            _LOGGER.error(f"Error fetching automation: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def put(self, request: web.Request, automation_id: str) -> web.Response:
        """Handle PUT request - update automation."""
        _LOGGER.info(f"📱 Automations HTTP PUT request for id={automation_id}")

        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            data = await request.json()
            trigger = data.get('trigger', {})
            _LOGGER.debug(f"📱 Updating automation {automation_id} with data: name={data.get('name')}, trigger={trigger}, actions={data.get('actions')}, conditions={data.get('conditions')}")
            automation = store.update(int(automation_id), data)
            if not automation:
                return web.json_response(
                    {"success": False, "error": "Automation not found"},
                    status=404
                )
            await store.async_save()
            return web.json_response({
                "success": True,
                "automation": automation
            })
        except Exception as e:
            _LOGGER.error(f"Error updating automation: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def delete(self, request: web.Request, automation_id: str) -> web.Response:
        """Handle DELETE request - delete automation."""
        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            success = store.delete(int(automation_id))
            if not success:
                return web.json_response(
                    {"success": False, "error": "Automation not found"},
                    status=404
                )
            await store.async_save()
            return web.json_response({"success": True})
        except Exception as e:
            _LOGGER.error(f"Error deleting automation: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class AutomationToggleView(HomeAssistantView):
    """HTTP view to toggle automation enabled state."""

    url = "/api/power_sync/automations/{automation_id}/toggle"
    name = "api:power_sync:automation_toggle"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_store(self):
        """Get the automation store from hass.data."""
        if DOMAIN not in self._hass.data:
            return None
        return self._hass.data[DOMAIN].get("automation_store")

    async def post(self, request: web.Request, automation_id: str) -> web.Response:
        """Handle POST request - toggle automation."""
        store = self._get_store()
        if not store:
            return web.json_response(
                {"success": False, "error": "Automation store not initialized"},
                status=503
            )

        try:
            result = store.toggle(int(automation_id))
            if result is None:
                return web.json_response(
                    {"success": False, "error": "Automation not found"},
                    status=404
                )
            await store.async_save()
            return web.json_response({
                "success": True,
                "enabled": result
            })
        except Exception as e:
            _LOGGER.error(f"Error toggling automation: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class AutomationGroupsView(HomeAssistantView):
    """HTTP view to get automation groups."""

    url = "/api/power_sync/automations/groups"
    name = "api:power_sync:automation_groups"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_store(self):
        """Get the automation store from hass.data."""
        if DOMAIN not in self._hass.data:
            return None
        return self._hass.data[DOMAIN].get("automation_store")

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request - get all group names."""
        store = self._get_store()
        if not store:
            return web.json_response({
                "success": True,
                "groups": ["Default Group"]
            })

        try:
            automations = store.get_all()
            groups = set()
            for auto in automations:
                group = auto.get("group_name", "Default Group")
                if group:
                    groups.add(group)
            if not groups:
                groups.add("Default Group")
            return web.json_response({
                "success": True,
                "groups": sorted(list(groups))
            })
        except Exception as e:
            _LOGGER.error(f"Error fetching groups: {e}", exc_info=True)
            return web.json_response({
                "success": True,
                "groups": ["Default Group"]
            })


class PushTokenRegisterView(HomeAssistantView):
    """HTTP view to register push notification tokens."""

    url = "/api/power_sync/push/register"
    name = "api:power_sync:push_register"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Handle POST request - register push token."""
        _LOGGER.info("📱 Push token registration request received")

        try:
            data = await request.json()
            push_token = data.get("push_token")
            platform = data.get("platform", "unknown")
            device_name = data.get("device_name", "Unknown device")

            if not push_token:
                return web.json_response(
                    {"success": False, "error": "push_token is required"},
                    status=400
                )

            # Store push token in hass.data for quick access
            if DOMAIN not in self._hass.data:
                self._hass.data[DOMAIN] = {}

            if "push_tokens" not in self._hass.data[DOMAIN]:
                self._hass.data[DOMAIN]["push_tokens"] = {}

            self._hass.data[DOMAIN]["push_tokens"][push_token] = {
                "token": push_token,
                "platform": platform,
                "device_name": device_name,
                "registered_at": datetime.now().isoformat(),
            }

            # Also persist to AutomationStore for survival across restarts
            # Find any config entry to get the automation store
            for entry_id, entry_data in self._hass.data.get(DOMAIN, {}).items():
                if isinstance(entry_data, dict) and "automation_store" in entry_data:
                    store = entry_data["automation_store"]
                    store.register_push_token(push_token, platform, device_name)
                    await store.async_save()
                    _LOGGER.info(f"✅ Push token persisted to storage for {device_name} ({platform})")
                    break

            # Log token type for debugging
            token_type = "Expo" if push_token.startswith("ExponentPushToken") else "Unknown/FCM"
            _LOGGER.info(f"✅ Push token registered for {device_name} ({platform}) - Token type: {token_type}")
            if not push_token.startswith("ExponentPushToken"):
                _LOGGER.warning(f"⚠️ Token does not start with 'ExponentPushToken' - notifications may not work! Token prefix: {push_token[:20]}...")
            return web.json_response({"success": True})

        except Exception as e:
            _LOGGER.error(f"Error registering push token: {e}", exc_info=True)
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )


class CurrentWeatherView(HomeAssistantView):
    """HTTP view to get current weather for mobile app dashboard."""

    url = "/api/power_sync/weather"
    name = "api:power_sync:weather"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request - fetch current weather."""
        from .automations.weather import async_get_current_weather
        from .const import CONF_OPENWEATHERMAP_API_KEY, CONF_WEATHER_LOCATION

        try:
            # Get config entry
            entries = self._hass.config_entries.async_entries(DOMAIN)
            if not entries:
                return web.json_response({
                    "success": False,
                    "error": "PowerSync not configured"
                }, status=400)

            entry = entries[0]

            # Get API key from config
            api_key = entry.options.get(
                CONF_OPENWEATHERMAP_API_KEY,
                entry.data.get(CONF_OPENWEATHERMAP_API_KEY)
            )

            if not api_key:
                return web.json_response({
                    "success": False,
                    "error": "OpenWeatherMap API key not configured"
                }, status=400)

            # Get weather location from config
            weather_location = entry.options.get(
                CONF_WEATHER_LOCATION,
                entry.data.get(CONF_WEATHER_LOCATION)
            )

            # Get timezone from config
            timezone = entry.options.get(
                "timezone",
                entry.data.get("timezone", "Australia/Brisbane")
            )

            # Fetch weather
            weather_data = await async_get_current_weather(
                self._hass, api_key, timezone, weather_location
            )

            if not weather_data:
                return web.json_response({
                    "success": False,
                    "error": "Failed to fetch weather data"
                }, status=500)

            return web.json_response({
                "success": True,
                "condition": weather_data.get("condition"),
                "description": weather_data.get("description"),
                "temperature_c": weather_data.get("temperature_c"),
                "humidity": weather_data.get("humidity"),
                "cloud_cover": weather_data.get("cloud_cover"),
                "is_night": weather_data.get("is_night", False),
            })

        except Exception as e:
            _LOGGER.error(f"Error fetching weather: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)


class EVStatusView(HomeAssistantView):
    """HTTP view to get EV integration status for mobile app."""

    url = "/api/power_sync/ev/status"
    name = "api:power_sync:ev:status"
    requires_auth = True

    # Supported Tesla integrations in HA
    TESLA_INTEGRATIONS = ["tesla_fleet", "teslemetry"]

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_powersync_config(self) -> dict:
        """Get PowerSync config entry options."""
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if entries:
            return dict(entries[0].options)
        return {}

    def _get_tesla_ble_status(self) -> dict:
        """Check if Tesla BLE entities are available."""
        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)

        # Only check for BLE if it's configured
        if ev_provider not in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            return {"available": False, "configured": False}

        prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        # Check if the BLE status entity exists
        status_entity = TESLA_BLE_BINARY_STATUS.format(prefix=prefix)
        state = self._hass.states.get(status_entity)

        if state is not None:
            return {
                "available": True,
                "configured": True,
                "connected": state.state == "on",
                "entity_prefix": prefix,
            }

        return {"available": False, "configured": True, "entity_prefix": prefix}

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for EV status."""
        try:
            # Get PowerSync config for EV provider setting
            config = self._get_powersync_config()
            ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)

            # Check for Tesla Fleet/Teslemetry integration
            active_integration = None
            tesla_entries = []
            fleet_api_available = False

            if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
                for integration in self.TESLA_INTEGRATIONS:
                    if integration in self._hass.config_entries.async_domains():
                        entries = self._hass.config_entries.async_entries(integration)
                        if entries:
                            active_integration = integration
                            tesla_entries = entries
                            fleet_api_available = True
                            break

            has_credentials = len(tesla_entries) > 0

            # Check Tesla BLE status
            ble_status = self._get_tesla_ble_status()

            # Count vehicles
            vehicle_count = 0

            # Count from Fleet API
            if active_integration and tesla_entries:
                device_registry = dr.async_get(self._hass)

                for device in device_registry.devices.values():
                    for identifier in device.identifiers:
                        if identifier[0] in self.TESLA_INTEGRATIONS:
                            potential_vin = identifier[1]
                            if len(str(potential_vin)) == 17 and not str(potential_vin).isdigit():
                                vehicle_count += 1
                            break

            # If BLE is available and no Fleet API vehicles, count BLE as 1 vehicle
            if ble_status.get("available") and vehicle_count == 0:
                vehicle_count = 1

            # Determine overall configured status
            is_configured = has_credentials or ble_status.get("available", False)

            return web.json_response({
                "success": True,
                "configured": is_configured,
                "linked": is_configured,
                "has_access_token": has_credentials,
                "token_expires_at": None,
                "vehicle_count": vehicle_count,
                "integration": active_integration,
                "ev_provider": ev_provider,
                "tesla_ble": ble_status,
            })

        except Exception as e:
            _LOGGER.error(f"Error getting EV status: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)


class EVVehiclesView(HomeAssistantView):
    """HTTP view to get Tesla vehicles for mobile app."""

    url = "/api/power_sync/ev/vehicles"
    name = "api:power_sync:ev:vehicles"
    requires_auth = True

    # Supported Tesla integrations in HA
    TESLA_INTEGRATIONS = ["tesla_fleet", "teslemetry"]

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_powersync_config(self) -> dict:
        """Get PowerSync config entry options."""
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if entries:
            return dict(entries[0].options)
        return {}

    def _get_tesla_ble_vehicle(self, prefix: str) -> dict | None:
        """Get vehicle data from Tesla BLE entities."""
        # Check if BLE status entity exists
        status_entity = TESLA_BLE_BINARY_STATUS.format(prefix=prefix)
        status_state = self._hass.states.get(status_entity)

        if status_state is None:
            _LOGGER.debug(f"EV BLE: Status entity {status_entity} not found")
            return None

        # Get charge level
        battery_level = None
        charge_level_entity = TESLA_BLE_SENSOR_CHARGE_LEVEL.format(prefix=prefix)
        charge_level_state = self._hass.states.get(charge_level_entity)
        if charge_level_state and charge_level_state.state not in ("unknown", "unavailable"):
            try:
                battery_level = int(float(charge_level_state.state))
            except (ValueError, TypeError):
                pass

        # Get charging state
        charging_state = None
        charging_state_entity = TESLA_BLE_SENSOR_CHARGING_STATE.format(prefix=prefix)
        charging_state_state = self._hass.states.get(charging_state_entity)
        if charging_state_state:
            if charging_state_state.state in ("unknown", "unavailable"):
                # Check if car is asleep
                asleep_entity = TESLA_BLE_BINARY_ASLEEP.format(prefix=prefix)
                asleep_state = self._hass.states.get(asleep_entity)
                if asleep_state and asleep_state.state == "on":
                    charging_state = "Asleep"
                else:
                    charging_state = "Unknown"
            else:
                charging_state = charging_state_state.state

        # Get charge limit
        charge_limit = None
        charge_limit_entity = TESLA_BLE_SENSOR_CHARGE_LIMIT.format(prefix=prefix)
        charge_limit_state = self._hass.states.get(charge_limit_entity)
        if charge_limit_state and charge_limit_state.state not in ("unknown", "unavailable"):
            try:
                charge_limit = int(float(charge_limit_state.state))
            except (ValueError, TypeError):
                pass

        # Check if plugged in (charge flap open is a proxy)
        is_plugged_in = False
        charge_flap_entity = f"binary_sensor.{prefix}_charge_flap"
        charge_flap_state = self._hass.states.get(charge_flap_entity)
        if charge_flap_state and charge_flap_state.state == "on":
            is_plugged_in = True

        # Check BLE connection status
        is_online = status_state.state == "on"

        _LOGGER.debug(
            f"EV BLE: Found vehicle via BLE - battery={battery_level}, "
            f"charging={charging_state}, limit={charge_limit}, online={is_online}"
        )

        return {
            "id": 1,
            "vehicle_id": f"ble_{prefix}",
            "vin": None,  # BLE doesn't provide VIN
            "display_name": f"Tesla (BLE)",
            "model": None,
            "battery_level": battery_level,
            "charging_state": charging_state,
            "charge_limit_soc": charge_limit,
            "is_plugged_in": is_plugged_in,
            "charger_power": None,
            "is_online": is_online,
            "data_updated_at": datetime.now().isoformat(),
            "source": "tesla_ble",
        }

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request for vehicle list."""
        try:
            vehicles = []

            # Get PowerSync config for EV provider setting
            config = self._get_powersync_config()
            ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
            ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

            # Check for Tesla Fleet/Teslemetry integration
            active_integration = None
            tesla_entries = []

            if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
                for integration in self.TESLA_INTEGRATIONS:
                    if integration in self._hass.config_entries.async_domains():
                        entries = self._hass.config_entries.async_entries(integration)
                        if entries:
                            active_integration = integration
                            tesla_entries = entries
                            break

            # Get vehicles from Fleet API
            if active_integration and tesla_entries:
                device_registry = dr.async_get(self._hass)
                entity_registry = er.async_get(self._hass)

                vehicle_id = 0
                for entry in tesla_entries:
                    for device in device_registry.devices.values():
                        is_tesla_vehicle = False
                        vin = None

                        for identifier in device.identifiers:
                            if identifier[0] in self.TESLA_INTEGRATIONS:
                                potential_vin = str(identifier[1])
                                if len(potential_vin) == 17 and not potential_vin.isdigit():
                                    is_tesla_vehicle = True
                                    vin = potential_vin
                                break

                        if not is_tesla_vehicle:
                            continue

                        vehicle_id += 1

                        battery_level = None
                        charging_state = None
                        charge_limit = None
                        is_plugged_in = False
                        charger_power = None

                        device_entities = []
                        sensor_entities = []
                        for entity in entity_registry.entities.values():
                            if entity.device_id != device.id:
                                continue
                            device_entities.append(entity.entity_id)

                            if entity.entity_id.startswith("sensor."):
                                state = self._hass.states.get(entity.entity_id)
                                state_val = state.state if state else "no_state"
                                sensor_entities.append(f"{entity.entity_id}={state_val}")

                            state = self._hass.states.get(entity.entity_id)
                            if not state:
                                continue

                            entity_id_lower = entity.entity_id.lower()

                            if ("battery" in entity_id_lower and
                                "sensor." in entity_id_lower and
                                "range" not in entity_id_lower and
                                "heater" not in entity_id_lower):
                                if state.state not in ("unknown", "unavailable"):
                                    try:
                                        val = float(state.state)
                                        if 0 <= val <= 100 and battery_level is None:
                                            battery_level = int(val)
                                    except (ValueError, TypeError):
                                        pass

                            if (("charging" in entity_id_lower or "charge_state" in entity_id_lower) and
                                "sensor." in entity_id_lower and
                                "limit" not in entity_id_lower and
                                "rate" not in entity_id_lower and
                                "power" not in entity_id_lower):
                                if state.state in ("unknown", "unavailable") and charging_state is None:
                                    charging_state = "Asleep"
                                elif state.state not in ("unknown", "unavailable") and charging_state is None:
                                    # Capitalize first letter to match app's expected format
                                    # Tesla Fleet: charging, complete, stopped, etc.
                                    # App expects: Charging, Complete, Stopped, etc.
                                    charging_state = state.state.capitalize()

                            if "charge_limit" in entity_id_lower or "charge_limit_soc" in entity_id_lower:
                                if state.state not in ("unknown", "unavailable"):
                                    try:
                                        charge_limit = int(float(state.state))
                                    except (ValueError, TypeError):
                                        pass

                            if ("plugged" in entity_id_lower or
                                "cable" in entity_id_lower or
                                "charger_connected" in entity_id_lower):
                                if state.state in ("on", "true", "connected"):
                                    is_plugged_in = True

                            if ("charger_power" in entity_id_lower or
                                "charge_rate" in entity_id_lower or
                                "charging_power" in entity_id_lower):
                                if state.state not in ("unknown", "unavailable"):
                                    try:
                                        charger_power = float(state.state)
                                    except (ValueError, TypeError):
                                        pass

                        _LOGGER.debug(f"EV: Device {device.name} has {len(device_entities)} entities")

                        vehicles.append({
                            "id": vehicle_id,
                            "vehicle_id": vin or str(device.id),
                            "vin": vin,
                            "display_name": device.name or f"Tesla {vehicle_id}",
                            "model": device.model,
                            "battery_level": battery_level,
                            "charging_state": charging_state,
                            "charge_limit_soc": charge_limit,
                            "is_plugged_in": is_plugged_in,
                            "charger_power": charger_power,
                            "is_online": True,
                            "data_updated_at": datetime.now().isoformat(),
                            "source": "fleet_api",
                        })

            # Get/supplement with Tesla BLE data
            if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
                ble_vehicle = self._get_tesla_ble_vehicle(ble_prefix)

                if ble_vehicle:
                    if ev_provider == EV_PROVIDER_BOTH and vehicles:
                        # Supplement existing Fleet API vehicle with BLE data if available
                        # BLE data is more real-time, so prefer it when available
                        for v in vehicles:
                            if ble_vehicle.get("battery_level") is not None:
                                v["battery_level"] = ble_vehicle["battery_level"]
                            if ble_vehicle.get("charging_state") and ble_vehicle["charging_state"] != "Unknown":
                                v["charging_state"] = ble_vehicle["charging_state"]
                            if ble_vehicle.get("charge_limit_soc") is not None:
                                v["charge_limit_soc"] = ble_vehicle["charge_limit_soc"]
                            v["ble_connected"] = ble_vehicle.get("is_online", False)
                    elif not vehicles:
                        # No Fleet API vehicles, use BLE as primary
                        vehicles.append(ble_vehicle)

            if not vehicles:
                message = "No Tesla vehicles found"
                if ev_provider == EV_PROVIDER_FLEET_API:
                    message = "No Tesla integration installed (tesla_fleet or teslemetry)"
                elif ev_provider == EV_PROVIDER_TESLA_BLE:
                    message = f"Tesla BLE entities not found (prefix: {ble_prefix})"
                return web.json_response({
                    "success": True,
                    "vehicles": [],
                    "message": message
                })

            return web.json_response({
                "success": True,
                "vehicles": vehicles,
            })

        except Exception as e:
            _LOGGER.error(f"Error getting vehicles: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)


class EVVehiclesSyncView(HomeAssistantView):
    """HTTP view to sync/refresh Tesla vehicles."""

    url = "/api/power_sync/ev/vehicles/sync"
    name = "api:power_sync:ev:vehicles:sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Handle POST request to sync vehicles."""
        try:
            # Trigger a refresh of tesla_fleet integration
            tesla_entries = self._hass.config_entries.async_entries("tesla_fleet")
            for entry in tesla_entries:
                await self._hass.config_entries.async_reload(entry.entry_id)

            # Return updated vehicle list
            vehicles_view = EVVehiclesView(self._hass)
            return await vehicles_view.get(request)

        except Exception as e:
            _LOGGER.error(f"Error syncing vehicles: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)


class EVVehicleCommandView(HomeAssistantView):
    """HTTP view to send commands to Tesla vehicles."""

    url = "/api/power_sync/ev/vehicles/{vehicle_id}/command"
    name = "api:power_sync:ev:vehicles:command"
    requires_auth = True

    # Supported Tesla integrations
    TESLA_INTEGRATIONS = ["tesla_fleet", "teslemetry"]

    def __init__(self, hass: HomeAssistant):
        """Initialize the view."""
        self._hass = hass

    def _get_powersync_config(self) -> dict:
        """Get PowerSync config entry options."""
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if entries:
            return dict(entries[0].options)
        return {}

    async def _get_tesla_ev_entity(self, entity_pattern: str, vehicle_vin: str | None = None) -> str | None:
        """Find a Tesla EV entity by pattern."""
        import re

        entity_registry = er.async_get(self._hass)
        device_registry = dr.async_get(self._hass)

        # Find devices from Tesla integrations
        tesla_devices = []
        for device in device_registry.devices.values():
            for identifier in device.identifiers:
                # Handle identifiers with varying tuple lengths
                if len(identifier) < 2:
                    continue
                domain = identifier[0]
                identifier_value = identifier[1]
                if domain in self.TESLA_INTEGRATIONS:
                    if len(str(identifier_value)) == 17 and not str(identifier_value).isdigit():
                        if vehicle_vin is None or identifier_value == vehicle_vin:
                            tesla_devices.append(device)
                            break

        if not tesla_devices:
            return None

        target_device = tesla_devices[0]

        pattern = re.compile(entity_pattern, re.IGNORECASE)
        for entity in entity_registry.entities.values():
            if entity.device_id == target_device.id:
                if pattern.match(entity.entity_id):
                    return entity.entity_id

        return None

    async def _is_vehicle_asleep(self, vehicle_vin: str | None = None) -> bool:
        """Check if vehicle is asleep."""
        # Check binary_sensor.*_asleep (custom integration)
        asleep_entity = await self._get_tesla_ev_entity(r"binary_sensor\..*_asleep$", vehicle_vin)
        if asleep_entity:
            state = self._hass.states.get(asleep_entity)
            if state and state.state == "on":
                _LOGGER.debug(f"Vehicle is asleep (from {asleep_entity})")
                return True

        # Check binary_sensor.*_online (if asleep not available)
        online_entity = await self._get_tesla_ev_entity(r"binary_sensor\..*_online$", vehicle_vin)
        if online_entity:
            state = self._hass.states.get(online_entity)
            if state and state.state == "off":
                _LOGGER.debug(f"Vehicle is offline/asleep (from {online_entity})")
                return True

        return False

    async def _wait_for_vehicle_awake(self, vehicle_vin: str | None = None, timeout: int = 30) -> bool:
        """Wait for vehicle to wake up, polling every 2 seconds."""
        for i in range(timeout // 2):
            if not await self._is_vehicle_asleep(vehicle_vin):
                _LOGGER.info(f"Vehicle is awake after {i * 2} seconds")
                return True
            _LOGGER.debug(f"Waiting for vehicle to wake... ({i * 2}s)")
            await asyncio.sleep(2)

        _LOGGER.warning(f"Vehicle did not wake within {timeout} seconds")
        return False

    async def _wake_vehicle(self, vehicle_vin: str | None = None) -> bool:
        """Wake up a Tesla vehicle and wait for it to be awake."""
        # Check if already awake
        if not await self._is_vehicle_asleep(vehicle_vin):
            _LOGGER.debug("Vehicle is already awake")
            return True

        _LOGGER.info("Vehicle is asleep, sending wake command...")

        # Try BLE first
        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        wake_sent = False

        if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            wake_entity = TESLA_BLE_BUTTON_WAKE_UP.format(prefix=ble_prefix)
            if self._hass.states.get(wake_entity):
                try:
                    await self._hass.services.async_call(
                        "button", "press",
                        {"entity_id": wake_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Sent wake command via BLE: {wake_entity}")
                    wake_sent = True
                except Exception as e:
                    _LOGGER.warning(f"BLE wake failed: {e}")

        # Try Fleet API
        if not wake_sent and ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
            wake_entity = await self._get_tesla_ev_entity(r"button\..*wake(_up)?$", vehicle_vin)
            if wake_entity:
                try:
                    await self._hass.services.async_call(
                        "button", "press",
                        {"entity_id": wake_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Sent wake command via Fleet API: {wake_entity}")
                    wake_sent = True
                except Exception as e:
                    _LOGGER.error(f"Fleet API wake failed: {e}")
                    return False

        if not wake_sent:
            _LOGGER.warning("No wake entity found")
            return False

        # Wait for vehicle to wake up (up to 30 seconds)
        return await self._wait_for_vehicle_awake(vehicle_vin, timeout=30)

    async def _is_vehicle_at_home(self, vehicle_vin: str | None = None) -> bool:
        """Check if vehicle is at home using binary_sensor or device_tracker."""
        # First try: binary_sensor.*_located_at_home (Teslemetry)
        # This is the most reliable method
        home_entity = await self._get_tesla_ev_entity(r"binary_sensor\..*_located_at_home$", vehicle_vin)
        if home_entity:
            state = self._hass.states.get(home_entity)
            if state and state.state not in ("unavailable", "unknown"):
                is_home = state.state.lower() == "on"
                _LOGGER.debug(f"Vehicle at home from {home_entity}: {state.state} (at_home={is_home})")
                return is_home

        # Second try: device_tracker.*_location
        location_entity = await self._get_tesla_ev_entity(r"device_tracker\..*_location$", vehicle_vin)
        if location_entity:
            state = self._hass.states.get(location_entity)
            if state and state.state not in ("unavailable", "unknown"):
                is_home = state.state.lower() == "home"
                _LOGGER.debug(f"Vehicle location from {location_entity}: {state.state} (at_home={is_home})")
                return is_home

        _LOGGER.warning("Could not determine vehicle location - no location entity found")
        return True  # Default to True to not block commands if we can't check

    async def _is_vehicle_plugged_in(self, vehicle_vin: str | None = None) -> bool:
        """Check if vehicle is plugged in."""
        # Check binary_sensor.*_charger (Tesla Fleet)
        charger_entity = await self._get_tesla_ev_entity(r"binary_sensor\..*_charger$", vehicle_vin)
        if charger_entity:
            state = self._hass.states.get(charger_entity)
            if state:
                is_plugged = state.state.lower() == "on"
                _LOGGER.debug(f"Vehicle plugged in from {charger_entity}: {state.state} (plugged={is_plugged})")
                return is_plugged

        # Check binary_sensor.*_charge_cable (Teslemetry)
        cable_entity = await self._get_tesla_ev_entity(r"binary_sensor\..*_charge_cable$", vehicle_vin)
        if cable_entity:
            state = self._hass.states.get(cable_entity)
            if state:
                is_plugged = state.state.lower() == "on"
                _LOGGER.debug(f"Vehicle plugged in from {cable_entity}: {state.state} (plugged={is_plugged})")
                return is_plugged

        _LOGGER.warning("Could not determine if vehicle is plugged in")
        return False

    async def _get_vehicle_charging_state(self, vehicle_vin: str | None = None) -> str:
        """Get current charging state."""
        # Tesla Fleet uses sensor.*_charging (no _state suffix)
        charging_entity = await self._get_tesla_ev_entity(r"sensor\..*_charging$", vehicle_vin)
        if charging_entity:
            state = self._hass.states.get(charging_entity)
            if state and state.state not in ("unavailable", "unknown"):
                return state.state.lower()
        return ""

    async def _start_charging(self, vehicle_vin: str | None = None) -> tuple[bool, str]:
        """Start charging. Returns (success, message)."""
        # Check preconditions
        if not await self._is_vehicle_at_home(vehicle_vin):
            msg = "Vehicle is not at home"
            _LOGGER.warning(msg)
            return False, msg

        if not await self._is_vehicle_plugged_in(vehicle_vin):
            msg = "Vehicle is not plugged in"
            _LOGGER.warning(msg)
            return False, msg

        charging_state = await self._get_vehicle_charging_state(vehicle_vin)
        if charging_state == "charging":
            msg = "Vehicle is already charging"
            _LOGGER.info(msg)
            return True, msg  # Return success since desired state is achieved

        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        # Try BLE first
        if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            charger_entity = TESLA_BLE_SWITCH_CHARGER.format(prefix=ble_prefix)
            if self._hass.states.get(charger_entity):
                try:
                    await self._hass.services.async_call(
                        "switch", "turn_on",
                        {"entity_id": charger_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Started charging via BLE: {charger_entity}")
                    return True, "Charging started"
                except Exception as e:
                    _LOGGER.warning(f"BLE start charging failed: {e}")

        # Try Fleet API
        if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
            await self._wake_vehicle(vehicle_vin)
            # Tesla Fleet uses switch.X_charge, not button.X_charge_start
            charge_switch_entity = await self._get_tesla_ev_entity(r"switch\..*_charge$", vehicle_vin)
            if charge_switch_entity:
                try:
                    await self._hass.services.async_call(
                        "switch", "turn_on",
                        {"entity_id": charge_switch_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Started charging via Fleet API: {charge_switch_entity}")
                    return True, "Charging started"
                except Exception as e:
                    _LOGGER.error(f"Fleet API start charging failed: {e}")
                    return False, f"Failed to start charging: {e}"
            else:
                _LOGGER.error("Could not find charge switch entity for Tesla vehicle")

        msg = f"Start charging failed - no suitable entity found (provider: {ev_provider})"
        _LOGGER.warning(msg)
        return False, msg

    async def _stop_charging(self, vehicle_vin: str | None = None) -> tuple[bool, str]:
        """Stop charging. Returns (success, message)."""
        # Check if actually charging
        charging_state = await self._get_vehicle_charging_state(vehicle_vin)
        if charging_state and charging_state != "charging":
            msg = f"Vehicle is not charging (state: {charging_state})"
            _LOGGER.info(msg)
            return True, msg  # Return success since desired state is achieved

        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        # Try BLE first
        if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            charger_entity = TESLA_BLE_SWITCH_CHARGER.format(prefix=ble_prefix)
            if self._hass.states.get(charger_entity):
                try:
                    await self._hass.services.async_call(
                        "switch", "turn_off",
                        {"entity_id": charger_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Stopped charging via BLE: {charger_entity}")
                    return True, "Charging stopped"
                except Exception as e:
                    _LOGGER.warning(f"BLE stop charging failed: {e}")

        # Try Fleet API
        if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
            await self._wake_vehicle(vehicle_vin)
            # Tesla Fleet uses switch.X_charge, not button.X_charge_stop
            charge_switch_entity = await self._get_tesla_ev_entity(r"switch\..*_charge$", vehicle_vin)
            if charge_switch_entity:
                try:
                    await self._hass.services.async_call(
                        "switch", "turn_off",
                        {"entity_id": charge_switch_entity},
                        blocking=True,
                    )
                    _LOGGER.info(f"Stopped charging via Fleet API: {charge_switch_entity}")
                    return True, "Charging stopped"
                except Exception as e:
                    _LOGGER.error(f"Fleet API stop charging failed: {e}")
                    return False, f"Failed to stop charging: {e}"

        return False, "No suitable entity found to stop charging"

    async def _set_charge_limit(self, percent: int, vehicle_vin: str | None = None) -> tuple[bool, str]:
        """Set charge limit percentage. Returns (success, message)."""
        # Clamp to valid range (50-100%)
        percent = max(50, min(100, int(percent)))

        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        # Try BLE first
        if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            limit_entity = TESLA_BLE_NUMBER_CHARGING_LIMIT.format(prefix=ble_prefix)
            if self._hass.states.get(limit_entity):
                try:
                    await self._hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": limit_entity, "value": percent},
                        blocking=True,
                    )
                    _LOGGER.info(f"Set charge limit to {percent}% via BLE: {limit_entity}")
                    return True, f"Charge limit set to {percent}%"
                except Exception as e:
                    _LOGGER.warning(f"BLE set charge limit failed: {e}")

        # Try Fleet API
        if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
            await self._wake_vehicle(vehicle_vin)
            limit_entity = await self._get_tesla_ev_entity(r"number\..*charge_limit$", vehicle_vin)
            if limit_entity:
                try:
                    await self._hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": limit_entity, "value": percent},
                        blocking=True,
                    )
                    _LOGGER.info(f"Set charge limit to {percent}% via Fleet API: {limit_entity}")
                    return True, f"Charge limit set to {percent}%"
                except Exception as e:
                    _LOGGER.error(f"Fleet API set charge limit failed: {e}")
                    return False, f"Failed to set charge limit: {e}"

        return False, "No suitable entity found to set charge limit"

    async def _set_charging_amps(self, amps: int, vehicle_vin: str | None = None) -> tuple[bool, str]:
        """Set charging amperage. Returns (success, message)."""
        # Clamp to valid range (1-48A for most, up to 80A for some)
        amps = max(1, min(80, int(amps)))

        # Check if plugged in - can only set amps when connected
        if not await self._is_vehicle_plugged_in(vehicle_vin):
            msg = "Vehicle is not plugged in - cannot set charging amps"
            _LOGGER.warning(msg)
            return False, msg

        config = self._get_powersync_config()
        ev_provider = config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)

        # Try BLE first (BLE supports same 5-32A range as cloud API)
        if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
            amps_entity = TESLA_BLE_NUMBER_CHARGING_AMPS.format(prefix=ble_prefix)
            if self._hass.states.get(amps_entity):
                # Tesla vehicles refuse charging below 5A
                ble_amps = max(5, min(32, amps))
                try:
                    await self._hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": amps_entity, "value": ble_amps},
                        blocking=True,
                    )
                    _LOGGER.info(f"Set charging amps to {ble_amps}A via BLE: {amps_entity}")
                    return True, f"Charging amps set to {ble_amps}A"
                except Exception as e:
                    _LOGGER.warning(f"BLE set charging amps failed: {e}")

        # Try Fleet API
        if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
            await self._wake_vehicle(vehicle_vin)
            # Tesla Fleet uses charge_current, Teslemetry uses charging_amps
            amps_entity = await self._get_tesla_ev_entity(r"number\..*(charging_amps|charge_current)$", vehicle_vin)
            if amps_entity:
                try:
                    await self._hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": amps_entity, "value": amps},
                        blocking=True,
                    )
                    _LOGGER.info(f"Set charging amps to {amps}A via Fleet API: {amps_entity}")
                    return True, f"Charging amps set to {amps}A"
                except Exception as e:
                    _LOGGER.error(f"Fleet API set charging amps failed: {e}")
                    return False, f"Failed to set charging amps: {e}"

        return False, "No suitable entity found to set charging amps"

    async def post(self, request: web.Request, vehicle_id: str) -> web.Response:
        """Handle POST request to send vehicle command."""
        try:
            data = await request.json()
            command = data.get("command")

            if not command:
                return web.json_response({
                    "success": False,
                    "error": "Missing 'command' parameter"
                }, status=400)

            valid_commands = ["wake_up", "start_charging", "stop_charging", "set_charge_limit", "set_charging_amps"]
            if command not in valid_commands:
                return web.json_response({
                    "success": False,
                    "error": f"Invalid command. Must be one of: {', '.join(valid_commands)}"
                }, status=400)

            vehicle_vin = data.get("vin")
            success = False
            message = ""

            if command == "wake_up":
                success = await self._wake_vehicle(vehicle_vin)
                message = "Vehicle is awake" if success else "Failed to wake vehicle"

            elif command == "start_charging":
                success, message = await self._start_charging(vehicle_vin)

            elif command == "stop_charging":
                success, message = await self._stop_charging(vehicle_vin)

            elif command == "set_charge_limit":
                percent = data.get("value") or data.get("percent") or data.get("limit")
                if percent is None:
                    return web.json_response({
                        "success": False,
                        "error": "Missing 'value' parameter for set_charge_limit (50-100)"
                    }, status=400)
                success, message = await self._set_charge_limit(int(percent), vehicle_vin)

            elif command == "set_charging_amps":
                amps = data.get("value") or data.get("amps")
                if amps is None:
                    return web.json_response({
                        "success": False,
                        "error": "Missing 'value' parameter for set_charging_amps (1-48)"
                    }, status=400)
                success, message = await self._set_charging_amps(int(amps), vehicle_vin)

            if success:
                return web.json_response({
                    "success": True,
                    "data": {"message": message}
                })
            else:
                return web.json_response({
                    "success": False,
                    "error": message
                }, status=500)

        except Exception as e:
            _LOGGER.error(f"Error executing vehicle command: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PowerSync from a config entry."""
    _LOGGER.info("=" * 60)
    _LOGGER.info("PowerSync integration loading...")
    _LOGGER.info("Domain: %s", DOMAIN)
    _LOGGER.info("Entry ID: %s", entry.entry_id)
    _LOGGER.info("Entry state: %s", entry.state)
    _LOGGER.info("=" * 60)

    # Update entry title if it has an old/incorrect name
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
    )
    has_amber = bool(entry.data.get(CONF_AMBER_API_TOKEN))

    # Determine correct title based on provider
    if electricity_provider == "globird" or (not has_amber and entry.data.get(CONF_AEMO_SPIKE_ENABLED)):
        expected_title = "PowerSync Globird"
    elif electricity_provider == "flow_power":
        expected_title = "PowerSync Flow Power"
    else:
        expected_title = "PowerSync Amber"

    # Update title if it doesn't match (migration for old entries)
    if entry.title != expected_title:
        _LOGGER.info(f"Updating entry title from '{entry.title}' to '{expected_title}'")
        hass.config_entries.async_update_entry(entry, title=expected_title)

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

    # Check if this is a Sigenergy setup (no Tesla needed)
    is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))
    tesla_coordinator = None
    sigenergy_coordinator = None
    token_getter = None  # Will be set for Tesla users

    if is_sigenergy:
        _LOGGER.info("Running in Sigenergy mode - Tesla credentials not required")

        # Initialize Sigenergy Modbus coordinator if Modbus host is configured
        sigenergy_modbus_host = entry.options.get(
            CONF_SIGENERGY_MODBUS_HOST,
            entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
        )
        if sigenergy_modbus_host:
            sigenergy_modbus_port = entry.options.get(
                CONF_SIGENERGY_MODBUS_PORT,
                entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
            )
            sigenergy_modbus_slave_id = entry.options.get(
                CONF_SIGENERGY_MODBUS_SLAVE_ID,
                entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 1)
            )
            _LOGGER.info(
                "Initializing Sigenergy Modbus coordinator: %s:%s (slave %s)",
                sigenergy_modbus_host, sigenergy_modbus_port, sigenergy_modbus_slave_id
            )
            sigenergy_coordinator = SigenergyEnergyCoordinator(
                hass,
                sigenergy_modbus_host,
                port=sigenergy_modbus_port,
                slave_id=sigenergy_modbus_slave_id,
            )
        else:
            _LOGGER.warning("Sigenergy mode enabled but no Modbus host configured - energy sensors will be unavailable")
    else:
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
    if tesla_coordinator:
        await tesla_coordinator.async_config_entry_first_refresh()
    if sigenergy_coordinator:
        try:
            await sigenergy_coordinator.async_config_entry_first_refresh()
            _LOGGER.info("Sigenergy Modbus coordinator initialized successfully")
        except Exception as e:
            _LOGGER.warning("Sigenergy Modbus coordinator failed to initialize: %s", e)
            # Don't fail the entire setup - allow other features to work
            sigenergy_coordinator = None

    # Initialize demand charge coordinator if enabled (Tesla only - requires grid power data)
    demand_charge_coordinator = None
    demand_charge_enabled = entry.options.get(
        CONF_DEMAND_CHARGE_ENABLED,
        entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False)
    )
    if demand_charge_enabled and tesla_coordinator:
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

    # Initialize Solcast Solar Forecast Coordinator if enabled
    solcast_coordinator = None
    solcast_enabled = entry.options.get(
        CONF_SOLCAST_ENABLED,
        entry.data.get(CONF_SOLCAST_ENABLED, False)
    )
    solcast_api_key = entry.options.get(
        CONF_SOLCAST_API_KEY,
        entry.data.get(CONF_SOLCAST_API_KEY, "")
    )
    solcast_resource_id = entry.options.get(
        CONF_SOLCAST_RESOURCE_ID,
        entry.data.get(CONF_SOLCAST_RESOURCE_ID, "")
    )

    if solcast_enabled and solcast_api_key and solcast_resource_id:
        from .coordinator import SolcastForecastCoordinator

        solcast_coordinator = SolcastForecastCoordinator(
            hass,
            api_key=solcast_api_key,
            resource_id=solcast_resource_id,
        )
        try:
            await solcast_coordinator.async_config_entry_first_refresh()
            _LOGGER.info(
                "Solcast Forecast Coordinator initialized for site %s",
                solcast_resource_id[:8] + "..." if len(solcast_resource_id) > 8 else solcast_resource_id,
            )
        except Exception as e:
            _LOGGER.error("Failed to initialize Solcast coordinator: %s", e)
            solcast_coordinator = None

    # Initialize persistent storage for data that survives HA restarts
    # (like Teslemetry's RestoreEntity pattern for export rule state)
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    stored_data = await store.async_load() or {}
    cached_export_rule = stored_data.get("cached_export_rule")
    if cached_export_rule:
        _LOGGER.info(f"Restored cached_export_rule='{cached_export_rule}' from persistent storage")

    # Restore battery health data from storage
    battery_health = stored_data.get("battery_health")
    if battery_health:
        _LOGGER.info(f"Restored battery health from storage: {battery_health.get('degradation_percent')}% degradation")

    # Restore force charge/discharge state from storage (survives HA restarts)
    force_mode_state = stored_data.get("force_mode_state")
    if force_mode_state:
        _LOGGER.info(f"Found persisted force mode state: {force_mode_state}")

    # Store coordinators and WebSocket client in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "amber_coordinator": amber_coordinator,
        "tesla_coordinator": tesla_coordinator,
        "sigenergy_coordinator": sigenergy_coordinator,  # For Sigenergy Modbus energy data
        "demand_charge_coordinator": demand_charge_coordinator,
        "aemo_spike_manager": aemo_spike_manager,
        "aemo_sensor_coordinator": aemo_sensor_coordinator,  # For Flow Power AEMO-only mode
        "solcast_coordinator": solcast_coordinator,  # For Solcast solar forecasting
        "ws_client": ws_client,  # Store for cleanup on unload
        "entry": entry,
        "auto_sync_cancel": None,  # Will store the timer cancel function
        "aemo_spike_cancel": None,  # Will store the AEMO spike check cancel function
        "demand_charging_cancel": None,  # Will store the demand period grid charging cancel function
        "grid_charging_disabled_for_demand": False,  # Track if grid charging is disabled for demand period
        "cached_export_rule": cached_export_rule,  # Restored from persistent storage
        "battery_health": battery_health,  # Restored from persistent storage (from mobile app TEDAPI scans)
        "force_mode_state": force_mode_state,  # Restored force charge/discharge state
        "store": store,  # Reference to Store for saving updates
        "token_getter": token_getter,  # Function to get fresh Tesla API token
        "is_sigenergy": is_sigenergy,  # Track battery system type
    }

    # Helper function to update and persist cached export rule
    async def update_cached_export_rule(new_rule: str) -> None:
        """Update the cached export rule in memory and persist to storage."""
        hass.data[DOMAIN][entry.entry_id]["cached_export_rule"] = new_rule
        store = hass.data[DOMAIN][entry.entry_id]["store"]
        # Preserve other stored data (like battery_health)
        stored_data = await store.async_load() or {}
        stored_data["cached_export_rule"] = new_rule
        await store.async_save(stored_data)
        _LOGGER.debug(f"Persisted cached_export_rule='{new_rule}' to storage")
        # Signal sensor to update
        async_dispatcher_send(hass, f"power_sync_curtailment_updated_{entry.entry_id}")

    # Helper function to get live status from Tesla API
    async def get_live_status() -> dict | None:
        """Get current live status from Tesla API.

        Returns:
            Dict with battery_soc, grid_power, solar_power, etc. or None if unavailable
            grid_power: Negative = exporting to grid, Positive = importing from grid
        """
        try:
            current_token, current_provider = token_getter()
            if not current_token:
                _LOGGER.debug("No Tesla API token available for live status check")
                return None

            session = async_get_clientsession(hass)
            api_base_url = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }

            async with session.get(
                f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/live_status",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    site_status = data.get("response", {})
                    result = {
                        "battery_soc": site_status.get("percentage_charged"),
                        "grid_power": site_status.get("grid_power"),  # Negative = exporting
                        "solar_power": site_status.get("solar_power"),
                        "battery_power": site_status.get("battery_power"),  # Negative = charging
                        "load_power": site_status.get("load_power"),
                    }
                    _LOGGER.debug(f"Live status: SOC={result['battery_soc']}%, grid={result['grid_power']}W, solar={result['solar_power']}W")
                    return result
                else:
                    _LOGGER.debug(f"Failed to get live_status: {response.status}")

        except Exception as e:
            _LOGGER.debug(f"Error getting live status: {e}")

        return None

    # Smart AC-coupled curtailment check
    async def should_curtail_ac_coupled(import_price: float | None, export_earnings: float | None) -> bool:
        """Smart curtailment logic for AC-coupled solar systems.

        For AC-coupled systems, we curtail the inverter when:
        1. Import price is negative (get paid to import - curtail to maximize grid import), OR
        2. Actually exporting (grid_power < 0) AND export earnings are negative, OR
        3. Battery is full (100%) AND export is unprofitable, OR
        4. Solar producing but battery NOT charging AND exporting at negative price

        Args:
            import_price: Current import price in c/kWh (negative = get paid to import)
            export_earnings: Current export earnings in c/kWh

        Returns:
            True if we should curtail, False if we should allow production
        """
        # Get live status for grid_power, battery_soc, solar_power, battery_power
        live_status = await get_live_status()

        if live_status is None:
            _LOGGER.debug("Could not get live status - not curtailing AC solar (conservative approach)")
            return False

        grid_power = live_status.get("grid_power")  # Negative = exporting
        battery_soc = live_status.get("battery_soc")
        solar_power = live_status.get("solar_power", 0) or 0
        battery_power = live_status.get("battery_power", 0) or 0  # Negative = charging
        load_power = live_status.get("load_power", 0) or 0

        _LOGGER.debug(
            f"AC-Coupled check: solar={solar_power:.0f}W, battery={battery_power:.0f}W (neg=charging), "
            f"grid={grid_power}W (neg=export), load={load_power:.0f}W, SOC={battery_soc}%"
        )

        # Compute state flags
        battery_is_charging = battery_power < -50  # At least 50W charging
        is_exporting = grid_power is not None and grid_power < -100  # Exporting more than 100W

        # Get configurable restore SOC threshold (restore inverter when battery drops below this)
        restore_soc = entry.options.get(
            CONF_INVERTER_RESTORE_SOC,
            entry.data.get(CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC)
        )

        # PRIORITY CHECK 1: If import price is negative, ALWAYS curtail AC solar
        # Getting paid to import from grid is better than free solar - maximize grid import
        # This takes precedence over battery charging (charge from grid instead)
        if import_price is not None and import_price < 0:
            _LOGGER.info(
                f"🔌 AC-COUPLED: Import price negative ({import_price:.2f}c/kWh) - curtailing to maximize grid import "
                f"(solar={solar_power:.0f}W, battery={battery_power:.0f}W)"
            )
            return True

        # PRIORITY CHECK 2: If exporting at negative price - but allow if battery absorbing
        # When battery is charging AND has room (SOC < 90%), small exports are OK
        # The value of charging the battery (for later peak discharge) exceeds small export losses
        if is_exporting and export_earnings is not None and export_earnings < 0:
            # Check if battery is absorbing the solar and has room
            battery_has_room = battery_soc is not None and battery_soc < 90
            if battery_is_charging and battery_has_room:
                # Battery is actively charging and has capacity - allow inverter to run
                # Small negative export is acceptable while battery absorbs solar
                _LOGGER.info(
                    f"🔋 AC-COUPLED: Exporting {abs(grid_power):.0f}W at negative price ({export_earnings:.2f}c/kWh) "
                    f"BUT battery charging at {abs(battery_power):.0f}W with room (SOC={battery_soc:.0f}%) "
                    f"- allowing inverter to run (battery value exceeds export loss)"
                )
                return False
            else:
                # Battery full or not charging - curtail to stop negative export
                _LOGGER.info(
                    f"🔌 AC-COUPLED: Exporting {abs(grid_power):.0f}W at negative price ({export_earnings:.2f}c/kWh) "
                    f"- should curtail (battery not absorbing or full, SOC={battery_soc}%)"
                )
                return True

        # RESTORE CHECK: If battery SOC < restore threshold, allow inverter to run
        # This ensures battery stays topped up before evening peak
        # Only applies when NOT exporting at negative price (checked above)
        if battery_soc is not None and battery_soc < restore_soc:
            if battery_is_charging or battery_soc < 100:  # Battery can still absorb
                _LOGGER.info(
                    f"🔋 AC-COUPLED: Battery SOC {battery_soc:.0f}% < restore threshold {restore_soc}% "
                    f"- allowing inverter to run (topping up battery)"
                )
                return False

        # PRIORITY CHECK 3: If battery is charging (absorbing solar) and not exporting, don't curtail
        # Solar going to battery is good (when import price is not negative)
        if battery_is_charging and not is_exporting:
            _LOGGER.info(
                f"⚡ AC-COUPLED: Battery charging ({abs(battery_power):.0f}W) at SOC {battery_soc:.0f}%, "
                f"not exporting (grid={grid_power}W) - skipping curtailment (solar being absorbed)"
            )
            return False

        # Check 4: If actually exporting (grid_power < 0) AND export earnings are negative
        # Only curtail when we're actually paying to export, not just when export price is negative
        if grid_power is not None and grid_power < 0:  # Negative = exporting
            if export_earnings is not None and export_earnings < 0:
                _LOGGER.info(f"🔌 AC-COUPLED: Exporting {abs(grid_power):.0f}W at negative price ({export_earnings:.2f}c/kWh) - should curtail")
                return True
            else:
                _LOGGER.debug(f"Exporting {abs(grid_power):.0f}W but price is OK ({export_earnings:.2f}c/kWh) - not curtailing")
        else:
            _LOGGER.debug(f"Not exporting (grid={grid_power}W) - no need to curtail for negative export")

        # Check 3: Battery full (100%) AND export is unprofitable (< 1c/kWh)
        if battery_soc is not None and battery_soc >= 100:
            if export_earnings is not None and export_earnings < 1:
                _LOGGER.info(f"🔌 AC-COUPLED: Battery full ({battery_soc:.0f}%) AND export unprofitable ({export_earnings:.2f}c/kWh) - should curtail")
                return True
            else:
                _LOGGER.debug(f"Battery full ({battery_soc:.0f}%) but export still profitable ({export_earnings:.2f}c/kWh) - not curtailing")
                return False

        # Check 4: Solar producing but battery NOT absorbing AND exporting at negative price
        # (battery_is_charging already computed above)
        if solar_power > 100 and not battery_is_charging and is_exporting:
            if export_earnings is not None and export_earnings < 0:
                _LOGGER.info(
                    f"🔌 AC-COUPLED: Solar producing {solar_power:.0f}W but battery NOT charging "
                    f"(battery_power={battery_power:.0f}W), exporting {abs(grid_power):.0f}W at negative price "
                    f"({export_earnings:.2f}c/kWh) - should curtail"
                )
                return True

        # Default: don't curtail - solar is being used productively
        _LOGGER.debug(f"No curtailment conditions met - allowing solar production")
        return False

    # Smart DC curtailment check (for Tesla export='never')
    async def should_curtail_dc(export_earnings: float | None) -> bool:
        """Smart curtailment logic for DC-coupled solar (Tesla Powerwall export rule).

        For DC-coupled systems, we only curtail (set export='never') when:
        1. Battery is full (100%), OR
        2. Battery is NOT charging (not absorbing solar)

        If the battery is actively charging and not full, solar is being used
        productively so we don't need to block export.

        Args:
            export_earnings: Current export earnings in c/kWh

        Returns:
            True if we should curtail, False if we should allow (battery absorbing solar)
        """
        # Get live status for battery_soc and battery_power
        live_status = await get_live_status()

        if live_status is None:
            _LOGGER.debug("Could not get live status for DC curtailment check - applying curtailment (conservative)")
            return True

        battery_soc = live_status.get("battery_soc")
        battery_power = live_status.get("battery_power", 0) or 0  # Negative = charging
        grid_power = live_status.get("grid_power", 0) or 0  # Negative = exporting

        _LOGGER.debug(
            f"DC curtailment check: SOC={battery_soc}%, battery={battery_power:.0f}W (neg=charging), "
            f"grid={grid_power:.0f}W (neg=export), export_earnings={export_earnings}c/kWh"
        )

        # Compute state flags
        battery_is_charging = battery_power < -50  # At least 50W charging
        is_exporting = grid_power < -100  # Exporting more than 100W

        # PRIORITY CHECK: If battery is charging AND not exporting, don't curtail
        # Solar going to battery is always good - takes precedence over everything
        if battery_is_charging and not is_exporting:
            _LOGGER.info(
                f"⚡ DC-COUPLED: Battery charging ({abs(battery_power):.0f}W) at SOC {battery_soc:.0f}%, "
                f"not exporting (grid={grid_power:.0f}W) - skipping curtailment (solar being absorbed)"
            )
            return False

        # Check 1: If battery is full (100%) AND exporting, curtail
        if battery_soc is not None and battery_soc >= 100:
            if is_exporting:
                _LOGGER.info(f"🔋 DC-COUPLED: Battery full ({battery_soc:.0f}%) AND exporting - should curtail")
                return True
            else:
                _LOGGER.debug(f"Battery full ({battery_soc:.0f}%) but not exporting - not curtailing")
                return False

        # Check 2: If not exporting, no need to curtail
        if not is_exporting:
            _LOGGER.info(
                f"⚡ DC-COUPLED: Not exporting (grid={grid_power:.0f}W) - skipping curtailment"
            )
            return False

        # Battery not charging and exporting - should curtail
        _LOGGER.info(
            f"🔋 DC-COUPLED: Battery not charging ({battery_power:.0f}W), exporting ({abs(grid_power):.0f}W) "
            f"at {export_earnings:.2f}c/kWh - should curtail"
        )
        return True

    # Helper function for AC-coupled inverter curtailment
    async def apply_inverter_curtailment(curtail: bool, import_price: float | None = None, export_earnings: float | None = None) -> bool:
        """Apply or remove inverter curtailment for AC-coupled solar systems.

        Args:
            curtail: True to curtail (shutdown inverter), False to restore normal operation

        Returns:
            True if operation succeeded, False otherwise
        """
        inverter_enabled = entry.options.get(
            CONF_AC_INVERTER_CURTAILMENT_ENABLED,
            entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        )

        if not inverter_enabled:
            _LOGGER.debug("AC-coupled inverter curtailment not enabled in config - skipping")
            return True  # Not enabled, nothing to do

        inverter_brand = entry.options.get(
            CONF_INVERTER_BRAND,
            entry.data.get(CONF_INVERTER_BRAND, "sungrow")
        )
        inverter_host = entry.options.get(
            CONF_INVERTER_HOST,
            entry.data.get(CONF_INVERTER_HOST, "")
        )
        inverter_port = entry.options.get(
            CONF_INVERTER_PORT,
            entry.data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
        )
        inverter_slave_id = entry.options.get(
            CONF_INVERTER_SLAVE_ID,
            entry.data.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)
        )
        inverter_model = entry.options.get(
            CONF_INVERTER_MODEL,
            entry.data.get(CONF_INVERTER_MODEL)
        )
        inverter_token = entry.options.get(
            CONF_INVERTER_TOKEN,
            entry.data.get(CONF_INVERTER_TOKEN)
        )
        fronius_load_following = entry.options.get(
            CONF_FRONIUS_LOAD_FOLLOWING,
            entry.data.get(CONF_FRONIUS_LOAD_FOLLOWING, False)
        )

        # Enphase Enlighten credentials for automatic JWT token refresh
        enphase_username = entry.options.get(
            CONF_ENPHASE_USERNAME,
            entry.data.get(CONF_ENPHASE_USERNAME)
        )
        enphase_password = entry.options.get(
            CONF_ENPHASE_PASSWORD,
            entry.data.get(CONF_ENPHASE_PASSWORD)
        )
        enphase_serial = entry.options.get(
            CONF_ENPHASE_SERIAL,
            entry.data.get(CONF_ENPHASE_SERIAL)
        )
        enphase_normal_profile = entry.options.get(
            CONF_ENPHASE_NORMAL_PROFILE,
            entry.data.get(CONF_ENPHASE_NORMAL_PROFILE)
        )
        enphase_zero_export_profile = entry.options.get(
            CONF_ENPHASE_ZERO_EXPORT_PROFILE,
            entry.data.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE)
        )
        enphase_is_installer = entry.options.get(
            CONF_ENPHASE_IS_INSTALLER,
            entry.data.get(CONF_ENPHASE_IS_INSTALLER, False)
        )

        if not inverter_host:
            _LOGGER.warning("Inverter curtailment enabled but no host configured")
            return False

        try:
            controller = get_inverter_controller(
                brand=inverter_brand,
                host=inverter_host,
                port=inverter_port,
                slave_id=inverter_slave_id,
                model=inverter_model,
                token=inverter_token,
                load_following=fronius_load_following,
                enphase_username=enphase_username,
                enphase_password=enphase_password,
                enphase_serial=enphase_serial,
                enphase_normal_profile=enphase_normal_profile,
                enphase_zero_export_profile=enphase_zero_export_profile,
                enphase_is_installer=enphase_is_installer,
            )

            if not controller:
                _LOGGER.error(f"Unsupported inverter brand: {inverter_brand}")
                return False

            # Cache controller for fast load-following updates
            hass.data[DOMAIN][entry.entry_id]["inverter_controller"] = controller

            if curtail:
                # Use smart AC-coupled curtailment logic
                # Only curtail if: import price < 0 OR (battery = 100% AND export < 1c)
                should_curtail = await should_curtail_ac_coupled(import_price, export_earnings)

                if not should_curtail:
                    # Smart logic says don't curtail - battery can still absorb solar
                    # Check if inverter is currently curtailed and needs restoring
                    inverter_last_state = hass.data[DOMAIN][entry.entry_id].get("inverter_last_state")
                    if inverter_last_state == "curtailed":
                        _LOGGER.info(f"⚡ AC-COUPLED: Battery absorbing solar - RESTORING previously curtailed inverter")
                        success = await controller.restore()
                        if success:
                            _LOGGER.info(f"✅ Inverter restored (battery can absorb solar)")
                            hass.data[DOMAIN][entry.entry_id]["inverter_last_state"] = "running"
                            hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = None
                        else:
                            _LOGGER.error(f"❌ Failed to restore inverter")
                        return success
                    else:
                        _LOGGER.info(f"⚡ AC-COUPLED: Skipping inverter curtailment (battery can absorb solar)")
                        return True  # Success - intentionally not curtailing

                # For Zeversolar, Sigenergy, Sungrow, and Enphase, use load-following curtailment
                # Limit = home load + battery charge rate (so we don't export but still charge battery)
                home_load_w = None
                if inverter_brand in ("zeversolar", "sigenergy", "sungrow", "enphase"):
                    live_status = await get_live_status()
                    if live_status and live_status.get("load_power"):
                        home_load_w = int(live_status.get("load_power", 0))
                        # Add battery charge rate if battery is charging
                        # battery_power < 0 means charging (negative = consuming power from solar)
                        # battery_power > 0 means discharging (positive = providing power)
                        battery_power = live_status.get("battery_power", 0) or 0
                        # Negate to get positive charge rate (e.g., -2580W charging → 2580W)
                        battery_charge_w = max(0, -int(battery_power))
                        if battery_charge_w > 50:  # At least 50W charging
                            total_load_w = home_load_w + battery_charge_w
                            _LOGGER.info(f"🔌 LOAD-FOLLOWING: Home={home_load_w}W + Battery charging={battery_charge_w}W = {total_load_w}W")
                            home_load_w = total_load_w
                        else:
                            _LOGGER.info(f"🔌 LOAD-FOLLOWING: Home load is {home_load_w}W (battery not charging or <50W)")

                _LOGGER.info(f"🔴 Curtailing inverter at {inverter_host}")

                # Pass home_load_w for load-following (Zeversolar)
                if home_load_w is not None and hasattr(controller, 'curtail'):
                    # Check if curtail accepts home_load_w parameter
                    import inspect
                    sig = inspect.signature(controller.curtail)
                    if 'home_load_w' in sig.parameters:
                        success = await controller.curtail(home_load_w=home_load_w)
                    else:
                        success = await controller.curtail()
                else:
                    success = await controller.curtail()

                if success:
                    if home_load_w is not None:
                        _LOGGER.info(f"✅ Inverter load-following curtailment to {home_load_w}W")
                    else:
                        _LOGGER.info(f"✅ Inverter curtailed successfully")
                    # Store last state
                    hass.data[DOMAIN][entry.entry_id]["inverter_last_state"] = "curtailed"
                    hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = home_load_w
                    # Track DPEL update time for Enphase refresh logic
                    from datetime import datetime
                    hass.data[DOMAIN][entry.entry_id]["last_dpel_update_time"] = datetime.now()
                else:
                    _LOGGER.error(f"❌ Failed to curtail inverter")
                return success
            else:
                _LOGGER.info(f"🟢 Restoring inverter at {inverter_host}")
                success = await controller.restore()
                if success:
                    _LOGGER.info(f"✅ Inverter restored successfully")
                    # Store last state
                    hass.data[DOMAIN][entry.entry_id]["inverter_last_state"] = "running"
                    hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = None  # Clear power limit
                else:
                    _LOGGER.error(f"❌ Failed to restore inverter")
                return success

        except Exception as e:
            _LOGGER.error(f"Error controlling inverter: {e}", exc_info=True)
            return False

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

    async def _get_nem_region_from_amber() -> Optional[str]:
        """Auto-detect NEM region from Amber site's network field.

        Fetches Amber site info and maps the electricity network to NEM region.
        Caches the result in hass.data to avoid repeated API calls.
        """
        # Check cache first
        cached_region = hass.data[DOMAIN][entry.entry_id].get("amber_nem_region")
        if cached_region:
            return cached_region

        # Network to NEM region mapping
        NETWORK_TO_NEM_REGION = {
            # NSW networks
            "Ausgrid": "NSW1",
            "Endeavour Energy": "NSW1",
            "Essential Energy": "NSW1",
            # ACT network (part of NSW1 NEM region)
            "Evoenergy": "NSW1",
            # VIC networks
            "AusNet Services": "VIC1",
            "CitiPower": "VIC1",
            "Jemena": "VIC1",
            "Powercor": "VIC1",
            "United Energy": "VIC1",
            # QLD networks
            "Energex": "QLD1",
            "Ergon Energy": "QLD1",
            # SA networks
            "SA Power Networks": "SA1",
            # TAS networks
            "TasNetworks": "TAS1",
        }

        try:
            amber_token = entry.data.get(CONF_AMBER_API_TOKEN)
            amber_site_id = entry.data.get(CONF_AMBER_SITE_ID)

            if not amber_token:
                _LOGGER.debug("No Amber API token for NEM region auto-detection")
                return None

            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            session = async_get_clientsession(hass)
            headers = {"Authorization": f"Bearer {amber_token}"}

            # If no site ID stored, fetch all sites and use first active one
            if not amber_site_id:
                _LOGGER.debug("No Amber site ID in config, fetching sites list...")
                try:
                    async with session.get(
                        f"{AMBER_API_BASE_URL}/sites",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as sites_response:
                        if sites_response.status == 200:
                            sites = await sites_response.json()
                            # Prefer active site
                            active_sites = [s for s in sites if s.get("status") == "active"]
                            if active_sites:
                                amber_site_id = active_sites[0]["id"]
                            elif sites:
                                amber_site_id = sites[0]["id"]
                            if amber_site_id:
                                _LOGGER.info(f"Auto-selected Amber site: {amber_site_id}")
                        else:
                            _LOGGER.debug(f"Failed to fetch Amber sites: HTTP {sites_response.status}")
                except Exception as e:
                    _LOGGER.debug(f"Error fetching Amber sites: {e}")

            if not amber_site_id:
                _LOGGER.debug("Could not determine Amber site ID for NEM region auto-detection")
                return None

            # Fetch Amber site info
            async with session.get(
                f"{AMBER_API_BASE_URL}/sites/{amber_site_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    site_info = await response.json()
                    network = site_info.get("network")

                    if network:
                        nem_region = NETWORK_TO_NEM_REGION.get(network)
                        if nem_region:
                            _LOGGER.info(f"Auto-detected NEM region: {nem_region} (network: {network})")
                            # Cache the result
                            hass.data[DOMAIN][entry.entry_id]["amber_nem_region"] = nem_region
                            return nem_region
                        else:
                            _LOGGER.warning(f"Unknown network '{network}' - cannot determine NEM region")
                    else:
                        _LOGGER.debug("Amber site info doesn't include network field")
                else:
                    _LOGGER.debug(f"Failed to fetch Amber site info: HTTP {response.status}")

        except Exception as e:
            _LOGGER.debug(f"Error auto-detecting NEM region: {e}")

        return None

    async def _sync_tariff_to_sigenergy(forecast_data: list, sync_mode: str, current_actual_interval: dict = None) -> None:
        """Sync Amber prices to Sigenergy Cloud API.

        Converts Amber forecast data to Sigenergy's expected format and uploads
        buy/sell prices via the Sigenergy Cloud API.

        Supports the same price modification features as Tesla:
        - Export Boost: Artificially increase sell prices to encourage battery discharge
        - Chip Mode: Suppress exports unless price exceeds threshold
        - Spike Protection: Cap buy prices during extreme spikes
        """
        try:
            from .sigenergy_api import (
                SigenergyAPIClient,
                convert_amber_prices_to_sigenergy,
                apply_export_boost_sigenergy,
                apply_chip_mode_sigenergy,
                apply_spike_protection_sigenergy,
            )
        except ImportError as e:
            _LOGGER.error(f"Failed to import sigenergy_api: {e}")
            return

        try:
            # Get Sigenergy credentials from config entry
            station_id = entry.data.get(CONF_SIGENERGY_STATION_ID)
            username = entry.data.get(CONF_SIGENERGY_USERNAME)
            pass_enc = entry.data.get(CONF_SIGENERGY_PASS_ENC)
            device_id = entry.data.get(CONF_SIGENERGY_DEVICE_ID)

            if not all([station_id, username, pass_enc, device_id]):
                _LOGGER.error("Missing Sigenergy Cloud credentials for tariff sync")
                return

            if not forecast_data:
                _LOGGER.warning("No forecast data available for Sigenergy tariff sync")
                return

            # Get forecast type from options
            forecast_type = entry.options.get(
                CONF_AMBER_FORECAST_TYPE, entry.data.get(CONF_AMBER_FORECAST_TYPE, "predicted")
            )

            # Get NEM region for timezone selection (SA1 = Adelaide, QLD1 = Brisbane, etc.)
            # Priority: 1) Explicit AEMO region setting, 2) Auto-detect from Amber site network
            nem_region = entry.options.get(
                CONF_AEMO_REGION, entry.data.get(CONF_AEMO_REGION)
            )

            # Auto-detect NEM region from Amber site info if not explicitly configured
            if not nem_region:
                nem_region = await _get_nem_region_from_amber()

            # Convert Amber forecast to Sigenergy format
            general_prices = [p for p in forecast_data if p.get("channelType") == "general"]
            feedin_prices = [p for p in forecast_data if p.get("channelType") == "feedIn"]

            # Debug: Log sample data structure for each channel to diagnose price extraction
            if general_prices:
                sample_general = general_prices[0]
                _LOGGER.debug(f"Sample general interval: type={sample_general.get('type')}, "
                             f"perKwh={sample_general.get('perKwh')}, "
                             f"advancedPrice={sample_general.get('advancedPrice')}")
            if feedin_prices:
                sample_feedin = feedin_prices[0]
                _LOGGER.debug(f"Sample feedIn interval: type={sample_feedin.get('type')}, "
                             f"perKwh={sample_feedin.get('perKwh')}, "
                             f"advancedPrice={sample_feedin.get('advancedPrice')}")

            buy_prices = convert_amber_prices_to_sigenergy(
                general_prices, price_type="buy", forecast_type=forecast_type,
                current_actual_interval=current_actual_interval, nem_region=nem_region
            )
            sell_prices = convert_amber_prices_to_sigenergy(
                feedin_prices, price_type="sell", forecast_type=forecast_type,
                current_actual_interval=current_actual_interval, nem_region=nem_region
            )

            if not buy_prices:
                _LOGGER.warning("No buy prices converted for Sigenergy sync")
                return

            # Apply price modifications (same features as Tesla)
            # 1. Spike Protection - cap buy prices during extreme spikes
            spike_protection_enabled = entry.options.get(
                CONF_SPIKE_PROTECTION_ENABLED,
                entry.data.get(CONF_SPIKE_PROTECTION_ENABLED, False)
            )
            if spike_protection_enabled:
                # Use 100c/kWh as threshold (same as Tesla's $1/kWh spike threshold)
                # Replace with 50c/kWh (moderate price to discourage but not completely block)
                buy_prices = apply_spike_protection_sigenergy(
                    buy_prices,
                    threshold_cents=100.0,
                    replacement_cents=50.0,
                )

            # 2. Export Boost - artificially increase sell prices to encourage discharge
            export_boost_enabled = entry.options.get(CONF_EXPORT_BOOST_ENABLED, False)
            if export_boost_enabled and sell_prices:
                offset = entry.options.get(CONF_EXPORT_PRICE_OFFSET, 0) or 0
                min_price = entry.options.get(CONF_EXPORT_MIN_PRICE, 0) or 0
                boost_start = entry.options.get(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START)
                boost_end = entry.options.get(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END)
                threshold = entry.options.get(CONF_EXPORT_BOOST_THRESHOLD, DEFAULT_EXPORT_BOOST_THRESHOLD)
                _LOGGER.info(
                    "Applying Sigenergy export boost: offset=%.1fc, min=%.1fc, threshold=%.1fc, window=%s-%s",
                    offset, min_price, threshold, boost_start, boost_end
                )
                sell_prices = apply_export_boost_sigenergy(
                    sell_prices, offset, min_price, boost_start, boost_end, threshold
                )

            # 3. Chip Mode - suppress exports unless price exceeds threshold
            chip_mode_enabled = entry.options.get(CONF_CHIP_MODE_ENABLED, False)
            if chip_mode_enabled and sell_prices:
                chip_start = entry.options.get(CONF_CHIP_MODE_START, DEFAULT_CHIP_MODE_START)
                chip_end = entry.options.get(CONF_CHIP_MODE_END, DEFAULT_CHIP_MODE_END)
                chip_threshold = entry.options.get(CONF_CHIP_MODE_THRESHOLD, DEFAULT_CHIP_MODE_THRESHOLD)
                _LOGGER.info(
                    "Applying Sigenergy Chip Mode: window=%s-%s, threshold=%.1fc",
                    chip_start, chip_end, chip_threshold
                )
                sell_prices = apply_chip_mode_sigenergy(
                    sell_prices, chip_start, chip_end, chip_threshold
                )

            # Debug: Log price ranges to diagnose buy/sell mismatch
            buy_values = [p["price"] for p in buy_prices]
            sell_values = [p["price"] for p in sell_prices] if sell_prices else []
            _LOGGER.debug(f"Buy prices range (after modifications): {min(buy_values):.1f} to {max(buy_values):.1f} c/kWh")
            if sell_values:
                _LOGGER.debug(f"Sell prices range (after modifications): {min(sell_values):.1f} to {max(sell_values):.1f} c/kWh")

            # Get stored tokens to avoid re-authentication
            stored_access_token = entry.data.get(CONF_SIGENERGY_ACCESS_TOKEN)
            stored_refresh_token = entry.data.get(CONF_SIGENERGY_REFRESH_TOKEN)
            stored_expires_at = entry.data.get(CONF_SIGENERGY_TOKEN_EXPIRES_AT)

            # Parse expires_at if stored as string
            token_expires_at = None
            if stored_expires_at:
                try:
                    if isinstance(stored_expires_at, str):
                        token_expires_at = datetime.fromisoformat(stored_expires_at)
                    else:
                        token_expires_at = stored_expires_at
                except (ValueError, TypeError):
                    _LOGGER.debug("Could not parse stored token expiration, will re-authenticate if needed")

            # Callback to persist refreshed tokens to config entry
            async def _persist_sigenergy_tokens(token_info: dict) -> None:
                """Persist refreshed Sigenergy tokens to config entry."""
                try:
                    new_data = {**entry.data}
                    new_data[CONF_SIGENERGY_ACCESS_TOKEN] = token_info.get("access_token")
                    new_data[CONF_SIGENERGY_REFRESH_TOKEN] = token_info.get("refresh_token")
                    new_data[CONF_SIGENERGY_TOKEN_EXPIRES_AT] = token_info.get("expires_at")
                    hass.config_entries.async_update_entry(entry, data=new_data)
                    _LOGGER.debug("Persisted refreshed Sigenergy tokens to config entry")
                except Exception as e:
                    _LOGGER.warning(f"Failed to persist Sigenergy tokens: {e}")

            # Create Sigenergy client with stored tokens and refresh callback
            client = SigenergyAPIClient(
                username=username,
                pass_enc=pass_enc,
                device_id=device_id,
                access_token=stored_access_token,
                refresh_token=stored_refresh_token,
                token_expires_at=token_expires_at,
                on_token_refresh=_persist_sigenergy_tokens,
            )

            result = await client.set_tariff_rate(
                station_id=station_id,
                buy_prices=buy_prices,
                sell_prices=sell_prices if sell_prices else buy_prices,
                plan_name="PowerSync Amber",
            )

            if result.get("success"):
                _LOGGER.info(f"✅ Sigenergy tariff synced successfully ({sync_mode})")
                # Store tariff data for mobile app API
                hass.data[DOMAIN][entry.entry_id]["sigenergy_tariff"] = {
                    "buy_prices": buy_prices,
                    "sell_prices": sell_prices if sell_prices else buy_prices,
                    "synced_at": datetime.now().isoformat(),
                    "sync_mode": sync_mode,
                }
            else:
                error = result.get("error", "Unknown error")
                _LOGGER.error(f"❌ Sigenergy tariff sync failed: {error}")

        except Exception as e:
            _LOGGER.error(f"❌ Error in Sigenergy tariff sync: {e}", exc_info=True)

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
        # Determine battery system type for routing
        battery_system = entry.data.get(CONF_BATTERY_SYSTEM, "tesla")

        # Skip TOU sync for Globird - they use AEMO spike detection only, not rate plan sync
        electricity_provider_check = entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
        )
        if electricity_provider_check == "globird":
            _LOGGER.debug("⏭️  TOU sync skipped - Globird uses AEMO spike detection only")
            return

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
        site_info = None
        if tesla_coordinator:
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

        # Route to appropriate battery system for tariff sync
        _LOGGER.info(f"🔀 Routing tariff sync: battery_system={battery_system}")
        if battery_system == "sigenergy":
            # Sigenergy-specific tariff sync via Cloud API
            # Pass current_actual_interval for live 5-min price injection
            _LOGGER.info("🔀 Using Sigenergy Cloud API for tariff sync")
            await _sync_tariff_to_sigenergy(forecast_data, sync_mode, current_actual_interval)
            return

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

            # Apply Chip Mode for Amber users (if enabled) - suppress exports unless above threshold
            chip_mode_enabled = entry.options.get(CONF_CHIP_MODE_ENABLED, False)
            if chip_mode_enabled:
                from .tariff_converter import apply_chip_mode
                chip_start = entry.options.get(CONF_CHIP_MODE_START, DEFAULT_CHIP_MODE_START)
                chip_end = entry.options.get(CONF_CHIP_MODE_END, DEFAULT_CHIP_MODE_END)
                chip_threshold = entry.options.get(CONF_CHIP_MODE_THRESHOLD, DEFAULT_CHIP_MODE_THRESHOLD)
                _LOGGER.info(
                    "Applying Chip Mode: window=%s-%s, threshold=%.1fc",
                    chip_start, chip_end, chip_threshold
                )
                tariff = apply_chip_mode(tariff, chip_start, chip_end, chip_threshold)

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
        async_dispatcher_send(hass, f"power_sync_tariff_updated_{entry.entry_id}")

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

            # Alpha: Force mode toggle for faster Powerwall response
            # Only toggle on settled prices, not forecast (reduces unnecessary toggles)
            force_mode_toggle = entry.options.get(
                CONF_FORCE_TARIFF_MODE_TOGGLE,
                entry.data.get(CONF_FORCE_TARIFF_MODE_TOGGLE, False)
            )
            if force_mode_toggle and sync_mode != 'initial_forecast':
                try:
                    site_id = entry.data[CONF_TESLA_ENERGY_SITE_ID]
                    api_base = TESLEMETRY_API_BASE_URL if current_provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL
                    headers = {"Authorization": f"Bearer {current_token}", "Content-Type": "application/json"}
                    session = async_get_clientsession(hass)

                    # First check current operation mode - respect user's manual self_consumption setting
                    current_mode = None
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            site_info = data.get("response", {})
                            current_mode = site_info.get("default_real_mode")
                            _LOGGER.debug(f"Current operation mode: {current_mode}")

                    # Determine if we should skip the toggle
                    skip_toggle = False
                    force_retoggle = False

                    if current_mode == 'self_consumption':
                        # Check if we recently toggled - if so, Tesla might have reverted the mode
                        # Only respect "user setting" if we haven't toggled in the last 10 minutes
                        from homeassistant.util import dt as dt_util
                        last_toggle_time = hass.data[DOMAIN].get(entry.entry_id, {}).get("last_force_toggle_time")
                        now = dt_util.utcnow()

                        if last_toggle_time and (now - last_toggle_time).total_seconds() < 600:
                            # We toggled recently - Tesla likely reverted the mode, not user
                            _LOGGER.info(f"⚠️ Mode reverted to self_consumption after recent toggle ({(now - last_toggle_time).total_seconds():.0f}s ago) - will re-toggle")
                            force_retoggle = True
                        else:
                            # User has manually set self_consumption mode - don't override their choice
                            _LOGGER.info(f"⏭️  Skipping force toggle - already in self_consumption mode (respecting user setting)")
                            skip_toggle = True
                    elif current_mode and current_mode != 'autonomous':
                        # Not in TOU mode (e.g., backup mode) - don't toggle
                        _LOGGER.info(f"⏭️  Skipping force toggle - not in TOU mode (current: {current_mode})")
                        skip_toggle = True

                    if not skip_toggle:
                        # In autonomous (TOU) mode or need to re-toggle - check if already optimizing before toggling
                        async with session.get(
                            f"{api_base}/api/1/energy_sites/{site_id}/live_status",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                site_status = data.get("response", {})
                                grid_power = site_status.get("grid_power", 0)
                                battery_power = site_status.get("battery_power", 0)
                            else:
                                grid_power = 0
                                battery_power = 0

                        if not force_retoggle and grid_power < 0:
                            # Negative grid_power means exporting - already doing what we want
                            _LOGGER.info(f"⏭️  Skipping force toggle - already exporting ({abs(grid_power):.0f}W to grid)")
                        elif not force_retoggle and battery_power < 0:
                            # Negative battery_power means charging - already doing what we want
                            _LOGGER.info(f"⏭️  Skipping force toggle - battery already charging ({abs(battery_power):.0f}W)")
                        else:
                            if force_retoggle:
                                _LOGGER.info(f"🔄 Re-toggling to autonomous (Tesla reverted mode)")
                            else:
                                _LOGGER.info(f"🔄 Force mode toggle - grid: {grid_power:.0f}W, battery: {battery_power:.0f}W")

                                # Switch to self_consumption first (only if not already in self_consumption)
                                async with session.post(
                                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                                    headers=headers,
                                    json={"default_real_mode": "self_consumption"},
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as response:
                                    if response.status == 200:
                                        _LOGGER.debug("Switched to self_consumption mode")

                                # Wait briefly
                                await asyncio.sleep(5)

                            # Switch back to autonomous (with retries and verification - critical to not stay in self_consumption)
                            switched_back = False
                            for attempt in range(3):
                                try:
                                    async with session.post(
                                        f"{api_base}/api/1/energy_sites/{site_id}/operation",
                                        headers=headers,
                                        json={"default_real_mode": "autonomous"},
                                        timeout=aiohttp.ClientTimeout(total=30),
                                    ) as response:
                                        if response.status != 200:
                                            resp_text = await response.text()
                                            _LOGGER.warning(f"Could not switch back to autonomous: {response.status} {resp_text} (attempt {attempt+1}/3)")
                                            if attempt < 2:
                                                await asyncio.sleep(3)
                                            continue

                                    # Verify the mode actually changed
                                    await asyncio.sleep(2)
                                    async with session.get(
                                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                                        headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=30),
                                    ) as verify_response:
                                        if verify_response.status == 200:
                                            verify_data = await verify_response.json()
                                            current_mode = verify_data.get("response", {}).get("default_real_mode")
                                            if current_mode == "autonomous":
                                                _LOGGER.info("🔄 Force mode toggle complete - verified autonomous")
                                                switched_back = True
                                                # Track successful toggle time to detect Tesla reversions
                                                from homeassistant.util import dt as dt_util
                                                if entry.entry_id not in hass.data[DOMAIN]:
                                                    hass.data[DOMAIN][entry.entry_id] = {}
                                                hass.data[DOMAIN][entry.entry_id]["last_force_toggle_time"] = dt_util.utcnow()
                                                break
                                            else:
                                                _LOGGER.warning(f"⚠️ Mode verification failed: expected 'autonomous', got '{current_mode}' (attempt {attempt+1}/3)")
                                        else:
                                            _LOGGER.warning(f"Could not verify mode (status {verify_response.status}) - assuming success")
                                            switched_back = True
                                            # Track toggle time even when assuming success
                                            from homeassistant.util import dt as dt_util
                                            if entry.entry_id not in hass.data[DOMAIN]:
                                                hass.data[DOMAIN][entry.entry_id] = {}
                                            hass.data[DOMAIN][entry.entry_id]["last_force_toggle_time"] = dt_util.utcnow()
                                            break

                                except Exception as e:
                                    _LOGGER.warning(f"Switch back to autonomous failed: {e} (attempt {attempt+1}/3)")

                                if attempt < 2:  # Don't sleep after last attempt
                                    await asyncio.sleep(3)

                            if not switched_back:
                                _LOGGER.error("❌ CRITICAL: Failed to switch back to autonomous after 3 attempts - system may be stuck in self_consumption mode!")
                                # Send push notification for critical failure
                                try:
                                    from .automations.actions import _send_expo_push
                                    await _send_expo_push(
                                        hass,
                                        "⚠️ PowerSync Alert",
                                        "Failed to restore normal operation after force charge/discharge. System may be stuck - please check manually."
                                    )
                                except Exception as notify_err:
                                    _LOGGER.warning(f"Could not send failure notification: {notify_err}")
                except Exception as e:
                    _LOGGER.warning(f"Force mode toggle failed: {e}")

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
        if tesla_coordinator:
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
            CONF_BATTERY_CURTAILMENT_ENABLED,
            entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
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
            import_price = None  # General/buy price
            for price_data in current_prices:
                if price_data.get("channelType") == "feedIn":
                    feedin_price = price_data.get("perKwh", 0)
                elif price_data.get("channelType") == "general":
                    import_price = price_data.get("perKwh", 0)

            if feedin_price is None:
                _LOGGER.warning("No feed-in price found in Amber data")
                return

            # Amber returns feed-in prices as NEGATIVE when you're paid to export
            # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
            # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
            # So we want to curtail when feedin_price > 0 (user would pay to export)
            export_earnings = -feedin_price  # Convert to positive = earnings per kWh
            _LOGGER.info(f"Current prices from Amber: import={import_price}c/kWh, export earnings={export_earnings:.2f}c/kWh")

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
                _LOGGER.info(f"🚫 CURTAILMENT CHECK: Export earnings {export_earnings:.2f}c/kWh (<1c)")

                # Always apply Tesla export='never' when export earnings are negative
                # This is a safety net - even if battery is absorbing now, it might stop
                # and we don't want excess solar going to grid at negative prices
                dc_should_curtail = await should_curtail_dc(export_earnings)

                if not dc_should_curtail:
                    # Battery is absorbing - log it but STILL apply export='never' as safety net
                    _LOGGER.info(f"⚡ DC-COUPLED: Battery absorbing solar, but applying export='never' as safety net")
                else:
                    _LOGGER.info(f"⚡ DC-COUPLED: Battery not absorbing, applying export='never'")

                _LOGGER.info(f"🚫 CURTAILMENT TRIGGERED: Applying DC curtailment (export='never')")

                # If already curtailed AND verified from API, no action needed
                # If using cache, always apply curtailment to be safe (cache may be stale)
                if current_export_rule == "never" and not using_cached_rule:
                    _LOGGER.info(f"✅ Already curtailed (export='never', verified from API) - no action needed")

                    # Still need to ensure AC-coupled inverter is curtailed (independent of Tesla state)
                    await apply_inverter_curtailment(curtail=True, import_price=import_price, export_earnings=export_earnings)

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

                        # Also curtail AC-coupled inverter if configured (uses smart logic)
                        await apply_inverter_curtailment(curtail=True, import_price=import_price, export_earnings=export_earnings)

                        _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c)")

                # If currently curtailed, restore to battery_ok (or manual override rule if set)
                if current_export_rule == "never":
                    # Check for manual override
                    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                    manual_override = entry_data.get("manual_export_override", False)
                    if manual_override:
                        restore_rule = entry_data.get("manual_export_rule") or "battery_ok"
                        _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT (manual override active): 'never' → '{restore_rule}'")
                    else:
                        restore_rule = "battery_ok"
                        _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → '{restore_rule}'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": restore_rule},
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

                        # Also restore AC-coupled inverter if configured
                        await apply_inverter_curtailment(curtail=False)

                        _LOGGER.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")

                    # Only restore AC-coupled inverter if it was previously curtailed
                    # This prevents spamming the inverter with restore commands at night when it's off
                    inverter_last_state = hass.data[DOMAIN][entry.entry_id].get("inverter_last_state")
                    if inverter_last_state == "curtailed":
                        _LOGGER.info(f"🔄 Inverter was curtailed - restoring to normal")
                        await apply_inverter_curtailment(curtail=False)

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
            CONF_BATTERY_CURTAILMENT_ENABLED,
            entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
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

            # Also extract import price for smart AC-coupled curtailment
            general_data = websocket_data.get('general', {}) if websocket_data else None
            import_price = general_data.get('perKwh') if general_data else None

            # Amber returns feed-in prices as NEGATIVE when you're paid to export
            # e.g., feedin_price = -10.44 means you get paid 10.44c/kWh (good!)
            # e.g., feedin_price = +5.00 means you pay 5c/kWh to export (bad!)
            # So we want to curtail when feedin_price > 0 (user would pay to export)
            export_earnings = -feedin_price  # Convert to positive = earnings per kWh
            _LOGGER.info(f"Current prices (WebSocket): import={import_price}c/kWh, export earnings={export_earnings:.2f}c/kWh")

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
                _LOGGER.info(f"🚫 CURTAILMENT CHECK: Export earnings {export_earnings:.2f}c/kWh (<1c)")

                # Always apply Tesla export='never' when export earnings are negative
                # This is a safety net - even if battery is absorbing now, it might stop
                # and we don't want excess solar going to grid at negative prices
                dc_should_curtail = await should_curtail_dc(export_earnings)

                if not dc_should_curtail:
                    # Battery is absorbing - log it but STILL apply export='never' as safety net
                    _LOGGER.info(f"⚡ DC-COUPLED: Battery absorbing solar, but applying export='never' as safety net")
                else:
                    _LOGGER.info(f"⚡ DC-COUPLED: Battery not absorbing, applying export='never'")

                _LOGGER.info(f"🚫 CURTAILMENT TRIGGERED: Applying DC curtailment (export='never')")

                # If already curtailed AND verified from API, no action needed
                # If using cache, always apply curtailment to be safe (cache may be stale)
                if current_export_rule == "never" and not using_cached_rule:
                    _LOGGER.info(f"✅ Already curtailed (export='never', verified from API) - no action needed")

                    # Still need to ensure AC-coupled inverter is curtailed (independent of Tesla state)
                    await apply_inverter_curtailment(curtail=True, import_price=import_price, export_earnings=export_earnings)

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

                        # Also curtail AC-coupled inverter if configured (uses smart logic)
                        await apply_inverter_curtailment(curtail=True, import_price=import_price, export_earnings=export_earnings)

                        _LOGGER.info(f"📊 Action summary: Curtailment active (earnings: {export_earnings:.2f}c/kWh, export: 'never')")

                    except Exception as err:
                        _LOGGER.error(f"Error applying curtailment: {err}")
                        return

            # NORMAL MODE: Export earnings >= 1c/kWh (worth exporting)
            else:
                _LOGGER.info(f"✅ NORMAL OPERATION: Export earnings {export_earnings:.2f}c/kWh (>=1c)")

                if current_export_rule == "never":
                    # Check for manual override
                    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                    manual_override = entry_data.get("manual_export_override", False)
                    if manual_override:
                        restore_rule = entry_data.get("manual_export_rule") or "battery_ok"
                        _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT (manual override active): 'never' → '{restore_rule}'")
                    else:
                        restore_rule = "battery_ok"
                        _LOGGER.info(f"🔄 RESTORING FROM CURTAILMENT: 'never' → '{restore_rule}'")
                    try:
                        async with session.post(
                            f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/grid_import_export",
                            headers=headers,
                            json={"customer_preferred_export_rule": restore_rule},
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

                        # Also restore AC-coupled inverter if configured
                        await apply_inverter_curtailment(curtail=False)

                        _LOGGER.info(f"📊 Action summary: Restored to normal (earnings: {export_earnings:.2f}c/kWh, export: 'battery_ok')")

                    except Exception as err:
                        _LOGGER.error(f"Error restoring from curtailment: {err}")
                        return
                else:
                    _LOGGER.debug(f"Already in normal mode (export='{current_export_rule}') - no action needed")

                    # Only restore AC-coupled inverter if it was previously curtailed
                    # This prevents spamming the inverter with restore commands at night when it's off
                    inverter_last_state = hass.data[DOMAIN][entry.entry_id].get("inverter_last_state")
                    if inverter_last_state == "curtailed":
                        _LOGGER.info(f"🔄 Inverter was curtailed - restoring to normal")
                        await apply_inverter_curtailment(curtail=False)

                    _LOGGER.info(f"📊 Action summary: No change needed (earnings: {export_earnings:.2f}c/kWh, export: '{current_export_rule}')")

        except Exception as e:
            _LOGGER.error(f"❌ Unexpected error in solar curtailment check: {e}", exc_info=True)

        _LOGGER.info("=== Solar curtailment check complete ===")

    hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU, handle_sync_tou)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, handle_sync_now)

    # ======================================================================
    # FORCE DISCHARGE AND RESTORE NORMAL SERVICES
    # ======================================================================

    # Get persisted force mode state (survives HA restarts)
    persisted_force_state = hass.data[DOMAIN][entry.entry_id].get("force_mode_state") or {}

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
        "saved_backup_reserve": None,
        "expires_at": None,
        "cancel_expiry_timer": None,
    }

    # Helper function to persist force mode state to storage
    async def persist_force_mode_state() -> None:
        """Persist current force charge/discharge state to storage."""
        store = hass.data[DOMAIN][entry.entry_id]["store"]
        stored_data = await store.async_load() or {}

        # Only save what's needed to restore after restart
        state_to_save = None
        if force_charge_state["active"]:
            state_to_save = {
                "mode": "charge",
                "expires_at": force_charge_state["expires_at"].isoformat() if force_charge_state["expires_at"] else None,
                "saved_tariff": force_charge_state["saved_tariff"],
                "saved_operation_mode": force_charge_state["saved_operation_mode"],
                "saved_backup_reserve": force_charge_state["saved_backup_reserve"],
            }
        elif force_discharge_state["active"]:
            state_to_save = {
                "mode": "discharge",
                "expires_at": force_discharge_state["expires_at"].isoformat() if force_discharge_state["expires_at"] else None,
                "saved_tariff": force_discharge_state["saved_tariff"],
                "saved_operation_mode": force_discharge_state["saved_operation_mode"],
            }

        stored_data["force_mode_state"] = state_to_save
        await store.async_save(stored_data)
        if state_to_save:
            _LOGGER.debug(f"Persisted force mode state: {state_to_save['mode']} expires {state_to_save['expires_at']}")
        else:
            _LOGGER.debug("Cleared persisted force mode state")

    # Restore force mode state from persistence (after HA restart)
    async def restore_force_mode_from_persistence():
        """Restore force charge/discharge state after HA restart."""
        from homeassistant.util import dt as dt_util

        if not persisted_force_state:
            return

        mode = persisted_force_state.get("mode")
        expires_at_str = persisted_force_state.get("expires_at")

        if not mode or not expires_at_str:
            _LOGGER.info("No valid persisted force mode state to restore")
            return

        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            # Ensure timezone-aware
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=dt_util.UTC)

            now = dt_util.utcnow()

            if now >= expires_at:
                # Force mode has expired - trigger restore
                _LOGGER.info(f"⏰ Persisted force {mode} has expired (was {expires_at_str}), auto-restoring")
                # Clear the persisted state first
                store = hass.data[DOMAIN][entry.entry_id]["store"]
                stored_data = await store.async_load() or {}
                stored_data["force_mode_state"] = None
                await store.async_save(stored_data)
                # Don't call restore_normal here - the Tesla should already be in that mode
                # Just ensure we sync TOU to get the correct pricing back
                _LOGGER.info("Triggering TOU sync to restore correct pricing after expired force mode")
            else:
                # Force mode is still active - restore state and re-setup timer
                remaining_seconds = (expires_at - now).total_seconds()
                remaining_minutes = remaining_seconds / 60
                _LOGGER.info(f"🔄 Restoring force {mode} from persistence ({remaining_minutes:.1f} min remaining)")

                if mode == "charge":
                    force_charge_state["active"] = True
                    force_charge_state["expires_at"] = expires_at
                    force_charge_state["saved_tariff"] = persisted_force_state.get("saved_tariff")
                    force_charge_state["saved_operation_mode"] = persisted_force_state.get("saved_operation_mode")
                    force_charge_state["saved_backup_reserve"] = persisted_force_state.get("saved_backup_reserve")

                    # Re-setup expiry timer
                    async def auto_restore_charge(_now):
                        if force_charge_state["active"]:
                            _LOGGER.info("⏰ Force charge expired (restored timer), auto-restoring")
                            await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                    force_charge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                        hass, auto_restore_charge, expires_at
                    )

                    # Dispatch event for UI
                    async_dispatcher_send(hass, f"{DOMAIN}_force_charge_state", {
                        "active": True,
                        "expires_at": expires_at.isoformat(),
                        "duration": int(remaining_minutes),
                    })
                    _LOGGER.info(f"✅ Force charge restored from persistence, expires in {remaining_minutes:.1f} min")

                elif mode == "discharge":
                    force_discharge_state["active"] = True
                    force_discharge_state["expires_at"] = expires_at
                    force_discharge_state["saved_tariff"] = persisted_force_state.get("saved_tariff")
                    force_discharge_state["saved_operation_mode"] = persisted_force_state.get("saved_operation_mode")

                    # Re-setup expiry timer
                    async def auto_restore_discharge(_now):
                        if force_discharge_state["active"]:
                            _LOGGER.info("⏰ Force discharge expired (restored timer), auto-restoring")
                            await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                    force_discharge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                        hass, auto_restore_discharge, expires_at
                    )

                    # Dispatch event for UI
                    async_dispatcher_send(hass, f"{DOMAIN}_force_discharge_state", {
                        "active": True,
                        "expires_at": expires_at.isoformat(),
                        "duration": int(remaining_minutes),
                    })
                    _LOGGER.info(f"✅ Force discharge restored from persistence, expires in {remaining_minutes:.1f} min")

        except Exception as e:
            _LOGGER.error(f"Error restoring force mode from persistence: {e}", exc_info=True)

    # Schedule the restoration to run after setup is complete
    if persisted_force_state:
        hass.async_create_task(restore_force_mode_from_persistence())

    async def handle_force_discharge(call: ServiceCall) -> None:
        """Force discharge mode - switches to autonomous with high export tariff."""
        from homeassistant.util import dt as dt_util

        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        _LOGGER.info(f"🔋 Force discharge service called (context: user_id={context.user_id}, parent_id={context.parent_id})")

        duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        # Convert to int if string (from HA service selector)
        try:
            duration = int(duration)
        except (ValueError, TypeError):
            duration = DEFAULT_DISCHARGE_DURATION
        if duration not in DISCHARGE_DURATIONS:
            duration = DEFAULT_DISCHARGE_DURATION

        _LOGGER.info(f"🔋 FORCE DISCHARGE: Activating for {duration} minutes")

        # Check if this is a Sigenergy system
        is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))
        if is_sigenergy:
            try:
                from .inverters.sigenergy import SigenergyController
                modbus_host = entry.options.get(
                    CONF_SIGENERGY_MODBUS_HOST,
                    entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
                )
                if not modbus_host:
                    _LOGGER.error("Force discharge: Sigenergy Modbus host not configured")
                    return

                modbus_port = entry.options.get(
                    CONF_SIGENERGY_MODBUS_PORT,
                    entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
                )
                modbus_slave_id = entry.options.get(
                    CONF_SIGENERGY_MODBUS_SLAVE_ID,
                    entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 1)
                )

                controller = SigenergyController(
                    host=modbus_host,
                    port=modbus_port,
                    slave_id=modbus_slave_id,
                )

                # Set high discharge rate and restore export limit
                discharge_result = await controller.set_discharge_rate_limit(10.0)
                export_result = await controller.restore_export_limit()
                await controller.disconnect()

                if discharge_result and export_result:
                    force_discharge_state["active"] = True
                    force_discharge_state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)
                    _LOGGER.info(f"✅ Sigenergy FORCE DISCHARGE ACTIVE for {duration} minutes")

                    # Dispatch event for switch entity
                    async_dispatcher_send(hass, f"{DOMAIN}_force_discharge_state", {
                        "active": True,
                        "expires_at": force_discharge_state["expires_at"].isoformat(),
                        "duration": duration,
                    })

                    # Schedule auto-restore
                    if force_discharge_state.get("cancel_expiry_timer"):
                        force_discharge_state["cancel_expiry_timer"]()

                    async def auto_restore_sigenergy(_now):
                        if force_discharge_state["active"]:
                            _LOGGER.info("⏰ Sigenergy force discharge expired, auto-restoring")
                            await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                    force_discharge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                        hass,
                        auto_restore_sigenergy,
                        force_discharge_state["expires_at"],
                    )
                    await persist_force_mode_state()
                else:
                    _LOGGER.error(f"Sigenergy force discharge failed: discharge={discharge_result}, export={export_result}")
                return
            except Exception as e:
                _LOGGER.error(f"Error in Sigenergy force discharge: {e}", exc_info=True)
                return

        try:
            # Get current token and provider using helper function
            current_token, provider = get_tesla_api_token(hass, entry)

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

                # Use async_track_point_in_utc_time for one-time expiry (not recurring daily)
                force_discharge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                    hass,
                    auto_restore,
                    force_discharge_state["expires_at"],
                )

                # Persist state to survive HA restarts
                await persist_force_mode_state()
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

        _LOGGER.info(f"Created discharge tariff: buy=${buy_rate}/kWh, sell=${sell_rate_discharge}/kWh for {discharge_periods} periods")

        return tariff

    async def handle_force_charge(call: ServiceCall) -> None:
        """Force charge mode - switches to autonomous with free import tariff."""
        from homeassistant.util import dt as dt_util

        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        _LOGGER.info(f"🔌 Force charge service called (context: user_id={context.user_id}, parent_id={context.parent_id})")

        duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        # Convert to int if string (from HA service selector)
        try:
            duration = int(duration)
        except (ValueError, TypeError):
            duration = DEFAULT_DISCHARGE_DURATION
        if duration not in DISCHARGE_DURATIONS:
            duration = DEFAULT_DISCHARGE_DURATION

        _LOGGER.info(f"🔌 FORCE CHARGE: Activating for {duration} minutes")

        # Check if this is a Sigenergy system
        is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))
        if is_sigenergy:
            try:
                from .inverters.sigenergy import SigenergyController
                modbus_host = entry.options.get(
                    CONF_SIGENERGY_MODBUS_HOST,
                    entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
                )
                if not modbus_host:
                    _LOGGER.error("Force charge: Sigenergy Modbus host not configured")
                    return

                modbus_port = entry.options.get(
                    CONF_SIGENERGY_MODBUS_PORT,
                    entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
                )
                modbus_slave_id = entry.options.get(
                    CONF_SIGENERGY_MODBUS_SLAVE_ID,
                    entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 1)
                )

                controller = SigenergyController(
                    host=modbus_host,
                    port=modbus_port,
                    slave_id=modbus_slave_id,
                )

                # Cancel active discharge mode if switching to charge
                if force_discharge_state["active"]:
                    _LOGGER.info("Canceling active discharge mode to enable charge mode")
                    if force_discharge_state.get("cancel_expiry_timer"):
                        force_discharge_state["cancel_expiry_timer"]()
                        force_discharge_state["cancel_expiry_timer"] = None
                    force_discharge_state["active"] = False
                    force_discharge_state["expires_at"] = None

                # Set high charge rate and prevent discharge
                charge_result = await controller.set_charge_rate_limit(10.0)
                discharge_result = await controller.set_discharge_rate_limit(0)
                await controller.disconnect()

                if charge_result and discharge_result:
                    force_charge_state["active"] = True
                    force_charge_state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)
                    _LOGGER.info(f"✅ Sigenergy FORCE CHARGE ACTIVE for {duration} minutes")

                    # Dispatch event for UI
                    async_dispatcher_send(hass, f"{DOMAIN}_force_charge_state", {
                        "active": True,
                        "expires_at": force_charge_state["expires_at"].isoformat(),
                        "duration": duration,
                    })

                    # Schedule auto-restore
                    if force_charge_state.get("cancel_expiry_timer"):
                        force_charge_state["cancel_expiry_timer"]()

                    async def auto_restore_charge_sigenergy(_now):
                        if force_charge_state["active"]:
                            _LOGGER.info("⏰ Sigenergy force charge expired, auto-restoring")
                            await handle_restore_normal(ServiceCall(DOMAIN, SERVICE_RESTORE_NORMAL, {}))

                    force_charge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                        hass,
                        auto_restore_charge_sigenergy,
                        force_charge_state["expires_at"],
                    )
                    await persist_force_mode_state()
                else:
                    _LOGGER.error(f"Sigenergy force charge failed: charge={charge_result}, discharge={discharge_result}")
                return
            except Exception as e:
                _LOGGER.error(f"Error in Sigenergy force charge: {e}", exc_info=True)
                return

        try:
            # Get current token and provider using helper function
            current_token, provider = get_tesla_api_token(hass, entry)

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

                # Step 2: Get and save current operation mode and backup reserve
                async with session.get(
                    f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        site_info = data.get("response", {})
                        force_charge_state["saved_operation_mode"] = site_info.get("default_real_mode")
                        force_charge_state["saved_backup_reserve"] = site_info.get("backup_reserve_percent")
                        _LOGGER.info("Saved operation mode: %s, backup reserve: %s%%",
                                     force_charge_state["saved_operation_mode"],
                                     force_charge_state["saved_backup_reserve"])
                        if force_charge_state["saved_backup_reserve"] is None:
                            _LOGGER.warning("backup_reserve_percent not in site_info response - will use default on restore")
                    else:
                        text = await response.text()
                        _LOGGER.error(f"Failed to get site_info for saving: {response.status} - {text}")

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

            # Step 3b: Set backup reserve to 100% to force charging
            _LOGGER.info("Setting backup reserve to 100%% to force charging...")
            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/backup",
                headers=headers,
                json={"backup_reserve_percent": 100},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info("Set backup reserve to 100%%")
                else:
                    _LOGGER.warning("Could not set backup reserve: %s", response.status)

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

                # Use async_track_point_in_utc_time for one-time expiry (not recurring daily)
                force_charge_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                    hass,
                    auto_restore_charge,
                    force_charge_state["expires_at"],
                )

                # Persist state to survive HA restarts
                await persist_force_mode_state()
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

        _LOGGER.info(f"Created charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {charge_periods} periods")

        return tariff

    async def handle_restore_normal(call: ServiceCall) -> None:
        """Restore normal operation - restore saved tariff or trigger Amber sync."""
        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        _LOGGER.info(f"🔄 Restore normal service called (context: user_id={context.user_id}, parent_id={context.parent_id})")
        _LOGGER.info("🔄 RESTORE NORMAL: Restoring normal operation")

        # Cancel any pending expiry timers (discharge and charge)
        if force_discharge_state.get("cancel_expiry_timer"):
            force_discharge_state["cancel_expiry_timer"]()
            force_discharge_state["cancel_expiry_timer"] = None
        if force_charge_state.get("cancel_expiry_timer"):
            force_charge_state["cancel_expiry_timer"]()
            force_charge_state["cancel_expiry_timer"] = None

        # Check if this is a Sigenergy system
        is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))
        if is_sigenergy:
            try:
                from .inverters.sigenergy import SigenergyController
                modbus_host = entry.options.get(
                    CONF_SIGENERGY_MODBUS_HOST,
                    entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
                )
                if not modbus_host:
                    _LOGGER.warning("Restore normal: Sigenergy Modbus host not configured")
                else:
                    modbus_port = entry.options.get(
                        CONF_SIGENERGY_MODBUS_PORT,
                        entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
                    )
                    modbus_slave_id = entry.options.get(
                        CONF_SIGENERGY_MODBUS_SLAVE_ID,
                        entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 1)
                    )

                    controller = SigenergyController(
                        host=modbus_host,
                        port=modbus_port,
                        slave_id=modbus_slave_id,
                    )

                    # Restore normal operation: allow both charge and discharge
                    charge_result = await controller.set_charge_rate_limit(10.0)
                    discharge_result = await controller.set_discharge_rate_limit(10.0)
                    await controller.disconnect()

                    if charge_result and discharge_result:
                        _LOGGER.info("✅ Sigenergy normal operation restored (charge/discharge enabled)")
                    else:
                        _LOGGER.warning(f"Sigenergy restore partial: charge={charge_result}, discharge={discharge_result}")

                # Clear state
                force_discharge_state["active"] = False
                force_discharge_state["saved_tariff"] = None
                force_discharge_state["saved_operation_mode"] = None
                force_discharge_state["expires_at"] = None
                force_charge_state["active"] = False
                force_charge_state["saved_tariff"] = None
                force_charge_state["saved_operation_mode"] = None
                force_charge_state["saved_backup_reserve"] = None
                force_charge_state["expires_at"] = None

                _LOGGER.info("✅ SIGENERGY NORMAL OPERATION RESTORED")

                # Send push notification for successful restore
                try:
                    from .automations.actions import _send_expo_push
                    await _send_expo_push(
                        hass,
                        "✅ PowerSync",
                        "Normal operation restored successfully."
                    )
                except Exception as notify_err:
                    _LOGGER.debug(f"Could not send success notification: {notify_err}")

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

                await persist_force_mode_state()
                return
            except Exception as e:
                _LOGGER.error(f"Error in Sigenergy restore normal: {e}", exc_info=True)
                return

        try:
            # Get current token and provider using helper function
            current_token, provider = get_tesla_api_token(hass, entry)

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

            # IMMEDIATELY switch to self_consumption to stop any ongoing export/import
            # This ensures discharge stops right away, before tariff restoration completes
            if force_discharge_state.get("active") or force_charge_state.get("active"):
                _LOGGER.info("Immediately switching to self_consumption to stop forced charge/discharge")
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "self_consumption"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info("Switched to self_consumption mode - export/import stopped")
                    else:
                        _LOGGER.warning(f"Could not switch to self_consumption: {response.status}")

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
                    # Send push notification for restore failure
                    try:
                        from .automations.actions import _send_expo_push
                        await _send_expo_push(
                            hass,
                            "⚠️ PowerSync Alert",
                            f"Failed to restore operation mode (error {response.status}). Please check your battery settings."
                        )
                    except Exception as notify_err:
                        _LOGGER.debug(f"Could not send notification: {notify_err}")

            # Restore backup reserve if it was saved during force charge
            # If saved value is None, try to get current value from site_info or use default
            saved_backup_reserve = force_charge_state.get("saved_backup_reserve")

            if saved_backup_reserve is None:
                # Try to get current backup reserve from API to check if it's at 100%
                _LOGGER.warning("No saved backup reserve found - checking current value")
                try:
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            current_reserve = data.get("response", {}).get("backup_reserve_percent")
                            _LOGGER.info(f"Current backup reserve is {current_reserve}%")
                            # If it's at 100% (force charge set it), restore to default 20%
                            if current_reserve == 100:
                                saved_backup_reserve = 20
                                _LOGGER.info("Backup reserve at 100% from force charge - will restore to default 20%")
                except Exception as e:
                    _LOGGER.warning(f"Could not check current backup reserve: {e}")

            if saved_backup_reserve is not None:
                _LOGGER.info(f"Restoring backup reserve to: {saved_backup_reserve}%")
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/backup",
                    headers=headers,
                    json={"backup_reserve_percent": saved_backup_reserve},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info(f"✅ Restored backup reserve to {saved_backup_reserve}%")
                    else:
                        text = await response.text()
                        _LOGGER.error(f"Failed to restore backup reserve: {response.status} - {text}")
                        # Send push notification for backup reserve restore failure
                        try:
                            from .automations.actions import _send_expo_push
                            await _send_expo_push(
                                hass,
                                "⚠️ PowerSync Alert",
                                f"Failed to restore backup reserve to {saved_backup_reserve}%. Please check your battery settings."
                            )
                        except Exception as notify_err:
                            _LOGGER.debug(f"Could not send notification: {notify_err}")
            else:
                _LOGGER.warning("Could not determine backup reserve to restore")

            # Clear discharge state
            force_discharge_state["active"] = False
            force_discharge_state["saved_tariff"] = None
            force_discharge_state["saved_operation_mode"] = None
            force_discharge_state["expires_at"] = None

            # Clear charge state
            force_charge_state["active"] = False
            force_charge_state["saved_tariff"] = None
            force_charge_state["saved_operation_mode"] = None
            force_charge_state["saved_backup_reserve"] = None
            force_charge_state["expires_at"] = None

            _LOGGER.info("✅ NORMAL OPERATION RESTORED")

            # Send push notification for successful restore
            try:
                from .automations.actions import _send_expo_push
                await _send_expo_push(
                    hass,
                    "✅ PowerSync",
                    "Normal operation restored successfully."
                )
            except Exception as notify_err:
                _LOGGER.debug(f"Could not send success notification: {notify_err}")

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

            # Clear persisted state (no longer needed after restore)
            await persist_force_mode_state()

        except Exception as e:
            _LOGGER.error(f"Error in restore normal: {e}", exc_info=True)

    # ======================================================================
    # POWERWALL SETTINGS SERVICES (for mobile app Controls)
    # ======================================================================

    async def handle_set_backup_reserve(call: ServiceCall) -> None:
        """Set the Powerwall backup reserve percentage."""
        percent = call.data.get("percent")
        if percent is None:
            _LOGGER.error("Missing 'percent' parameter for set_backup_reserve")
            return

        try:
            percent = int(percent)
            if percent < 0 or percent > 100:
                _LOGGER.error(f"Invalid backup reserve percent: {percent}. Must be 0-100.")
                return
        except (ValueError, TypeError):
            _LOGGER.error(f"Invalid backup reserve percent: {percent}")
            return

        _LOGGER.info(f"🔋 Setting backup reserve to {percent}%")

        try:
            current_token, provider = get_tesla_api_token(hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for set_backup_reserve")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/backup",
                headers=headers,
                json={"backup_reserve_percent": percent},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ Backup reserve set to {percent}%")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set backup reserve: {response.status} - {text}")

        except Exception as e:
            _LOGGER.error(f"Error setting backup reserve: {e}", exc_info=True)

    async def handle_set_operation_mode(call: ServiceCall) -> None:
        """Set the Powerwall operation mode."""
        mode = call.data.get("mode")
        if mode not in ("autonomous", "self_consumption"):
            _LOGGER.error(f"Invalid operation mode: {mode}. Must be 'autonomous' or 'self_consumption'.")
            return

        _LOGGER.info(f"⚙️ Setting operation mode to {mode}")

        try:
            current_token, provider = get_tesla_api_token(hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for set_operation_mode")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/operation",
                headers=headers,
                json={"default_real_mode": mode},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ Operation mode set to {mode}")
                    # When user explicitly sets self_consumption, clear the force toggle time
                    # This tells TOU sync that this is intentional, not a Tesla reversion
                    if mode == "self_consumption":
                        if entry.entry_id in hass.data[DOMAIN]:
                            hass.data[DOMAIN][entry.entry_id].pop("last_force_toggle_time", None)
                            _LOGGER.debug("Cleared last_force_toggle_time (user set self_consumption)")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set operation mode: {response.status} - {text}")

        except Exception as e:
            _LOGGER.error(f"Error setting operation mode: {e}", exc_info=True)

    async def handle_set_grid_export(call: ServiceCall) -> None:
        """Set the grid export rule."""
        rule = call.data.get("rule")
        if rule not in ("never", "pv_only", "battery_ok"):
            _LOGGER.error(f"Invalid grid export rule: {rule}. Must be 'never', 'pv_only', or 'battery_ok'.")
            return

        _LOGGER.info(f"📤 Setting grid export rule to {rule}")

        try:
            current_token, provider = get_tesla_api_token(hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for set_grid_export")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                headers=headers,
                json={"customer_preferred_export_rule": rule},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ Grid export rule set to {rule}")
                    # If solar curtailment is enabled, mark this as a manual override
                    solar_curtailment_enabled = entry.options.get(
                        CONF_BATTERY_CURTAILMENT_ENABLED,
                        entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
                    )
                    if solar_curtailment_enabled:
                        entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
                        entry_data["manual_export_override"] = True
                        entry_data["manual_export_rule"] = rule
                        _LOGGER.info(f"📌 Manual export override enabled: {rule}")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set grid export rule: {response.status} - {text}")

        except Exception as e:
            _LOGGER.error(f"Error setting grid export rule: {e}", exc_info=True)

    async def handle_set_grid_export_auto(call: ServiceCall) -> None:
        """Clear manual export override and return to automatic control."""
        _LOGGER.info("🔄 Clearing manual export override - returning to auto control")
        try:
            entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
            entry_data["manual_export_override"] = False
            entry_data["manual_export_rule"] = None
            _LOGGER.info("✅ Manual export override cleared")
        except Exception as e:
            _LOGGER.error(f"Error clearing manual export override: {e}", exc_info=True)

    async def handle_set_grid_charging(call: ServiceCall) -> None:
        """Enable or disable grid charging."""
        enabled = call.data.get("enabled")
        if enabled is None:
            _LOGGER.error("Missing 'enabled' parameter for set_grid_charging")
            return

        # Convert to bool (HA may pass True/False or "true"/"false")
        if isinstance(enabled, str):
            enabled = enabled.lower() == "true"
        enabled = bool(enabled)

        _LOGGER.info(f"🔌 Setting grid charging to {'enabled' if enabled else 'disabled'}")

        try:
            current_token, provider = get_tesla_api_token(hass, entry)
            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id or not current_token:
                _LOGGER.error("Missing Tesla site ID or token for set_grid_charging")
                return

            session = async_get_clientsession(hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Note: Tesla API uses inverted logic - disallow_charge_from_grid_with_solar_installed
            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                headers=headers,
                json={"disallow_charge_from_grid_with_solar_installed": not enabled},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ Grid charging {'enabled' if enabled else 'disabled'}")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set grid charging: {response.status} - {text}")

        except Exception as e:
            _LOGGER.error(f"Error setting grid charging: {e}", exc_info=True)

    # Register force discharge, force charge, and restore normal services
    hass.services.async_register(DOMAIN, SERVICE_FORCE_DISCHARGE, handle_force_discharge)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_CHARGE, handle_force_charge)
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_NORMAL, handle_restore_normal)

    # Register Powerwall settings services
    hass.services.async_register(DOMAIN, SERVICE_SET_BACKUP_RESERVE, handle_set_backup_reserve)
    hass.services.async_register(DOMAIN, SERVICE_SET_OPERATION_MODE, handle_set_operation_mode)
    hass.services.async_register(DOMAIN, SERVICE_SET_GRID_EXPORT, handle_set_grid_export)
    hass.services.async_register(DOMAIN, SERVICE_SET_GRID_CHARGING, handle_set_grid_charging)
    hass.services.async_register(DOMAIN, "set_grid_export_auto", handle_set_grid_export_auto)

    _LOGGER.info("🔋 Force charge/discharge, restore, and Powerwall settings services registered")

    # ======================================================================
    # AC INVERTER MANUAL CURTAIL/RESTORE SERVICES
    # ======================================================================

    async def handle_curtail_inverter(call: ServiceCall) -> None:
        """Manually curtail the AC-coupled inverter.

        Supports two modes via 'mode' parameter:
        - 'load_following' (default): Limit production to home load (Zeversolar/Sigenergy)
                                      or zero-export mode (other brands)
        - 'shutdown': Full shutdown/0% output (for inverters that support it)
        """
        mode = call.data.get("mode", "load_following")
        _LOGGER.info(f"🔴 Manual inverter curtailment requested (mode: {mode})")

        inverter_enabled = entry.options.get(
            CONF_AC_INVERTER_CURTAILMENT_ENABLED,
            entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        )

        if not inverter_enabled:
            _LOGGER.warning("Inverter curtailment not enabled in config")
            return

        inverter_brand = entry.options.get(
            CONF_INVERTER_BRAND,
            entry.data.get(CONF_INVERTER_BRAND, "sungrow")
        )
        inverter_host = entry.options.get(
            CONF_INVERTER_HOST,
            entry.data.get(CONF_INVERTER_HOST, "")
        )
        inverter_port = entry.options.get(
            CONF_INVERTER_PORT,
            entry.data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
        )
        inverter_slave_id = entry.options.get(
            CONF_INVERTER_SLAVE_ID,
            entry.data.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)
        )
        inverter_model = entry.options.get(
            CONF_INVERTER_MODEL,
            entry.data.get(CONF_INVERTER_MODEL)
        )
        inverter_token = entry.options.get(
            CONF_INVERTER_TOKEN,
            entry.data.get(CONF_INVERTER_TOKEN)
        )
        fronius_load_following = entry.options.get(
            CONF_FRONIUS_LOAD_FOLLOWING,
            entry.data.get(CONF_FRONIUS_LOAD_FOLLOWING, False)
        )
        # Enphase Enlighten credentials for automatic JWT token refresh
        enphase_username = entry.options.get(
            CONF_ENPHASE_USERNAME,
            entry.data.get(CONF_ENPHASE_USERNAME)
        )
        enphase_password = entry.options.get(
            CONF_ENPHASE_PASSWORD,
            entry.data.get(CONF_ENPHASE_PASSWORD)
        )
        enphase_serial = entry.options.get(
            CONF_ENPHASE_SERIAL,
            entry.data.get(CONF_ENPHASE_SERIAL)
        )
        enphase_normal_profile = entry.options.get(
            CONF_ENPHASE_NORMAL_PROFILE,
            entry.data.get(CONF_ENPHASE_NORMAL_PROFILE)
        )
        enphase_zero_export_profile = entry.options.get(
            CONF_ENPHASE_ZERO_EXPORT_PROFILE,
            entry.data.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE)
        )
        enphase_is_installer = entry.options.get(
            CONF_ENPHASE_IS_INSTALLER,
            entry.data.get(CONF_ENPHASE_IS_INSTALLER, False)
        )

        if not inverter_host:
            _LOGGER.warning("No inverter host configured")
            return

        try:
            controller = get_inverter_controller(
                brand=inverter_brand,
                host=inverter_host,
                port=inverter_port,
                slave_id=inverter_slave_id,
                model=inverter_model,
                token=inverter_token,
                load_following=fronius_load_following,
                enphase_username=enphase_username,
                enphase_password=enphase_password,
                enphase_serial=enphase_serial,
                enphase_normal_profile=enphase_normal_profile,
                enphase_zero_export_profile=enphase_zero_export_profile,
                enphase_is_installer=enphase_is_installer,
            )

            home_load_w = None

            if mode == "shutdown":
                # Full shutdown mode - pass 0 or None to trigger full curtailment
                _LOGGER.info(f"🔴 Shutting down {inverter_brand} inverter at {inverter_host}")
                # For Zeversolar, home_load_w=0 triggers 0% shutdown
                # For others, curtail() does their native shutdown/zero-export
                if inverter_brand == "zeversolar":
                    home_load_w = 0
            else:
                # Load-following mode - get home load for dynamic limiting
                if inverter_brand in ("zeversolar", "sigenergy", "sungrow", "enphase"):
                    live_status = await get_live_status()
                    if live_status and live_status.get("load_power"):
                        home_load_w = int(live_status.get("load_power", 0))
                        _LOGGER.info(f"🔌 Load-following: Home load is {home_load_w}W")

                _LOGGER.info(f"🔴 Curtailing {inverter_brand} inverter at {inverter_host}")

            # Call curtail with appropriate parameters
            if home_load_w is not None and hasattr(controller, 'curtail'):
                import inspect
                sig = inspect.signature(controller.curtail)
                if 'home_load_w' in sig.parameters:
                    success = await controller.curtail(home_load_w=home_load_w)
                else:
                    success = await controller.curtail()
            else:
                success = await controller.curtail()

            if success:
                if mode == "shutdown":
                    _LOGGER.info(f"✅ Inverter shut down (0% output)")
                elif home_load_w is not None and home_load_w > 0:
                    _LOGGER.info(f"✅ Inverter curtailed (load-following to {home_load_w}W)")
                else:
                    _LOGGER.info(f"✅ Inverter curtailed successfully")
                hass.data[DOMAIN][entry.entry_id]["inverter_last_state"] = "curtailed"
                hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = home_load_w
            else:
                _LOGGER.error("❌ Failed to curtail inverter")

            await controller.disconnect()

        except Exception as e:
            _LOGGER.error(f"Error curtailing inverter: {e}")

    async def handle_restore_inverter(call: ServiceCall) -> None:
        """Manually restore the AC-coupled inverter to normal operation."""
        _LOGGER.info("🟢 Manual inverter restore requested")

        inverter_enabled = entry.options.get(
            CONF_AC_INVERTER_CURTAILMENT_ENABLED,
            entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        )

        if not inverter_enabled:
            _LOGGER.warning("Inverter curtailment not enabled in config")
            return

        inverter_brand = entry.options.get(
            CONF_INVERTER_BRAND,
            entry.data.get(CONF_INVERTER_BRAND, "sungrow")
        )
        inverter_host = entry.options.get(
            CONF_INVERTER_HOST,
            entry.data.get(CONF_INVERTER_HOST, "")
        )
        inverter_port = entry.options.get(
            CONF_INVERTER_PORT,
            entry.data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
        )
        inverter_slave_id = entry.options.get(
            CONF_INVERTER_SLAVE_ID,
            entry.data.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)
        )
        inverter_model = entry.options.get(
            CONF_INVERTER_MODEL,
            entry.data.get(CONF_INVERTER_MODEL)
        )
        inverter_token = entry.options.get(
            CONF_INVERTER_TOKEN,
            entry.data.get(CONF_INVERTER_TOKEN)
        )
        fronius_load_following = entry.options.get(
            CONF_FRONIUS_LOAD_FOLLOWING,
            entry.data.get(CONF_FRONIUS_LOAD_FOLLOWING, False)
        )
        # Enphase Enlighten credentials for automatic JWT token refresh
        enphase_username = entry.options.get(
            CONF_ENPHASE_USERNAME,
            entry.data.get(CONF_ENPHASE_USERNAME)
        )
        enphase_password = entry.options.get(
            CONF_ENPHASE_PASSWORD,
            entry.data.get(CONF_ENPHASE_PASSWORD)
        )
        enphase_serial = entry.options.get(
            CONF_ENPHASE_SERIAL,
            entry.data.get(CONF_ENPHASE_SERIAL)
        )
        enphase_normal_profile = entry.options.get(
            CONF_ENPHASE_NORMAL_PROFILE,
            entry.data.get(CONF_ENPHASE_NORMAL_PROFILE)
        )
        enphase_zero_export_profile = entry.options.get(
            CONF_ENPHASE_ZERO_EXPORT_PROFILE,
            entry.data.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE)
        )
        enphase_is_installer = entry.options.get(
            CONF_ENPHASE_IS_INSTALLER,
            entry.data.get(CONF_ENPHASE_IS_INSTALLER, False)
        )

        if not inverter_host:
            _LOGGER.warning("No inverter host configured")
            return

        try:
            controller = get_inverter_controller(
                brand=inverter_brand,
                host=inverter_host,
                port=inverter_port,
                slave_id=inverter_slave_id,
                model=inverter_model,
                token=inverter_token,
                load_following=fronius_load_following,
                enphase_username=enphase_username,
                enphase_password=enphase_password,
                enphase_serial=enphase_serial,
                enphase_normal_profile=enphase_normal_profile,
                enphase_zero_export_profile=enphase_zero_export_profile,
                enphase_is_installer=enphase_is_installer,
            )

            _LOGGER.info(f"🟢 Restoring {inverter_brand} inverter at {inverter_host}")

            success = await controller.restore()

            if success:
                _LOGGER.info(f"✅ Inverter restored to normal operation")
                hass.data[DOMAIN][entry.entry_id]["inverter_last_state"] = "normal"
                hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = None
            else:
                _LOGGER.error("❌ Failed to restore inverter")

            await controller.disconnect()

        except Exception as e:
            _LOGGER.error(f"Error restoring inverter: {e}")

    hass.services.async_register(DOMAIN, SERVICE_CURTAIL_INVERTER, handle_curtail_inverter)
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_INVERTER, handle_restore_inverter)

    _LOGGER.info("🔌 AC inverter curtail/restore services registered")

    # ======================================================================
    # CALENDAR HISTORY SERVICE (for mobile app energy summaries)
    # ======================================================================

    async def handle_get_calendar_history(call: ServiceCall) -> dict:
        """Handle get_calendar_history service call - returns energy history data."""
        period = call.data.get("period", "day")

        # Validate period
        valid_periods = ["day", "week", "month", "year"]
        if period not in valid_periods:
            _LOGGER.error(f"Invalid period '{period}'. Must be one of: {valid_periods}")
            return {"success": False, "error": f"Invalid period. Must be one of: {valid_periods}"}

        _LOGGER.info(f"📊 Calendar history requested for period: {period}")

        # Get Tesla coordinator
        tesla_coordinator = hass.data[DOMAIN][entry.entry_id].get("tesla_coordinator")
        if not tesla_coordinator:
            _LOGGER.error("Tesla coordinator not available")
            return {"success": False, "error": "Tesla coordinator not available"}

        # Fetch calendar history
        history = await tesla_coordinator.async_get_calendar_history(period=period)

        if not history:
            _LOGGER.error("Failed to fetch calendar history")
            return {"success": False, "error": "Failed to fetch calendar history from Tesla API"}

        # Transform time_series to match mobile app format
        # Include both normalized fields AND detailed Tesla breakdown fields
        time_series = []
        for entry_data in history.get("time_series", []):
            time_series.append({
                "timestamp": entry_data.get("timestamp", ""),
                # Normalized fields for compatibility
                "solar_generation": entry_data.get("solar_energy_exported", 0),
                "battery_discharge": entry_data.get("battery_energy_exported", 0),
                "battery_charge": entry_data.get("battery_energy_imported", 0),
                "grid_import": entry_data.get("grid_energy_imported", 0),
                "grid_export": entry_data.get("grid_energy_exported_from_solar", 0) + entry_data.get("grid_energy_exported_from_battery", 0),
                "home_consumption": entry_data.get("consumer_energy_imported_from_grid", 0) + entry_data.get("consumer_energy_imported_from_solar", 0) + entry_data.get("consumer_energy_imported_from_battery", 0),
                # Detailed breakdown fields from Tesla API (for detail screens)
                "solar_energy_exported": entry_data.get("solar_energy_exported", 0),
                "battery_energy_exported": entry_data.get("battery_energy_exported", 0),
                "battery_energy_imported_from_grid": entry_data.get("battery_energy_imported_from_grid", 0),
                "battery_energy_imported_from_solar": entry_data.get("battery_energy_imported_from_solar", 0),
                "consumer_energy_imported_from_grid": entry_data.get("consumer_energy_imported_from_grid", 0),
                "consumer_energy_imported_from_solar": entry_data.get("consumer_energy_imported_from_solar", 0),
                "consumer_energy_imported_from_battery": entry_data.get("consumer_energy_imported_from_battery", 0),
                "grid_energy_exported_from_solar": entry_data.get("grid_energy_exported_from_solar", 0),
                "grid_energy_exported_from_battery": entry_data.get("grid_energy_exported_from_battery", 0),
            })

        result = {
            "success": True,
            "period": period,
            "time_series": time_series,
            "serial_number": history.get("serial_number"),
            "installation_date": history.get("installation_date"),
        }

        _LOGGER.info(f"✅ Calendar history returned: {len(time_series)} records for period '{period}'")
        return result

    # Register with response support (HA 2024.1+)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CALENDAR_HISTORY,
        handle_get_calendar_history,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("📊 Calendar history service registered")

    # Register HTTP endpoint for calendar history (REST API alternative)
    hass.http.register_view(CalendarHistoryView(hass))
    _LOGGER.info("📊 Calendar history HTTP endpoint registered at /api/power_sync/calendar_history")

    # Register HTTP endpoint for Powerwall settings (for mobile app Controls)
    hass.http.register_view(PowerwallSettingsView(hass))
    _LOGGER.info("⚙️ Powerwall settings HTTP endpoint registered at /api/power_sync/powerwall_settings")

    # Register HTTP endpoint for Powerwall type (for mobile app Settings)
    hass.http.register_view(PowerwallTypeView(hass))
    _LOGGER.info("🔋 Powerwall type HTTP endpoint registered at /api/power_sync/powerwall_type")

    # Register HTTP endpoint for Inverter status (for mobile app Solar controls)
    hass.http.register_view(InverterStatusView(hass))
    _LOGGER.info("☀️ Inverter status HTTP endpoint registered at /api/power_sync/inverter_status")

    # Register HTTP endpoint for Sigenergy tariff (for mobile app dashboard)
    hass.http.register_view(SigenergyTariffView(hass))
    _LOGGER.info("📊 Sigenergy tariff HTTP endpoint registered at /api/power_sync/sigenergy_tariff")

    # Register HTTP endpoint for Config (for mobile app auto-detection)
    config_view = ConfigView(hass)
    hass.http.register_view(config_view)
    _LOGGER.info("📱 Config HTTP endpoint registered at /api/power_sync/backend_config")

    # Also register at legacy URL for backwards compatibility
    hass.http.register_view(ConfigViewLegacy(hass, config_view))
    _LOGGER.info("📱 Config HTTP endpoint also registered at /api/power_sync/config (legacy)")

    # Register HTTP endpoint for Tariff Price (for Globird users without API)
    hass.http.register_view(TariffPriceView(hass))
    _LOGGER.info("💰 Tariff price HTTP endpoint registered at /api/power_sync/tariff_price")

    # Register HTTP endpoints for Automations (for mobile app)
    hass.http.register_view(AutomationsView(hass))
    hass.http.register_view(AutomationDetailView(hass))
    hass.http.register_view(AutomationToggleView(hass))
    hass.http.register_view(AutomationGroupsView(hass))
    _LOGGER.info("⚡ Automations HTTP endpoints registered at /api/power_sync/automations")

    # Register HTTP endpoint for push token registration
    hass.http.register_view(PushTokenRegisterView(hass))
    _LOGGER.info("📱 Push token registration endpoint registered at /api/power_sync/push/register")

    # Register HTTP endpoint for current weather (for mobile app dashboard)
    hass.http.register_view(CurrentWeatherView(hass))
    _LOGGER.info("🌤️ Current weather HTTP endpoint registered at /api/power_sync/weather")

    # Register HTTP endpoints for EV/Tesla vehicles (for mobile app EV section)
    hass.http.register_view(EVStatusView(hass))
    hass.http.register_view(EVVehiclesView(hass))
    hass.http.register_view(EVVehiclesSyncView(hass))
    hass.http.register_view(EVVehicleCommandView(hass))
    _LOGGER.info("🚗 EV HTTP endpoints registered at /api/power_sync/ev/*")

    # ======================================================================
    # SYNC BATTERY HEALTH SERVICE (from mobile app TEDAPI scans)
    # ======================================================================

    async def handle_sync_battery_health(call: ServiceCall) -> dict:
        """Handle sync_battery_health service call - receives battery health from mobile app."""
        original_capacity_wh = call.data.get("original_capacity_wh")
        current_capacity_wh = call.data.get("current_capacity_wh")
        degradation_percent = call.data.get("degradation_percent")
        battery_count = call.data.get("battery_count", 1)
        scanned_at = call.data.get("scanned_at", datetime.now().isoformat())
        individual_batteries = call.data.get("individual_batteries")  # Optional per-battery data

        # Validate required fields
        if original_capacity_wh is None or current_capacity_wh is None or degradation_percent is None:
            _LOGGER.error("Missing required battery health fields")
            return {"success": False, "error": "Missing required fields: original_capacity_wh, current_capacity_wh, degradation_percent"}

        # Calculate health percentage (can be > 100% if batteries have more capacity than spec)
        health_percent = round((current_capacity_wh / original_capacity_wh) * 100, 1) if original_capacity_wh > 0 else 0

        _LOGGER.info(
            f"🔋 Battery health received: {health_percent}% health ({current_capacity_wh}Wh / {original_capacity_wh}Wh, {battery_count} units)"
        )

        # Build battery health data
        battery_health_data = {
            "original_capacity_wh": original_capacity_wh,
            "current_capacity_wh": current_capacity_wh,
            "degradation_percent": degradation_percent,
            "battery_count": battery_count,
            "scanned_at": scanned_at,
        }

        # Include individual battery data if provided
        if individual_batteries:
            battery_health_data["individual_batteries"] = individual_batteries
            _LOGGER.info(f"  → Individual batteries: {len(individual_batteries)} units")

        # Store in hass.data for sensor to read on startup
        hass.data[DOMAIN][entry.entry_id]["battery_health"] = battery_health_data

        # Persist to storage
        store = hass.data[DOMAIN][entry.entry_id].get("store")
        if store:
            stored_data = await store.async_load() or {}
            stored_data["battery_health"] = battery_health_data
            await store.async_save(stored_data)
            _LOGGER.debug("Battery health persisted to storage")

        # Notify sensor via dispatcher
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_battery_health_update_{entry.entry_id}",
            battery_health_data,
        )

        return {
            "success": True,
            "message": f"Battery health synced: {health_percent}% health",
            "data": battery_health_data,
        }

    # Register with response support
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_BATTERY_HEALTH,
        handle_sync_battery_health,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("🔋 Battery health sync service registered")

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
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
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

    # Set up fast load-following update (every 30 seconds) for responsive power limiting
    # This only updates the power limit when already in load-following mode, doesn't change curtail/restore decisions
    async def fast_load_following_update(now):
        """Update inverter power limit based on current home load (runs every 30s when in load-following mode)."""
        try:
            entry_data = hass.data[DOMAIN].get(entry.entry_id, {})

            # Check if AC curtailment is enabled
            inverter_curtailment_enabled = entry.options.get(
                CONF_AC_INVERTER_CURTAILMENT_ENABLED,
                entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
            )
            if not inverter_curtailment_enabled:
                return

            # Check if currently in load-following mode (curtailed state)
            inverter_last_state = entry_data.get("inverter_last_state")
            if inverter_last_state != "curtailed":
                return  # Only update when already in load-following mode

            # Get inverter config
            inverter_brand = entry.options.get(CONF_INVERTER_BRAND, entry.data.get(CONF_INVERTER_BRAND))
            inverter_host = entry.options.get(CONF_INVERTER_HOST, entry.data.get(CONF_INVERTER_HOST))

            # Only Zeversolar, Sigenergy, Sungrow, and Enphase support load-following
            if inverter_brand not in ("zeversolar", "sigenergy", "sungrow", "enphase"):
                return

            if not inverter_host:
                return

            # Get current home load from Tesla API
            live_status = await get_live_status()
            if not live_status or not live_status.get("load_power"):
                return

            home_load_w = int(live_status.get("load_power", 0))

            # Add battery charge rate if charging
            battery_power = live_status.get("battery_power", 0) or 0
            battery_charge_w = max(0, -int(battery_power))  # Negative = charging
            if battery_charge_w > 50:
                home_load_w += battery_charge_w

            # Get current power limit to avoid unnecessary updates
            current_limit = entry_data.get("inverter_power_limit_w")
            last_dpel_time = entry_data.get("last_dpel_update_time")

            # For Enphase, always re-apply DPEL at least every 45 seconds since it may timeout
            # For other brands, only update if changed by more than 50W
            from datetime import datetime, timedelta
            now_time = datetime.now()
            force_reapply = False
            if inverter_brand == "enphase":
                if last_dpel_time is None or (now_time - last_dpel_time) > timedelta(seconds=45):
                    force_reapply = True
                    _LOGGER.debug(f"Enphase DPEL refresh needed (last update: {last_dpel_time})")

            if not force_reapply and current_limit is not None and abs(home_load_w - current_limit) < 50:
                return

            # Get inverter controller
            controller = entry_data.get("inverter_controller")
            if not controller:
                return

            # Update power limit
            import inspect
            if hasattr(controller, 'curtail'):
                sig = inspect.signature(controller.curtail)
                if 'home_load_w' in sig.parameters:
                    success = await controller.curtail(home_load_w=home_load_w)
                    if success:
                        _LOGGER.debug(f"⚡ Fast load-following update: {home_load_w}W")
                        hass.data[DOMAIN][entry.entry_id]["inverter_power_limit_w"] = home_load_w
                        hass.data[DOMAIN][entry.entry_id]["last_dpel_update_time"] = datetime.now()
        except Exception as err:
            _LOGGER.debug(f"Fast load-following update error (non-critical): {err}")

    # Run every 30 seconds at :00 and :30
    load_following_cancel_timer = async_track_utc_time_change(
        hass,
        fast_load_following_update,
        second=[0, 30],
    )
    hass.data[DOMAIN][entry.entry_id]["load_following_cancel"] = load_following_cancel_timer
    _LOGGER.info("Fast load-following update scheduled every 30 seconds")

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

    # ========================================
    # AUTOMATIONS ENGINE SETUP
    # ========================================
    # Initialize the automation store and engine for user-defined automations
    from .automations import AutomationStore, AutomationEngine

    automation_store = AutomationStore(hass)
    await automation_store.async_load()

    automation_engine = AutomationEngine(hass, automation_store, entry)

    # Store automation components in hass.data
    hass.data[DOMAIN][entry.entry_id]["automation_store"] = automation_store
    hass.data[DOMAIN][entry.entry_id]["automation_engine"] = automation_engine
    # Also store at domain level for HTTP API access
    hass.data[DOMAIN]["automation_store"] = automation_store

    # Restore persisted push tokens to hass.data for notification sending
    persisted_tokens = automation_store.get_push_tokens()
    if persisted_tokens:
        if "push_tokens" not in hass.data[DOMAIN]:
            hass.data[DOMAIN]["push_tokens"] = {}
        hass.data[DOMAIN]["push_tokens"].update(persisted_tokens)
        _LOGGER.info(f"📱 Restored {len(persisted_tokens)} push token(s) from storage")

    # Set up automation evaluation timer (every 30 seconds)
    async def auto_evaluate_automations(now):
        """Evaluate all user automations."""
        try:
            triggered_count = await automation_engine.async_evaluate_all()
            if triggered_count > 0:
                _LOGGER.info(f"🤖 Automation evaluation: {triggered_count} automation(s) triggered")
        except Exception as e:
            _LOGGER.error(f"Error evaluating automations: {e}")

    automation_cancel_timer = async_track_utc_time_change(
        hass,
        auto_evaluate_automations,
        second=[0, 30],  # Every 30 seconds
    )
    hass.data[DOMAIN][entry.entry_id]["automation_cancel"] = automation_cancel_timer
    _LOGGER.info("🤖 Automation evaluation scheduled every 30 seconds")

    # Register automation CRUD services
    async def handle_list_automations(call: ServiceCall) -> dict:
        """List all automations."""
        automations = automation_store.get_all()
        return {"automations": automations}

    async def handle_create_automation(call: ServiceCall) -> dict:
        """Create a new automation."""
        automation_data = dict(call.data)
        automation = automation_store.create(automation_data)
        await automation_store.async_save()
        return {"automation": automation}

    async def handle_update_automation(call: ServiceCall) -> dict:
        """Update an existing automation."""
        automation_id = call.data.get("automation_id")
        automation_data = {k: v for k, v in call.data.items() if k != "automation_id"}
        automation = automation_store.update(automation_id, automation_data)
        if automation:
            await automation_store.async_save()
            return {"automation": automation}
        return {"error": "Automation not found"}

    async def handle_delete_automation(call: ServiceCall) -> dict:
        """Delete an automation."""
        automation_id = call.data.get("automation_id")
        success = automation_store.delete(automation_id)
        if success:
            await automation_store.async_save()
            return {"success": True}
        return {"error": "Automation not found"}

    async def handle_toggle_automation(call: ServiceCall) -> dict:
        """Toggle an automation's enabled state."""
        automation_id = call.data.get("automation_id")
        new_state = automation_store.toggle(automation_id)
        if new_state is not None:
            await automation_store.async_save()
            return {"enabled": new_state}
        return {"error": "Automation not found"}

    async def handle_pause_automation(call: ServiceCall) -> dict:
        """Pause an automation."""
        automation_id = call.data.get("automation_id")
        success = automation_store.pause(automation_id)
        if success:
            await automation_store.async_save()
            return {"success": True}
        return {"error": "Automation not found"}

    async def handle_resume_automation(call: ServiceCall) -> dict:
        """Resume a paused automation."""
        automation_id = call.data.get("automation_id")
        success = automation_store.resume(automation_id)
        if success:
            await automation_store.async_save()
            return {"success": True}
        return {"error": "Automation not found"}

    async def handle_list_groups(call: ServiceCall) -> dict:
        """List all automation groups."""
        groups = automation_store.get_groups()
        return {"groups": groups}

    # Register automation services with response support
    hass.services.async_register(
        DOMAIN,
        "list_automations",
        handle_list_automations,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "create_automation",
        handle_create_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "update_automation",
        handle_update_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "delete_automation",
        handle_delete_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "toggle_automation",
        handle_toggle_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "pause_automation",
        handle_pause_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "resume_automation",
        handle_resume_automation,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "list_automation_groups",
        handle_list_groups,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _LOGGER.info("🤖 Automation services registered")

    _LOGGER.info("=" * 60)
    _LOGGER.info("PowerSync integration setup complete!")
    _LOGGER.info("Domain '%s' registered successfully", DOMAIN)
    _LOGGER.info("Mobile app should now detect the integration")
    _LOGGER.info("=" * 60)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading PowerSync integration")

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

    # Cancel the load-following timer if it exists
    if load_following_cancel := entry_data.get("load_following_cancel"):
        load_following_cancel()
        _LOGGER.debug("Cancelled load-following timer")

    # Cancel the AEMO spike timer if it exists
    if aemo_spike_cancel := entry_data.get("aemo_spike_cancel"):
        aemo_spike_cancel()
        _LOGGER.debug("Cancelled AEMO spike timer")

    # Cancel the demand period grid charging timer if it exists
    if demand_charging_cancel := entry_data.get("demand_charging_cancel"):
        demand_charging_cancel()
        _LOGGER.debug("Cancelled demand period grid charging timer")

    # Cancel the automation evaluation timer if it exists
    if automation_cancel := entry_data.get("automation_cancel"):
        automation_cancel()
        _LOGGER.debug("Cancelled automation evaluation timer")

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
