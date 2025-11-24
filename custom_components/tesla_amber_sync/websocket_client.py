"""Amber Electric WebSocket client for real-time price updates (async version for Home Assistant)"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import websockets

_LOGGER = logging.getLogger(__name__)


class AmberWebSocketClient:
    """
    Async WebSocket client for Amber Electric real-time price updates.

    Maintains a persistent connection to Amber's WebSocket API and caches
    the latest price data for instant access. Auto-reconnects with exponential
    backoff on connection failures.

    Designed for Home Assistant's asyncio event loop.
    """

    WS_URL = "wss://api-ws.amber.com.au"

    def __init__(self, api_token: str, site_id: str):
        """
        Initialize WebSocket client.

        Args:
            api_token: Amber API token (PSK key)
            site_id: Amber site ID to subscribe to
        """
        self.api_token = api_token
        self.site_id = site_id

        # Connection state
        self._websocket = None
        self._running = False
        self._task = None

        # Price cache (no lock needed - single asyncio event loop)
        self._cached_prices: Dict[str, Any] = {}
        self._last_update: Optional[datetime] = None

        # Health monitoring
        self._connection_status = "disconnected"  # disconnected, connecting, connected, reconnecting
        self._message_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None

        # Reconnection settings
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 60  # Max 60 seconds

        _LOGGER.info(f"AmberWebSocketClient initialized for site {site_id}")

    async def start(self):
        """Start the WebSocket client as an asyncio task."""
        if self._running:
            _LOGGER.warning("WebSocket client already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._connect_and_listen())
        _LOGGER.info("✅ WebSocket client task started")

    async def stop(self):
        """Stop the WebSocket client and clean up."""
        _LOGGER.info("Stopping WebSocket client")
        self._running = False

        # Close WebSocket connection
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception as e:
                _LOGGER.error(f"Error closing WebSocket: {e}")
            finally:
                self._websocket = None

        # Cancel task
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        _LOGGER.info("WebSocket client stopped")

    async def _connect_and_listen(self):
        """Connect to WebSocket and listen for messages with auto-reconnect."""
        while self._running:
            try:
                self._connection_status = "connecting"
                _LOGGER.info(f"Connecting to Amber WebSocket at {self.WS_URL}")

                # Set up headers with authentication
                headers = {
                    "authorization": f"Bearer {self.api_token}"
                }

                # Connect to WebSocket
                async with websockets.connect(
                    self.WS_URL,
                    extra_headers=headers,
                    ping_interval=30,  # Send ping every 30 seconds
                    ping_timeout=10,   # Wait 10 seconds for pong
                ) as websocket:
                    self._websocket = websocket
                    self._connection_status = "connected"
                    self._reconnect_delay = 1  # Reset reconnect delay on successful connection
                    _LOGGER.info("✅ WebSocket connected successfully")

                    # Send subscription request
                    subscribe_message = {
                        "service": "live-prices",
                        "action": "subscribe",
                        "data": {
                            "siteId": self.site_id
                        }
                    }
                    await websocket.send(json.dumps(subscribe_message))
                    _LOGGER.info(f"📡 Subscribed to live prices for site {self.site_id}")

                    # Listen for messages
                    async for message in websocket:
                        if not self._running:
                            break

                        try:
                            self._handle_message(message)
                        except Exception as e:
                            _LOGGER.error(f"Error handling message: {e}", exc_info=True)
                            self._error_count += 1
                            self._last_error = str(e)

            except websockets.exceptions.ConnectionClosed as e:
                _LOGGER.warning(f"WebSocket connection closed: {e}")
                self._connection_status = "disconnected"
                self._websocket = None

            except Exception as e:
                _LOGGER.error(f"WebSocket error: {e}", exc_info=True)
                self._connection_status = "disconnected"
                self._websocket = None
                self._error_count += 1
                self._last_error = str(e)

            # Reconnect with exponential backoff if still running
            if self._running:
                self._connection_status = "reconnecting"
                _LOGGER.info(f"Reconnecting in {self._reconnect_delay} seconds...")
                await asyncio.sleep(self._reconnect_delay)

                # Exponential backoff (double delay each time, up to max)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    def _handle_message(self, message: str):
        """
        Handle incoming WebSocket message.

        Args:
            message: Raw message string from WebSocket
        """
        try:
            data = json.loads(message)

            # Log message for debugging
            _LOGGER.debug(f"WebSocket message received: {data}")
            self._message_count += 1

            # Validate message structure
            if not isinstance(data, dict):
                _LOGGER.warning(f"Unexpected message format (not a dict): {type(data)}")
                return

            # Handle subscription confirmation
            if data.get("action") == "subscribe" and data.get("status") == 200:
                _LOGGER.info("✅ Subscription confirmed by server")
                return

            # Handle price updates
            if data.get("action") == "price-update" or (
                "data" in data and
                isinstance(data.get("data"), dict) and
                "prices" in data.get("data", {})
            ):
                price_data = data.get("data", {})

                # Verify site ID matches (if siteId is present)
                if "siteId" in price_data and price_data.get("siteId") != self.site_id:
                    _LOGGER.warning(f"Received price update for different site: {price_data.get('siteId')}")
                    return

                # Update cache with new prices
                # Convert Amber's "prices" array to general/feedIn dict format
                prices_array = price_data.get("prices", [])
                converted_prices = {}

                for price in prices_array:
                    channel = price.get("channelType")
                    if channel in ["general", "feedIn"]:
                        converted_prices[channel] = price

                # Store the converted price data
                self._cached_prices = converted_prices
                self._last_update = datetime.now(timezone.utc)

                # Log the price update
                general_price = converted_prices.get("general", {}).get("perKwh")
                feedin_price = converted_prices.get("feedIn", {}).get("perKwh")
                if general_price is not None and feedin_price is not None:
                    _LOGGER.info(f"💰 Price update: buy={general_price:.2f}¢/kWh, sell={feedin_price:.2f}¢/kWh")

            elif data.get("type") == "subscription-success":
                _LOGGER.info("✅ Subscription confirmed by server")

            elif data.get("type") == "error":
                error_msg = data.get("message", "Unknown error")
                _LOGGER.error(f"WebSocket error from server: {error_msg}")
                self._error_count += 1
                self._last_error = error_msg

            else:
                _LOGGER.debug(f"Unhandled message type: {data.get('type')}")

        except json.JSONDecodeError as e:
            _LOGGER.error(f"Failed to parse WebSocket message as JSON: {e}")
            self._error_count += 1
            self._last_error = f"JSON parse error: {e}"

        except Exception as e:
            _LOGGER.error(f"Error processing WebSocket message: {e}", exc_info=True)
            self._error_count += 1
            self._last_error = str(e)

    def get_latest_prices(self, max_age_seconds: int = 360) -> Optional[list]:
        """
        Get the latest cached prices from WebSocket.

        Args:
            max_age_seconds: Maximum age of cached data in seconds (default: 360 = 6 minutes)

        Returns:
            List of price data, or None if no recent data available.
            Format matches Amber API /prices/current endpoint:
            [
                {"type": "CurrentInterval", "perKwh": 36.19, "channelType": "general", ...},
                {"type": "CurrentInterval", "perKwh": -10.44, "channelType": "feedIn", ...}
            ]
        """
        if not self._cached_prices or not self._last_update:
            return None

        # Check if data is stale
        age = (datetime.now(timezone.utc) - self._last_update).total_seconds()
        if age > max_age_seconds:
            _LOGGER.warning(f"Cached WebSocket data is {age:.1f}s old (max: {max_age_seconds}s)")
            return None

        # Convert to Amber API format
        # WebSocket provides: {"general": {...}, "feedIn": {...}}
        # API expects: [{"channelType": "general", ...}, {"channelType": "feedIn", ...}]
        result = []

        if "general" in self._cached_prices:
            general_data = self._cached_prices["general"].copy()
            general_data["channelType"] = "general"
            general_data["type"] = "CurrentInterval"  # WebSocket always provides current
            result.append(general_data)

        if "feedIn" in self._cached_prices:
            feedin_data = self._cached_prices["feedIn"].copy()
            feedin_data["channelType"] = "feedIn"
            feedin_data["type"] = "CurrentInterval"
            result.append(feedin_data)

        return result if result else None

    def get_health_status(self) -> Dict[str, Any]:
        """
        Get WebSocket connection health status.

        Returns:
            Dictionary with health metrics
        """
        last_update_str = self._last_update.isoformat() if self._last_update else None
        age_seconds = (datetime.now(timezone.utc) - self._last_update).total_seconds() if self._last_update else None

        return {
            "status": self._connection_status,
            "connected": self._connection_status == "connected",
            "last_update": last_update_str,
            "age_seconds": age_seconds,
            "message_count": self._message_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "has_cached_data": bool(self._cached_prices),
        }
