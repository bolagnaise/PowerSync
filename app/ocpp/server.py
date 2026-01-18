# app/ocpp/server.py
"""
OCPP 1.6J Central System WebSocket server.

Runs in a background thread alongside Flask, managing connections from
OCPP-compliant EV chargers.
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Dict, Optional, Any, Callable
from functools import partial

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

# Global server instance
_ocpp_server: Optional['OCPPServer'] = None


def get_ocpp_server() -> Optional['OCPPServer']:
    """Get the global OCPP server instance."""
    return _ocpp_server


class OCPPServer:
    """
    OCPP Central System server running in a background thread.

    This server accepts WebSocket connections from OCPP chargers and handles
    the OCPP protocol using the mobilityhouse/ocpp library.
    """

    def __init__(self, host: str = '0.0.0.0', port: int = 9000, app=None):
        """
        Initialize the OCPP server.

        Args:
            host: Host address to bind to (default: all interfaces)
            port: Port number to listen on (default: 9000)
            app: Flask application instance for database access
        """
        self.host = host
        self.port = port
        self.app = app
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._running = False

        # Connected charge points: {charger_id: ChargePointHandler}
        self._charge_points: Dict[str, 'ChargePointHandler'] = {}

        # Event callbacks for automation triggers
        self._event_callbacks: list[Callable] = []

    def start(self):
        """Start the OCPP server in a background thread."""
        if not WEBSOCKETS_AVAILABLE:
            _LOGGER.error("websockets library not available. Install with: pip install websockets ocpp")
            return False

        if self._running:
            _LOGGER.warning("OCPP server already running")
            return True

        _LOGGER.info(f"Starting OCPP server on {self.host}:{self.port}")

        # Create a new event loop for the background thread
        self._loop = asyncio.new_event_loop()

        # Start the server in a background thread
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

        # Set global instance
        global _ocpp_server
        _ocpp_server = self

        self._running = True
        return True

    def stop(self):
        """Stop the OCPP server."""
        if not self._running:
            return

        _LOGGER.info("Stopping OCPP server...")
        self._running = False

        if self._loop:
            # Schedule server shutdown
            self._loop.call_soon_threadsafe(self._shutdown)

        if self._thread:
            self._thread.join(timeout=5)

        global _ocpp_server
        _ocpp_server = None

    def _run_server(self):
        """Run the asyncio event loop in the background thread."""
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            _LOGGER.error(f"OCPP server error: {e}")
        finally:
            self._loop.close()

    async def _serve(self):
        """Main server coroutine."""
        try:
            self._server = await websockets.serve(
                self._on_connect,
                self.host,
                self.port,
                subprotocols=['ocpp1.6'],
                ping_interval=30,
                ping_timeout=60,
            )
            _LOGGER.info(f"OCPP server listening on ws://{self.host}:{self.port}")

            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)

        except Exception as e:
            _LOGGER.error(f"Failed to start OCPP server: {e}")
            raise

    def _shutdown(self):
        """Shutdown the server (called from event loop thread)."""
        if self._server:
            self._server.close()

    async def _on_connect(self, websocket: 'WebSocketServerProtocol', path: str):
        """
        Handle new WebSocket connection from a charge point.

        The charger ID is extracted from the connection path:
        ws://server:9000/{charger_id}
        """
        # Extract charger ID from path (e.g., /charger123 -> charger123)
        charger_id = path.strip('/')
        if not charger_id:
            _LOGGER.warning("Connection without charger ID, rejecting")
            await websocket.close(1008, "Missing charger ID in path")
            return

        # Check requested subprotocol
        requested_protocols = websocket.request_headers.get('Sec-WebSocket-Protocol', '')
        _LOGGER.info(f"Charger '{charger_id}' connecting (protocols: {requested_protocols})")

        # Create charge point handler
        from .charge_point import ChargePointHandler
        handler = ChargePointHandler(charger_id, websocket, self)

        # Register the charge point
        self._charge_points[charger_id] = handler

        # Update database
        self._update_charger_connected(charger_id, True)

        try:
            # Start handling messages
            await handler.start()
        except Exception as e:
            _LOGGER.error(f"Error handling charger '{charger_id}': {e}")
        finally:
            # Unregister on disconnect
            self._charge_points.pop(charger_id, None)
            self._update_charger_connected(charger_id, False)
            _LOGGER.info(f"Charger '{charger_id}' disconnected")

    def _update_charger_connected(self, charger_id: str, connected: bool):
        """Update charger connection status in database."""
        if not self.app:
            return

        try:
            with self.app.app_context():
                from app import db
                from app.models import OCPPCharger

                charger = OCPPCharger.query.filter_by(charger_id=charger_id).first()
                if charger:
                    charger.is_connected = connected
                    charger.last_seen = datetime.utcnow()
                    if not connected:
                        charger.status = 'Unavailable'
                    db.session.commit()
                elif connected:
                    # New charger connecting - will be registered on BootNotification
                    _LOGGER.info(f"New charger '{charger_id}' - awaiting BootNotification")
        except Exception as e:
            _LOGGER.error(f"Database error updating charger status: {e}")

    def get_charge_point(self, charger_id: str) -> Optional['ChargePointHandler']:
        """Get a connected charge point handler by ID."""
        return self._charge_points.get(charger_id)

    def get_connected_chargers(self) -> list[str]:
        """Get list of connected charger IDs."""
        return list(self._charge_points.keys())

    def register_event_callback(self, callback: Callable):
        """
        Register a callback for OCPP events (for automation triggers).

        Callback signature: callback(event_type: str, charger_id: str, data: dict)
        """
        self._event_callbacks.append(callback)

    def emit_event(self, event_type: str, charger_id: str, data: dict):
        """Emit an event to all registered callbacks."""
        for callback in self._event_callbacks:
            try:
                callback(event_type, charger_id, data)
            except Exception as e:
                _LOGGER.error(f"Event callback error: {e}")

    # =========================================================================
    # Control Methods (called from Flask routes/automations)
    # =========================================================================

    def remote_start_transaction(
        self,
        charger_id: str,
        id_tag: str = "PowerSync",
        connector_id: int = 1
    ) -> bool:
        """
        Send RemoteStartTransaction to a charger.

        Args:
            charger_id: ID of the charger
            id_tag: Authorization tag (default: "PowerSync")
            connector_id: Connector to start (default: 1)

        Returns:
            True if command was accepted
        """
        cp = self.get_charge_point(charger_id)
        if not cp:
            _LOGGER.warning(f"Cannot start transaction: charger '{charger_id}' not connected")
            return False

        # Run async command in the server's event loop
        future = asyncio.run_coroutine_threadsafe(
            cp.remote_start_transaction(id_tag, connector_id),
            self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            _LOGGER.error(f"RemoteStartTransaction failed: {e}")
            return False

    def remote_stop_transaction(self, charger_id: str, transaction_id: int) -> bool:
        """
        Send RemoteStopTransaction to a charger.

        Args:
            charger_id: ID of the charger
            transaction_id: Transaction ID to stop

        Returns:
            True if command was accepted
        """
        cp = self.get_charge_point(charger_id)
        if not cp:
            _LOGGER.warning(f"Cannot stop transaction: charger '{charger_id}' not connected")
            return False

        future = asyncio.run_coroutine_threadsafe(
            cp.remote_stop_transaction(transaction_id),
            self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            _LOGGER.error(f"RemoteStopTransaction failed: {e}")
            return False

    def set_charging_profile(
        self,
        charger_id: str,
        connector_id: int,
        limit_watts: int,
        duration_seconds: Optional[int] = None
    ) -> bool:
        """
        Set a charging power limit on a charger.

        Args:
            charger_id: ID of the charger
            connector_id: Connector ID (usually 1)
            limit_watts: Power limit in watts
            duration_seconds: Optional duration for the limit

        Returns:
            True if command was accepted
        """
        cp = self.get_charge_point(charger_id)
        if not cp:
            _LOGGER.warning(f"Cannot set charging profile: charger '{charger_id}' not connected")
            return False

        future = asyncio.run_coroutine_threadsafe(
            cp.set_charging_profile(connector_id, limit_watts, duration_seconds),
            self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            _LOGGER.error(f"SetChargingProfile failed: {e}")
            return False

    def clear_charging_profile(self, charger_id: str, connector_id: int = 1) -> bool:
        """Clear any charging profiles on a connector."""
        cp = self.get_charge_point(charger_id)
        if not cp:
            return False

        future = asyncio.run_coroutine_threadsafe(
            cp.clear_charging_profile(connector_id),
            self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            _LOGGER.error(f"ClearChargingProfile failed: {e}")
            return False

    def reset_charger(self, charger_id: str, hard: bool = False) -> bool:
        """
        Reset a charger.

        Args:
            charger_id: ID of the charger
            hard: If True, perform hard reset; otherwise soft reset

        Returns:
            True if command was accepted
        """
        cp = self.get_charge_point(charger_id)
        if not cp:
            return False

        future = asyncio.run_coroutine_threadsafe(
            cp.reset(hard),
            self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            _LOGGER.error(f"Reset failed: {e}")
            return False
