# app/sigenergy_client.py
"""Sigenergy Cloud API client for PowerSync.

Handles authentication and tariff synchronization with Sigenergy battery systems.
Based on https://github.com/Talie5in/amber2sigen
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class SigenergyClient:
    """Client for Sigenergy Cloud API."""

    # API Endpoints
    BASE_URL = "https://api-aus.sigencloud.com"
    AUTH_ENDPOINT = "/auth/oauth/token"
    SAVE_PRICE_ENDPOINT = "/device/stationelecsetprice/save"
    STATIONS_ENDPOINT = "/device/station/list"

    # Basic Auth header for token endpoint (base64 of "sigen:sigen")
    BASIC_AUTH = "Basic c2lnZW46c2lnZW4="

    def __init__(
        self,
        username: Optional[str] = None,
        pass_enc: Optional[str] = None,
        device_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ):
        """Initialize Sigenergy client.

        Args:
            username: Sigenergy account email
            pass_enc: Encrypted password (from browser dev tools)
            device_id: 13-digit device identifier
            access_token: OAuth access token (if already authenticated)
            refresh_token: OAuth refresh token (for token refresh)
        """
        self.username = username
        self.pass_enc = pass_enc
        self.device_id = device_id or "1756353655250"  # Default device ID
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at: Optional[datetime] = None

    def authenticate(self) -> dict:
        """Authenticate with Sigenergy and get access tokens.

        Returns:
            dict with access_token, refresh_token, expires_in on success
            dict with error key on failure
        """
        if not self.username or not self.pass_enc:
            return {"error": "Username and encrypted password are required"}

        url = f"{self.BASE_URL}{self.AUTH_ENDPOINT}"

        headers = {
            "Authorization": self.BASIC_AUTH,
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
            logger.info(f"Authenticating with Sigenergy for user: {self.username}")
            response = requests.post(url, headers=headers, data=data, timeout=30)

            if response.status_code != 200:
                logger.error(f"Sigenergy auth failed: {response.status_code} - {response.text}")
                return {"error": f"Authentication failed: {response.status_code}"}

            result = response.json()

            # Sigenergy wraps the token data in a "data" key
            token_data = result.get("data", result)

            if "access_token" not in token_data:
                logger.error(f"No access_token in response: {result}")
                return {"error": "Invalid response - no access token"}

            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token")

            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            logger.info("Sigenergy authentication successful")
            return {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_in": expires_in,
                "expires_at": self.token_expires_at.isoformat(),
            }

        except requests.exceptions.Timeout:
            logger.error("Sigenergy auth timeout")
            return {"error": "Connection timeout"}
        except requests.exceptions.RequestException as e:
            logger.error(f"Sigenergy auth error: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Sigenergy auth unexpected error: {e}")
            return {"error": str(e)}

    def refresh_access_token(self) -> dict:
        """Refresh the access token using the refresh token.

        Returns:
            dict with new tokens on success, error dict on failure
        """
        if not self.refresh_token:
            return {"error": "No refresh token available"}

        url = f"{self.BASE_URL}{self.AUTH_ENDPOINT}"

        headers = {
            "Authorization": self.BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }

        try:
            logger.info("Refreshing Sigenergy access token")
            response = requests.post(url, headers=headers, data=data, timeout=30)

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code}")
                return {"error": f"Token refresh failed: {response.status_code}"}

            result = response.json()
            token_data = result.get("data", result)

            if "access_token" not in token_data:
                return {"error": "Invalid refresh response"}

            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token", self.refresh_token)

            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            logger.info("Token refresh successful")
            return {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_in": expires_in,
            }

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return {"error": str(e)}

    def _ensure_token(self) -> bool:
        """Ensure we have a valid access token, refreshing or authenticating if needed.

        Returns:
            True if we have a valid token, False otherwise
        """
        if not self.access_token:
            # No token - try to authenticate
            logger.info("No access token, authenticating...")
            result = self.authenticate()
            if "error" in result:
                logger.error(f"Authentication failed: {result['error']}")
                return False
            return True

        # Check if token is expired or about to expire (5 min buffer)
        if self.token_expires_at:
            if datetime.utcnow() >= self.token_expires_at - timedelta(minutes=5):
                logger.info("Token expired or expiring soon, refreshing...")
                result = self.refresh_access_token()
                if "error" in result:
                    # Refresh failed - try full re-authentication
                    logger.warning("Token refresh failed, attempting full re-authentication...")
                    result = self.authenticate()
                    if "error" in result:
                        logger.error(f"Re-authentication failed: {result['error']}")
                        return False

        return True

    def get_stations(self) -> dict:
        """Get list of stations for the authenticated user.

        Returns:
            dict with stations list on success, error dict on failure
        """
        if not self._ensure_token():
            return {"error": "Not authenticated"}

        url = f"{self.BASE_URL}{self.STATIONS_ENDPOINT}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            logger.info("Fetching Sigenergy stations")
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code != 200:
                logger.error(f"Get stations failed: {response.status_code}")
                return {"error": f"Failed to get stations: {response.status_code}"}

            result = response.json()
            stations = result.get("data", result.get("rows", []))

            logger.info(f"Found {len(stations) if isinstance(stations, list) else 0} stations")
            return {"stations": stations if isinstance(stations, list) else []}

        except Exception as e:
            logger.error(f"Get stations error: {e}")
            return {"error": str(e)}

    def set_tariff_rate(
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
        if not self._ensure_token():
            return {"error": "Not authenticated"}

        url = f"{self.BASE_URL}{self.SAVE_PRICE_ENDPOINT}"

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
            logger.info(f"Setting tariff for Sigenergy station {station_id}")
            response = requests.post(url, headers=headers, json=payload, timeout=30)

            if response.status_code != 200:
                logger.error(f"Set tariff failed: {response.status_code} - {response.text}")
                return {"error": f"Failed to set tariff: {response.status_code}"}

            result = response.json()

            # Check for success in response
            if result.get("code") == 0 or result.get("success"):
                logger.info(f"Tariff updated successfully for station {station_id}")
                return {"success": True, "message": "Tariff updated"}
            else:
                error_msg = result.get("msg", result.get("message", "Unknown error"))
                logger.error(f"Set tariff API error: {error_msg}")
                return {"error": error_msg}

        except Exception as e:
            logger.error(f"Set tariff error: {e}")
            return {"error": str(e)}

    def test_connection(self) -> tuple[bool, str]:
        """Test the connection to Sigenergy API.

        Returns:
            Tuple of (success: bool, message: str)
        """
        result = self.authenticate()
        if "error" in result:
            return False, result["error"]

        stations = self.get_stations()
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

    # Determine NEM region - priority: explicit nem_region > default Sydney
    detected_region = nem_region

    # Get timezone from NEM region, default to Sydney
    tz_name = NEM_REGION_TIMEZONES.get(detected_region, "Australia/Sydney")
    detected_tz = ZoneInfo(tz_name)
    logger.debug(f"Using timezone: {detected_tz} (NEM region: {detected_region or 'default Sydney'})")

    # Calculate current 30-min slot for ActualInterval injection (using local time)
    now = datetime.now(detected_tz)
    current_slot_minute = 0 if now.minute < 30 else 30
    current_slot_key = f"{now.hour:02d}:{current_slot_minute:02d}"
    logger.debug(f"Current 30-min period: {current_slot_key} ({detected_tz})")

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
                    logger.info(
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
                logger.debug(
                    f"Using fallback {price_type} price for {time_range}: {avg_price:.2f}c/kWh"
                )
            else:
                # No data and no fallback available - use 0
                avg_price = 0.0
                logger.warning(
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
        logger.info(
            f"Sigenergy {price_type} prices: {len(result)} periods, "
            f"range {min(prices):.1f}-{max(prices):.1f}c/kWh, "
            f"peak at {peak_slot['timeRange']} ({peak_slot['price']:.1f}c)"
        )

        # Log full pricing schedule for debugging/app display
        # Format: "00:00=15.2, 00:30=14.8, 01:00=13.5, ..."
        slot_str = ", ".join([f"{p['timeRange'].split('-')[0]}={p['price']:.1f}" for p in result])
        logger.debug(f"Sigenergy {price_type} schedule: {slot_str}")

    return result


def get_sigenergy_client(user) -> Optional[SigenergyClient]:
    """Create a SigenergyClient from a user object.

    Args:
        user: User model instance with Sigenergy credentials

    Returns:
        SigenergyClient instance or None if credentials missing
    """
    from app.utils import decrypt_token

    if not user.sigenergy_username:
        return None

    # Decrypt stored credentials
    pass_enc = None
    if user.sigenergy_pass_enc_encrypted:
        pass_enc = decrypt_token(user.sigenergy_pass_enc_encrypted)

    access_token = None
    if user.sigenergy_access_token_encrypted:
        access_token = decrypt_token(user.sigenergy_access_token_encrypted)

    refresh_token = None
    if user.sigenergy_refresh_token_encrypted:
        refresh_token = decrypt_token(user.sigenergy_refresh_token_encrypted)

    client = SigenergyClient(
        username=user.sigenergy_username,
        pass_enc=pass_enc,
        device_id=user.sigenergy_device_id,
        access_token=access_token,
        refresh_token=refresh_token,
    )

    # Set token expiry if stored
    if user.sigenergy_token_expires_at:
        client.token_expires_at = user.sigenergy_token_expires_at

    return client
