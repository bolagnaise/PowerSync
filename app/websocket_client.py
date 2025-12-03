# app/websocket_client.py
"""Amber Electric WebSocket client for real-time price updates"""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import websockets

logger = logging.getLogger(__name__)


class AmberWebSocketClient:
    """
    WebSocket client for Amber Electric real-time price updates.

    Maintains a persistent connection to Amber's WebSocket API and caches
    the latest price data for instant access. Auto-reconnects with exponential
    backoff on connection failures.

    Thread-safe for use in Flask multi-threaded environment.
    """

    WS_URL = "wss://api-ws.amber.com.au"

    def __init__(self, api_token: str, site_id: str, sync_callback=None):
        """
        Initialize WebSocket client.

        Args:
            api_token: Amber API token (PSK key)
            site_id: Amber site ID to subscribe to
            sync_callback: Optional callback function to trigger Tesla sync on price updates
        """
        self.api_token = api_token
        self.site_id = site_id

        # Connection state
        self._websocket = None
        self._running = False
        self._thread = None
        self._loop = None

        # Price cache (thread-safe with lock)
        self._price_lock = threading.Lock()
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

        # Stale cache warning debounce (only warn once until data is fresh again)
        self._stale_warning_logged = False

        # Tesla sync triggering
        self._sync_callback = sync_callback
        self._last_sync_trigger: Optional[datetime] = None
        self._sync_cooldown_seconds = 60  # Minimum 60s between sync triggers

        logger.info(f"AmberWebSocketClient initialized for site {site_id}")

    def start(self):
        """Start the WebSocket client in a background thread."""
        if self._running:
            logger.warning("WebSocket client already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name="AmberWebSocket")
        self._thread.start()
        logger.info("✅ WebSocket client thread started")

    def stop(self):
        """Stop the WebSocket client and clean up."""
        logger.info("Stopping WebSocket client")
        self._running = False

        if self._loop and self._loop.is_running():
            # Schedule cleanup in the event loop
            asyncio.run_coroutine_threadsafe(self._cleanup(), self._loop)

        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        logger.info("WebSocket client stopped")

    def _run_event_loop(self):
        """Run the asyncio event loop in the background thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            logger.error(f"Event loop error: {e}", exc_info=True)
        finally:
            if self._loop:
                self._loop.close()

    async def _cleanup(self):
        """Clean up WebSocket connection."""
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
            finally:
                self._websocket = None

    async def _connect_and_listen(self):
        """Connect to WebSocket and listen for messages with auto-reconnect."""
        while self._running:
            try:
                self._connection_status = "connecting"
                logger.info(f"Connecting to Amber WebSocket at {self.WS_URL}")

                # Set up headers with authentication
                headers = {
                    "authorization": f"Bearer {self.api_token}"
                }

                # Connect to WebSocket
                async with websockets.connect(
                    self.WS_URL,
                    additional_headers=headers,
                    ping_interval=30,  # Send ping every 30 seconds
                    ping_timeout=10,   # Wait 10 seconds for pong
                ) as websocket:
                    self._websocket = websocket
                    self._connection_status = "connected"
                    self._reconnect_delay = 1  # Reset reconnect delay on successful connection
                    logger.info("✅ WebSocket connected successfully")

                    # Send subscription request
                    subscribe_message = {
                        "service": "live-prices",
                        "action": "subscribe",
                        "data": {
                            "siteId": self.site_id
                        }
                    }
                    subscribe_json = json.dumps(subscribe_message)
                    logger.info(f"📡 Sending subscription: {subscribe_json}")
                    await websocket.send(subscribe_json)
                    logger.info(f"📡 Subscription sent for site {self.site_id}, waiting for response...")

                    # Listen for messages
                    async for message in websocket:
                        if not self._running:
                            break

                        try:
                            self._handle_message(message)
                        except Exception as e:
                            logger.error(f"Error handling message: {e}", exc_info=True)
                            self._error_count += 1
                            self._last_error = str(e)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                self._connection_status = "disconnected"
                self._websocket = None

            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)
                self._connection_status = "disconnected"
                self._websocket = None
                self._error_count += 1
                self._last_error = str(e)

            # Reconnect with exponential backoff if still running
            if self._running:
                self._connection_status = "reconnecting"
                logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
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
            # Log raw message for debugging (full message, not truncated)
            logger.info(f"📨 Raw WebSocket message ({len(message)} bytes): {message}")

            data = json.loads(message)
            self._message_count += 1

            # Log parsed message structure
            logger.info(f"📨 Parsed message keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

            # Expected format from Amber WebSocket:
            # {
            #   "type": "price-update",
            #   "data": {
            #     "siteId": "...",
            #     "general": {...},
            #     "feedIn": {...}
            #   }
            # }

            # Validate message structure
            if not isinstance(data, dict):
                logger.warning(f"Unexpected message format (not a dict): {type(data)}")
                return

            # Handle subscription confirmation
            if data.get("action") == "subscribe" and data.get("status") == 200:
                logger.info("✅ Subscription confirmed by server")
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
                    logger.warning(f"Received price update for different site: {price_data.get('siteId')}")
                    return

                # Update cache with new prices
                # Convert Amber's "prices" array to general/feedIn dict format
                prices_array = price_data.get("prices", [])
                converted_prices = {}

                for price in prices_array:
                    channel = price.get("channelType")
                    if channel in ["general", "feedIn"]:
                        converted_prices[channel] = price

                with self._price_lock:
                    # Store the converted price data
                    self._cached_prices = converted_prices
                    self._last_update = datetime.now(timezone.utc)
                    self._stale_warning_logged = False  # Reset stale warning on fresh data

                    # Log the price update
                    general_price = converted_prices.get("general", {}).get("perKwh")
                    feedin_price = converted_prices.get("feedIn", {}).get("perKwh")
                    if general_price is not None and feedin_price is not None:
                        logger.info(f"💰 Price update: buy={general_price:.2f}¢/kWh, sell={feedin_price:.2f}¢/kWh")

                # Notify coordinator when price data arrives (cooldown prevents notification spam)
                if self._should_trigger_sync():
                    self._trigger_sync(converted_prices)

            elif data.get("type") == "subscription-success":
                logger.info("✅ Subscription confirmed by server")

            elif data.get("type") == "error":
                error_msg = data.get("message", "Unknown error")
                logger.error(f"WebSocket error from server: {error_msg}")
                self._error_count += 1
                self._last_error = error_msg

            else:
                # Log ALL unhandled messages at INFO level for debugging
                # This helps us understand what Amber actually sends
                logger.info(f"📨 Unhandled message - action={data.get('action')}, type={data.get('type')}, service={data.get('service')}, keys={list(data.keys())}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message as JSON: {e}")
            self._error_count += 1
            self._last_error = f"JSON parse error: {e}"

        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}", exc_info=True)
            self._error_count += 1
            self._last_error = str(e)

    def _should_trigger_sync(self) -> bool:
        """
        Check if enough time has passed since last sync trigger.

        Implements cooldown to prevent rapid re-syncs from WebSocket message bursts.

        Returns:
            bool: True if sync should be triggered, False if in cooldown period
        """
        if self._last_sync_trigger is None:
            return True

        elapsed = (datetime.now(timezone.utc) - self._last_sync_trigger).total_seconds()
        if elapsed < self._sync_cooldown_seconds:
            logger.debug(f"Sync cooldown active ({elapsed:.0f}s < {self._sync_cooldown_seconds}s)")
            return False

        return True

    def _trigger_sync(self, prices_data):
        """
        Notify sync coordinator of new price data.

        Runs callback in thread pool executor to avoid blocking WebSocket event loop.
        Updates last trigger time to implement cooldown.

        Args:
            prices_data: Dictionary with price data to pass to coordinator
        """
        if not self._sync_callback:
            return

        try:
            self._last_sync_trigger = datetime.now(timezone.utc)

            # Run callback in separate thread to avoid blocking WebSocket
            from concurrent.futures import ThreadPoolExecutor
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WS-Notify")
            future = executor.submit(self._sync_callback, prices_data)

            # Don't wait for completion - fire and forget
            logger.info("📡 Notified sync coordinator of WebSocket price update")

        except Exception as e:
            logger.error(f"Error notifying sync coordinator: {e}", exc_info=True)

    def get_latest_prices(self, max_age_seconds: int = 10) -> Optional[Dict[str, Any]]:
        """
        Get the latest cached prices from WebSocket.

        Args:
            max_age_seconds: Maximum age of cached data in seconds (default: 10)

        Returns:
            Dictionary with price data, or None if no recent data available.
            Format matches Amber API /prices/current endpoint:
            [
                {"type": "CurrentInterval", "perKwh": 36.19, "channelType": "general", ...},
                {"type": "CurrentInterval", "perKwh": -10.44, "channelType": "feedIn", ...}
            ]
        """
        with self._price_lock:
            if not self._cached_prices or not self._last_update:
                return None

            # Check if data is stale
            age = (datetime.now(timezone.utc) - self._last_update).total_seconds()
            if age > max_age_seconds:
                # Only warn once until data becomes fresh again (debounce spam)
                if not self._stale_warning_logged:
                    logger.warning(f"Cached WebSocket data is {age:.1f}s old (max: {max_age_seconds}s) - using REST fallback")
                    self._stale_warning_logged = True
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
        with self._price_lock:
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

    def ensure_running(self) -> bool:
        """
        Check if WebSocket thread is alive and restart if needed.

        Call this periodically to ensure the WebSocket connection stays
        alive even after unexpected failures.

        Returns:
            bool: True if thread was restarted, False if already running
        """
        if not self._running:
            # Client was explicitly stopped, don't restart
            return False

        if self._thread is None or not self._thread.is_alive():
            logger.warning("WebSocket thread died unexpectedly - restarting...")
            self._connection_status = "reconnecting"
            self._thread = threading.Thread(
                target=self._run_event_loop,
                daemon=True,
                name="AmberWebSocket"
            )
            self._thread.start()
            logger.info("WebSocket thread restarted successfully")
            return True

        return False
