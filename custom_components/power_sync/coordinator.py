"""Data update coordinators for PowerSync with improved error handling."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import Any
import asyncio

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    UPDATE_INTERVAL_PRICES,
    UPDATE_INTERVAL_ENERGY,
    AMBER_API_BASE_URL,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    POWER_SYNC_USER_AGENT,
)


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


async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    max_retries: int = 3,
    timeout_seconds: int = 60,
    **kwargs
) -> dict[str, Any]:
    """Fetch data with exponential backoff retry logic.

    Args:
        session: aiohttp client session
        url: URL to fetch
        headers: Request headers
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Request timeout in seconds (default: 60)
        **kwargs: Additional arguments to pass to session.get()

    Returns:
        JSON response data

    Raises:
        UpdateFailed: If all retries fail
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            # Exponential backoff: 2^attempt seconds (1s, 2s, 4s)
            if attempt > 0:
                wait_time = 2 ** attempt
                _LOGGER.info(f"Retry attempt {attempt + 1}/{max_retries} after {wait_time}s delay")
                await asyncio.sleep(wait_time)

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **kwargs
            ) as response:
                if response.status == 200:
                    return await response.json()

                # Log the error but continue retrying on 5xx errors
                error_text = await response.text()

                if response.status >= 500:
                    _LOGGER.warning(
                        f"Server error (attempt {attempt + 1}/{max_retries}): {response.status} - {error_text[:200]}"
                    )
                    last_error = UpdateFailed(f"Server error: {response.status}")
                    continue  # Retry on 5xx errors
                else:
                    # Don't retry on 4xx client errors
                    raise UpdateFailed(f"Client error {response.status}: {error_text}")

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                f"Network error (attempt {attempt + 1}/{max_retries}): {err}"
            )
            last_error = UpdateFailed(f"Network error: {err}")
            continue  # Retry on network errors

        except asyncio.TimeoutError:
            _LOGGER.warning(
                f"Timeout error (attempt {attempt + 1}/{max_retries}): Request exceeded {timeout_seconds}s"
            )
            last_error = UpdateFailed(f"Timeout after {timeout_seconds}s")
            continue  # Retry on timeout

    # All retries failed
    raise last_error or UpdateFailed("All retry attempts failed")


class AmberPriceCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Amber electricity price data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_token: str,
        site_id: str | None = None,
        ws_client=None,
    ) -> None:
        """Initialize the coordinator."""
        self.api_token = api_token
        self.site_id = site_id
        self.session = async_get_clientsession(hass)
        self.ws_client = ws_client  # WebSocket client for real-time prices

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_amber_prices",
            update_interval=UPDATE_INTERVAL_PRICES,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Amber API with WebSocket-first approach."""
        headers = {"Authorization": f"Bearer {self.api_token}"}

        try:
            # Try WebSocket first for current prices (real-time, low latency)
            current_prices = None
            if self.ws_client:
                # Retry logic: Try for 10 seconds with 2-second intervals (5 attempts)
                max_age_seconds = 60  # Reduced from 360s to 60s for fresher data
                retry_attempts = 5
                retry_interval = 2  # seconds

                for attempt in range(retry_attempts):
                    current_prices = self.ws_client.get_latest_prices(max_age_seconds=max_age_seconds)

                    if current_prices:
                        # Get health status to log data age
                        health = self.ws_client.get_health_status()
                        age = health.get('age_seconds', 'unknown')
                        _LOGGER.info(f"✓ Using WebSocket prices (age: {age}s, attempt: {attempt + 1}/{retry_attempts})")
                        break

                    # If not last attempt, wait before retry
                    if attempt < retry_attempts - 1:
                        _LOGGER.debug(f"WebSocket data unavailable/stale, retrying in {retry_interval}s (attempt {attempt + 1}/{retry_attempts})")
                        await asyncio.sleep(retry_interval)

                # All retries exhausted
                if not current_prices:
                    _LOGGER.info(f"WebSocket prices unavailable after {retry_attempts} attempts ({max_age_seconds}s staleness threshold), falling back to REST API")

            # Fall back to REST API if WebSocket unavailable
            if not current_prices:
                _LOGGER.info("⚠ Using REST API for current prices (WebSocket unavailable)")
                current_prices = await _fetch_with_retry(
                    self.session,
                    f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices/current",
                    headers,
                    max_retries=2,  # Less retries for Amber (usually more reliable)
                    timeout_seconds=30,
                )

            # Dual-resolution forecast approach to ensure complete data coverage:
            # 1. Fetch 1 hour at 5-min resolution for CurrentInterval/ActualInterval spike detection
            # 2. Fetch 48 hours at 30-min resolution for complete TOU schedule building
            # (The Amber API doesn't provide 48 hours of 5-min data, causing missing sell prices)

            # Step 1: Get 5-min resolution data for current period spike detection
            forecast_5min = await _fetch_with_retry(
                self.session,
                f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices",
                headers,
                params={"next": 1, "resolution": 5},
                max_retries=2,
                timeout_seconds=30,
            )

            # Step 2: Get 30-min resolution data for full 48-hour TOU schedule
            forecast_30min = await _fetch_with_retry(
                self.session,
                f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices",
                headers,
                params={"next": 48, "resolution": 30},
                max_retries=2,
                timeout_seconds=30,
            )

            return {
                "current": current_prices,
                "forecast": forecast_30min,  # Use 30-min forecast for TOU schedule
                "forecast_5min": forecast_5min,  # Keep 5-min for CurrentInterval extraction
                "last_update": dt_util.utcnow(),
            }

        except UpdateFailed:
            raise  # Re-raise UpdateFailed exceptions
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching Amber data: {err}") from err


class TeslaEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Tesla energy data from Tesla API (Teslemetry or Fleet API)."""

    def __init__(
        self,
        hass: HomeAssistant,
        site_id: str,
        api_token: str,
        api_provider: str = TESLA_PROVIDER_TESLEMETRY,
        token_getter: callable = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            site_id: Tesla energy site ID
            api_token: Initial API token (used if token_getter not provided)
            api_provider: API provider (teslemetry or fleet_api)
            token_getter: Optional callable that returns (token, provider) tuple.
                          If provided, this is called before each request to get fresh token.
        """
        self.site_id = site_id
        self._api_token = api_token  # Fallback token
        self._token_getter = token_getter  # Callable to get fresh token
        self.api_provider = api_provider
        self.session = async_get_clientsession(hass)
        self._site_info_cache = None  # Cache site_info since timezone doesn't change

        # Determine API base URL based on provider
        if api_provider == TESLA_PROVIDER_FLEET_API:
            self.api_base_url = FLEET_API_BASE_URL
            _LOGGER.info(f"TeslaEnergyCoordinator initialized with Fleet API for site {site_id}")
        else:
            self.api_base_url = TESLEMETRY_API_BASE_URL
            _LOGGER.info(f"TeslaEnergyCoordinator initialized with Teslemetry for site {site_id}")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tesla_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    def _get_current_token(self) -> str:
        """Get the current API token, fetching fresh if token_getter is available."""
        if self._token_getter:
            try:
                token, provider = self._token_getter()
                if token:
                    # Update provider and base URL if it changed
                    if provider != self.api_provider:
                        self.api_provider = provider
                        if provider == TESLA_PROVIDER_FLEET_API:
                            self.api_base_url = FLEET_API_BASE_URL
                        else:
                            self.api_base_url = TESLEMETRY_API_BASE_URL
                        _LOGGER.debug(f"Token provider changed to {provider}")
                    return token
            except Exception as e:
                _LOGGER.warning(f"Token getter failed, using fallback token: {e}")
        return self._api_token

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tesla API (Teslemetry or Fleet API)."""
        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            # Get live status from Tesla API with retry logic
            # Note: Both Teslemetry and Fleet API can be slow, so we use retries
            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/live_status",
                headers,
                max_retries=3,  # More retries for reliability
                timeout_seconds=60,  # Longer timeout
            )

            live_status = data.get("response", {})
            _LOGGER.debug("Tesla API live_status response: %s", live_status)

            # Map Teslemetry API response to our data structure
            energy_data = {
                "solar_power": live_status.get("solar_power", 0) / 1000,  # Convert W to kW
                "grid_power": live_status.get("grid_power", 0) / 1000,
                "battery_power": live_status.get("battery_power", 0) / 1000,
                "load_power": live_status.get("load_power", 0) / 1000,
                "battery_level": live_status.get("percentage_charged", 0),
                "last_update": dt_util.utcnow(),
            }

            return energy_data

        except UpdateFailed:
            raise  # Re-raise UpdateFailed exceptions
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching Tesla energy data: {err}") from err

    async def async_get_site_info(self) -> dict[str, Any] | None:
        """
        Fetch site_info from Tesla API (Teslemetry or Fleet API).

        Includes installation_time_zone which is critical for correct TOU schedule alignment.
        Results are cached since site info (especially timezone) doesn't change.

        Returns:
            Site info dict containing installation_time_zone, or None if fetch fails
        """
        # Return cached value if available
        if self._site_info_cache:
            _LOGGER.debug("Returning cached site_info")
            return self._site_info_cache

        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            _LOGGER.info(f"Fetching site_info for site {self.site_id}")

            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/site_info",
                headers,
                max_retries=3,
                timeout_seconds=60,
            )

            site_info = data.get("response", {})

            # Log timezone info for debugging
            installation_tz = site_info.get("installation_time_zone")
            if installation_tz:
                _LOGGER.info(f"Found Powerwall timezone: {installation_tz}")
            else:
                _LOGGER.warning("No installation_time_zone in site_info response")

            # Cache the result
            self._site_info_cache = site_info

            return site_info

        except UpdateFailed as err:
            _LOGGER.error(f"Failed to fetch site_info: {err}")
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error fetching site_info: {err}")
            return None

    async def set_grid_charging_enabled(self, enabled: bool) -> bool:
        """
        Enable or disable grid charging (imports) for the Powerwall.

        Args:
            enabled: True to allow grid charging, False to disallow

        Returns:
            bool: True if successful, False otherwise
        """
        # Note: The API field is inverted - True means charging is DISALLOWED
        disallow_value = not enabled

        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            _LOGGER.info(f"Setting grid charging {'enabled' if enabled else 'disabled'} for site {self.site_id}")

            url = f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/grid_import_export"
            payload = {
                "disallow_charge_from_grid_with_solar_installed": disallow_value
            }

            async with self.session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status not in [200, 201, 202]:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set grid charging: {response.status} - {text}")
                    return False

                data = await response.json()
                _LOGGER.debug(f"Set grid charging response: {data}")

                # Check for actual success in response body
                response_data = data.get("response", data)
                if isinstance(response_data, dict) and "result" in response_data:
                    if not response_data["result"]:
                        reason = response_data.get("reason", "Unknown reason")
                        _LOGGER.error(f"Set grid charging failed: {reason}")
                        return False

                _LOGGER.info(f"✅ Grid charging {'enabled' if enabled else 'disabled'} successfully for site {self.site_id}")
                return True

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout setting grid charging")
            return False
        except Exception as err:
            _LOGGER.error(f"Error setting grid charging: {err}")
            return False

    async def async_get_calendar_history(
        self,
        period: str = "day",
        kind: str = "energy",
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch calendar history from Tesla API.

        Args:
            period: 'day', 'week', 'month', 'year', or 'lifetime'
            kind: 'energy' or 'power'
            end_date: Optional end date in YYYY-MM-DD format (defaults to today)

        Returns:
            Calendar history data with time_series array, or None if fetch fails
        """
        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            # Get site timezone from site_info
            site_info = await self.async_get_site_info()
            timezone = "Australia/Brisbane"  # Default fallback
            if site_info:
                timezone = site_info.get("installation_time_zone", timezone)

            # Calculate end_date in site's timezone
            from zoneinfo import ZoneInfo
            from datetime import timedelta
            user_tz = ZoneInfo(timezone)

            # Use provided end_date or default to now
            if end_date:
                try:
                    reference_date = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=user_tz)
                except ValueError:
                    reference_date = datetime.now(user_tz)
            else:
                reference_date = datetime.now(user_tz)

            end_dt = reference_date.replace(hour=23, minute=59, second=59)
            end_date_iso = end_dt.isoformat()

            _LOGGER.info(f"Fetching calendar history for site {self.site_id}: period={period}, kind={kind}, end_date={end_date}")

            params = {
                "kind": kind,
                "period": period,
                "end_date": end_date_iso,
                "time_zone": timezone,
            }

            url = f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/calendar_history"

            async with self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Failed to fetch calendar history: {response.status} - {text}")
                    return None

                data = await response.json()
                result = data.get("response", {})
                time_series = result.get("time_series", [])

                _LOGGER.info(f"Fetched {len(time_series)} raw records from Tesla for period='{period}'")

                # Tesla API often returns all historical data regardless of period
                # Filter client-side based on requested period and end_date
                if time_series and period in ["day", "week", "month", "year"]:
                    # Calculate cutoff date based on period, relative to reference_date
                    if period == "day":
                        cutoff = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "week":
                        cutoff = (reference_date - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "month":
                        cutoff = (reference_date - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "year":
                        cutoff = (reference_date - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)

                    # End of reference day as upper bound
                    end_of_day = reference_date.replace(hour=23, minute=59, second=59, microsecond=999999)

                    filtered_series = []
                    for entry in time_series:
                        try:
                            ts_str = entry.get("timestamp", "")
                            if ts_str:
                                entry_dt = datetime.fromisoformat(ts_str)
                                if cutoff <= entry_dt <= end_of_day:
                                    filtered_series.append(entry)
                        except (ValueError, TypeError) as e:
                            _LOGGER.warning(f"Failed to parse timestamp: {entry.get('timestamp')}: {e}")
                            continue

                    _LOGGER.info(f"Filtered calendar history from {len(time_series)} to {len(filtered_series)} records for period='{period}' (cutoff={cutoff.date()}, end={end_of_day.date()})")
                    time_series = filtered_series

                _LOGGER.info(f"Successfully fetched calendar history: {len(time_series)} records for period='{period}'")

                return {
                    "period": period,
                    "time_series": time_series,
                    "serial_number": result.get("serial_number"),
                    "installation_date": result.get("installation_date"),
                }

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout fetching calendar history")
            return None
        except Exception as err:
            _LOGGER.error(f"Error fetching calendar history: {err}")
            return None


class DemandChargeCoordinator(DataUpdateCoordinator):
    """Coordinator to track demand charges."""

    def __init__(
        self,
        hass: HomeAssistant,
        tesla_coordinator: TeslaEnergyCoordinator,
        enabled: bool = False,
        rate: float = 0.0,
        start_time: str = "14:00",
        end_time: str = "20:00",
        days: str = "All Days",
        billing_day: int = 1,
        daily_supply_charge: float = 0.0,
        monthly_supply_charge: float = 0.0,
    ) -> None:
        """Initialize the coordinator."""
        self.tesla_coordinator = tesla_coordinator
        self.enabled = enabled
        self.rate = rate
        self.start_time = start_time
        self.end_time = end_time
        self.days = days
        self.billing_day = billing_day
        self.daily_supply_charge = daily_supply_charge
        self.monthly_supply_charge = monthly_supply_charge

        # Track peak demand (persists across coordinator updates)
        self._peak_demand_kw = 0.0
        self._last_billing_day_check = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_demand_charge",
            update_interval=timedelta(minutes=1),  # Check every minute
        )

    def _is_in_peak_period(self, now: datetime) -> bool:
        """Check if current time is within peak period and correct day."""
        try:
            # Check if today matches the configured days filter
            weekday = now.weekday()  # 0=Monday, 6=Sunday
            if self.days == "Weekdays Only" and weekday >= 5:
                return False  # Saturday or Sunday
            elif self.days == "Weekends Only" and weekday < 5:
                return False  # Monday through Friday

            # Check if current time is within peak period
            # Handle both "HH:MM" and "HH:MM:SS" formats
            start_parts = self.start_time.split(":")
            start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
            end_parts = self.end_time.split(":")
            end_hour, end_minute = int(end_parts[0]), int(end_parts[1])

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_hour * 60 + start_minute
            end_minutes = end_hour * 60 + end_minute

            # Handle overnight periods (e.g., 22:00 to 06:00)
            if end_minutes <= start_minutes:
                # Peak period wraps around midnight
                return current_minutes >= start_minutes or current_minutes < end_minutes
            else:
                # Normal daytime peak period
                return start_minutes <= current_minutes < end_minutes

        except (ValueError, AttributeError) as err:
            _LOGGER.error("Invalid time format for demand charge period: %s", err)
            return False

    async def _async_update_data(self) -> dict[str, Any]:
        """Update demand charge tracking data."""
        if not self.enabled:
            return {
                "in_peak_period": False,
                "grid_import_power_kw": 0.0,
                "peak_demand_kw": 0.0,
                "estimated_cost": 0.0,
            }

        # Check for billing cycle reset
        now = dt_util.now()
        current_day = now.day

        # If we've crossed the billing day, reset peak demand
        if self._last_billing_day_check is not None:
            # Check if we've passed the billing day since last check
            last_check_day = self._last_billing_day_check.day
            if current_day == self.billing_day and last_check_day != self.billing_day:
                _LOGGER.info("Billing cycle reset triggered on day %d", self.billing_day)
                self.reset_peak_demand()

        self._last_billing_day_check = now

        # Get current grid power from Tesla coordinator
        tesla_data = self.tesla_coordinator.data or {}
        grid_power_kw = tesla_data.get("grid_power", 0.0)

        # Grid import is positive, export is negative
        # We only care about import for demand charges
        grid_import_kw = max(0, grid_power_kw)

        # Update peak demand if current import exceeds it
        if grid_import_kw > self._peak_demand_kw:
            self._peak_demand_kw = grid_import_kw
            _LOGGER.info("New peak demand: %.2f kW", self._peak_demand_kw)

        # Check if in peak period
        now = dt_util.now()
        in_peak_period = self._is_in_peak_period(now)

        # Calculate estimated demand charge cost (peak demand * rate)
        estimated_demand_cost = self._peak_demand_kw * self.rate

        # Calculate days elapsed in current billing cycle
        days_elapsed = self._calculate_days_elapsed(now)

        # Calculate days until next billing cycle reset
        days_until_reset = self._calculate_days_until_reset(now)

        # Calculate daily supply charge cost (accumulates daily)
        daily_supply_cost = self.daily_supply_charge * days_elapsed

        # Calculate total monthly cost
        total_monthly_cost = estimated_demand_cost + daily_supply_cost + self.monthly_supply_charge

        return {
            "in_peak_period": in_peak_period,
            "grid_import_power_kw": grid_import_kw,
            "peak_demand_kw": self._peak_demand_kw,
            "estimated_cost": estimated_demand_cost,
            "daily_supply_charge_cost": daily_supply_cost,
            "monthly_supply_charge": self.monthly_supply_charge,
            "total_monthly_cost": total_monthly_cost,
            "days_until_reset": days_until_reset,
            "last_update": dt_util.utcnow(),
        }

    def reset_peak_demand(self) -> None:
        """Reset peak demand tracking (e.g., at start of new billing cycle)."""
        _LOGGER.info("Resetting peak demand from %.2f kW to 0", self._peak_demand_kw)
        self._peak_demand_kw = 0.0

    def _calculate_days_elapsed(self, now: datetime) -> int:
        """Calculate days elapsed since last billing day."""
        current_day = now.day

        if current_day >= self.billing_day:
            # We're past the billing day this month
            days_elapsed = current_day - self.billing_day + 1
        else:
            # We haven't reached the billing day this month yet
            # Need to count from last month's billing day
            # Get the last day of previous month
            first_of_this_month = now.replace(day=1)
            last_month = first_of_this_month - timedelta(days=1)
            last_day_of_last_month = last_month.day

            # Days from billing day last month to end of last month
            if self.billing_day <= last_day_of_last_month:
                days_in_last_month = last_day_of_last_month - self.billing_day + 1
            else:
                # Billing day doesn't exist in last month (e.g., Feb 30)
                # Start from last day of last month
                days_in_last_month = 1

            # Plus days in current month
            days_elapsed = days_in_last_month + current_day

        return days_elapsed

    def _calculate_days_until_reset(self, now: datetime) -> int:
        """Calculate days until next billing cycle reset."""
        current_day = now.day

        if current_day < self.billing_day:
            # Next reset is this month
            return self.billing_day - current_day
        else:
            # Next reset is next month
            # Get the last day of this month
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_month = now.replace(month=now.month + 1, day=1)

            last_day_this_month = (next_month - timedelta(days=1)).day

            # Days remaining in this month plus billing day in next month
            days_remaining_this_month = last_day_this_month - current_day
            return days_remaining_this_month + self.billing_day


class AEMOPriceCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches AEMO price data directly from AEMO API.

    This coordinator provides an alternative to AmberPriceCoordinator for users
    who want to use AEMO wholesale pricing without an Amber subscription.

    Fetches data directly from AEMO NEMWeb - no external integration required.
    The data is converted to Amber-compatible format so the existing tariff
    converter can be reused.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        region: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)
            session: aiohttp client session for API requests
        """
        from .aemo_api import AEMOAPIClient

        self.region = region
        self._client = AEMOAPIClient(session)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_aemo",
            update_interval=timedelta(minutes=5),  # Match AEMO update frequency
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AEMO API and convert to Amber-compatible format.

        Returns:
            dict with 'current', 'forecast', and 'last_update' in Amber-compatible format
        """
        try:
            # Fetch current price (5-min dispatch price)
            current_price_data = await self._client.get_region_price(self.region)

            # Fetch forecast (pre-dispatch prices)
            # Request 96 periods (48 hours) to ensure full coverage for rolling 24h window
            forecast = await self._client.get_price_forecast(self.region, periods=96)

            if not forecast:
                raise UpdateFailed(f"Failed to fetch AEMO forecast for {self.region}")

            # Get current price - prefer current dispatch price, fall back to first forecast
            if current_price_data:
                # Convert $/MWh to c/kWh: $/MWh / 10 = c/kWh
                current_price_cents = current_price_data["price"] / 10.0
                price_source = "dispatch"
            else:
                # Fall back to first forecast period
                current_price_cents = forecast[0]["perKwh"] if forecast else 0
                price_source = "forecast"
                _LOGGER.warning("Could not get current AEMO price, using forecast")

            # Create current price in Amber format
            current_prices = [
                {
                    "perKwh": current_price_cents,
                    "channelType": "general",
                    "type": "CurrentInterval",
                },
                {
                    "perKwh": -current_price_cents,
                    "channelType": "feedIn",
                    "type": "CurrentInterval",
                },
            ]

            _LOGGER.info(
                "AEMO API data for %s: current=%.2fc/kWh (%s), forecast_periods=%d",
                self.region, current_price_cents, price_source, len(forecast) // 2
            )

            return {
                "current": current_prices,
                "forecast": forecast,
                "last_update": dt_util.utcnow(),
                "source": "aemo_api",
            }

        except Exception as err:
            raise UpdateFailed(f"Error fetching AEMO data: {err}") from err


# Keep old name as alias for backwards compatibility
AEMOSensorCoordinator = AEMOPriceCoordinator


class SigenergyEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Sigenergy energy data via Modbus.

    Polls the Sigenergy inverter system via Modbus TCP to get real-time
    power data (solar, battery, grid, load) and battery state of charge.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 1,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            host: IP address of Sigenergy system
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
        """
        from .inverters.sigenergy import SigenergyController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._controller = SigenergyController(host, port, slave_id)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sigenergy_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Sigenergy system via Modbus."""
        try:
            status = await self._controller.get_status()

            attrs = status.attributes or {}

            # Map Sigenergy data to standard format (same as Tesla)
            # Power values in kW from Modbus, we keep them in kW for sensors
            solar_kw = attrs.get("pv_power_kw", 0)
            grid_kw = attrs.get("grid_power_kw", 0)  # Positive = importing, negative = exporting

            # Sigenergy battery sign convention is OPPOSITE to Tesla:
            # Sigenergy Modbus: Positive = charging (into battery), Negative = discharging (out of battery)
            # Tesla/PowerSync: Positive = discharging (out of battery), Negative = charging (into battery)
            # So we negate the value to match Tesla convention
            battery_kw_raw = attrs.get("battery_power_kw", 0)
            battery_kw = -battery_kw_raw  # Flip sign to match Tesla convention

            # Calculate home load from energy balance:
            # Load = Solar + Battery_Discharge + Grid_Import
            # With sign convention: Load = Solar - Battery_Charging + Grid (where grid negative = export)
            # Simplified: Load = Solar + Grid + Battery (all with proper signs)
            load_kw = solar_kw + grid_kw + battery_kw

            energy_data = {
                "solar_power": solar_kw,  # kW
                "grid_power": grid_kw,  # kW, positive = importing, negative = exporting
                "battery_power": battery_kw,  # kW, positive = discharging, negative = charging
                "load_power": load_kw,  # kW, calculated from energy balance
                "battery_level": attrs.get("battery_soc", 0),  # %
                "last_update": dt_util.utcnow(),
                # Extra Sigenergy-specific data
                "active_power_kw": attrs.get("active_power_kw", 0),
                "export_limit_kw": attrs.get("export_limit_kw"),
                "ems_work_mode": attrs.get("ems_work_mode"),
                "is_curtailed": status.is_curtailed,
                # Battery health data
                "battery_soh": attrs.get("battery_soh"),  # % State of Health
                "battery_capacity_kwh": attrs.get("battery_capacity_kwh"),  # kWh rated capacity
            }

            _LOGGER.debug(
                "Sigenergy data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW, curtailed=%s",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
                energy_data["is_curtailed"],
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching Sigenergy energy data: {err}") from err

    async def async_shutdown(self) -> None:
        """Disconnect from Sigenergy system on shutdown."""
        await self._controller.disconnect()


class SolcastForecastCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Solcast solar production forecasts.

    Fetches PV power forecasts from Solcast API and caches them locally.
    Updates every 3 hours to stay within API limits (10 calls/day for hobbyist tier).

    Supports multiple resource IDs for split arrays (e.g., east/west facing panels).
    Provide comma-separated resource IDs and forecasts will be combined by summing values.
    """

    # Solcast API base URL
    SOLCAST_API_URL = "https://api.solcast.com.au"

    # Update interval - 3 hours to stay within 10 calls/day limit
    UPDATE_INTERVAL = timedelta(hours=3)

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        resource_id: str,
        capacity_kw: float | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            api_key: Solcast API key
            resource_id: Rooftop site resource ID(s) - comma-separated for split arrays
            capacity_kw: System capacity in kW (optional, for validation)
        """
        self._api_key = api_key
        # Support comma-separated resource IDs for split arrays
        self._resource_ids = [rid.strip() for rid in resource_id.split(",") if rid.strip()]
        self._capacity_kw = capacity_kw
        self._session = async_get_clientsession(hass)

        # Cache for full-day forecast (stored on first fetch of the day)
        self._daily_forecast_date: str | None = None  # Date string (YYYY-MM-DD)
        self._daily_forecast_kwh: float | None = None  # Full day's forecast
        self._daily_forecast_peak_kw: float | None = None  # Peak for the day

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solcast_forecast",
            update_interval=self.UPDATE_INTERVAL,
        )

    async def _try_read_from_solcast_integration(self) -> dict[str, Any] | None:
        """Try to read forecast data from the Solcast HA integration.

        If the Solcast integration is installed, we read from its sensors instead
        of making our own API calls. This avoids doubling API usage (10 calls/day limit).

        Sensor entity IDs used:
        - sensor.solcast_pv_forecast_forecast_today (kWh)
        - sensor.solcast_pv_forecast_forecast_tomorrow (kWh)
        - sensor.solcast_pv_forecast_forecast_remaining_today (kWh)
        - sensor.solcast_pv_forecast_peak_forecast_today (W)
        - sensor.solcast_pv_forecast_peak_forecast_tomorrow (W)
        - sensor.solcast_pv_forecast_power_now (W)

        Returns:
            Forecast data dict if Solcast integration is available, None otherwise
        """
        try:
            # Check for Solcast integration sensors - forecast_today is the key sensor
            today_state = self.hass.states.get("sensor.solcast_pv_forecast_forecast_today")
            if not today_state or today_state.state in ("unavailable", "unknown", None, ""):
                return None

            # Get all the sensor values
            tomorrow_state = self.hass.states.get("sensor.solcast_pv_forecast_forecast_tomorrow")
            remaining_state = self.hass.states.get("sensor.solcast_pv_forecast_forecast_remaining_today")
            peak_today_state = self.hass.states.get("sensor.solcast_pv_forecast_peak_forecast_today")
            peak_tomorrow_state = self.hass.states.get("sensor.solcast_pv_forecast_peak_forecast_tomorrow")
            power_now_state = self.hass.states.get("sensor.solcast_pv_forecast_power_now")

            # Parse values - these are already in kWh
            today_forecast = float(today_state.state) if today_state.state else 0
            tomorrow_forecast = float(tomorrow_state.state) if tomorrow_state and tomorrow_state.state not in ("unavailable", "unknown", None, "") else 0
            remaining = float(remaining_state.state) if remaining_state and remaining_state.state not in ("unavailable", "unknown", None, "") else today_forecast

            # Peak values are in W - convert to kW
            today_peak = None
            if peak_today_state and peak_today_state.state not in ("unavailable", "unknown", None, ""):
                today_peak = float(peak_today_state.state) / 1000.0  # W to kW

            tomorrow_peak = None
            if peak_tomorrow_state and peak_tomorrow_state.state not in ("unavailable", "unknown", None, ""):
                tomorrow_peak = float(peak_tomorrow_state.state) / 1000.0  # W to kW

            # Current power estimate is in W - convert to kW
            current_estimate = None
            if power_now_state and power_now_state.state not in ("unavailable", "unknown", None, ""):
                current_estimate = float(power_now_state.state) / 1000.0  # W to kW

            # Try to get detailed hourly forecast from sensor attributes
            # The Solcast HA integration stores this in various attribute names
            detailed_forecast = None
            if today_state.attributes:
                # Try common attribute names used by Solcast HA integration
                detailed_forecast = (
                    today_state.attributes.get("detailedForecast") or
                    today_state.attributes.get("forecast_today") or
                    today_state.attributes.get("detailedHourly") or
                    today_state.attributes.get("forecasts")
                )

            # Build hourly forecast data for chart overlay
            hourly_forecast = []
            if detailed_forecast and isinstance(detailed_forecast, list):
                now = dt_util.now()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

                for period in detailed_forecast:
                    try:
                        # Parse period end time and pv_estimate
                        period_end_str = period.get("period_end", "")
                        pv_estimate = period.get("pv_estimate", 0) or 0

                        if period_end_str:
                            period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                            period_local = dt_util.as_local(period_end)

                            # Only include today's data for the chart
                            if today_start <= period_local <= today_end:
                                hourly_forecast.append({
                                    "time": period_local.strftime("%H:%M"),
                                    "hour": period_local.hour,
                                    "pv_estimate_kw": round(pv_estimate, 2),
                                })
                    except (ValueError, TypeError, KeyError):
                        continue

            _LOGGER.info(
                f"Solcast (from HA integration): Today={today_forecast:.1f}kWh, "
                f"remaining={remaining:.1f}kWh, Tomorrow={tomorrow_forecast:.1f}kWh, "
                f"hourly_points={len(hourly_forecast)}"
            )

            return {
                "available": True,
                "today_forecast_kwh": round(today_forecast, 2),
                "today_remaining_kwh": round(remaining, 2),
                "today_total_kwh": round(today_forecast, 2),
                "tomorrow_total_kwh": round(tomorrow_forecast, 2),
                "today_peak_kw": round(today_peak, 2) if today_peak else None,
                "tomorrow_peak_kw": round(tomorrow_peak, 2) if tomorrow_peak else None,
                "current_estimate_kw": round(current_estimate, 2) if current_estimate else None,
                "hourly_forecast": hourly_forecast,  # For chart overlay
                "forecast_periods": len(hourly_forecast),
                "last_update": dt_util.utcnow(),
                "source": "solcast_integration",
            }

        except (ValueError, TypeError, AttributeError) as e:
            _LOGGER.debug(f"Could not read from Solcast integration: {e}")
            return None

    async def _fetch_forecast_for_resource(self, resource_id: str) -> list[dict] | None:
        """Fetch forecast for a single resource ID.

        Args:
            resource_id: Solcast rooftop site resource ID

        Returns:
            List of forecast periods or None on error
        """
        url = f"{self.SOLCAST_API_URL}/rooftop_sites/{resource_id}/forecasts"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        params = {"hours": 48, "format": "json"}

        async with self._session.get(url, headers=headers, params=params) as response:
            if response.status == 401:
                raise UpdateFailed("Solcast API authentication failed - check API key")
            if response.status == 429:
                _LOGGER.warning(f"Solcast API rate limit for resource {resource_id[:8]}...")
                return None
            if response.status != 200:
                _LOGGER.error(f"Solcast API error for resource {resource_id[:8]}: {response.status}")
                return None

            data = await response.json()
            return data.get("forecasts", [])

    async def _fetch_estimated_actuals_for_resource(self, resource_id: str) -> list[dict] | None:
        """Fetch estimated actuals (past production) for a single resource ID.

        Args:
            resource_id: Solcast rooftop site resource ID

        Returns:
            List of estimated actual periods or None on error
        """
        url = f"{self.SOLCAST_API_URL}/rooftop_sites/{resource_id}/estimated_actuals"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        # Get last 24 hours of estimated actuals (covers today's past production)
        params = {"hours": 24, "format": "json"}

        try:
            async with self._session.get(url, headers=headers, params=params) as response:
                if response.status == 401:
                    _LOGGER.warning("Solcast estimated_actuals auth failed")
                    return None
                if response.status == 429:
                    _LOGGER.warning(f"Solcast API rate limit for estimated_actuals {resource_id[:8]}...")
                    return None
                if response.status != 200:
                    _LOGGER.debug(f"Solcast estimated_actuals error for {resource_id[:8]}: {response.status}")
                    return None

                data = await response.json()
                return data.get("estimated_actuals", [])
        except Exception as e:
            _LOGGER.debug(f"Error fetching estimated_actuals: {e}")
            return None

    def _combine_forecasts(self, base: list[dict], additional: list[dict]) -> list[dict]:
        """Combine forecasts from multiple resources by summing pv_estimate values.

        Args:
            base: Base forecast list
            additional: Additional forecast list to add

        Returns:
            Combined forecast list with summed values
        """
        additional_lookup = {f.get("period_end"): f for f in additional}

        combined = []
        for forecast in base:
            period_end = forecast.get("period_end")
            result = dict(forecast)

            if period_end in additional_lookup:
                add_f = additional_lookup[period_end]
                if result.get("pv_estimate") is not None and add_f.get("pv_estimate") is not None:
                    result["pv_estimate"] = result["pv_estimate"] + add_f["pv_estimate"]
                if result.get("pv_estimate10") is not None and add_f.get("pv_estimate10") is not None:
                    result["pv_estimate10"] = result["pv_estimate10"] + add_f["pv_estimate10"]
                if result.get("pv_estimate90") is not None and add_f.get("pv_estimate90") is not None:
                    result["pv_estimate90"] = result["pv_estimate90"] + add_f["pv_estimate90"]

            combined.append(result)

        return combined

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast data from Solcast.

        First checks if the Solcast HA integration is installed - if so, reads from
        its sensors to avoid doubling API calls. Only makes direct API calls if the
        Solcast integration is not available.

        Supports multiple resource IDs - values are combined by summing.
        """
        # First, check if Solcast HA integration is installed and has data
        # This avoids doubling API calls if user has both integrations
        solcast_data = await self._try_read_from_solcast_integration()
        if solcast_data:
            _LOGGER.debug("Using data from Solcast HA integration (no API calls needed)")
            return solcast_data

        # Solcast integration not available - make our own API calls
        try:
            async with asyncio.timeout(60):  # Longer timeout for multiple API calls
                # Fetch forecasts from first resource
                forecasts = await self._fetch_forecast_for_resource(self._resource_ids[0])
                if not forecasts:
                    _LOGGER.warning("No forecasts from Solcast API")
                    return self.data or {"available": False}

                # Try to fetch estimated actuals (past production based on satellite data)
                # This is optional - if it fails, we'll use cached full-day forecast
                estimated_actuals = None
                try:
                    estimated_actuals = await self._fetch_estimated_actuals_for_resource(self._resource_ids[0])
                except Exception as e:
                    _LOGGER.debug(f"Could not fetch estimated_actuals: {e}")

                # If multiple resources, fetch and combine
                if len(self._resource_ids) > 1:
                    for resource_id in self._resource_ids[1:]:
                        additional_forecasts = await self._fetch_forecast_for_resource(resource_id)
                        if additional_forecasts:
                            forecasts = self._combine_forecasts(forecasts, additional_forecasts)
                        else:
                            _LOGGER.warning(f"Failed to fetch forecast from resource {resource_id[:8]}...")

                        if estimated_actuals:
                            try:
                                additional_actuals = await self._fetch_estimated_actuals_for_resource(resource_id)
                                if additional_actuals:
                                    estimated_actuals = self._combine_forecasts(estimated_actuals, additional_actuals)
                            except Exception:
                                pass

                    _LOGGER.info(f"Combined data from {len(self._resource_ids)} Solcast sites")

            if not forecasts:
                return {"available": False}

            # Calculate totals
            now = dt_util.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            tomorrow_end = today_end + timedelta(days=1)

            today_past = 0.0  # Production that already happened today (from estimated_actuals)
            today_remaining = 0.0  # Future production today (from forecasts)
            tomorrow_total = 0.0
            today_peak = 0.0
            tomorrow_peak = 0.0
            current_estimate = None
            period_hours = 0.5  # 30-minute periods

            # Sum up past production from estimated_actuals (today only)
            if estimated_actuals:
                for actual in estimated_actuals:
                    period_end_str = actual.get("period_end", "")
                    pv_estimate = actual.get("pv_estimate", 0) or 0

                    try:
                        period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                        period_end_local = dt_util.as_local(period_end)

                        # Only count today's past production
                        if today_start <= period_end_local <= now:
                            today_past += pv_estimate * period_hours
                            today_peak = max(today_peak, pv_estimate)
                    except (ValueError, TypeError):
                        pass

            # Sum up future production from forecasts
            for forecast in forecasts:
                period_end_str = forecast.get("period_end", "")
                pv_estimate = forecast.get("pv_estimate", 0) or 0

                try:
                    period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                    period_end_local = dt_util.as_local(period_end)

                    # Set current estimate to first forecast period
                    if current_estimate is None:
                        current_estimate = pv_estimate

                    if period_end_local <= today_end:
                        today_remaining += pv_estimate * period_hours
                        today_peak = max(today_peak, pv_estimate)
                    elif period_end_local <= tomorrow_end:
                        tomorrow_total += pv_estimate * period_hours
                        tomorrow_peak = max(tomorrow_peak, pv_estimate)

                except (ValueError, TypeError) as e:
                    _LOGGER.debug(f"Error parsing forecast period: {e}")

            # Full day calculation
            today_str = now.strftime("%Y-%m-%d")

            if today_past > 0:
                # We have estimated actuals - use actual + remaining
                today_forecast = today_past + today_remaining
                # Update cache with this more accurate value
                self._daily_forecast_date = today_str
                self._daily_forecast_kwh = today_forecast
                self._daily_forecast_peak_kw = today_peak
                _LOGGER.info(
                    f"Solcast forecast updated: Today total={today_forecast:.1f}kWh "
                    f"(past={today_past:.1f}kWh + remaining={today_remaining:.1f}kWh), "
                    f"peak={today_peak:.2f}kW, Tomorrow={tomorrow_total:.1f}kWh"
                )
            else:
                # No estimated actuals - use cached full-day or remaining as fallback
                if self._daily_forecast_date != today_str:
                    # New day - cache the current remaining as the full-day estimate
                    self._daily_forecast_date = today_str
                    self._daily_forecast_kwh = today_remaining
                    self._daily_forecast_peak_kw = today_peak
                    today_forecast = today_remaining
                    _LOGGER.info(
                        f"Solcast: New day, cached forecast for {today_str}: {today_remaining:.1f}kWh"
                    )
                else:
                    # Use cached value (from earlier fetch today)
                    today_forecast = self._daily_forecast_kwh or today_remaining
                    today_peak = self._daily_forecast_peak_kw or today_peak
                    _LOGGER.info(
                        f"Solcast forecast updated: Today={today_forecast:.1f}kWh (cached), "
                        f"remaining={today_remaining:.1f}kWh, Tomorrow={tomorrow_total:.1f}kWh"
                    )

            return {
                "available": True,
                "today_forecast_kwh": round(today_forecast, 2),  # Full day (actuals + forecast)
                "today_remaining_kwh": round(today_remaining, 2),  # Remaining from now
                "today_total_kwh": round(today_forecast, 2),  # Alias for backward compat
                "tomorrow_total_kwh": round(tomorrow_total, 2),
                "today_peak_kw": round(today_peak, 2),
                "tomorrow_peak_kw": round(tomorrow_peak, 2),
                "current_estimate_kw": round(current_estimate, 2) if current_estimate else None,
                "forecast_periods": len(forecasts),
                "last_update": dt_util.utcnow(),
            }

        except asyncio.TimeoutError as err:
            raise UpdateFailed("Timeout fetching Solcast forecast") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error fetching Solcast forecast: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching Solcast forecast: {err}") from err


class OctopusPriceCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Octopus Energy UK price data.

    Fetches half-hourly import and export rates from the Octopus Energy API.
    Converts to Amber-compatible format for use with existing tariff conversion.

    Key differences from Amber:
    - Prices in pence/kWh (not cents)
    - Prices include VAT (5%)
    - 30-minute intervals
    - Prices published daily after 4pm UK time for next day
    - Can go negative (you get paid to use electricity)
    - Price cap at 100p/kWh
    """

    def __init__(
        self,
        hass: HomeAssistant,
        product_code: str,
        tariff_code: str,
        gsp_region: str,
        export_product_code: str | None = None,
        export_tariff_code: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            product_code: Octopus product code (e.g., "AGILE-24-10-01")
            tariff_code: Full tariff code including region (e.g., "E-1R-AGILE-24-10-01-A")
            gsp_region: UK Grid Supply Point region code (e.g., "A")
            export_product_code: Optional export product code for Agile Outgoing/Flux
            export_tariff_code: Optional export tariff code
        """
        from .octopus_api import OctopusAPIClient

        self.product_code = product_code
        self.tariff_code = tariff_code
        self.gsp_region = gsp_region
        self.export_product_code = export_product_code
        self.export_tariff_code = export_tariff_code
        self.session = async_get_clientsession(hass)
        self._client = OctopusAPIClient(self.session)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_octopus_prices",
            update_interval=timedelta(minutes=30),  # Octopus updates less frequently than Amber
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Octopus API and convert to Amber-compatible format.

        Returns:
            dict with 'current', 'forecast', 'export_rates', and 'last_update'
            in Amber-compatible format for use with tariff conversion.
        """
        try:
            from datetime import timezone

            now = datetime.now(timezone.utc)

            # Fetch import rates for next 48 hours
            period_from = now - timedelta(hours=1)  # Include recent past
            period_to = now + timedelta(hours=48)

            import_rates = await self._client.get_current_rates(
                self.product_code,
                self.tariff_code,
                period_from=period_from,
                period_to=period_to,
                page_size=200,  # 48 hours = 96 periods, add buffer
            )

            if not import_rates:
                raise UpdateFailed(
                    f"No import rates returned from Octopus API for {self.tariff_code}"
                )

            # Fetch export rates if configured
            export_rates = []
            if self.export_product_code and self.export_tariff_code:
                export_rates = await self._client.get_export_rates(
                    self.export_product_code,
                    self.export_tariff_code,
                    period_from=period_from,
                    period_to=period_to,
                    page_size=200,
                )

            # Convert to Amber-compatible format
            current_prices = []
            forecast_prices = []

            for rate in import_rates:
                valid_from_str = rate.get("valid_from", "")
                valid_to_str = rate.get("valid_to", "")
                price_pence = rate.get("value_inc_vat", 0)

                if not valid_from_str or not valid_to_str:
                    continue

                # Parse timestamps
                try:
                    valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                    valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                # Determine interval type based on timing
                # Octopus uses valid_to as the interval end time (same convention as Amber's nemTime)
                if valid_from <= now < valid_to:
                    interval_type = "CurrentInterval"
                elif valid_to <= now:
                    interval_type = "ActualInterval"
                else:
                    interval_type = "ForecastInterval"

                # Build Amber-compatible price entry
                # Note: price_pence is in pence/kWh, which maps directly to cents for Tesla
                # (Tesla doesn't care about currency, just the numeric value)
                amber_entry = {
                    "nemTime": valid_to.isoformat(),  # Amber uses interval END time
                    "perKwh": price_pence,  # pence/kWh (treated as cents)
                    "channelType": "general",
                    "type": interval_type,
                    "duration": 30,  # 30-minute intervals
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                }

                if interval_type == "CurrentInterval":
                    current_prices.append(amber_entry)
                forecast_prices.append(amber_entry)

            # Process export rates if available
            export_forecast = []
            for rate in export_rates:
                valid_from_str = rate.get("valid_from", "")
                valid_to_str = rate.get("valid_to", "")
                price_pence = rate.get("value_inc_vat", 0)

                if not valid_from_str or not valid_to_str:
                    continue

                try:
                    valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                    valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if valid_from <= now < valid_to:
                    interval_type = "CurrentInterval"
                elif valid_to <= now:
                    interval_type = "ActualInterval"
                else:
                    interval_type = "ForecastInterval"

                # Export prices: Amber uses negative for "you get paid"
                # Octopus export rates are positive (payment to you)
                # Convert to Amber convention: negative = payment to you
                amber_entry = {
                    "nemTime": valid_to.isoformat(),
                    "perKwh": -price_pence,  # Negative = you get paid
                    "channelType": "feedIn",
                    "type": interval_type,
                    "duration": 30,
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                }

                if interval_type == "CurrentInterval":
                    current_prices.append(amber_entry)
                export_forecast.append(amber_entry)

            # If no export rates configured, create synthetic export prices
            # (typically 0 for non-export tariffs, or use SEG rates)
            if not export_rates:
                for rate in import_rates:
                    valid_from_str = rate.get("valid_from", "")
                    valid_to_str = rate.get("valid_to", "")

                    if not valid_from_str or not valid_to_str:
                        continue

                    try:
                        valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                        valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if valid_from <= now < valid_to:
                        interval_type = "CurrentInterval"
                    elif valid_to <= now:
                        interval_type = "ActualInterval"
                    else:
                        interval_type = "ForecastInterval"

                    # Default export rate: Smart Export Guarantee minimum (typically 4.1p)
                    # or 0 if tariff doesn't support export
                    default_export_pence = 4.1  # SEG minimum

                    amber_entry = {
                        "nemTime": valid_to.isoformat(),
                        "perKwh": -default_export_pence,  # Negative = you get paid
                        "channelType": "feedIn",
                        "type": interval_type,
                        "duration": 30,
                        "valid_from": valid_from.isoformat(),
                        "valid_to": valid_to.isoformat(),
                    }

                    if interval_type == "CurrentInterval":
                        current_prices.append(amber_entry)
                    export_forecast.append(amber_entry)

            # Combine import and export forecasts
            combined_forecast = forecast_prices + export_forecast

            # Log summary
            current_import = next(
                (p["perKwh"] for p in current_prices if p["channelType"] == "general"),
                None,
            )
            current_export = next(
                (p["perKwh"] for p in current_prices if p["channelType"] == "feedIn"),
                None,
            )

            _LOGGER.info(
                "Octopus API data for %s: current_import=%.2fp/kWh, current_export=%.2fp/kWh, "
                "forecast_periods=%d (import=%d, export=%d)",
                self.tariff_code,
                current_import or 0,
                -(current_export or 0),  # Un-negate for display
                len(combined_forecast),
                len(forecast_prices),
                len(export_forecast),
            )

            return {
                "current": current_prices,
                "forecast": combined_forecast,
                "export_rates": export_forecast,
                "last_update": dt_util.utcnow(),
                "source": "octopus_api",
                "product_code": self.product_code,
                "tariff_code": self.tariff_code,
                "gsp_region": self.gsp_region,
            }

        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching Octopus data: {err}") from err
