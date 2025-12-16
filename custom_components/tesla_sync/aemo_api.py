"""AEMO (Australian Energy Market Operator) API client for Home Assistant.

Fetches real-time electricity pricing data from the National Electricity Market (NEM).
No authentication required - uses public API endpoints.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import datetime
from typing import Any

import aiohttp
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

# NEM time is always AEST (no daylight saving)
NEM_TIMEZONE = ZoneInfo("Australia/Brisbane")


class AEMOAPIClient:
    """Client for AEMO NEM Data API.

    Fetches real-time electricity pricing data from the National Electricity Market.
    No authentication required - uses public API endpoints.
    """

    BASE_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"
    PREDISPATCH_URL = "https://nemweb.com.au/Reports/Current/Predispatch_Reports/"

    # NEM Regions
    REGIONS = {
        "NSW1": "New South Wales",
        "QLD1": "Queensland",
        "VIC1": "Victoria",
        "SA1": "South Australia",
        "TAS1": "Tasmania",
    }

    # Class-level cache for pre-dispatch forecast (shared across instances)
    # AEMO updates pre-dispatch files every 30 minutes, so we cache to avoid redundant downloads
    _predispatch_cache: dict[str, Any] = {
        "filename": None,  # Last downloaded filename
        "data": {},  # Parsed data by region: {'NSW1': [...], 'QLD1': [...], ...}
        "timestamp": None,  # When the cache was populated
    }

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        """Initialize AEMO API client.

        Args:
            session: Optional aiohttp session. If not provided, creates one per request.
        """
        self._session = session
        _LOGGER.info("AEMOAPIClient initialized")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_current_prices(self) -> dict[str, dict[str, Any]] | None:
        """Get current 5-minute dispatch prices for all NEM regions.

        Returns:
            dict: Price data for all regions or None on error
            Example: {
                'NSW1': {'price': 72.06, 'timestamp': '2025-11-08T21:00:00', 'status': 'FIRM'},
                'QLD1': {'price': 69.89, 'timestamp': '2025-11-08T21:00:00', 'status': 'FIRM'},
                ...
            }
        """
        try:
            _LOGGER.info("Fetching current AEMO NEM prices")
            session = await self._get_session()

            async with session.get(self.BASE_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                data = await response.json()

            # Extract regional prices from the ELEC_NEM_SUMMARY data
            prices: dict[str, dict[str, Any]] = {}

            if "ELEC_NEM_SUMMARY" in data:
                for item in data["ELEC_NEM_SUMMARY"]:
                    region = item.get("REGIONID")
                    if region in self.REGIONS:
                        prices[region] = {
                            "price": float(item.get("PRICE", 0)),  # Wholesale price in $/MWh
                            "timestamp": item.get("SETTLEMENTDATE"),
                            "status": item.get("PRICE_STATUS", "UNKNOWN"),
                            "demand": float(item.get("TOTALDEMAND", 0)),
                            "region_name": self.REGIONS[region],
                        }

            _LOGGER.info("Successfully fetched AEMO prices for %d regions", len(prices))
            _LOGGER.debug("AEMO price data: %s", prices)
            return prices

        except aiohttp.ClientError as err:
            _LOGGER.error("Error fetching AEMO prices: %s", err)
            return None
        except (KeyError, ValueError) as err:
            _LOGGER.error("Error parsing AEMO data: %s", err)
            return None

    async def get_region_price(self, region: str) -> dict[str, Any] | None:
        """Get current price for a specific region.

        Args:
            region: Region code (NSW1, QLD1, VIC1, SA1, TAS1)

        Returns:
            dict: Price data for the region or None
        """
        if region not in self.REGIONS:
            _LOGGER.error("Invalid region: %s. Must be one of %s", region, list(self.REGIONS.keys()))
            return None

        prices = await self.get_current_prices()
        if prices:
            return prices.get(region)
        return None

    async def check_price_spike(
        self, region: str, threshold_dollars_per_mwh: float
    ) -> tuple[bool, float | None, dict[str, Any] | None]:
        """Check if current price exceeds threshold (price spike detection).

        Args:
            region: Region code (NSW1, QLD1, VIC1, SA1, TAS1)
            threshold_dollars_per_mwh: Spike threshold in $/MWh (e.g., 300)

        Returns:
            tuple: (is_spike: bool, current_price: float, price_data: dict)
        """
        price_data = await self.get_region_price(region)
        if not price_data:
            return False, None, None

        current_price = price_data["price"]
        is_spike = current_price >= threshold_dollars_per_mwh

        if is_spike:
            _LOGGER.warning(
                "PRICE SPIKE DETECTED in %s: $%s/MWh (threshold: $%s/MWh)",
                region, current_price, threshold_dollars_per_mwh
            )
        else:
            _LOGGER.debug(
                "Normal price in %s: $%s/MWh (threshold: $%s/MWh)",
                region, current_price, threshold_dollars_per_mwh
            )

        return is_spike, current_price, price_data

    async def get_price_forecast(
        self, region: str, periods: int = 96
    ) -> list[dict[str, Any]] | None:
        """Get AEMO 30-min pre-dispatch price forecast.

        Fetches directly from AEMO's NEMWeb pre-dispatch reports (ZIP/CSV).
        Returns data in Amber-compatible format for tariff converter reuse.

        Uses class-level caching to avoid re-downloading the same file.
        AEMO updates pre-dispatch files every 30 minutes.

        Args:
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)
            periods: Number of 30-minute periods to fetch (default 96 = 48 hours)

        Returns:
            list: Price intervals in Amber-compatible format:
            [
                {
                    'nemTime': '2025-12-13T19:30:00+10:00',
                    'perKwh': 11.0,  # cents/kWh
                    'channelType': 'general',
                    'type': 'ForecastInterval',
                    'duration': 30
                },
                ...
            ]
        """
        if region not in self.REGIONS:
            _LOGGER.error("Invalid region: %s. Must be one of %s", region, list(self.REGIONS.keys()))
            return None

        try:
            session = await self._get_session()

            # Step 1: Get list of available pre-dispatch files from NEMWeb
            async with session.get(
                self.PREDISPATCH_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                index_html = await response.text()

            # Step 2: Find latest PUBLIC_PREDISPATCH file
            files = re.findall(r"PUBLIC_PREDISPATCH_\d+_\d+_LEGACY\.zip", index_html)
            if not files:
                _LOGGER.error("No pre-dispatch files found in AEMO NEMWeb directory")
                return None

            latest_file = sorted(files)[-1]  # Get most recent by timestamp

            # Step 3: Check cache - return cached data if file hasn't changed
            cache = AEMOAPIClient._predispatch_cache
            if cache["filename"] == latest_file and region in cache["data"]:
                cached_intervals = cache["data"][region]
                _LOGGER.info(
                    "Using cached AEMO forecast for %s (%d periods, file: %s)",
                    region, len(cached_intervals) // 2, latest_file
                )
                # Return requested number of periods (or all if fewer available)
                return cached_intervals[: periods * 2] if len(cached_intervals) > periods * 2 else cached_intervals

            # Step 4: Download the ZIP file (cache miss or new file)
            file_url = f"{self.PREDISPATCH_URL}{latest_file}"
            _LOGGER.info("Downloading AEMO pre-dispatch: %s", latest_file)

            async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                response.raise_for_status()
                zip_content = await response.read()

            # Step 5: Parse CSV from ZIP - extract ALL regions for caching
            region_data: dict[str, list[dict[str, Any]]] = {r: [] for r in self.REGIONS}
            seen_timestamps: dict[str, set[str]] = {r: set() for r in self.REGIONS}

            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                # The ZIP contains a single CSV file with all data tables
                csv_files = [f for f in zf.namelist() if f.endswith(".CSV") or f.endswith(".csv")]
                if not csv_files:
                    _LOGGER.error("No CSV file in pre-dispatch ZIP: %s", zf.namelist())
                    return None

                _LOGGER.debug("Found CSV file: %s", csv_files[0])

                with zf.open(csv_files[0]) as f:
                    reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))

                    for row in reader:
                        # AEMO pre-dispatch CSV format (PDREGION table):
                        # D,PDREGION,,5,DateTime,RunNo,REGIONID,PeriodDateTime,RRP,...
                        # Column 0: Record type (D = data)
                        # Column 1: Table name (PDREGION)
                        # Column 6: Region ID (NSW1, QLD1, VIC1, SA1, TAS1)
                        # Column 7: Period DateTime (forecast period)
                        # Column 8: RRP in $/MWh
                        if len(row) < 9 or row[0] != "D":
                            continue

                        try:
                            # Check if this is a PDREGION row (contains price data)
                            table_name = row[1] if len(row) > 1 else ""
                            if table_name != "PDREGION":
                                continue

                            # Extract region
                            row_region = row[6] if len(row) > 6 else None
                            if row_region not in self.REGIONS:
                                continue

                            # Extract period datetime and RRP
                            datetime_str = row[7] if len(row) > 7 else None
                            rrp_str = row[8] if len(row) > 8 else None

                            if not datetime_str or not rrp_str:
                                continue

                            # Skip duplicates (same timestamp for same region)
                            if datetime_str in seen_timestamps[row_region]:
                                continue
                            seen_timestamps[row_region].add(datetime_str)

                            # Parse datetime (format: YYYY/MM/DD HH:MM:SS)
                            dt = datetime.strptime(datetime_str, "%Y/%m/%d %H:%M:%S")
                            dt = dt.replace(tzinfo=NEM_TIMEZONE)

                            # Parse RRP ($/MWh) and convert to c/kWh
                            rrp = float(rrp_str)
                            price_cents = rrp / 10.0  # $/MWh / 10 = c/kWh

                            # Add import (general) price
                            region_data[row_region].append({
                                "nemTime": dt.isoformat(),
                                "perKwh": price_cents,
                                "channelType": "general",
                                "type": "ForecastInterval",
                                "duration": 30,
                            })

                            # Add export (feedIn) price - same as import for AEMO
                            # (will be overridden by Flow Power Happy Hour rates)
                            region_data[row_region].append({
                                "nemTime": dt.isoformat(),
                                "perKwh": -price_cents,  # Amber convention: negative = you get paid
                                "channelType": "feedIn",
                                "type": "ForecastInterval",
                                "duration": 30,
                            })

                        except (ValueError, IndexError) as err:
                            _LOGGER.debug("Skipping row due to parse error: %s", err)
                            continue

            # Sort each region's data by timestamp
            for r in region_data:
                region_data[r].sort(key=lambda x: x["nemTime"])

            # Update cache with all regions
            AEMOAPIClient._predispatch_cache = {
                "filename": latest_file,
                "data": region_data,
                "timestamp": datetime.now(NEM_TIMEZONE).isoformat(),
            }

            # Log cache update
            region_counts = {r: len(d) // 2 for r, d in region_data.items() if d}
            _LOGGER.info("Cached AEMO forecast for all regions: %s", region_counts)

            # Return requested region's data
            intervals = region_data.get(region, [])
            if not intervals:
                _LOGGER.error("No price data found for region %s in pre-dispatch file", region)
                return None

            _LOGGER.info("Successfully parsed %d AEMO forecast periods for %s", len(intervals) // 2, region)
            return intervals[: periods * 2] if len(intervals) > periods * 2 else intervals

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error fetching AEMO pre-dispatch: %s", err)
            return None
        except zipfile.BadZipFile as err:
            _LOGGER.error("Invalid ZIP file from AEMO: %s", err)
            return None
        except Exception as err:
            _LOGGER.error("Error fetching AEMO price forecast: %s", err)
            return None
