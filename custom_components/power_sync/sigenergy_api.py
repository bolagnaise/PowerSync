"""Sigenergy Cloud API client for Home Assistant PowerSync integration.

Handles authentication and tariff synchronization with Sigenergy battery systems.
Async implementation using aiohttp for Home Assistant compatibility.
Based on https://github.com/Talie5in/amber2sigen
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from .const import (
    SIGENERGY_API_BASE_URL,
    SIGENERGY_AUTH_ENDPOINT,
    SIGENERGY_SAVE_PRICE_ENDPOINT,
    SIGENERGY_STATIONS_ENDPOINT,
    SIGENERGY_BASIC_AUTH,
)

_LOGGER = logging.getLogger(__name__)


class SigenergyAPIClient:
    """Async client for Sigenergy Cloud API."""

    def __init__(
        self,
        username: Optional[str] = None,
        pass_enc: Optional[str] = None,
        device_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """Initialize Sigenergy client.

        Args:
            username: Sigenergy account email
            pass_enc: Encrypted password (from browser dev tools)
            device_id: 13-digit device identifier
            access_token: OAuth access token (if already authenticated)
            refresh_token: OAuth refresh token (for token refresh)
            session: Optional aiohttp session to reuse
        """
        self.username = username
        self.pass_enc = pass_enc
        self.device_id = device_id or "1756353655250"  # Default device ID
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at: Optional[datetime] = None
        self._session = session
        self._own_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self):
        """Close the session if we own it."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def authenticate(self) -> dict:
        """Authenticate with Sigenergy and get access tokens.

        Returns:
            dict with access_token, refresh_token, expires_in on success
            dict with error key on failure
        """
        if not self.username or not self.pass_enc:
            return {"error": "Username and encrypted password are required"}

        url = f"{SIGENERGY_API_BASE_URL}{SIGENERGY_AUTH_ENDPOINT}"

        headers = {
            "Authorization": SIGENERGY_BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "username": self.username,
            "password": self.pass_enc,
            "scope": "server",
            "grant_type": "password",
            "userDeviceId": self.device_id,
        }

        try:
            session = await self._get_session()
            _LOGGER.info(f"Authenticating with Sigenergy for user: {self.username}")

            async with session.post(url, headers=headers, data=data, timeout=30) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Sigenergy auth failed: {response.status} - {text}")
                    return {"error": f"Authentication failed: {response.status}"}

                result = await response.json()

                # Sigenergy wraps the token data in a "data" key
                token_data = result.get("data", result)

                if "access_token" not in token_data:
                    _LOGGER.error(f"No access_token in response: {result}")
                    return {"error": "Invalid response - no access token"}

                self.access_token = token_data["access_token"]
                self.refresh_token = token_data.get("refresh_token")

                expires_in = token_data.get("expires_in", 3600)
                self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

                _LOGGER.info("Sigenergy authentication successful")
                return {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": expires_in,
                    "expires_at": self.token_expires_at.isoformat(),
                }

        except aiohttp.ClientError as e:
            _LOGGER.error(f"Sigenergy auth error: {e}")
            return {"error": str(e)}
        except Exception as e:
            _LOGGER.error(f"Sigenergy auth unexpected error: {e}")
            return {"error": str(e)}

    async def refresh_access_token(self) -> dict:
        """Refresh the access token using the refresh token.

        Returns:
            dict with new tokens on success, error dict on failure
        """
        if not self.refresh_token:
            return {"error": "No refresh token available"}

        url = f"{SIGENERGY_API_BASE_URL}{SIGENERGY_AUTH_ENDPOINT}"

        headers = {
            "Authorization": SIGENERGY_BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }

        try:
            session = await self._get_session()
            _LOGGER.info("Refreshing Sigenergy access token")

            async with session.post(url, headers=headers, data=data, timeout=30) as response:
                if response.status != 200:
                    _LOGGER.error(f"Token refresh failed: {response.status}")
                    return {"error": f"Token refresh failed: {response.status}"}

                result = await response.json()
                token_data = result.get("data", result)

                if "access_token" not in token_data:
                    return {"error": "Invalid refresh response"}

                self.access_token = token_data["access_token"]
                self.refresh_token = token_data.get("refresh_token", self.refresh_token)

                expires_in = token_data.get("expires_in", 3600)
                self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

                _LOGGER.info("Token refresh successful")
                return {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": expires_in,
                }

        except Exception as e:
            _LOGGER.error(f"Token refresh error: {e}")
            return {"error": str(e)}

    async def _ensure_token(self) -> bool:
        """Ensure we have a valid access token, refreshing or authenticating if needed.

        Returns:
            True if we have a valid token, False otherwise
        """
        if not self.access_token:
            # No token - try to authenticate
            _LOGGER.info("No access token, authenticating...")
            result = await self.authenticate()
            if "error" in result:
                _LOGGER.error(f"Authentication failed: {result['error']}")
                return False
            return True

        # Check if token is expired or about to expire (5 min buffer)
        if self.token_expires_at:
            if datetime.utcnow() >= self.token_expires_at - timedelta(minutes=5):
                _LOGGER.info("Token expired or expiring soon, refreshing...")
                result = await self.refresh_access_token()
                if "error" in result:
                    # Refresh failed - try full re-authentication
                    _LOGGER.warning("Token refresh failed, attempting full re-authentication...")
                    result = await self.authenticate()
                    if "error" in result:
                        _LOGGER.error(f"Re-authentication failed: {result['error']}")
                        return False

        return True

    async def get_stations(self) -> dict:
        """Get list of stations for the authenticated user.

        Returns:
            dict with stations list on success, error dict on failure
        """
        if not await self._ensure_token():
            return {"error": "Not authenticated"}

        url = f"{SIGENERGY_API_BASE_URL}{SIGENERGY_STATIONS_ENDPOINT}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            session = await self._get_session()
            _LOGGER.info("Fetching Sigenergy stations")

            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status != 200:
                    _LOGGER.error(f"Get stations failed: {response.status}")
                    return {"error": f"Failed to get stations: {response.status}"}

                result = await response.json()
                stations = result.get("data", result.get("rows", []))

                _LOGGER.info(f"Found {len(stations) if isinstance(stations, list) else 0} stations")
                return {"stations": stations if isinstance(stations, list) else []}

        except Exception as e:
            _LOGGER.error(f"Get stations error: {e}")
            return {"error": str(e)}

    async def set_tariff_rate(
        self,
        station_id: str,
        buy_prices: list[dict],
        sell_prices: list[dict],
        plan_name: str = "PowerSync",
    ) -> dict:
        """Set tariff pricing for a station.

        Args:
            station_id: The station ID to update
            buy_prices: List of {timeRange: "HH:MM-HH:MM", price: float} for buy rates
            sell_prices: List of {timeRange: "HH:MM-HH:MM", price: float} for sell rates
            plan_name: Name for the pricing plan

        Returns:
            dict with success status or error
        """
        if not await self._ensure_token():
            return {"error": "Not authenticated"}

        url = f"{SIGENERGY_API_BASE_URL}{SIGENERGY_SAVE_PRICE_ENDPOINT}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        # Build the payload in Sigenergy's expected format
        payload = {
            "stationId": int(station_id),
            "priceMode": 1,  # Static pricing mode
            "buyPrice": {
                "dynamicPricing": None,
                "staticPricing": {
                    "providerName": "Amber",
                    "tariffCode": "",
                    "tariffName": "",
                    "currencyCode": "Cent",
                    "subAreaName": "",
                    "planName": f"{plan_name} 30-min",
                    "combinedPrices": [
                        {
                            "monthRange": "01-12",
                            "weekPrices": [
                                {
                                    "weekRange": "1-7",
                                    "timeRange": buy_prices,
                                }
                            ],
                        }
                    ],
                },
            },
            "sellPrice": {
                "dynamicPricing": None,
                "staticPricing": {
                    "providerName": "Amber",
                    "tariffCode": "",
                    "tariffName": "",
                    "currencyCode": "Cent",
                    "subAreaName": "",
                    "planName": f"{plan_name} 30-min",
                    "combinedPrices": [
                        {
                            "monthRange": "01-12",
                            "weekPrices": [
                                {
                                    "weekRange": "1-7",
                                    "timeRange": sell_prices,
                                }
                            ],
                        }
                    ],
                },
            },
        }

        try:
            session = await self._get_session()
            _LOGGER.info(f"Setting tariff for Sigenergy station {station_id}")

            async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Set tariff failed: {response.status} - {text}")
                    return {"error": f"Failed to set tariff: {response.status}"}

                result = await response.json()

                # Check for success in response
                if result.get("code") == 0 or result.get("success"):
                    _LOGGER.info(f"Tariff updated successfully for station {station_id}")
                    return {"success": True, "message": "Tariff updated"}
                else:
                    error_msg = result.get("msg", result.get("message", "Unknown error"))
                    _LOGGER.error(f"Set tariff API error: {error_msg}")
                    return {"error": error_msg}

        except Exception as e:
            _LOGGER.error(f"Set tariff error: {e}")
            return {"error": str(e)}

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to Sigenergy API.

        Returns:
            Tuple of (success: bool, message: str)
        """
        result = await self.authenticate()
        if "error" in result:
            return False, result["error"]

        stations = await self.get_stations()
        if "error" in stations:
            return False, stations["error"]

        station_count = len(stations.get("stations", []))
        return True, f"Connected successfully. Found {station_count} station(s)."


def convert_amber_prices_to_sigenergy(
    amber_prices: list[dict],
    price_type: str = "buy",
    forecast_type: str = "predicted",
    current_actual_interval: Optional[dict] = None,
) -> list[dict]:
    """Convert Amber price data to Sigenergy timeRange format.

    Uses same price extraction logic as Tesla tariff converter for consistency.
    Optionally injects live 5-min ActualInterval price for current period to catch spikes.

    Args:
        amber_prices: List of Amber price intervals with nemTime/startTime/endTime and perKwh
        price_type: 'buy' for import prices, 'sell' for export prices
        forecast_type: Amber forecast type to use ('predicted', 'low', 'high')
        current_actual_interval: Dict with 'general' and 'feedIn' ActualInterval data (optional)
                                If provided, uses this for the current 30-min period instead of averaging

    Returns:
        List of {timeRange: "HH:MM-HH:MM", price: float} in cents
    """
    # Calculate current 30-min slot for ActualInterval injection
    now = datetime.now()
    current_slot_minute = 0 if now.minute < 30 else 30
    current_slot_key = f"{now.hour:02d}:{current_slot_minute:02d}"

    # Group prices by 30-minute slots
    slots = {}

    for price in amber_prices:
        # Get the timestamp
        start_time = price.get("startTime") or price.get("nemTime")
        if not start_time:
            continue

        # Parse the timestamp
        if isinstance(start_time, str):
            try:
                dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            dt = start_time

        # Round down to 30-minute slot
        slot_minute = 0 if dt.minute < 30 else 30
        slot_key = f"{dt.hour:02d}:{slot_minute:02d}"

        # Price extraction - matches Tesla tariff converter logic
        # - ActualInterval (past): Use perKwh (actual settled price)
        # - CurrentInterval (now): Use perKwh or advancedPrice
        # - ForecastInterval (future): Use advancedPrice (with forecast type selection)
        interval_type = price.get("type", "unknown")
        advanced_price = price.get("advancedPrice")

        if interval_type == "ForecastInterval" and advanced_price:
            # ForecastInterval: Prefer advancedPrice with forecast type selection
            if isinstance(advanced_price, dict):
                # Dict format: {predicted, low, high}
                per_kwh_cents = advanced_price.get(forecast_type, advanced_price.get("predicted", 0))
            elif isinstance(advanced_price, (int, float)):
                # Numeric format (legacy)
                per_kwh_cents = advanced_price
            else:
                per_kwh_cents = price.get("perKwh", 0)
        elif interval_type == "CurrentInterval" and advanced_price:
            # CurrentInterval with advancedPrice available
            if isinstance(advanced_price, dict):
                per_kwh_cents = advanced_price.get(forecast_type, advanced_price.get("predicted", 0))
            else:
                per_kwh_cents = advanced_price if isinstance(advanced_price, (int, float)) else price.get("perKwh", 0)
        else:
            # ActualInterval or fallback: Use perKwh (actual retail price)
            per_kwh_cents = price.get("perKwh", 0)

        # For sell prices (feedIn channel), Amber uses negative values (you receive money)
        # We negate to convert to Sigenergy's convention (positive = you receive)
        # Note: Unlike Tesla, Sigenergy can handle negative prices - no clamping to zero
        # During extreme negative wholesale prices, sell price can become negative (you pay to export)
        if price_type == "sell":
            per_kwh_cents = -per_kwh_cents

        # Store the price (average overlapping 5-min intervals)
        if slot_key not in slots:
            slots[slot_key] = []
        slots[slot_key].append(per_kwh_cents)

    # Build the timeRange array (48 slots for 24 hours)
    result = []
    for hour in range(24):
        for minute in [0, 30]:
            slot_key = f"{hour:02d}:{minute:02d}"
            end_minute = minute + 30
            end_hour = hour
            if end_minute >= 60:
                end_minute = 0
                end_hour = (hour + 1) % 24

            time_range = f"{hour:02d}:{minute:02d}-{end_hour:02d}:{end_minute:02d}"

            # SPECIAL CASE: Use ActualInterval for current period if available
            # This captures short-term (5-min) price spikes that would otherwise be averaged out
            if slot_key == current_slot_key and current_actual_interval:
                # Determine which channel to use based on price_type
                channel_key = "general" if price_type == "buy" else "feedIn"
                interval_data = current_actual_interval.get(channel_key)

                if interval_data:
                    actual_price = interval_data.get("perKwh", 0)
                    # For sell prices, negate (Amber feedIn is negative = you receive)
                    # No clamping - Sigenergy handles negative prices unlike Tesla
                    if price_type == "sell":
                        actual_price = -actual_price
                    _LOGGER.info(
                        f"Using ActualInterval for current {price_type} period {time_range}: {actual_price:.2f}c/kWh"
                    )
                    result.append({
                        "timeRange": time_range,
                        "price": round(actual_price, 2),
                    })
                    continue

            # Get average price for this slot, default to 0 if no data
            if slot_key in slots and slots[slot_key]:
                avg_price = sum(slots[slot_key]) / len(slots[slot_key])
            else:
                avg_price = 0.0

            result.append({
                "timeRange": time_range,
                "price": round(avg_price, 2),
            })

    return result
