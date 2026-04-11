"""EPEX Day-Ahead Price API client.

Fetches day-ahead electricity price predictions from the EPEX Predictor API
(https://epexpredictor.batzill.com). Supports European bidding zones including
Germany, Austria, Belgium, Netherlands, Denmark, and Sweden.

Data sourced from Energy-charts.info, ENTSO-E (prices), and Open-Meteo (weather).
Free API on fair-use basis - no authentication required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

from .const import EPEX_API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class EPEXAPIClient:
    """Client for the EPEX Day-Ahead Predictor API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the EPEX API client.

        Args:
            session: aiohttp client session for API requests
        """
        self._session = session

    async def get_prices(
        self,
        region: str = "DE",
        surcharge: float = 0.0,
        tax_percent: float = 0.0,
        hours: int = -1,
    ) -> list[dict]:
        """Fetch price predictions from the EPEX Predictor API.

        Args:
            region: Bidding zone code (DE, AT, BE, NL, SE1-4, DK1-2)
            surcharge: Fixed surcharge in ct/kWh (network fees, levies)
            tax_percent: Tax percentage to add (e.g. 21 for 21% VAT)
            hours: Hours to predict (-1 = all available, typically ~48h)

        Returns:
            List of price entries with 'startsAt' (ISO timestamp) and 'total' (ct/kWh)
        """
        params = {
            "region": region,
            "surcharge": surcharge,
            "taxPercent": tax_percent,
            "hours": hours,
            "unit": "CT_PER_KWH",
            "hourly": "true",
        }

        url = f"{EPEX_API_BASE_URL}/prices"

        try:
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "EPEX API returned status %d for region %s", resp.status, region
                    )
                    return []

                data = await resp.json()
                prices = data.get("prices", [])
                known_until = data.get("knownUntil")

                _LOGGER.debug(
                    "EPEX API returned %d price entries for %s (known until %s)",
                    len(prices),
                    region,
                    known_until,
                )
                return prices

        except aiohttp.ClientError as err:
            _LOGGER.error("EPEX API request failed for %s: %s", region, err)
            return []
        except Exception as err:
            _LOGGER.error(
                "Unexpected error fetching EPEX prices for %s: %s", region, err
            )
            return []

    async def validate_region(self, region: str) -> bool:
        """Validate that a region returns price data.

        Args:
            region: Bidding zone code to validate

        Returns:
            True if the region returns valid price data
        """
        prices = await self.get_prices(region=region, hours=1)
        return len(prices) > 0
