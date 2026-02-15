"""Data update coordinators for PowerSync with improved error handling."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
import logging
import re
from typing import Any
import asyncio

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
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


class EnergyAccumulator:
    """Accumulates daily energy totals from instantaneous power readings.

    Integrates power (kW) over time to estimate daily energy (kWh).
    Resets at local midnight. Data is approximate and lost on restart.
    """

    def __init__(self) -> None:
        self._last_update: datetime | None = None
        self._last_date: Any = None
        self.solar_kwh: float = 0.0
        self.grid_import_kwh: float = 0.0
        self.grid_export_kwh: float = 0.0
        self.battery_charge_kwh: float = 0.0
        self.battery_discharge_kwh: float = 0.0
        self.load_kwh: float = 0.0

    def update(
        self,
        solar_kw: float,
        grid_kw: float,
        battery_kw: float,
        load_kw: float,
    ) -> None:
        """Update accumulators with current power readings.

        Sign conventions (standard PowerSync format):
            solar_kw: always >= 0
            grid_kw: positive = importing, negative = exporting
            battery_kw: positive = discharging, negative = charging
            load_kw: always >= 0
        """
        now = dt_util.now()  # Local time for midnight reset

        # Reset at local midnight
        if self._last_date is not None and now.date() != self._last_date:
            _LOGGER.info(
                "Energy accumulator midnight reset: solar=%.2f grid_in=%.2f grid_out=%.2f "
                "charge=%.2f discharge=%.2f load=%.2f kWh",
                self.solar_kwh, self.grid_import_kwh, self.grid_export_kwh,
                self.battery_charge_kwh, self.battery_discharge_kwh, self.load_kwh,
            )
            self.solar_kwh = 0.0
            self.grid_import_kwh = 0.0
            self.grid_export_kwh = 0.0
            self.battery_charge_kwh = 0.0
            self.battery_discharge_kwh = 0.0
            self.load_kwh = 0.0

        # Integrate power × time
        if self._last_update is not None:
            delta_h = (now - self._last_update).total_seconds() / 3600
            if 0 < delta_h < 0.1:  # Sanity: skip if > 6 min gap (stale/restart)
                self.solar_kwh += max(0, solar_kw) * delta_h
                self.grid_import_kwh += max(0, grid_kw) * delta_h
                self.grid_export_kwh += max(0, -grid_kw) * delta_h
                self.battery_charge_kwh += max(0, -battery_kw) * delta_h
                self.battery_discharge_kwh += max(0, battery_kw) * delta_h
                self.load_kwh += max(0, load_kw) * delta_h

        self._last_update = now
        self._last_date = now.date()

    def as_dict(self) -> dict[str, float]:
        """Return accumulated totals as a dict for energy_summary."""
        return {
            "pv_today_kwh": round(self.solar_kwh, 3),
            "grid_import_today_kwh": round(self.grid_import_kwh, 3),
            "grid_export_today_kwh": round(self.grid_export_kwh, 3),
            "charge_today_kwh": round(self.battery_charge_kwh, 3),
            "discharge_today_kwh": round(self.battery_discharge_kwh, 3),
            "load_today_kwh": round(self.load_kwh, 3),
        }


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


def _parse_retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Parse Retry-After header from an HTTP response.

    Returns delay in seconds, or None if header is missing/invalid.
    Supports both delta-seconds and HTTP-date formats.
    """
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        # Try delta-seconds first (e.g. "30")
        return max(1.0, min(float(retry_after), 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        pass
    try:
        # Try HTTP-date format (e.g. "Tue, 11 Feb 2026 03:00:00 GMT")
        from email.utils import parsedate_to_datetime
        retry_date = parsedate_to_datetime(retry_after)
        from homeassistant.util import dt as dt_util
        delay = (retry_date - dt_util.utcnow()).total_seconds()
        return max(1.0, min(delay, 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        return None


async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    max_retries: int = 3,
    timeout_seconds: int = 60,
    **kwargs
) -> dict[str, Any]:
    """Fetch data with exponential backoff retry logic.

    Respects Retry-After headers from 429/503 responses. Retries on
    5xx server errors and 429 rate limits; fails immediately on other 4xx.

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
    retry_after_delay = None  # Set by Retry-After header

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                # Use Retry-After delay if available, otherwise exponential backoff
                wait_time = retry_after_delay or (2 ** attempt)
                retry_after_delay = None  # Reset for next attempt
                _LOGGER.info(
                    "Retry attempt %d/%d after %.0fs delay",
                    attempt + 1, max_retries, wait_time,
                )
                await asyncio.sleep(wait_time)

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **kwargs
            ) as response:
                if response.status == 200:
                    return await response.json()

                error_text = await response.text()

                if response.status == 429:
                    # Rate limited — retry with Retry-After if provided
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Rate limited 429 (attempt %d/%d): %s (retry-after: %s)",
                        attempt + 1, max_retries, error_text[:200],
                        f"{retry_after_delay:.0f}s" if retry_after_delay else "not set",
                    )
                    last_error = UpdateFailed(f"Rate limited: 429")
                    continue

                if response.status >= 500:
                    # Server error — retry, respect Retry-After if present
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Server error (attempt %d/%d): %s - %s",
                        attempt + 1, max_retries, response.status, error_text[:200],
                    )
                    last_error = UpdateFailed(f"Server error: {response.status}")
                    continue

                # Other 4xx client errors — don't retry
                raise UpdateFailed(f"Client error {response.status}: {error_text}")

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Network error (attempt %d/%d): %s",
                attempt + 1, max_retries, err,
            )
            last_error = UpdateFailed(f"Network error: {err}")
            continue

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout error (attempt %d/%d): Request exceeded %ds",
                attempt + 1, max_retries, timeout_seconds,
            )
            last_error = UpdateFailed(f"Timeout after {timeout_seconds}s")
            continue

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


# ============================================================
# Amber Usage API — actual metered cost data from NEM
# ============================================================

USAGE_FETCH_INTERVAL = timedelta(hours=4)
USAGE_STORAGE_VERSION = 1
USAGE_STORAGE_KEY = "power_sync.amber_usage"
USAGE_MAX_DAYS = 365
AMBER_DEFAULT_MONTHLY_SUPPLY_FEE = 25.0  # Amber's standard $25/month supply charge

# Quality ranking for deciding whether to overwrite existing data
_QUALITY_RANK = {"estimated": 0, "mixed": 1, "billable": 2}


@dataclass
class DayUsage:
    """Actual metered usage and cost for a single day from Amber."""

    date: str                   # "YYYY-MM-DD"
    import_kwh: float           # general channel total
    export_kwh: float           # feedIn channel (absolute)
    controlled_load_kwh: float
    import_cost: float          # $ gross import
    export_earnings: float      # $ gross export earnings
    net_cost: float             # import_cost - export_earnings
    quality: str                # "estimated", "billable", or "mixed"


class AmberUsageCoordinator:
    """Fetches actual metered usage/cost from the Amber Usage API.

    Not a DataUpdateCoordinator — usage data updates infrequently (every 4h).
    Uses HA Store for persistence across restarts.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_token: str,
        site_id: str,
        entry_id: str,
        monthly_supply_fee: float = AMBER_DEFAULT_MONTHLY_SUPPLY_FEE,
    ) -> None:
        """Initialize the Amber usage coordinator."""
        self.hass = hass
        self._api_token = api_token
        self._site_id = site_id
        self._entry_id = entry_id
        self._monthly_supply_fee = monthly_supply_fee
        self._session = async_get_clientsession(hass)
        self._store = Store(hass, USAGE_STORAGE_VERSION, f"{USAGE_STORAGE_KEY}.{entry_id}")

        # In-memory state
        self._days: dict[str, DayUsage] = {}
        self._baselines: dict[str, float] = {}  # date → baseline_cost from optimizer
        self._last_fetch: datetime | None = None
        self._cancel_timer: Any = None
        self._cancel_initial: Any = None

    @property
    def last_fetch_iso(self) -> str | None:
        """Return the last fetch time as ISO string."""
        return self._last_fetch.isoformat() if self._last_fetch else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Load stored data and schedule periodic fetches."""
        await self._load_store()
        # Delay initial fetch 30-90s to avoid competing with price coordinator
        # at startup for Amber API rate limit budget
        import random
        delay = 30 + random.randint(0, 60)
        _LOGGER.info("Amber usage: first fetch in %ds (avoiding startup rate limit contention)", delay)
        self._cancel_initial = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self._fetch_usage())
        )
        from homeassistant.helpers.event import async_track_time_interval
        self._cancel_timer = async_track_time_interval(
            self.hass, self._scheduled_fetch, USAGE_FETCH_INTERVAL
        )

    async def async_stop(self) -> None:
        """Cancel the periodic timer and any pending initial fetch."""
        if self._cancel_initial:
            self._cancel_initial.cancel()
            self._cancel_initial = None
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

    async def _scheduled_fetch(self, _now=None) -> None:
        """Timer callback for periodic fetch."""
        await self._fetch_usage()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _load_store(self) -> None:
        """Load persisted usage data from HA Store."""
        stored = await self._store.async_load()
        if not stored:
            return
        for day_dict in stored.get("days", []):
            try:
                du = DayUsage(**day_dict)
                self._days[du.date] = du
            except (TypeError, KeyError):
                continue
        self._baselines = stored.get("baselines", {})
        last_ts = stored.get("last_fetch")
        if last_ts:
            try:
                self._last_fetch = datetime.fromisoformat(last_ts)
            except (ValueError, TypeError):
                pass
        _LOGGER.info("Amber usage: restored %d days from store", len(self._days))

    def _save_store(self) -> None:
        """Persist current data to HA Store (delayed write)."""
        data = {
            "days": [asdict(du) for du in self._days.values()],
            "baselines": self._baselines,
            "last_fetch": self._last_fetch.isoformat() if self._last_fetch else None,
        }
        self._store.async_delay_save(lambda: data, 60)

    # ------------------------------------------------------------------
    # API fetch
    # ------------------------------------------------------------------

    async def _fetch_usage(self) -> None:
        """Fetch usage data from Amber API.

        Uses _fetch_with_retry for consistent 429/retry handling with the
        price coordinator. Checks RateLimit-Remaining header proactively
        and skips the fetch if the budget is low, to avoid starving the
        more important real-time price fetches.

        Amber Usage API has a 7-day max range per request, so large
        back-fills are batched into 7-day chunks.
        """
        now = dt_util.now()
        today = now.date()

        # Determine date range
        if not self._days:
            # First run — fetch 90 days of history
            start_date = today - timedelta(days=90)
        else:
            # Subsequent runs — re-fetch last 3 days for quality upgrades
            start_date = today - timedelta(days=3)

        end_date = today

        headers = {"Authorization": f"Bearer {self._api_token}"}

        # Pre-flight: probe rate limit budget with a lightweight check.
        # If RateLimit-Remaining is low, skip this non-critical fetch
        # to preserve budget for the real-time price coordinator.
        try:
            async with self._session.get(
                f"{AMBER_API_BASE_URL}/sites/{self._site_id}/prices/current",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as probe_resp:
                remaining = probe_resp.headers.get("RateLimit-Remaining")
                if remaining is not None:
                    try:
                        remaining_int = int(remaining)
                        if remaining_int < 10:
                            _LOGGER.info(
                                "Amber usage: skipping fetch — only %d API calls remaining "
                                "(preserving budget for price updates)",
                                remaining_int,
                            )
                            return
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass  # Probe failed — proceed with fetch anyway

        # Amber Usage API allows max 7-day range per request — batch accordingly
        total_updated = 0
        chunk_start = start_date
        url = f"{AMBER_API_BASE_URL}/sites/{self._site_id}/usage"

        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=6), end_date)
            params = {
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
                "resolution": "30",
            }

            try:
                intervals = await _fetch_with_retry(
                    self._session,
                    url,
                    headers,
                    max_retries=2,
                    timeout_seconds=30,
                    params=params,
                )
                updated = self._process_intervals(intervals)
                total_updated += updated
                _LOGGER.debug(
                    "Amber usage chunk %s to %s: %d days updated",
                    chunk_start, chunk_end, updated,
                )
            except UpdateFailed as err:
                _LOGGER.warning("Amber usage fetch failed for %s to %s: %s", chunk_start, chunk_end, err)
            except Exception as err:
                _LOGGER.warning("Amber usage fetch failed unexpectedly for %s to %s: %s", chunk_start, chunk_end, err)

            chunk_start = chunk_end + timedelta(days=1)

        self._last_fetch = now
        self._prune_old_days()
        self._save_store()
        _LOGGER.info("Amber usage fetched: %d days updated (range %s to %s)", total_updated, start_date, end_date)

    def _process_intervals(self, intervals: list[dict]) -> int:
        """Aggregate 30-min intervals into daily DayUsage records.

        Returns count of days updated.
        """
        # Group by date and channel
        day_buckets: dict[str, dict[str, list[dict]]] = {}
        for iv in intervals:
            dt_str = iv.get("nemTime") or iv.get("startTime") or ""
            try:
                day_key = dt_str[:10]  # "YYYY-MM-DD"
                # Validate it's a real date
                date.fromisoformat(day_key)
            except (ValueError, IndexError):
                continue
            channel = iv.get("channelType", "general")
            day_buckets.setdefault(day_key, {}).setdefault(channel, []).append(iv)

        updated = 0
        for day_key, channels in day_buckets.items():
            import_kwh = 0.0
            export_kwh = 0.0
            controlled_kwh = 0.0
            import_cost = 0.0
            export_earnings = 0.0
            qualities: set[str] = set()

            for iv in channels.get("general", []):
                kwh = abs(iv.get("kwh", 0))
                import_kwh += kwh
                import_cost += iv.get("cost", 0)
                qualities.add(iv.get("quality", "estimated"))

            for iv in channels.get("feedIn", []):
                kwh = abs(iv.get("kwh", 0))
                export_kwh += kwh
                # Amber cost for feedIn is negative when earning
                export_earnings += abs(iv.get("cost", 0))
                qualities.add(iv.get("quality", "estimated"))

            for iv in channels.get("controlledLoad", []):
                kwh = abs(iv.get("kwh", 0))
                controlled_kwh += kwh
                import_cost += iv.get("cost", 0)
                qualities.add(iv.get("quality", "estimated"))

            if "billable" in qualities and "estimated" in qualities:
                quality = "mixed"
            elif "billable" in qualities:
                quality = "billable"
            else:
                quality = "estimated"

            new_du = DayUsage(
                date=day_key,
                import_kwh=round(import_kwh, 3),
                export_kwh=round(export_kwh, 3),
                controlled_load_kwh=round(controlled_kwh, 3),
                import_cost=round(import_cost, 4),
                export_earnings=round(export_earnings, 4),
                net_cost=round(import_cost - export_earnings, 4),
                quality=quality,
            )

            # Only overwrite if new data is same or better quality
            existing = self._days.get(day_key)
            if existing:
                existing_rank = _QUALITY_RANK.get(existing.quality, 0)
                new_rank = _QUALITY_RANK.get(quality, 0)
                if new_rank < existing_rank:
                    continue  # Don't downgrade quality

            self._days[day_key] = new_du
            updated += 1

        return updated

    def _prune_old_days(self) -> None:
        """Remove days older than USAGE_MAX_DAYS to limit storage."""
        cutoff = (dt_util.now().date() - timedelta(days=USAGE_MAX_DAYS)).isoformat()
        old_keys = [k for k in self._days if k < cutoff]
        for k in old_keys:
            del self._days[k]
        # Also prune baselines
        old_baselines = [k for k in self._baselines if k < cutoff]
        for k in old_baselines:
            del self._baselines[k]

    # ------------------------------------------------------------------
    # Baseline recording (called from optimization coordinator at midnight)
    # ------------------------------------------------------------------

    def record_baseline(self, date_str: str, baseline_cost: float) -> None:
        """Record the optimizer's baseline cost for a completed day."""
        self._baselines[date_str] = round(baseline_cost, 4)
        self._save_store()
        _LOGGER.info("Amber usage: recorded baseline $%.2f for %s", baseline_cost, date_str)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def get_summary(self, period: str) -> dict[str, Any]:
        """Get aggregated usage for a period.

        period: 'yesterday', 'week' (last 7 complete days), 'month' (calendar month to yesterday), 'last_month'
        """
        days = self._get_days_for_period(period)
        return self._aggregate(days)

    def get_savings_summary(self, period: str) -> dict[str, Any]:
        """Get aggregated usage with baseline and savings for a period."""
        days = self._get_days_for_period(period)
        result = self._aggregate(days)

        # Add baseline and savings.
        # Savings = baseline_energy - actual_energy (supply charge excluded
        # from savings calc since it's a fixed cost with or without battery).
        # Baseline includes supply charge so it reflects true "no battery" cost.
        baseline_total = 0.0
        baseline_days = 0
        supply_total = sum(self._daily_supply_fee(du.date) for du in days)
        for du in days:
            bl = self._baselines.get(du.date)
            if bl is not None:
                baseline_total += bl
                baseline_days += 1

        result["baseline_cost"] = round(baseline_total + supply_total, 2) if baseline_days > 0 else None
        result["savings"] = round(baseline_total - (result["net_cost"] - result["supply_charge"]), 2) if baseline_days > 0 else None
        result["baseline_days"] = baseline_days
        return result

    def get_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Get day-by-day data for a custom date range."""
        result = []
        for day_key in sorted(self._days.keys()):
            if start_date <= day_key <= end_date:
                du = self._days[day_key]
                d = asdict(du)
                daily_fee = self._daily_supply_fee(day_key)
                d["supply_charge"] = round(daily_fee, 2)
                d["net_cost"] = round(du.net_cost + daily_fee, 2)
                bl = self._baselines.get(day_key)
                d["baseline_cost"] = bl
                d["savings"] = round(bl - d["net_cost"], 2) if bl is not None else None
                result.append(d)
        return result

    def _get_days_for_period(self, period: str) -> list[DayUsage]:
        """Return list of DayUsage records for the given period."""
        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        if period == "yesterday":
            key = yesterday.isoformat()
            du = self._days.get(key)
            return [du] if du else []
        elif period == "week":
            start = (today - timedelta(days=7)).isoformat()
            end = yesterday.isoformat()
        elif period == "month":
            start = today.replace(day=1).isoformat()
            end = yesterday.isoformat()
        elif period == "last_month":
            first_this_month = today.replace(day=1)
            last_day_prev = first_this_month - timedelta(days=1)
            start = last_day_prev.replace(day=1).isoformat()
            end = last_day_prev.isoformat()
        else:
            return []

        return [
            self._days[k] for k in sorted(self._days.keys())
            if start <= k <= end
        ]

    def _daily_supply_fee(self, date_str: str) -> float:
        """Calculate the daily supply fee for a given date.

        Pro-rates the monthly fee by the actual number of days in that month
        so monthly totals always sum to exactly the monthly fee.
        """
        if self._monthly_supply_fee <= 0:
            return 0.0
        import calendar
        try:
            d = date.fromisoformat(date_str)
            days_in_month = calendar.monthrange(d.year, d.month)[1]
            return self._monthly_supply_fee / days_in_month
        except (ValueError, TypeError):
            return self._monthly_supply_fee / 30.0

    def _aggregate(self, days: list[DayUsage]) -> dict[str, Any]:
        """Aggregate a list of DayUsage into a summary dict.

        Includes the daily supply fee (pro-rated from monthly) in the totals.
        """
        if not days:
            return {
                "import_kwh": 0,
                "export_kwh": 0,
                "controlled_load_kwh": 0,
                "import_cost": 0,
                "export_earnings": 0,
                "supply_charge": 0,
                "net_cost": 0,
                "quality": "no_data",
                "days_count": 0,
            }
        qualities = set(du.quality for du in days)
        if len(qualities) == 1:
            quality = qualities.pop()
        elif "billable" in qualities and "estimated" in qualities:
            quality = "mixed"
        else:
            quality = "mixed"

        energy_cost = sum(du.net_cost for du in days)
        supply_charge = sum(self._daily_supply_fee(du.date) for du in days)

        return {
            "import_kwh": round(sum(du.import_kwh for du in days), 2),
            "export_kwh": round(sum(du.export_kwh for du in days), 2),
            "controlled_load_kwh": round(sum(du.controlled_load_kwh for du in days), 2),
            "import_cost": round(sum(du.import_cost for du in days), 2),
            "export_earnings": round(sum(du.export_earnings for du in days), 2),
            "supply_charge": round(supply_charge, 2),
            "net_cost": round(energy_cost + supply_charge, 2),
            "quality": quality,
            "days_count": len(days),
        }


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

            # Extract EV charging power from Tesla Wall Connectors
            ev_power_kw = 0.0
            wall_connectors_raw = live_status.get("wall_connectors")
            if wall_connectors_raw:
                try:
                    # wall_connectors can be a JSON string or a list
                    if isinstance(wall_connectors_raw, str):
                        import ast
                        wall_connectors = ast.literal_eval(wall_connectors_raw)
                    else:
                        wall_connectors = wall_connectors_raw
                    for wc in wall_connectors:
                        wc_power = wc.get("wall_connector_power", 0) or 0
                        if wc_power > 0:
                            ev_power_kw += wc_power / 1000
                except Exception:
                    pass

            # Map Teslemetry API response to our data structure
            energy_data = {
                "solar_power": live_status.get("solar_power", 0) / 1000,  # Convert W to kW
                "grid_power": live_status.get("grid_power", 0) / 1000,
                "battery_power": live_status.get("battery_power", 0) / 1000,
                "load_power": (live_status.get("load_power", 0) / 1000) - ev_power_kw,
                "battery_level": live_status.get("percentage_charged", 0),
                "ev_power": ev_power_kw,
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

            # Log battery capacity info for debugging
            _LOGGER.debug(f"Site info keys: {list(site_info.keys())}")
            components = site_info.get("components", {})
            if components:
                _LOGGER.debug(f"Site info components keys: {list(components.keys())}")
                # Log battery-related fields
                battery_fields = {k: v for k, v in site_info.items()
                                 if 'battery' in k.lower() or 'pack' in k.lower() or 'energy' in k.lower() or 'power' in k.lower()}
                if battery_fields:
                    _LOGGER.debug(f"Site info battery fields: {battery_fields}")
                component_battery = {k: v for k, v in components.items()
                                    if 'battery' in k.lower() or 'nameplate' in k.lower()}
                if component_battery:
                    _LOGGER.debug(f"Components battery fields: {component_battery}")

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
        energy_coordinator: DataUpdateCoordinator,
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
        self.tesla_coordinator = energy_coordinator
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

        # Get current grid power from energy coordinator (Tesla, FoxESS, Sigenergy, or Sungrow)
        energy_data = self.tesla_coordinator.data or {}
        grid_power_kw = energy_data.get("grid_power", 0.0)

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


class SungrowEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Sungrow SH-series battery system data via Modbus.

    Polls the Sungrow hybrid inverter via Modbus TCP to get real-time
    power data (solar, battery, grid, load), battery SOC/SOH, and control settings.
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
            host: IP address of Sungrow inverter
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
        """
        from .inverters.sungrow_sh import SungrowSHController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._controller = SungrowSHController(host, port, slave_id)
        self._energy_acc = EnergyAccumulator()

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sungrow_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Sungrow system via Modbus."""
        try:
            data = await self._controller.get_battery_data()

            # Map Sungrow data to standard format
            battery_power_w = data.get("battery_power", 0)  # Signed: positive = discharging
            export_power_w = data.get("export_power", 0)  # Signed: positive = exporting
            load_power_w = data.get("load_power", 0)

            # Convert to kW for consistency with other coordinators
            battery_kw = battery_power_w / 1000
            grid_kw = -export_power_w / 1000  # Invert: positive = importing, negative = exporting
            load_kw = load_power_w / 1000

            # Estimate solar from energy balance: Solar = Load + Export - Battery_Discharge
            # If battery charging (negative power), Solar = Load + Export + Battery_Charge
            solar_kw = load_kw - grid_kw - battery_kw

            # Accumulate daily energy from power readings
            self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw)

            energy_data = {
                "solar_power": max(0, solar_kw),  # kW, clamp to 0 if calculated negative
                "grid_power": grid_kw,  # kW, positive = importing, negative = exporting
                "battery_power": battery_kw,  # kW, positive = discharging, negative = charging
                "load_power": load_kw,  # kW
                "battery_level": data.get("battery_soc", 0),  # %
                "last_update": dt_util.utcnow(),
                # Sungrow-specific data
                "battery_soh": data.get("battery_soh"),  # % State of Health
                "battery_voltage": data.get("battery_voltage"),
                "battery_current": data.get("battery_current"),
                "battery_temp": data.get("battery_temp"),
                "ems_mode": data.get("ems_mode"),
                "ems_mode_name": data.get("ems_mode_name"),
                "charge_cmd": data.get("charge_cmd"),
                "min_soc": data.get("min_soc"),
                "max_soc": data.get("max_soc"),
                "backup_reserve": data.get("backup_reserve"),
                "charge_rate_limit_kw": data.get("charge_rate_limit_kw"),
                "discharge_rate_limit_kw": data.get("discharge_rate_limit_kw"),
                "export_limit_w": data.get("export_limit_w"),
                "export_limit_enabled": data.get("export_limit_enabled"),
                "energy_summary": self._energy_acc.as_dict(),
            }

            _LOGGER.debug(
                "Sungrow data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching Sungrow energy data: {err}") from err

    # Battery control methods - delegate to controller
    async def force_charge(self, duration_minutes: int = 30) -> bool:
        """Set Sungrow to forced charge mode.

        Args:
            duration_minutes: Duration in minutes (not used by Sungrow - charge until manually stopped)

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.force_charge()

    async def force_discharge(self, duration_minutes: int = 30) -> bool:
        """Set Sungrow to forced discharge mode.

        Args:
            duration_minutes: Duration in minutes (not used by Sungrow - discharge until manually stopped)

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.force_discharge()

    async def restore_normal(self) -> bool:
        """Restore Sungrow to self-consumption mode.

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve percentage.

        Args:
            percent: Backup reserve SOC percentage (0-100)

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.set_backup_reserve(percent)

    async def set_charge_rate_limit(self, kw: float) -> bool:
        """Set maximum charge rate in kW.

        Args:
            kw: Maximum charge rate in kW

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.set_charge_rate_limit(kw)

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        """Set maximum discharge rate in kW.

        Args:
            kw: Maximum discharge rate in kW

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.set_discharge_rate_limit(kw)

    async def set_export_limit(self, watts: int | None) -> bool:
        """Set export power limit in watts.

        Args:
            watts: Export limit in watts, or None to disable

        Returns:
            True if successful
        """
        async with self._controller:
            return await self._controller.set_export_limit(watts)

    async def async_shutdown(self) -> None:
        """Disconnect from Sungrow system on shutdown."""
        await self._controller.disconnect()


class FoxESSEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch FoxESS battery system data via Modbus.

    Polls the FoxESS inverter via Modbus TCP or RS485 to get real-time
    power data (solar, battery, grid, load), battery SOC, and control settings.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 247,
        connection_type: str = "tcp",
        serial_port: str | None = None,
        baudrate: int = 9600,
        model_family: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        from .inverters.foxess import FoxESSController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._controller = FoxESSController(
            host=host,
            port=port,
            slave_id=slave_id,
            connection_type=connection_type,
            serial_port=serial_port,
            baudrate=baudrate,
            model_family=model_family,
        )

        self._energy_acc = EnergyAccumulator()

        super().__init__(
            hass,
            _LOGGER,
            name="FoxESS Energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from FoxESS system via Modbus."""
        try:
            async with self._controller:
                status = await self._controller.get_status()
                energy_summary = await self._controller.get_energy_summary()

            if not status.attributes:
                raise UpdateFailed("No data from FoxESS controller")

            attrs = status.attributes

            # Map to standard format (convention: positive = discharging, negative = charging)
            battery_kw = attrs.get("battery_power_kw", 0) or 0
            grid_kw = attrs.get("grid_power_kw", 0) or 0
            load_kw = attrs.get("load_power_kw", 0) or 0
            solar_kw = attrs.get("pv_power_kw", 0) or 0
            ct2_kw = attrs.get("ct2_power_kw", 0) or 0

            # Total solar = DC PV strings + AC-coupled CT2 meter
            total_solar_kw = solar_kw + max(0, ct2_kw)

            # Accumulate daily energy from power readings
            self._energy_acc.update(total_solar_kw, grid_kw, battery_kw, load_kw)

            # Merge Modbus energy registers (charge/discharge) with accumulated values
            acc = self._energy_acc.as_dict()
            if energy_summary:
                # Prefer Modbus registers for charge/discharge (more accurate)
                acc["charge_today_kwh"] = energy_summary.get("charge_today_kwh", acc["charge_today_kwh"])
                acc["discharge_today_kwh"] = energy_summary.get("discharge_today_kwh", acc["discharge_today_kwh"])

            energy_data = {
                "solar_power": max(0, total_solar_kw),
                "ct2_power": ct2_kw,
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "battery_level": attrs.get("battery_soc", 0),
                "last_update": dt_util.utcnow(),
                # FoxESS-specific data
                "work_mode": attrs.get("work_mode"),
                "work_mode_name": attrs.get("work_mode_name"),
                "min_soc": attrs.get("min_soc"),
                "max_charge_current_a": attrs.get("max_charge_current_a"),
                "max_discharge_current_a": attrs.get("max_discharge_current_a"),
                "model_family": attrs.get("model_family"),
                "energy_summary": acc,
            }

            _LOGGER.debug(
                "FoxESS data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW, mode=%s",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
                energy_data.get("work_mode_name", "?"),
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching FoxESS energy data: {err}") from err

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set FoxESS to force charge mode.

        Args:
            duration_minutes: How long to charge
            power_w: Charge power in watts. If 0, reads max_charge_current from
                     the inverter and uses that (respects user's FoxESS app setting).
        """
        async with self._controller:
            if power_w <= 0 and self.data:
                # Use inverter's configured max charge current (set via FoxESS app)
                max_charge_a = self.data.get("max_charge_current_a")
                if max_charge_a and max_charge_a > 0:
                    power_w = max_charge_a * 300  # Conservative voltage estimate
                    _LOGGER.info("FoxESS force_charge using inverter max: %.0fA → %.0fW", max_charge_a, power_w)
            if power_w <= 0:
                power_w = 5000  # Fallback default
            return await self._controller.force_charge(duration_minutes, power_w=power_w)

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set FoxESS to force discharge mode.

        Args:
            duration_minutes: How long to discharge
            power_w: Discharge power in watts. If 0, reads max_discharge_current from
                     the inverter and uses that (respects user's FoxESS app setting).
        """
        async with self._controller:
            if power_w <= 0 and self.data:
                # Use inverter's configured max discharge current (set via FoxESS app)
                max_discharge_a = self.data.get("max_discharge_current_a")
                if max_discharge_a and max_discharge_a > 0:
                    power_w = max_discharge_a * 300  # Conservative voltage estimate
                    _LOGGER.info("FoxESS force_discharge using inverter max: %.0fA → %.0fW", max_discharge_a, power_w)
            if power_w <= 0:
                power_w = 5000  # Fallback default
            return await self._controller.force_discharge(duration_minutes, power_w=power_w)

    async def restore_normal(self) -> bool:
        """Restore FoxESS to normal (Self Use) operation."""
        async with self._controller:
            return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum SOC (backup reserve)."""
        async with self._controller:
            return await self._controller.set_backup_reserve(percent)

    async def set_work_mode(self, mode: int) -> bool:
        """Set FoxESS work mode."""
        async with self._controller:
            return await self._controller.set_work_mode(mode)

    async def set_charge_rate_limit(self, amps: float) -> bool:
        """Set maximum charge current in amps."""
        async with self._controller:
            return await self._controller.set_charge_rate_limit(amps)

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        """Set maximum discharge current in amps."""
        async with self._controller:
            return await self._controller.set_discharge_rate_limit(amps)

    async def async_shutdown(self) -> None:
        """Disconnect from FoxESS system on shutdown."""
        await self._controller.disconnect()


class SolcastForecastCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Solcast solar production forecasts.

    Fetches PV power forecasts from Solcast API and caches them locally.
    Dynamically adjusts update interval based on number of resource IDs to stay
    within Solcast's 10 calls/day hobbyist tier limit.

    Supports multiple resource IDs for split arrays (e.g., east/west facing panels).
    Provide comma-separated resource IDs and forecasts will be combined by summing values.
    """

    # Solcast API base URL
    SOLCAST_API_URL = "https://api.solcast.com.au"

    # Solcast hobbyist tier: 10 API calls per day
    DAILY_API_LIMIT = 10

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

        # Rate limiting tracking
        self._rate_limited = False
        self._last_rate_limit_time: datetime | None = None
        self._api_calls_today = 0
        self._api_calls_date: str | None = None

        # Calculate update interval based on number of resources
        # Each resource requires 1 API call per update
        # With 10 calls/day limit: interval = 24 / (10 / n_resources) hours
        n_resources = len(self._resource_ids)
        calls_per_update = n_resources  # We skip estimated_actuals to save calls
        max_updates_per_day = self.DAILY_API_LIMIT // calls_per_update
        # Leave some buffer - aim for 80% of max to avoid hitting limit
        safe_updates = max(1, int(max_updates_per_day * 0.8))
        update_hours = max(3, 24 // safe_updates)  # Minimum 3 hours

        self._update_interval = timedelta(hours=update_hours)

        _LOGGER.info(
            f"Solcast coordinator: {n_resources} resource(s), "
            f"{calls_per_update} API call(s)/update, "
            f"update interval: {update_hours}h ({safe_updates} updates/day)"
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solcast_forecast",
            update_interval=self._update_interval,
        )

    def _find_solcast_sensor(self, patterns: list[str]) -> Any | None:
        """Find a Solcast sensor by trying multiple possible entity ID patterns."""
        for pattern in patterns:
            state = self.hass.states.get(pattern)
            if state and state.state not in ("unavailable", "unknown", None, ""):
                return state
        return None

    async def _try_read_from_solcast_integration(self) -> dict[str, Any] | None:
        """Try to read forecast data from the Solcast HA integration.

        If the Solcast integration is installed, we read from its sensors instead
        of making our own API calls. This avoids doubling API usage (10 calls/day limit).

        Supports multiple naming conventions:
        - sensor.solcast_pv_forecast_* (current Solcast integration)
        - sensor.solcast_forecast_* (alternative naming)
        - sensor.solcast_* (older versions)

        Returns:
            Forecast data dict if Solcast integration is available, None otherwise
        """
        try:
            # Try multiple possible sensor names for today's forecast
            today_patterns = [
                "sensor.solcast_pv_forecast_forecast_today",
                "sensor.solcast_forecast_today",
                "sensor.solcast_pv_forecast_today",
            ]
            today_state = self._find_solcast_sensor(today_patterns)
            if not today_state:
                return None

            # Get all the sensor values - try multiple naming patterns
            tomorrow_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_tomorrow",
                "sensor.solcast_forecast_tomorrow",
                "sensor.solcast_pv_forecast_tomorrow",
            ])
            remaining_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_remaining_today",
                "sensor.solcast_forecast_remaining_today",
                "sensor.solcast_pv_forecast_remaining_today",
            ])
            peak_today_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_peak_forecast_today",
                "sensor.solcast_peak_forecast_today",
                "sensor.solcast_pv_forecast_peak_today",
            ])
            peak_tomorrow_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_peak_forecast_tomorrow",
                "sensor.solcast_peak_forecast_tomorrow",
                "sensor.solcast_pv_forecast_peak_tomorrow",
            ])
            power_now_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_power_now",
                "sensor.solcast_power_now",
                "sensor.solcast_pv_forecast_now",
            ])

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

            # Try to also get tomorrow's detailed forecast for optimizer (48h horizon)
            # Check the tomorrow forecast sensor for detailed data
            tomorrow_detailed = None
            tomorrow_state_obj = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_tomorrow",
                "sensor.solcast_forecast_tomorrow",
                "sensor.solcast_pv_forecast_tomorrow",
            ])
            if tomorrow_state_obj and tomorrow_state_obj.attributes:
                tomorrow_detailed = (
                    tomorrow_state_obj.attributes.get("detailedForecast") or
                    tomorrow_state_obj.attributes.get("forecast_tomorrow") or
                    tomorrow_state_obj.attributes.get("detailedHourly") or
                    tomorrow_state_obj.attributes.get("forecasts")
                )

            # Combine today and tomorrow forecasts for optimizer
            full_forecasts = []
            if detailed_forecast and isinstance(detailed_forecast, list):
                full_forecasts.extend(detailed_forecast)
            if tomorrow_detailed and isinstance(tomorrow_detailed, list):
                full_forecasts.extend(tomorrow_detailed)

            _LOGGER.info(
                f"Solcast (from HA integration): Today={today_forecast:.1f}kWh, "
                f"remaining={remaining:.1f}kWh, Tomorrow={tomorrow_forecast:.1f}kWh, "
                f"hourly_points={len(hourly_forecast)}, raw_periods={len(full_forecasts)}"
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
                "forecasts": full_forecasts if full_forecasts else None,  # Raw periods for optimizer
                "forecast_periods": len(full_forecasts) if full_forecasts else len(hourly_forecast),
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
                self._rate_limited = True
                self._last_rate_limit_time = dt_util.now()
                _LOGGER.warning(
                    f"Solcast API rate limit hit for resource {resource_id[:8]}... "
                    f"(API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}). "
                    f"Will use cached data until tomorrow."
                )
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

    def _track_api_call(self) -> None:
        """Track API call for rate limit awareness."""
        today_str = dt_util.now().strftime("%Y-%m-%d")
        if self._api_calls_date != today_str:
            # New day - reset counter
            self._api_calls_date = today_str
            self._api_calls_today = 0
            self._rate_limited = False

        self._api_calls_today += 1

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast data from Solcast.

        First checks if the Solcast HA integration is installed - if so, reads from
        its sensors to avoid doubling API calls. Only makes direct API calls if the
        Solcast integration is not available.

        Supports multiple resource IDs - values are combined by summing.

        IMPORTANT: We skip estimated_actuals API calls to conserve API budget.
        The hobbyist tier only allows 10 calls/day, and with split arrays each
        resource requires its own call. Estimated actuals are optional - we use
        cached full-day forecasts instead.
        """
        # First, check if Solcast HA integration is installed and has data
        # This avoids doubling API calls if user has both integrations
        solcast_data = await self._try_read_from_solcast_integration()
        if solcast_data:
            _LOGGER.debug("Using data from Solcast HA integration (no API calls needed)")
            return solcast_data

        # Check if we're rate limited
        if self._rate_limited:
            _LOGGER.warning(
                f"Solcast API rate limited - using cached data. "
                f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
            )
            return self.data or {"available": False}

        # Solcast integration not available - make our own API calls
        try:
            async with asyncio.timeout(60):  # Longer timeout for multiple API calls
                # Track that we're making API calls
                n_resources = len(self._resource_ids)
                _LOGGER.info(
                    f"Fetching Solcast forecast for {n_resources} resource(s). "
                    f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                )

                # Fetch forecasts from first resource
                self._track_api_call()
                forecasts = await self._fetch_forecast_for_resource(self._resource_ids[0])
                if not forecasts:
                    _LOGGER.warning("No forecasts from Solcast API")
                    return self.data or {"available": False}

                # NOTE: We intentionally skip estimated_actuals to save API calls
                # With 10 calls/day limit and split arrays, we need to conserve budget
                # The full-day forecast will be estimated from cached values instead
                estimated_actuals = None

                # If multiple resources, fetch and combine
                if len(self._resource_ids) > 1:
                    for resource_id in self._resource_ids[1:]:
                        self._track_api_call()
                        additional_forecasts = await self._fetch_forecast_for_resource(resource_id)
                        if additional_forecasts:
                            forecasts = self._combine_forecasts(forecasts, additional_forecasts)
                        else:
                            _LOGGER.warning(f"Failed to fetch forecast from resource {resource_id[:8]}...")

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
                "forecasts": forecasts,  # Raw forecast periods for optimizer
                "last_update": dt_util.utcnow(),
                "source": "api",
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
