"""Localvolts API client for PowerSync integration.

Localvolts is an Australian electricity retailer offering real-time wholesale
pricing via their API. Provides 5-minute NEM interval data with marginal
import/export prices.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import LOCALVOLTS_API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class LocalvoltsClient:
    """Client for the Localvolts API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        partner_id: str,
    ) -> None:
        """Initialize the client.

        Args:
            session: aiohttp client session
            api_key: Localvolts API key
            partner_id: Localvolts Partner ID
        """
        self.session = session
        self.api_key = api_key
        self.partner_id = partner_id

    @property
    def _headers(self) -> dict[str, str]:
        """Return auth headers for API requests."""
        return {
            "Authorization": f"apikey {self.api_key}",
            "partner": self.partner_id,
        }

    async def get_intervals(
        self,
        nmi: str,
        from_dt: str | None = None,
        to_dt: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch interval data from Localvolts API.

        Args:
            nmi: National Meter Identifier (or * for all authorized NMIs)
            from_dt: Optional start datetime (ISO 8601)
            to_dt: Optional end datetime (ISO 8601)

        Returns:
            List of interval dicts with keys like costsFlexUp, earningsFlexUp,
            intervalEnd, quality, etc.
        """
        params: dict[str, str] = {"NMI": nmi}
        if from_dt:
            params["from"] = from_dt
        if to_dt:
            params["to"] = to_dt

        url = f"{LOCALVOLTS_API_BASE_URL}/customer/interval"

        try:
            async with self.session.get(
                url,
                headers=self._headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list):
                        return data
                    # Some endpoints wrap the list
                    if isinstance(data, dict) and "intervals" in data:
                        return data["intervals"]
                    return data if isinstance(data, list) else []

                error_text = await response.text()
                _LOGGER.error(
                    "Localvolts API error %s: %s", response.status, error_text[:200]
                )
                return []

        except aiohttp.ClientError as err:
            _LOGGER.error("Localvolts API network error: %s", err)
            return []
        except Exception as err:
            _LOGGER.error("Localvolts API unexpected error: %s", err)
            return []

    async def validate_credentials(
        self, nmi: str
    ) -> dict[str, Any]:
        """Validate API credentials by fetching current interval.

        Args:
            nmi: National Meter Identifier

        Returns:
            Dict with 'success' bool and optional 'error' string.
        """
        url = f"{LOCALVOLTS_API_BASE_URL}/customer/interval"
        params: dict[str, str] = {"NMI": nmi}

        try:
            async with self.session.get(
                url,
                headers=self._headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    # Check we got valid data back
                    if isinstance(data, list) and len(data) > 0:
                        return {"success": True}
                    if isinstance(data, dict) and data.get("intervals"):
                        return {"success": True}
                    return {"success": False, "error": "no_data"}
                elif response.status == 401:
                    return {"success": False, "error": "invalid_auth"}
                elif response.status == 403:
                    return {"success": False, "error": "invalid_auth"}
                else:
                    error_text = await response.text()
                    _LOGGER.error(
                        "Localvolts validation error %s: %s",
                        response.status,
                        error_text[:200],
                    )
                    return {"success": False, "error": "cannot_connect"}

        except aiohttp.ClientError:
            return {"success": False, "error": "cannot_connect"}
        except Exception as err:
            _LOGGER.exception("Unexpected error validating Localvolts credentials: %s", err)
            return {"success": False, "error": "unknown"}
