"""Amber Electric WebSocket client for real-time price updates (threaded version for Home Assistant)"""
import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import websockets


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that obfuscates sensitive data like API keys and tokens.
    Shows first 4 and last 4 characters with asterisks in between.
    """

    @staticmethod
    def obfuscate(value: str, show_chars: int = 4) -> str:
        """Obfuscate a string showing only first and last N characters."""
        if len(value) <= show_chars * 2:
            return '*' * len(value)
        return f"{value[:show_chars]}{'*' * (len(value) - show_chars * 2)}{value[-show_chars:]}"

    def _obfuscate_string(self, text: str) -> str:
        """Apply all obfuscation patterns to a string."""
        if not text:
            return text

        # Handle Bearer tokens
        text = re.sub(
            r'(Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle psk_ tokens (Amber API keys)
        text = re.sub(
            r'(psk_)([a-zA-Z0-9]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle authorization headers in websocket/API logs
        text = re.sub(
            r'(authorization:\s*Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle site IDs (alphanumeric, like Amber 01KAR0YMB7JQDVZ10SN1SGA0CV)
        text = re.sub(
            r'(site[_\s]?[iI][dD]["\']?[\s:=]+["\']?)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text
        )

        # Handle "for site {id}" pattern
        text = re.sub(
            r'(for site\s+)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle email addresses
        text = re.sub(
            r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            lambda m: self.obfuscate(m.group(1)),
            text
        )

        # Handle Tesla energy site IDs (numeric, 13-20 digits) - in URLs and JSON
        text = re.sub(
            r'(energy_site[s]?[/\s:=]+["\']?)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle standalone long numeric IDs (Tesla energy site IDs in various contexts)
        text = re.sub(
            r'(\bsite\s+)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers in JSON format ('vin': 'XXX' or "vin": "XXX")
        text = re.sub(
            r'(["\']vin["\']:\s*["\'])([A-HJ-NPR-Z0-9]{17})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers plain format
        text = re.sub(
            r'(\bvin[\s:=]+)([A-HJ-NPR-Z0-9]{17})\b',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle DIN numbers in JSON format
        text = re.sub(
            r'(["\']din["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle DIN numbers plain format
        text = re.sub(
            r'(\bdin[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers in JSON format
        text = re.sub(
            r'(["\']serial_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers plain format
        text = re.sub(
            r'(serial[\s_]?(?:number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs in JSON format
        text = re.sub(
            r'(["\']gateway_id["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs plain format
        text = re.sub(
            r'(gateway[\s_]?(?:id)?[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers in JSON format
        text = re.sub(
            r'(["\']warp_site_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers plain format
        text = re.sub(
            r'(warp[\s_]?(?:site)?(?:[\s_]?number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle asset_site_id (UUIDs)
        text = re.sub(
            r'(["\']asset_site_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle device_id (UUIDs)
        text = re.sub(
            r'(["\']device_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        return text

    def _obfuscate_arg(self, arg) -> Any:
        """Obfuscate an argument only if it contains sensitive data, preserving type otherwise."""
        # Convert to string for pattern matching
        str_value = str(arg)
        obfuscated = self._obfuscate_string(str_value)

        # Only return string version if obfuscation actually changed something
        # This preserves numeric types for format specifiers like %d and %.3f
        if obfuscated != str_value:
            return obfuscated
        return arg

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        # Only convert args to strings if obfuscation patterns match
        # This preserves numeric types for format specifiers like %d and %.3f
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._obfuscate_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_arg(a) for a in record.args)

        return True


_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SensitiveDataFilter())


class AmberWebSocketClient:
    """
    Async WebSocket client for Amber Electric real-time price updates.

    Maintains a persistent connection to Amber's WebSocket API and caches
    the latest price data for instant access. Auto-reconnects with exponential
    backoff on connection failures.

    Designed for Home Assistant's asyncio event loop.
    """

    WS_URL = "wss://api-ws.amber.com.au"

    def __init__(self, api_token: str, site_id: str, sync_callback=None):
        """
        Initialize WebSocket client.

        Args:
            api_token: Amber API token (PSK key)
            site_id: Amber site ID to subscribe to
            sync_callback: Optional async callback function to trigger Tesla sync on price updates
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

        _LOGGER.info(f"AmberWebSocketClient initialized for site {site_id} (token: {api_token[:8]}...)")

    async def start(self):
        """Start the WebSocket client in a background thread (like Flask version)."""
        if self._running:
            _LOGGER.warning("WebSocket client already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name="AmberWebSocket")
        self._thread.start()
        _LOGGER.info("✅ WebSocket client thread started")

    def _run_event_loop(self):
        """Run the asyncio event loop in the background thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            _LOGGER.info("🔄 WebSocket thread event loop created")
            self._loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            _LOGGER.error(f"💥 Event loop error: {e}", exc_info=True)
            self._error_count += 1
            self._last_error = str(e)
        finally:
            if self._loop:
                self._loop.close()
            _LOGGER.info("WebSocket thread event loop closed")

    async def stop(self):
        """Stop the WebSocket client and clean up."""
        _LOGGER.info("Stopping WebSocket client")
        self._running = False

        # Schedule cleanup in the event loop if it exists
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self._loop)

        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        _LOGGER.info("WebSocket client stopped")

    async def _cleanup(self):
        """Clean up WebSocket connection."""
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception as e:
                _LOGGER.error(f"Error closing WebSocket: {e}")
            finally:
                self._websocket = None

    async def _connect_and_listen(self):
        """Connect to WebSocket and listen for messages with auto-reconnect."""
        _LOGGER.info("🚀 WebSocket task started, entering connection loop")
        while self._running:
            try:
                self._connection_status = "connecting"
                _LOGGER.info(f"Connecting to Amber WebSocket at {self.WS_URL}")

                # Set up headers with authentication
                headers = {
                    "authorization": f"Bearer {self.api_token}"
                }

                # Connect to WebSocket (no custom SSL context - match Flask behavior)
                async with websockets.connect(
                    self.WS_URL,
                    additional_headers=headers,
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
                    subscribe_json = json.dumps(subscribe_message)
                    _LOGGER.info(f"📡 Sending subscription: {subscribe_json}")
                    await websocket.send(subscribe_json)
                    _LOGGER.info(f"📡 Subscription sent for site {self.site_id}, waiting for response...")

                    # Listen for messages with periodic status logging
                    _LOGGER.info("🎧 Listening for WebSocket messages...")
                    last_status_log = datetime.now(timezone.utc)
                    message_wait_timeout = 120  # Log status every 2 minutes if no messages

                    while self._running:
                        try:
                            # Wait for message with timeout for status logging
                            message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=message_wait_timeout
                            )
                            self._handle_message(message)
                        except asyncio.TimeoutError:
                            # No message received within timeout - log status
                            elapsed = (datetime.now(timezone.utc) - last_status_log).total_seconds()
                            _LOGGER.debug(
                                f"⏳ WebSocket waiting for messages... "
                                f"(no data for {elapsed:.0f}s, connection: {self._connection_status}, "
                                f"messages received: {self._message_count})"
                            )
                            last_status_log = datetime.now(timezone.utc)
                            # Continue waiting - connection is still open
                            continue
                        except websockets.exceptions.ConnectionClosedOK as e:
                            # Clean closure (1000 normal, 1001 going away) - not an error, just reconnect
                            _LOGGER.debug(f"WebSocket closed cleanly: {e.code} {e.reason}")
                            break  # Exit inner loop to reconnect
                        except websockets.exceptions.ConnectionClosedError as e:
                            # Error closure (1002+) - log as warning
                            _LOGGER.warning(f"WebSocket connection closed with error: {e.code} {e.reason}")
                            break  # Exit inner loop to reconnect
                        except websockets.exceptions.ConnectionClosed as e:
                            # Catch-all for any other connection closed scenarios
                            _LOGGER.debug(f"WebSocket connection closed: {e.code} {e.reason}")
                            break  # Exit inner loop to reconnect
                        except Exception as e:
                            # Only log as error if it's NOT a connection closed exception
                            _LOGGER.error(f"Error handling message: {e}", exc_info=True)
                            self._error_count += 1
                            self._last_error = str(e)

                    # If we exit the loop normally (not due to exception), connection closed cleanly
                    _LOGGER.debug("WebSocket message loop ended, will reconnect")

            except websockets.exceptions.ConnectionClosedOK as e:
                # Clean closure during connect/subscribe - just reconnect
                _LOGGER.debug(f"WebSocket closed cleanly during setup: {e.code} {e.reason}")
                self._connection_status = "disconnected"
                self._websocket = None
            except websockets.exceptions.ConnectionClosed as e:
                _LOGGER.debug(f"WebSocket connection closed: {e.code} {e.reason}")
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
                _LOGGER.debug(f"WebSocket reconnecting in {self._reconnect_delay}s...")
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
            _LOGGER.info(f"📨 Raw WebSocket message ({len(message)} bytes): {message}")

            data = json.loads(message)

            # Log parsed message structure
            _LOGGER.info(f"📨 Parsed message keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
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

                # Store the converted price data (thread-safe)
                with self._price_lock:
                    self._cached_prices = converted_prices
                    self._last_update = datetime.now(timezone.utc)
                    self._stale_warning_logged = False  # Reset stale warning on fresh data

                # Log the price update
                general_price = converted_prices.get("general", {}).get("perKwh")
                feedin_price = converted_prices.get("feedIn", {}).get("perKwh")
                if general_price is not None and feedin_price is not None:
                    _LOGGER.info(f"💰 Price update: buy={general_price:.2f}¢/kWh, sell={feedin_price:.2f}¢/kWh")

                # Notify coordinator when price data arrives (cooldown prevents notification spam)
                if self._should_trigger_sync():
                    self._trigger_sync(converted_prices)

            elif data.get("type") == "subscription-success":
                _LOGGER.info("✅ Subscription confirmed by server")

            elif data.get("type") == "error":
                error_msg = data.get("message", "Unknown error")
                _LOGGER.error(f"WebSocket error from server: {error_msg}")
                self._error_count += 1
                self._last_error = error_msg

            else:
                # Log ALL unhandled messages at INFO level for debugging
                # This helps us understand what Amber actually sends
                _LOGGER.info(f"📨 Unhandled message - action={data.get('action')}, type={data.get('type')}, service={data.get('service')}, keys={list(data.keys())}")

        except json.JSONDecodeError as e:
            _LOGGER.error(f"Failed to parse WebSocket message as JSON: {e}")
            self._error_count += 1
            self._last_error = f"JSON parse error: {e}"

        except Exception as e:
            _LOGGER.error(f"Error processing WebSocket message: {e}", exc_info=True)
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
            _LOGGER.debug(f"Sync cooldown active ({elapsed:.0f}s < {self._sync_cooldown_seconds}s)")
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
            executor.submit(self._sync_callback, prices_data)

            # Don't wait for completion - fire and forget
            _LOGGER.info("📡 Notified sync coordinator of WebSocket price update")

        except Exception as e:
            _LOGGER.error(f"Error notifying sync coordinator: {e}", exc_info=True)

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
        with self._price_lock:
            if not self._cached_prices or not self._last_update:
                _LOGGER.debug(f"WebSocket cache empty: cached_prices={bool(self._cached_prices)}, last_update={self._last_update}, message_count={self._message_count}")
                return None

            # Check if data is stale
            age = (datetime.now(timezone.utc) - self._last_update).total_seconds()
            if age > max_age_seconds:
                # Only warn once until data becomes fresh again (debounce spam)
                if not self._stale_warning_logged:
                    _LOGGER.info(f"Cached WebSocket data is {age:.1f}s old (max: {max_age_seconds}s) - using REST fallback")
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
            has_cached = bool(self._cached_prices)

        return {
            "status": self._connection_status,
            "connected": self._connection_status == "connected",
            "last_update": last_update_str,
            "age_seconds": age_seconds,
            "message_count": self._message_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "has_cached_data": has_cached,
        }

    async def ensure_running(self) -> bool:
        """
        Check if WebSocket thread is alive and restart if needed.

        Call this periodically (e.g., from coordinator update) to ensure
        the WebSocket connection stays alive even after unexpected failures.

        Returns:
            bool: True if thread was restarted, False if already running
        """
        if not self._running:
            # Client was explicitly stopped, don't restart
            return False

        if self._thread is None or not self._thread.is_alive():
            _LOGGER.warning("WebSocket thread died unexpectedly - restarting...")
            self._connection_status = "reconnecting"
            self._thread = threading.Thread(
                target=self._run_event_loop,
                daemon=True,
                name="AmberWebSocket"
            )
            self._thread.start()
            _LOGGER.info("WebSocket thread restarted successfully")
            return True

        return False
