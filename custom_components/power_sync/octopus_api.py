"""Octopus Energy UK API client.

This module provides a client for the Octopus Energy REST API.
Octopus offers dynamic pricing with the Agile tariff that has half-hourly rates,
making it comparable to Amber Electric's pricing model.

API documentation: https://developer.octopus.energy/docs/api/

Key products:
- Agile Octopus: Dynamic half-hourly pricing based on wholesale electricity prices
- Octopus Go: EV-focused tariff with cheap overnight rates
- Octopus Flux: Solar/battery export tariff with time-of-use rates
- Tracker: Daily wholesale price tracking

Note: Price endpoints do not require authentication.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class OctopusAPIClient:
    """Client for Octopus Energy REST API."""

    BASE_URL = "https://api.octopus.energy/v1"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the API client.

        Args:
            session: aiohttp client session for making requests
        """
        self.session = session

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any] | None:
        """Make a GET request to the Octopus API.

        Args:
            endpoint: API endpoint path
            params: Optional query parameters
            timeout: Request timeout in seconds

        Returns:
            JSON response data or None if request failed
        """
        url = f"{self.BASE_URL}{endpoint}"

        try:
            async with self.session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    _LOGGER.error(
                        "Octopus API error %s for %s: %s",
                        response.status,
                        endpoint,
                        error_text[:200],
                    )
                    return None

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error calling Octopus API %s: %s", endpoint, err)
            return None
        except Exception as err:
            _LOGGER.exception("Unexpected error calling Octopus API %s: %s", endpoint, err)
            return None

    async def get_products(self) -> list[dict[str, Any]]:
        """Get list of all available products.

        Returns:
            List of product dictionaries
        """
        data = await self._get("/products/")
        if data:
            return data.get("results", [])
        return []

    async def get_product_info(self, product_code: str) -> dict[str, Any] | None:
        """Get product details.

        Args:
            product_code: Octopus product code (e.g., "AGILE-24-10-01")

        Returns:
            Product details dictionary or None if not found
        """
        return await self._get(f"/products/{product_code}/")

    async def get_current_rates(
        self,
        product_code: str,
        tariff_code: str,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Get import (standard unit) rates for a tariff.

        Args:
            product_code: Octopus product code (e.g., "AGILE-24-10-01")
            tariff_code: Full tariff code including region (e.g., "E-1R-AGILE-24-10-01-A")
            period_from: Start of period to fetch (optional)
            period_to: End of period to fetch (optional)
            page_size: Number of results per page (max 1500)

        Returns:
            List of rate dictionaries with value_inc_vat, valid_from, valid_to

        Example response item:
            {
                "value_exc_vat": 28.56,
                "value_inc_vat": 29.988,
                "valid_from": "2026-01-30T23:30:00Z",
                "valid_to": "2026-01-31T00:00:00Z",
                "payment_method": null
            }
        """
        params: dict[str, Any] = {"page_size": page_size}

        if period_from:
            params["period_from"] = period_from.isoformat()
        if period_to:
            params["period_to"] = period_to.isoformat()

        endpoint = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        data = await self._get(endpoint, params=params)

        if data:
            results = data.get("results", [])
            # Sort by valid_from ascending (oldest first)
            results.sort(key=lambda x: x.get("valid_from", ""))
            return results

        return []

    async def get_export_rates(
        self,
        product_code: str,
        tariff_code: str,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Get export rates for a tariff (for Flux/Agile Outgoing).

        Args:
            product_code: Octopus product code (e.g., "AGILE-OUTGOING-19-05-13")
            tariff_code: Full export tariff code including region
            period_from: Start of period to fetch (optional)
            period_to: End of period to fetch (optional)
            page_size: Number of results per page

        Returns:
            List of export rate dictionaries
        """
        params: dict[str, Any] = {"page_size": page_size}

        if period_from:
            params["period_from"] = period_from.isoformat()
        if period_to:
            params["period_to"] = period_to.isoformat()

        endpoint = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        data = await self._get(endpoint, params=params)

        if data:
            results = data.get("results", [])
            # Sort by valid_from ascending (oldest first)
            results.sort(key=lambda x: x.get("valid_from", ""))
            return results

        return []

    async def get_standing_charge(
        self,
        product_code: str,
        tariff_code: str,
    ) -> float | None:
        """Get daily standing charge in pence.

        Args:
            product_code: Octopus product code
            tariff_code: Full tariff code including region

        Returns:
            Daily standing charge in pence, or None if not found
        """
        endpoint = f"/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
        data = await self._get(endpoint)

        if data:
            results = data.get("results", [])
            if results:
                # Return the most recent standing charge (first result)
                return results[0].get("value_inc_vat")

        return None

    async def validate_tariff(
        self,
        product_code: str,
        tariff_code: str,
    ) -> bool:
        """Validate that a tariff exists and has price data.

        Args:
            product_code: Octopus product code
            tariff_code: Full tariff code including region

        Returns:
            True if tariff is valid and has price data
        """
        try:
            rates = await self.get_current_rates(
                product_code,
                tariff_code,
                page_size=1,
            )
            return len(rates) > 0
        except Exception:
            return False


def build_tariff_code(product_code: str, gsp_region: str) -> str:
    """Build the full tariff code from product code and GSP region.

    Octopus tariff codes follow the format: E-1R-{PRODUCT_CODE}-{GSP_CODE}
    for single-register import tariffs.

    Args:
        product_code: Octopus product code (e.g., "AGILE-24-10-01")
        gsp_region: UK Grid Supply Point region code (e.g., "A" for Eastern England)

    Returns:
        Full tariff code (e.g., "E-1R-AGILE-24-10-01-A")
    """
    return f"E-1R-{product_code}-{gsp_region}"


def build_export_tariff_code(product_code: str, gsp_region: str) -> str:
    """Build the full export tariff code from product code and GSP region.

    Export tariff codes follow the format: E-1R-{PRODUCT_CODE}-{GSP_CODE}

    Args:
        product_code: Octopus export product code (e.g., "AGILE-OUTGOING-19-05-13")
        gsp_region: UK Grid Supply Point region code

    Returns:
        Full export tariff code
    """
    return f"E-1R-{product_code}-{gsp_region}"


# Default Octopus product codes
# These are the latest versions as of 2026 - may need updating
OCTOPUS_PRODUCT_CODES = {
    "agile": "AGILE-24-10-01",
    "go": "GO-VAR-22-10-14",
    "flux_import": "FLUX-IMPORT-23-02-14",
    "flux_export": "FLUX-EXPORT-23-02-14",
    "tracker": "SILVER-FLEX-BB-23-02-08",
}

# Agile Outgoing export tariff for dynamic export pricing
OCTOPUS_AGILE_OUTGOING_CODE = "AGILE-OUTGOING-19-05-13"


async def get_agile_rates(
    session: aiohttp.ClientSession,
    gsp_region: str,
    hours_ahead: int = 48,
) -> list[dict[str, Any]]:
    """Convenience function to get Agile Octopus rates.

    Args:
        session: aiohttp client session
        gsp_region: UK GSP region code (A-P)
        hours_ahead: How many hours ahead to fetch (default 48)

    Returns:
        List of rate dictionaries
    """
    client = OctopusAPIClient(session)
    product_code = OCTOPUS_PRODUCT_CODES["agile"]
    tariff_code = build_tariff_code(product_code, gsp_region)

    # Calculate period range
    now = datetime.utcnow()
    period_from = now - timedelta(hours=1)  # Include some past data
    period_to = now + timedelta(hours=hours_ahead)

    return await client.get_current_rates(
        product_code,
        tariff_code,
        period_from=period_from,
        period_to=period_to,
    )


async def get_agile_export_rates(
    session: aiohttp.ClientSession,
    gsp_region: str,
    hours_ahead: int = 48,
) -> list[dict[str, Any]]:
    """Convenience function to get Agile Outgoing export rates.

    Args:
        session: aiohttp client session
        gsp_region: UK GSP region code (A-P)
        hours_ahead: How many hours ahead to fetch (default 48)

    Returns:
        List of export rate dictionaries
    """
    client = OctopusAPIClient(session)
    product_code = OCTOPUS_AGILE_OUTGOING_CODE
    tariff_code = build_export_tariff_code(product_code, gsp_region)

    # Calculate period range
    now = datetime.utcnow()
    period_from = now - timedelta(hours=1)
    period_to = now + timedelta(hours=hours_ahead)

    return await client.get_export_rates(
        product_code,
        tariff_code,
        period_from=period_from,
        period_to=period_to,
    )
