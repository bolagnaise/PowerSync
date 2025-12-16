"""Async AEMO API client for Home Assistant."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from .const import AEMO_API_BASE_URL, AEMO_REGIONS

_LOGGER = logging.getLogger(__name__)


class AEMOAPIClient:
    """Async client for AEMO (Australian Energy Market Operator) NEM Data API.

    Fetches 5-minute dispatch prices from the National Electricity Market.
    These are wholesale prices used for spike detection.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize AEMO API client.

        Args:
            session: aiohttp client session from Home Assistant
        """
        self._session = session

    async def get_current_prices(self) -> dict[str, dict[str, Any]]:
        """Fetch current 5-minute dispatch prices for all NEM regions.

        Returns:
            Dictionary keyed by region code (NSW1, QLD1, VIC1, SA1, TAS1)
            with price data for each region.

        Example return:
            {
                'NSW1': {
                    'price': 72.06,  # $/MWh
                    'timestamp': '2025-11-08T21:00:00',
                    'status': 'FIRM',
                    'demand': 8500.0,
                    'region_name': 'New South Wales'
                },
                ...
            }
        """
        try:
            async with self._session.get(
                AEMO_API_BASE_URL,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "AEMO API error: %s - %s",
                        response.status,
                        await response.text()
                    )
                    return {}

                data = await response.json()

                # Parse AEMO response format
                # Data is in 'ELEC_NEM_SUMMARY' array
                summary = data.get("ELEC_NEM_SUMMARY", [])

                if not summary:
                    _LOGGER.warning("No data in AEMO API response")
                    return {}

                # Group by region
                prices = {}
                for record in summary:
                    region = record.get("REGIONID")
                    if region in AEMO_REGIONS:
                        prices[region] = {
                            "price": float(record.get("PRICE", 0)),  # $/MWh
                            "timestamp": record.get("SETTLEMENTDATE"),
                            "status": record.get("PRICECONFIRMATIONFLAG", ""),
                            "demand": float(record.get("TOTALDEMAND", 0)),
                            "region_name": AEMO_REGIONS.get(region, region),
                        }

                _LOGGER.debug("Fetched AEMO prices for %d regions", len(prices))
                return prices

        except aiohttp.ClientError as err:
            _LOGGER.error("AEMO API connection error: %s", err)
            return {}
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching AEMO prices: %s", err)
            return {}

    async def get_region_price(self, region: str) -> dict[str, Any] | None:
        """Get current price for a specific NEM region.

        Args:
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)

        Returns:
            Price data dict or None if not available
        """
        if region not in AEMO_REGIONS:
            _LOGGER.error("Invalid AEMO region: %s", region)
            return None

        prices = await self.get_current_prices()
        return prices.get(region)

    async def check_price_spike(
        self,
        region: str,
        threshold_dollars_per_mwh: float,
    ) -> tuple[bool, float | None, dict[str, Any] | None]:
        """Check if current price exceeds the spike threshold.

        Args:
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)
            threshold_dollars_per_mwh: Price threshold in $/MWh to trigger spike

        Returns:
            Tuple of (is_spike, current_price, price_data)
            - is_spike: True if price >= threshold
            - current_price: Current price in $/MWh (None if fetch failed)
            - price_data: Full price data dict (None if fetch failed)
        """
        price_data = await self.get_region_price(region)

        if not price_data:
            _LOGGER.warning("Could not fetch AEMO price for %s", region)
            return False, None, None

        current_price = price_data["price"]
        is_spike = current_price >= threshold_dollars_per_mwh

        if is_spike:
            _LOGGER.warning(
                "PRICE SPIKE DETECTED in %s: $%.2f/MWh (threshold: $%.2f/MWh)",
                region,
                current_price,
                threshold_dollars_per_mwh,
            )
        else:
            _LOGGER.debug(
                "Normal price in %s: $%.2f/MWh (threshold: $%.2f/MWh)",
                region,
                current_price,
                threshold_dollars_per_mwh,
            )

        return is_spike, current_price, price_data
