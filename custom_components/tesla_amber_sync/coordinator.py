"""Data update coordinators for Tesla Sync with improved error handling."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
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
)

_LOGGER = logging.getLogger(__name__)


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
                current_prices = self.ws_client.get_latest_prices(max_age_seconds=360)
                if current_prices:
                    _LOGGER.debug("Using WebSocket prices (fresh)")
                else:
                    _LOGGER.debug("WebSocket prices unavailable or stale, falling back to REST API")

            # Fall back to REST API if WebSocket unavailable
            if not current_prices:
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
    ) -> None:
        """Initialize the coordinator."""
        self.site_id = site_id
        self.api_token = api_token
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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tesla API (Teslemetry or Fleet API)."""
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
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

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
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
