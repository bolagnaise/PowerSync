"""Sigenergy Cloud API client for Home Assistant PowerSync integration.

Handles authentication and tariff synchronization with Sigenergy battery systems.
Async implementation using aiohttp for Home Assistant compatibility.
Based on https://github.com/Talie5in/amber2sigen
"""

import base64
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from .const import (
    SIGENERGY_API_BASE_URL,
    SIGENERGY_AUTH_ENDPOINT,
    SIGENERGY_SAVE_PRICE_ENDPOINT,
    SIGENERGY_STATIONS_ENDPOINT,
    SIGENERGY_BASIC_AUTH,
)

_LOGGER = logging.getLogger(__name__)

# Sigenergy password encryption constants
_SIGENERGY_AES_KEY = b"sigensigensigenp"  # 16 bytes for AES-128
_SIGENERGY_AES_IV = b"sigensigensigenp"  # Same as key


def encode_sigenergy_password(plain_password: str) -> str:
    """Encode a plain password to Sigenergy's encrypted format.

    Sigenergy uses AES-128-CBC with PKCS7 padding, then Base64 encodes the result.
    Key and IV are both "sigensigensigenp".

    Args:
        plain_password: The plain text password

    Returns:
        Base64-encoded encrypted password (pass_enc format)
    """
    # PKCS7 padding to 16-byte block size
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(plain_password.encode("utf-8")) + padder.finalize()

    # AES-128-CBC encryption
    cipher = Cipher(algorithms.AES(_SIGENERGY_AES_KEY), modes.CBC(_SIGENERGY_AES_IV))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded_data) + encryptor.finalize()

    # Base64 encode
    return base64.b64encode(encrypted).decode("utf-8")


class SigenergyAPIClient:
    """Async client for Sigenergy Cloud API."""

    def __init__(
        self,
        username: Optional[str] = None,
        pass_enc: Optional[str] = None,
        device_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
        session: Optional[aiohttp.ClientSession] = None,
        on_token_refresh: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        """Initialize Sigenergy client.

        Args:
            username: Sigenergy account email
            pass_enc: Encrypted password (from browser dev tools)
            device_id: Optional device identifier (13 digits, no longer required by Sigenergy)
            access_token: OAuth access token (if already authenticated)
            refresh_token: OAuth refresh token (for token refresh)
            token_expires_at: Token expiration datetime (if known)
            session: Optional aiohttp session to reuse
            on_token_refresh: Async callback when tokens are refreshed (for persistence)
        """
        self.username = username
        self.pass_enc = pass_enc
        self.device_id = device_id  # Optional — Sigenergy may no longer require it
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        self._session = session
        self._own_session = False
        self._on_token_refresh = on_token_refresh

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
            "grant_type": "password",
            "agree": True,
        }
        # Only include userDeviceId if provided (no longer required by Sigenergy)
        if self.device_id:
            data["userDeviceId"] = self.device_id

        try:
            session = await self._get_session()
            _LOGGER.info(f"Authenticating with Sigenergy for user: {self.username}")

            async with session.post(url, headers=headers, data=data, timeout=30) as response:
                if response.status != 200:
                    _LOGGER.error(f"Sigenergy auth failed: {response.status}")
                    return {"error": f"Authentication failed: {response.status}"}

                result = await response.json()

                # Sigenergy wraps the token data in a "data" key
                token_data = result.get("data", result)

                if "access_token" not in token_data:
                    _LOGGER.error("Sigenergy auth response missing access_token (keys: %s)", list(token_data.keys()))
                    return {"error": "Invalid response - no access token"}

                self.access_token = token_data["access_token"]
                self.refresh_token = token_data.get("refresh_token")

                expires_in = token_data.get("expires_in", 3600)
                self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

                _LOGGER.info("Sigenergy authentication successful")

                token_info = {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": expires_in,
                    "expires_at": self.token_expires_at.isoformat(),
                }

                # Notify callback of new tokens (for persistence)
                if self._on_token_refresh:
                    try:
                        await self._on_token_refresh(token_info)
                    except Exception as e:
                        _LOGGER.warning(f"Token refresh callback failed: {e}")

                return token_info

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

                token_info = {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": expires_in,
                    "expires_at": self.token_expires_at.isoformat(),
                }

                # Notify callback of refreshed tokens (for persistence)
                if self._on_token_refresh:
                    try:
                        await self._on_token_refresh(token_info)
                    except Exception as e:
                        _LOGGER.warning(f"Token refresh callback failed: {e}")

                return token_info

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
                    _LOGGER.error(f"Set tariff failed: {response.status}")
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
    nem_region: Optional[str] = None,
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
        nem_region: NEM region code (NSW1, VIC1, QLD1, SA1, TAS1) for timezone selection

    Returns:
        List of {timeRange: "HH:MM-HH:MM", price: float} in cents
    """
    from zoneinfo import ZoneInfo

    # NEM region to timezone mapping
    # CRITICAL: Use proper timezone that handles DST, NOT the offset from Amber data
    # Amber provides timestamps with fixed offsets (e.g., +10:00 even during AEDT +11:00)
    NEM_REGION_TIMEZONES = {
        "NSW1": "Australia/Sydney",
        "VIC1": "Australia/Melbourne",
        "QLD1": "Australia/Brisbane",      # No DST
        "SA1": "Australia/Adelaide",       # UTC+9:30/+10:30
        "TAS1": "Australia/Hobart",
    }

    # Australian electricity network to NEM region mapping
    # Used to auto-detect NEM region from Amber site's network field
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

    # Determine NEM region - priority: explicit nem_region > auto-detect from network
    detected_region = nem_region
    if not detected_region:
        # Try to auto-detect from Amber price data (network field in site info)
        # For now, default to Sydney - caller should pass nem_region if available
        detected_region = None

    # Get timezone from NEM region, default to Sydney
    tz_name = NEM_REGION_TIMEZONES.get(detected_region, "Australia/Sydney")
    detected_tz = ZoneInfo(tz_name)
    _LOGGER.debug(f"Using timezone: {detected_tz} (NEM region: {detected_region or 'default Sydney'})")

    # Calculate current 30-min slot for ActualInterval injection (using local time)
    now = datetime.now(detected_tz)
    current_slot_minute = 0 if now.minute < 30 else 30
    current_slot_key = f"{now.hour:02d}:{current_slot_minute:02d}"
    _LOGGER.debug(f"Current 30-min period: {current_slot_key} ({detected_tz})")

    # Group prices by 30-minute slots
    slots = {}

    for price in amber_prices:
        # Get the timestamp - Amber's nemTime is the END of the interval
        nem_time = price.get("nemTime") or price.get("startTime")
        if not nem_time:
            continue

        # Parse the timestamp
        if isinstance(nem_time, str):
            try:
                timestamp = datetime.fromisoformat(nem_time.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            timestamp = nem_time

        # Get interval duration (Amber provides 5 or 30 minute intervals)
        duration = price.get("duration", 30)

        # CRITICAL: Use interval START time for bucketing (same as Tesla converter)
        # Amber's nemTime is the END of the interval, not the start
        # Example: nemTime=18:00, duration=30 → startTime=17:30 → slot "17:30"
        interval_start = timestamp - timedelta(minutes=duration)

        # Convert to local timezone to handle DST correctly
        interval_start_local = interval_start.astimezone(detected_tz)

        # Round down to 30-minute slot
        slot_minute = 0 if interval_start_local.minute < 30 else 30
        slot_key = f"{interval_start_local.hour:02d}:{slot_minute:02d}"

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
                _LOGGER.debug(
                    f"{nem_time} [{interval_type}]: advancedPrice.{forecast_type}={per_kwh_cents:.2f}c/kWh → slot {slot_key}"
                )
            elif isinstance(advanced_price, (int, float)):
                # Numeric format (legacy)
                per_kwh_cents = advanced_price
                _LOGGER.debug(
                    f"{nem_time} [{interval_type}]: advancedPrice={per_kwh_cents:.2f}c/kWh → slot {slot_key}"
                )
            else:
                per_kwh_cents = price.get("perKwh", 0)
                _LOGGER.debug(
                    f"{nem_time} [{interval_type}]: perKwh={per_kwh_cents:.2f}c/kWh (fallback) → slot {slot_key}"
                )
        elif interval_type == "CurrentInterval" and advanced_price:
            # CurrentInterval with advancedPrice available
            if isinstance(advanced_price, dict):
                per_kwh_cents = advanced_price.get(forecast_type, advanced_price.get("predicted", 0))
            else:
                per_kwh_cents = advanced_price if isinstance(advanced_price, (int, float)) else price.get("perKwh", 0)
            _LOGGER.debug(
                f"{nem_time} [{interval_type}]: {per_kwh_cents:.2f}c/kWh → slot {slot_key}"
            )
        else:
            # ActualInterval or fallback: Use perKwh (actual retail price)
            per_kwh_cents = price.get("perKwh", 0)
            _LOGGER.debug(
                f"{nem_time} [{interval_type}]: perKwh={per_kwh_cents:.2f}c/kWh → slot {slot_key}"
            )

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
    # Track last valid price for fallback when forecast data is missing
    # (matches Tesla tariff converter behavior)
    last_valid_price: Optional[float] = None
    result = []

    for hour in range(24):
        for minute in [0, 30]:
            slot_key = f"{hour:02d}:{minute:02d}"
            end_minute = minute + 30
            end_hour = hour
            if end_minute >= 60:
                end_minute = 0
                end_hour = hour + 1
                # Sigenergy uses "24:00" for midnight, not "00:00"
                if end_hour == 24:
                    end_hour_str = "24"
                else:
                    end_hour_str = f"{end_hour:02d}"
            else:
                end_hour_str = f"{end_hour:02d}"

            time_range = f"{hour:02d}:{minute:02d}-{end_hour_str}:{end_minute:02d}"

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
                    last_valid_price = actual_price  # Track for fallback
                    result.append({
                        "timeRange": time_range,
                        "price": round(actual_price, 2),
                    })
                    continue

            # Get average price for this slot
            if slot_key in slots and slots[slot_key]:
                avg_price = sum(slots[slot_key]) / len(slots[slot_key])
                last_valid_price = avg_price  # Track for fallback
            elif last_valid_price is not None:
                # No data for this slot - use last valid price as fallback
                # This handles cases where feedIn forecast doesn't extend as far as general
                avg_price = last_valid_price
                _LOGGER.debug(
                    f"Using fallback {price_type} price for {time_range}: {avg_price:.2f}c/kWh"
                )
            else:
                # No data and no fallback available - use 0
                avg_price = 0.0
                _LOGGER.warning(
                    f"No {price_type} price data for {time_range}, defaulting to 0"
                )

            result.append({
                "timeRange": time_range,
                "price": round(avg_price, 2),
            })

    # Log summary of converted prices
    if result:
        prices = [p["price"] for p in result]
        # Find peak period (highest price)
        max_idx = prices.index(max(prices))
        peak_slot = result[max_idx]
        _LOGGER.info(
            f"Sigenergy {price_type} prices: {len(result)} periods, "
            f"range {min(prices):.1f}-{max(prices):.1f}c/kWh, "
            f"peak at {peak_slot['timeRange']} ({peak_slot['price']:.1f}c)"
        )

        # Log full pricing schedule for debugging/app display
        # Format: "00:00=15.2, 00:30=14.8, 01:00=13.5, ..."
        slot_str = ", ".join([f"{p['timeRange'].split('-')[0]}={p['price']:.1f}" for p in result])
        _LOGGER.debug(f"Sigenergy {price_type} schedule: {slot_str}")

    return result


def _is_time_in_window(hour: int, minute: int, start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> bool:
    """Check if a time slot falls within a time window.

    Handles overnight windows (e.g., 22:00 to 06:00).
    """
    period_minutes = hour * 60 + minute
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute

    # Handle overnight windows (e.g., 22:00 to 06:00)
    if end_minutes <= start_minutes:
        return period_minutes >= start_minutes or period_minutes < end_minutes
    else:
        return start_minutes <= period_minutes < end_minutes


def apply_export_boost_sigenergy(
    sell_prices: list[dict],
    offset_cents: float = 0.0,
    min_price_cents: float = 0.0,
    boost_start: str = "17:00",
    boost_end: str = "21:00",
    activation_threshold_cents: float = 0.0,
) -> list[dict]:
    """Apply export price boost to Sigenergy sell prices.

    Artificially increases sell prices so the battery sees higher export value
    and is more willing to discharge. Useful when export prices are in a range
    where the algorithm may not trigger exports.

    Args:
        sell_prices: List of {timeRange: "HH:MM-HH:MM", price: float} in cents
        offset_cents: Fixed offset to add to export prices (c/kWh)
        min_price_cents: Minimum export price floor (c/kWh)
        boost_start: Time to start applying boost (HH:MM format)
        boost_end: Time to stop applying boost (HH:MM format)
        activation_threshold_cents: Minimum actual price to activate boost

    Returns:
        Modified list with boosted export prices
    """
    if offset_cents == 0 and min_price_cents == 0:
        _LOGGER.debug("Sigenergy export boost disabled (offset=0, min=0)")
        return sell_prices

    # Parse time window
    try:
        start_parts = boost_start.split(":")
        start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
        end_parts = boost_end.split(":")
        end_hour, end_minute = int(end_parts[0]), int(end_parts[1])
    except (ValueError, IndexError) as err:
        _LOGGER.error("Invalid export boost time format: %s", err)
        return sell_prices

    modified_count = 0
    skipped_count = 0
    boosted_prices = []

    for slot in sell_prices:
        time_range = slot.get("timeRange", "")
        original_price = slot.get("price", 0)

        # Parse slot start time from timeRange (e.g., "17:00-17:30")
        try:
            slot_start = time_range.split("-")[0]
            hour, minute = int(slot_start.split(":")[0]), int(slot_start.split(":")[1])
        except (ValueError, IndexError):
            continue

        # Check if slot is within boost window
        if not _is_time_in_window(hour, minute, start_hour, start_minute, end_hour, end_minute):
            continue

        # Skip boost if actual price is below activation threshold
        if activation_threshold_cents > 0 and original_price < activation_threshold_cents:
            skipped_count += 1
            _LOGGER.debug(
                "%s: Export boost skipped - price %.2fc below threshold %.1fc",
                time_range, original_price, activation_threshold_cents
            )
            continue

        # Apply offset
        boosted_price = original_price + offset_cents

        # Apply minimum floor
        boosted_price = max(boosted_price, min_price_cents)

        if boosted_price != original_price:
            modified_count += 1
            boosted_prices.append(boosted_price)
            _LOGGER.debug(
                "%s: Export boost %.2fc → %.2fc",
                time_range, original_price, boosted_price
            )

        slot["price"] = round(boosted_price, 2)

    # Log summary
    if boosted_prices:
        avg_boost = sum(boosted_prices) / len(boosted_prices)
        skip_msg = f", {skipped_count} skipped" if skipped_count > 0 else ""
        _LOGGER.info(
            "Sigenergy export boost applied to %d periods%s: avg=%.1fc, range=[%.1f-%.1fc]",
            modified_count, skip_msg, avg_boost, min(boosted_prices), max(boosted_prices)
        )
    elif skipped_count > 0:
        _LOGGER.info(
            "Sigenergy export boost: %d periods skipped (below threshold %.1fc)",
            skipped_count, activation_threshold_cents
        )

    return sell_prices


def apply_chip_mode_sigenergy(
    sell_prices: list[dict],
    chip_start: str = "22:00",
    chip_end: str = "06:00",
    threshold_cents: float = 30.0,
) -> list[dict]:
    """Apply Chip Mode to Sigenergy sell prices - suppress exports unless price exceeds threshold.

    During the configured time window, this sets export prices to 0 so the battery
    won't export. However, if the actual price is at or above the threshold, the
    original price is preserved to capture price spikes.

    Args:
        sell_prices: List of {timeRange: "HH:MM-HH:MM", price: float} in cents
        chip_start: Time to start suppressing exports (HH:MM format)
        chip_end: Time to stop suppressing exports (HH:MM format)
        threshold_cents: Price threshold (c/kWh) - only allow export above this

    Returns:
        Modified list with suppressed export prices
    """
    # Parse time window
    try:
        start_parts = chip_start.split(":")
        start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
        end_parts = chip_end.split(":")
        end_hour, end_minute = int(end_parts[0]), int(end_parts[1])
    except (ValueError, IndexError) as err:
        _LOGGER.error("Invalid Chip Mode time format: %s", err)
        return sell_prices

    suppressed_count = 0
    preserved_count = 0

    for slot in sell_prices:
        time_range = slot.get("timeRange", "")
        original_price = slot.get("price", 0)

        # Parse slot start time from timeRange
        try:
            slot_start = time_range.split("-")[0]
            hour, minute = int(slot_start.split(":")[0]), int(slot_start.split(":")[1])
        except (ValueError, IndexError):
            continue

        # Check if slot is within chip mode window
        if not _is_time_in_window(hour, minute, start_hour, start_minute, end_hour, end_minute):
            continue

        # If price is above threshold, preserve it (capture spikes)
        if original_price >= threshold_cents:
            preserved_count += 1
            _LOGGER.debug(
                "%s: Chip Mode preserved - price %.2fc >= threshold %.1fc",
                time_range, original_price, threshold_cents
            )
            continue

        # Suppress export by setting price to 0
        suppressed_count += 1
        _LOGGER.debug(
            "%s: Chip Mode suppressed - price %.2fc → 0c",
            time_range, original_price
        )
        slot["price"] = 0.0

    _LOGGER.info(
        "Sigenergy Chip Mode: %d periods suppressed, %d preserved (threshold=%.1fc)",
        suppressed_count, preserved_count, threshold_cents
    )

    return sell_prices


def apply_spike_protection_sigenergy(
    buy_prices: list[dict],
    threshold_cents: float = 100.0,
    replacement_cents: float = 50.0,
) -> list[dict]:
    """Apply spike protection to Sigenergy buy prices.

    During price spikes, this caps buy prices to prevent the battery from
    charging from the grid at extremely high prices.

    Args:
        buy_prices: List of {timeRange: "HH:MM-HH:MM", price: float} in cents
        threshold_cents: Price threshold above which to apply protection (c/kWh)
        replacement_cents: Price to use when spike is detected (c/kWh)

    Returns:
        Modified list with capped buy prices
    """
    protected_count = 0

    for slot in buy_prices:
        time_range = slot.get("timeRange", "")
        original_price = slot.get("price", 0)

        # If price exceeds threshold, cap it
        if original_price > threshold_cents:
            protected_count += 1
            _LOGGER.debug(
                "%s: Spike protection - price %.2fc → %.2fc (threshold=%.1fc)",
                time_range, original_price, replacement_cents, threshold_cents
            )
            slot["price"] = round(replacement_cents, 2)

    if protected_count > 0:
        _LOGGER.info(
            "Sigenergy spike protection: %d periods capped (threshold=%.1fc, replacement=%.1fc)",
            protected_count, threshold_cents, replacement_cents
        )

    return buy_prices
