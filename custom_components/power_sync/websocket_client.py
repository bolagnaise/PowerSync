"""Amber Electric WebSocket client for real-time price updates (interval-based polling version)"""
import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
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
    Interval-based WebSocket client for Amber Electric price updates.

    Connects to Amber's WebSocket API at each 5-minute interval boundary,
    fetches the current price, then disconnects. This approach avoids
    rate limiting issues that occur with persistent connections.

    Price intervals align with Amber's 5-minute pricing blocks:
    :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
    """

    WS_URL = "wss://api-ws.amber.com.au"
    INTERVAL_MINUTES = 5  # Amber price interval

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
        self._running = False
        self._thread = None
        self._loop = None

        # Price cache (thread-safe with lock)
        self._price_lock = threading.Lock()
        self._cached_prices: Dict[str, Any] = {}
        self._last_update: Optional[datetime] = None

        # Health monitoring
        self._connection_status = "disconnected"  # disconnected, connecting, connected
        self._message_count = 0
        self._fetch_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None

        # Stale cache warning debounce (only warn once until data is fresh again)
        self._stale_warning_logged = False

        # Tesla sync triggering
        self._sync_callback = sync_callback
        self._last_sync_trigger: Optional[datetime] = None
        self._sync_cooldown_seconds = 60  # Minimum 60s between sync triggers

        _LOGGER.info(f"AmberWebSocketClient initialized for site {site_id} (interval-based polling mode)")

    async def start(self):
        """Start the WebSocket client in a background thread."""
        if self._running:
            _LOGGER.warning("WebSocket client already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name="AmberWebSocket")
        self._thread.start()
        _LOGGER.info("WebSocket client thread started (interval-based polling)")

    def _run_event_loop(self):
        """Run the asyncio event loop in the background thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            _LOGGER.info("WebSocket thread event loop created")
            self._loop.run_until_complete(self._interval_polling_loop())
        except Exception as e:
            _LOGGER.error(f"Event loop error: {e}", exc_info=True)
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

        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        _LOGGER.info("WebSocket client stopped")

    def _get_next_interval_time(self) -> datetime:
        """
        Calculate the next 5-minute interval boundary.

        Returns:
            datetime: Next interval time (e.g., if now is 14:07, returns 14:10)
        """
        now = datetime.now(timezone.utc)
        # Round up to next 5-minute boundary
        minutes = now.minute
        next_interval_minute = ((minutes // self.INTERVAL_MINUTES) + 1) * self.INTERVAL_MINUTES

        if next_interval_minute >= 60:
            # Roll over to next hour
            next_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            next_time = now.replace(minute=next_interval_minute, second=0, microsecond=0)

        return next_time

    def _get_seconds_until_next_interval(self) -> float:
        """
        Calculate seconds until the next 5-minute interval.

        Returns:
            float: Seconds to wait (adds 10 seconds buffer for price to be available)
        """
        next_interval = self._get_next_interval_time()
        now = datetime.now(timezone.utc)
        wait_seconds = (next_interval - now).total_seconds()

        # Add 10 second buffer - connect shortly after interval starts
        # Price may take up to 45s to arrive, handled by timeout in _fetch_price_once
        return max(0, wait_seconds) + 10

    async def _interval_polling_loop(self):
        """
        Main loop that connects at each 5-minute interval.

        1. Wait until next interval boundary (+5s buffer)
        2. Connect to WebSocket
        3. Subscribe and wait for price update
        4. Disconnect
        5. Repeat
        """
        _LOGGER.info("Starting interval-based polling loop")

        # Do an immediate fetch on startup
        await self._fetch_price_once()

        while self._running:
            try:
                # Calculate wait time until next interval
                wait_seconds = self._get_seconds_until_next_interval()
                next_interval = self._get_next_interval_time()

                _LOGGER.debug(
                    f"Next price fetch at {next_interval.strftime('%H:%M:%S')} UTC "
                    f"(waiting {wait_seconds:.0f}s)"
                )

                # Wait until next interval (check running status periodically)
                wait_start = datetime.now(timezone.utc)
                while self._running:
                    remaining = wait_seconds - (datetime.now(timezone.utc) - wait_start).total_seconds()
                    if remaining <= 0:
                        break
                    # Sleep in small chunks to allow clean shutdown
                    await asyncio.sleep(min(remaining, 5))

                if not self._running:
                    break

                # Fetch price at this interval
                await self._fetch_price_once()

            except Exception as e:
                _LOGGER.error(f"Error in polling loop: {e}", exc_info=True)
                self._error_count += 1
                self._last_error = str(e)
                # Wait before retrying
                await asyncio.sleep(30)

    async def _fetch_price_once(self):
        """
        Connect to WebSocket, fetch current price, then disconnect.

        This is a single fetch operation:
        1. Connect to WebSocket
        2. Send subscription
        3. Wait for price update (with timeout)
        4. Store price in cache
        5. Disconnect
        """
        self._fetch_count += 1
        self._connection_status = "connecting"

        try:
            _LOGGER.debug(f"Connecting to Amber WebSocket for price fetch #{self._fetch_count}")

            headers = {
                "authorization": f"Bearer {self.api_token}"
            }

            # Connect with short timeout since we only need one message
            async with websockets.connect(
                self.WS_URL,
                additional_headers=headers,
                close_timeout=5,
            ) as websocket:
                self._connection_status = "connected"

                # Send subscription request
                subscribe_message = {
                    "service": "live-prices",
                    "action": "subscribe",
                    "data": {
                        "siteId": self.site_id
                    }
                }
                await websocket.send(json.dumps(subscribe_message))
                _LOGGER.debug(f"Subscription sent for site {self.site_id}")

                # Wait for messages (subscription confirmation + price update)
                # Timeout after 60 seconds - prices can arrive up to 45s after interval start
                price_received = False
                timeout = 60
                start_time = datetime.now(timezone.utc)

                while not price_received:
                    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                    if elapsed > timeout:
                        _LOGGER.warning(f"Timeout waiting for price update after {timeout}s")
                        break

                    try:
                        message = await asyncio.wait_for(
                            websocket.recv(),
                            timeout=timeout - elapsed
                        )
                        price_received = self._handle_message(message)
                    except asyncio.TimeoutError:
                        _LOGGER.warning("Timeout waiting for WebSocket message")
                        break

                # Connection will be closed by context manager

            self._connection_status = "disconnected"

            if price_received:
                _LOGGER.info(f"Price fetch #{self._fetch_count} successful")
            else:
                _LOGGER.warning(f"Price fetch #{self._fetch_count} completed without price update")

        except Exception as e:
            self._connection_status = "disconnected"
            _LOGGER.error(f"Error fetching price: {e}")
            self._error_count += 1
            self._last_error = str(e)

    def _handle_message(self, message: str) -> bool:
        """
        Handle incoming WebSocket message.

        Args:
            message: Raw message string from WebSocket

        Returns:
            bool: True if this was a price update message
        """
        try:
            data = json.loads(message)
            self._message_count += 1

            # Validate message structure
            if not isinstance(data, dict):
                _LOGGER.warning(f"Unexpected message format (not a dict): {type(data)}")
                return False

            # Handle subscription confirmation
            if data.get("action") == "subscribe" and data.get("status") == 200:
                _LOGGER.debug("Subscription confirmed by server")
                return False

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
                    return False

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
                    self._stale_warning_logged = False

                # Log the price update
                general_price = converted_prices.get("general", {}).get("perKwh")
                feedin_price = converted_prices.get("feedIn", {}).get("perKwh")
                if general_price is not None and feedin_price is not None:
                    _LOGGER.info(f"Price update: buy={general_price:.2f}c/kWh, sell={feedin_price:.2f}c/kWh")

                # Notify coordinator when price data arrives
                if self._should_trigger_sync():
                    self._trigger_sync(converted_prices)

                return True

            elif data.get("type") == "subscription-success":
                _LOGGER.debug("Subscription confirmed by server")
                return False

            elif data.get("type") == "error":
                error_msg = data.get("message", "Unknown error")
                _LOGGER.error(f"WebSocket error from server: {error_msg}")
                self._error_count += 1
                self._last_error = error_msg
                return False

            else:
                _LOGGER.debug(f"Unhandled message type: action={data.get('action')}, type={data.get('type')}")
                return False

        except json.JSONDecodeError as e:
            _LOGGER.error(f"Failed to parse WebSocket message as JSON: {e}")
            self._error_count += 1
            self._last_error = f"JSON parse error: {e}"
            return False

        except Exception as e:
            _LOGGER.error(f"Error processing WebSocket message: {e}", exc_info=True)
            self._error_count += 1
            self._last_error = str(e)
            return False

    def _should_trigger_sync(self) -> bool:
        """
        Check if enough time has passed since last sync trigger.

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

        Args:
            prices_data: Dictionary with price data to pass to coordinator
        """
        if not self._sync_callback:
            return

        try:
            self._last_sync_trigger = datetime.now(timezone.utc)

            # Run callback in separate thread to avoid blocking
            from concurrent.futures import ThreadPoolExecutor
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WS-Notify")
            executor.submit(self._sync_callback, prices_data)

            _LOGGER.debug("Notified sync coordinator of price update")

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
                _LOGGER.debug(f"WebSocket cache empty: cached_prices={bool(self._cached_prices)}, last_update={self._last_update}")
                return None

            # Check if data is stale
            age = (datetime.now(timezone.utc) - self._last_update).total_seconds()
            if age > max_age_seconds:
                if not self._stale_warning_logged:
                    _LOGGER.info(f"Cached WebSocket data is {age:.1f}s old (max: {max_age_seconds}s) - using REST fallback")
                    self._stale_warning_logged = True
                return None

            # Convert to Amber API format
            result = []

            if "general" in self._cached_prices:
                general_data = self._cached_prices["general"].copy()
                general_data["channelType"] = "general"
                general_data["type"] = "CurrentInterval"
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
            "mode": "interval-polling",
            "interval_minutes": self.INTERVAL_MINUTES,
            "connected": self._connection_status == "connected",
            "last_update": last_update_str,
            "age_seconds": age_seconds,
            "message_count": self._message_count,
            "fetch_count": self._fetch_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "has_cached_data": has_cached,
        }

    async def ensure_running(self) -> bool:
        """
        Check if WebSocket thread is alive and restart if needed.

        Returns:
            bool: True if thread was restarted, False if already running
        """
        if not self._running:
            return False

        if self._thread is None or not self._thread.is_alive():
            _LOGGER.warning("WebSocket thread died unexpectedly - restarting...")
            self._thread = threading.Thread(
                target=self._run_event_loop,
                daemon=True,
                name="AmberWebSocket"
            )
            self._thread.start()
            _LOGGER.info("WebSocket thread restarted successfully")
            return True

        return False
