#!/usr/bin/env python3
"""
Standalone OCPP 1.6J Central System WebSocket Server

This script runs the OCPP server as a standalone service, separate from Flask.
It connects to the same SQLite database as Flask to store charger data.

Usage:
    python ocpp_server.py [--host HOST] [--port PORT] [--db-path PATH]

Environment variables:
    OCPP_HOST: Host to bind to (default: 0.0.0.0)
    OCPP_PORT: Port to listen on (default: 9000)
    DATABASE_PATH: Path to SQLite database (default: instance/app.db)
    FLASK_APP_PATH: Path to Flask app for config (default: /home/pi/power-sync)

Example:
    # Run directly
    python ocpp_server.py

    # Or with options
    python ocpp_server.py --host 0.0.0.0 --port 9000

    # Run as systemd service
    sudo systemctl start powersync-ocpp
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('ocpp_server')

# Try to import required libraries
try:
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:
    logger.error("websockets library not found. Install with: pip install websockets")
    sys.exit(1)

try:
    from ocpp.routing import on, after
    from ocpp.v16 import ChargePoint as OcppChargePoint
    from ocpp.v16 import call, call_result
    from ocpp.v16.enums import (
        Action,
        RegistrationStatus,
        AuthorizationStatus,
        ChargePointStatus,
        RemoteStartStopStatus,
        ChargingProfilePurposeType,
        ChargingProfileKindType,
        ResetType,
        ResetStatus,
        ClearChargingProfileStatus,
    )
except ImportError:
    logger.error("ocpp library not found. Install with: pip install ocpp")
    sys.exit(1)

try:
    import sqlite3
except ImportError:
    logger.error("sqlite3 not available")
    sys.exit(1)


class DatabaseManager:
    """Handle SQLite database operations for charger data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_tables()

    def _ensure_tables(self):
        """Ensure OCPP tables exist in the database."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Check if tables exist
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='ocpp_charger'
            """)

            if not cursor.fetchone():
                logger.info("Creating OCPP database tables...")

                # Create OCPPCharger table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS ocpp_charger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        charger_id VARCHAR(100) UNIQUE NOT NULL,
                        display_name VARCHAR(100),
                        vendor VARCHAR(100),
                        model VARCHAR(100),
                        serial_number VARCHAR(100),
                        firmware_version VARCHAR(50),
                        is_connected BOOLEAN DEFAULT 0,
                        status VARCHAR(50) DEFAULT 'Unavailable',
                        error_code VARCHAR(50),
                        last_boot DATETIME,
                        last_seen DATETIME,
                        current_transaction_id INTEGER,
                        current_power_kw FLOAT,
                        current_energy_kwh FLOAT,
                        current_soc INTEGER,
                        meter_value_kwh FLOAT,
                        max_power_kw FLOAT DEFAULT 7.4,
                        enable_automations BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Create OCPPTransaction table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS ocpp_transaction (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        charger_id INTEGER NOT NULL,
                        user_id INTEGER,
                        transaction_id INTEGER NOT NULL,
                        connector_id INTEGER DEFAULT 1,
                        id_tag VARCHAR(50),
                        start_time DATETIME,
                        stop_time DATETIME,
                        meter_start FLOAT,
                        meter_stop FLOAT,
                        energy_kwh FLOAT,
                        stop_reason VARCHAR(50),
                        FOREIGN KEY (charger_id) REFERENCES ocpp_charger(id)
                    )
                """)

                conn.commit()
                logger.info("OCPP database tables created successfully")

        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
        finally:
            conn.close()

    def get_connection(self):
        """Get a new database connection."""
        return sqlite3.connect(self.db_path)

    def update_charger_connected(self, charger_id: str, connected: bool):
        """Update charger connection status."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ocpp_charger
                SET is_connected = ?, last_seen = ?, status = CASE WHEN ? = 0 THEN 'Unavailable' ELSE status END
                WHERE charger_id = ?
            """, (connected, datetime.utcnow(), connected, charger_id))
            conn.commit()

            if cursor.rowcount == 0 and connected:
                logger.info(f"New charger '{charger_id}' - will be registered on BootNotification")
        except Exception as e:
            logger.error(f"Database error updating charger status: {e}")
        finally:
            conn.close()

    def update_charger(self, charger_id: str, **kwargs):
        """Update charger record with arbitrary fields."""
        if not kwargs:
            return

        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            # Check if charger exists
            cursor.execute("SELECT id, user_id FROM ocpp_charger WHERE charger_id = ?", (charger_id,))
            row = cursor.fetchone()

            if not row:
                # Create new charger - find first user
                cursor.execute("SELECT id FROM user LIMIT 1")
                user_row = cursor.fetchone()
                user_id = user_row[0] if user_row else 1

                # Insert new charger
                cursor.execute("""
                    INSERT INTO ocpp_charger (user_id, charger_id, is_connected, last_seen)
                    VALUES (?, ?, 1, ?)
                """, (user_id, charger_id, datetime.utcnow()))
                conn.commit()
                logger.info(f"Auto-registered new charger: {charger_id}")

            # Build update query
            kwargs['last_seen'] = datetime.utcnow()
            set_clause = ', '.join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [charger_id]

            cursor.execute(f"""
                UPDATE ocpp_charger SET {set_clause} WHERE charger_id = ?
            """, values)
            conn.commit()

        except Exception as e:
            logger.error(f"Database error updating charger: {e}")
        finally:
            conn.close()

    def create_transaction(self, charger_id: str, transaction_id: int,
                          connector_id: int, id_tag: str, meter_start: int):
        """Create a new charging transaction."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            # Get charger db id and user_id
            cursor.execute("SELECT id, user_id FROM ocpp_charger WHERE charger_id = ?", (charger_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning(f"Charger {charger_id} not found for transaction")
                return

            db_charger_id, user_id = row

            cursor.execute("""
                INSERT INTO ocpp_transaction
                (charger_id, user_id, transaction_id, connector_id, id_tag, start_time, meter_start)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (db_charger_id, user_id, transaction_id, connector_id, id_tag,
                  datetime.utcnow(), meter_start / 1000.0))

            # Update charger current transaction
            cursor.execute("""
                UPDATE ocpp_charger
                SET current_transaction_id = ?, current_energy_kwh = 0
                WHERE charger_id = ?
            """, (transaction_id, charger_id))

            conn.commit()

        except Exception as e:
            logger.error(f"Database error creating transaction: {e}")
        finally:
            conn.close()

    def stop_transaction(self, charger_id: str, transaction_id: int,
                        meter_stop: int, reason: str):
        """Update transaction with stop data."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            # Get charger db id
            cursor.execute("SELECT id FROM ocpp_charger WHERE charger_id = ?", (charger_id,))
            row = cursor.fetchone()
            if not row:
                return

            db_charger_id = row[0]

            # Update transaction
            cursor.execute("""
                UPDATE ocpp_transaction
                SET stop_time = ?, meter_stop = ?, stop_reason = ?,
                    energy_kwh = ? - COALESCE(meter_start, 0)
                WHERE charger_id = ? AND transaction_id = ?
            """, (datetime.utcnow(), meter_stop / 1000.0, reason,
                  meter_stop / 1000.0, db_charger_id, transaction_id))

            # Clear charger current transaction
            cursor.execute("""
                UPDATE ocpp_charger
                SET current_transaction_id = NULL, current_energy_kwh = NULL, current_power_kw = NULL
                WHERE charger_id = ?
            """, (charger_id,))

            conn.commit()

        except Exception as e:
            logger.error(f"Database error stopping transaction: {e}")
        finally:
            conn.close()


class ChargePointHandler(OcppChargePoint):
    """OCPP 1.6 ChargePoint message handler."""

    def __init__(self, charger_id: str, websocket, server: 'OCPPCentralSystem'):
        super().__init__(charger_id, websocket)
        self.server = server
        self.db = server.db

        # Track state
        self.vendor: Optional[str] = None
        self.model: Optional[str] = None
        self.status: str = 'Unavailable'
        self.current_transaction_id: Optional[int] = None

    @on(Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor: str,
                                   charge_point_model: str, **kwargs):
        """Handle BootNotification from charger."""
        logger.info(f"BootNotification from {self.id}: {charge_point_vendor} {charge_point_model}")

        self.vendor = charge_point_vendor
        self.model = charge_point_model

        # Update database
        self.db.update_charger(
            self.id,
            vendor=charge_point_vendor,
            model=charge_point_model,
            serial_number=kwargs.get('charge_point_serial_number'),
            firmware_version=kwargs.get('firmware_version'),
            is_connected=True,
            last_boot=datetime.utcnow(),
        )

        return call_result.BootNotification(
            current_time=datetime.now(tz=timezone.utc).isoformat(),
            interval=60,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self):
        """Handle Heartbeat from charger."""
        self.db.update_charger(self.id, last_seen=datetime.utcnow())
        return call_result.Heartbeat(
            current_time=datetime.now(tz=timezone.utc).isoformat()
        )

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id: int, error_code: str,
                                     status: str, **kwargs):
        """Handle StatusNotification from charger."""
        logger.info(f"StatusNotification from {self.id}: connector={connector_id}, "
                   f"status={status}, error={error_code}")

        self.status = status
        self.db.update_charger(self.id, status=status, error_code=error_code)

        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id: int, id_tag: str,
                                   meter_start: int, timestamp: str, **kwargs):
        """Handle StartTransaction from charger."""
        import random
        transaction_id = random.randint(1, 999999)

        logger.info(f"StartTransaction from {self.id}: connector={connector_id}, "
                   f"id_tag={id_tag}, transaction_id={transaction_id}")

        self.current_transaction_id = transaction_id

        self.db.create_transaction(
            self.id, transaction_id, connector_id, id_tag, meter_start
        )

        return call_result.StartTransaction(
            transaction_id=transaction_id,
            id_tag_info={'status': AuthorizationStatus.accepted},
        )

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop: int, timestamp: str,
                                  transaction_id: int, **kwargs):
        """Handle StopTransaction from charger."""
        reason = kwargs.get('reason', 'Local')

        logger.info(f"StopTransaction from {self.id}: transaction_id={transaction_id}, "
                   f"reason={reason}, meter_stop={meter_stop}")

        self.current_transaction_id = None
        self.db.stop_transaction(self.id, transaction_id, meter_stop, reason)

        return call_result.StopTransaction(
            id_tag_info={'status': AuthorizationStatus.accepted},
        )

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        """Handle MeterValues from charger."""
        power_w = None
        energy_wh = None
        soc = None

        for mv in meter_value:
            for sv in mv.get('sampledValue', []):
                measurand = sv.get('measurand', 'Energy.Active.Import.Register')
                value = float(sv.get('value', 0))

                if 'Power' in measurand:
                    power_w = value
                elif 'Energy' in measurand:
                    energy_wh = value
                elif 'SoC' in measurand:
                    soc = int(value)

        update_data = {}
        if power_w is not None:
            update_data['current_power_kw'] = power_w / 1000.0
        if energy_wh is not None:
            update_data['current_energy_kwh'] = energy_wh / 1000.0
            update_data['meter_value_kwh'] = energy_wh / 1000.0
        if soc is not None:
            update_data['current_soc'] = soc

        if update_data:
            self.db.update_charger(self.id, **update_data)

        return call_result.MeterValues()

    @on(Action.authorize)
    async def on_authorize(self, id_tag: str):
        """Handle Authorize request - accept all tags."""
        logger.info(f"Authorize request from {self.id}: id_tag={id_tag}")
        return call_result.Authorize(
            id_tag_info={'status': AuthorizationStatus.accepted}
        )

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id: str, **kwargs):
        """Handle DataTransfer - vendor-specific messages."""
        logger.info(f"DataTransfer from {self.id}: vendor={vendor_id}")
        return call_result.DataTransfer(status='Accepted')

    # Remote command methods
    async def remote_start_transaction(self, id_tag: str, connector_id: int = 1) -> bool:
        """Send RemoteStartTransaction command."""
        try:
            request = call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
            response = await self.call(request)
            return response.status == RemoteStartStopStatus.accepted
        except Exception as e:
            logger.error(f"RemoteStartTransaction error: {e}")
            return False

    async def remote_stop_transaction(self, transaction_id: int) -> bool:
        """Send RemoteStopTransaction command."""
        try:
            request = call.RemoteStopTransaction(transaction_id=transaction_id)
            response = await self.call(request)
            return response.status == RemoteStartStopStatus.accepted
        except Exception as e:
            logger.error(f"RemoteStopTransaction error: {e}")
            return False

    async def set_charging_profile(self, connector_id: int, limit_watts: int,
                                   duration_seconds: Optional[int] = None) -> bool:
        """Set a charging power limit."""
        try:
            request = call.SetChargingProfile(
                connector_id=connector_id,
                cs_charging_profiles={
                    'chargingProfileId': 1,
                    'stackLevel': 0,
                    'chargingProfilePurpose': 'TxDefaultProfile',
                    'chargingProfileKind': 'Relative',
                    'chargingSchedule': {
                        'chargingRateUnit': 'W',
                        'chargingSchedulePeriod': [{'startPeriod': 0, 'limit': float(limit_watts)}],
                    },
                },
            )
            response = await self.call(request)
            return response.status == 'Accepted'
        except Exception as e:
            logger.error(f"SetChargingProfile error: {e}")
            return False

    async def reset(self, hard: bool = False) -> bool:
        """Reset the charge point."""
        try:
            reset_type = ResetType.hard if hard else ResetType.soft
            request = call.Reset(type=reset_type)
            response = await self.call(request)
            return response.status == ResetStatus.accepted
        except Exception as e:
            logger.error(f"Reset error: {e}")
            return False


class OCPPCentralSystem:
    """
    OCPP Central System - Standalone WebSocket Server.

    Accepts connections from OCPP 1.6J chargers and handles the protocol.
    """

    def __init__(self, host: str, port: int, db_path: str):
        self.host = host
        self.port = port
        self.db = DatabaseManager(db_path)
        self._server = None
        self._running = False
        self._charge_points: Dict[str, ChargePointHandler] = {}

    async def on_connect(self, websocket: WebSocketServerProtocol, path: str):
        """Handle new WebSocket connection from a charge point."""
        charger_id = path.strip('/')

        if not charger_id:
            logger.warning("Connection without charger ID, rejecting")
            await websocket.close(1008, "Missing charger ID in path")
            return

        requested_protocols = websocket.request_headers.get('Sec-WebSocket-Protocol', '')
        logger.info(f"Charger '{charger_id}' connecting (protocols: {requested_protocols})")

        # Create handler
        handler = ChargePointHandler(charger_id, websocket, self)
        self._charge_points[charger_id] = handler

        # Update database
        self.db.update_charger_connected(charger_id, True)

        try:
            await handler.start()
        except Exception as e:
            logger.error(f"Error handling charger '{charger_id}': {e}")
        finally:
            self._charge_points.pop(charger_id, None)
            self.db.update_charger_connected(charger_id, False)
            logger.info(f"Charger '{charger_id}' disconnected")

    async def start(self):
        """Start the OCPP WebSocket server."""
        logger.info(f"Starting OCPP Central System on ws://{self.host}:{self.port}")

        self._server = await websockets.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=['ocpp1.6'],
            ping_interval=30,
            ping_timeout=60,
        )

        self._running = True
        logger.info(f"OCPP server listening on ws://{self.host}:{self.port}")

        # Keep running until stopped
        await self._server.wait_closed()

    def stop(self):
        """Stop the server."""
        logger.info("Stopping OCPP server...")
        self._running = False
        if self._server:
            self._server.close()

    def get_connected_chargers(self) -> list:
        """Get list of connected charger IDs."""
        return list(self._charge_points.keys())

    def get_charge_point(self, charger_id: str) -> Optional[ChargePointHandler]:
        """Get a connected charge point handler."""
        return self._charge_points.get(charger_id)


def run_migrations(app_path: Path) -> bool:
    """
    Run Flask database migrations.

    Args:
        app_path: Path to the Flask application directory

    Returns:
        True if migrations ran successfully or weren't needed
    """
    import subprocess

    logger.info("Checking for database migrations...")

    try:
        # Run flask db upgrade
        env = os.environ.copy()
        env['FLASK_APP'] = 'run.py'

        result = subprocess.run(
            [str(app_path / 'venv' / 'bin' / 'flask'), 'db', 'upgrade'],
            cwd=str(app_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            if 'Running upgrade' in result.stdout:
                logger.info("Database migrations applied successfully")
            else:
                logger.info("Database schema is up to date")
            return True
        else:
            logger.warning(f"Migration command returned non-zero: {result.stderr}")
            return False

    except FileNotFoundError:
        logger.warning("Flask not found in venv, skipping migrations")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Migration timed out")
        return False
    except Exception as e:
        logger.warning(f"Could not run migrations: {e}")
        return True  # Continue anyway, tables will be created if needed


def main():
    """Main entry point for standalone OCPP server."""
    parser = argparse.ArgumentParser(
        description='Power Sync OCPP 1.6J Central System Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run with default settings
    python ocpp_server.py

    # Run on custom port
    python ocpp_server.py --port 9001

    # Specify database path
    python ocpp_server.py --db-path /path/to/app.db

    # Skip migrations (if already run by systemd)
    python ocpp_server.py --skip-migrate

Environment Variables:
    OCPP_HOST       Host to bind to (default: 0.0.0.0)
    OCPP_PORT       Port to listen on (default: 9000)
    DATABASE_PATH   Path to SQLite database
        """
    )

    parser.add_argument('--host', default=os.environ.get('OCPP_HOST', '0.0.0.0'),
                       help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=int(os.environ.get('OCPP_PORT', 9000)),
                       help='Port to listen on (default: 9000)')
    parser.add_argument('--db-path', default=os.environ.get('DATABASE_PATH'),
                       help='Path to SQLite database')
    parser.add_argument('--skip-migrate', action='store_true',
                       help='Skip running database migrations')

    args = parser.parse_args()

    # Determine database path
    db_path = args.db_path
    if not db_path:
        # Try common locations
        script_dir = Path(__file__).parent
        candidates = [
            script_dir / 'instance' / 'app.db',
            script_dir / 'app.db',
            Path.home() / 'power-sync' / 'instance' / 'app.db',
            Path('/home/pi/power-sync/instance/app.db'),
        ]

        for candidate in candidates:
            if candidate.exists():
                db_path = str(candidate)
                break

        if not db_path:
            # Use default location
            db_path = str(script_dir / 'instance' / 'app.db')
            os.makedirs(script_dir / 'instance', exist_ok=True)

    logger.info(f"Using database: {db_path}")

    # Run database migrations (unless skipped)
    if not args.skip_migrate:
        script_dir = Path(__file__).parent
        # Try to find Flask app directory
        app_paths = [
            script_dir,
            Path('/home/pi/power-sync'),
            Path.home() / 'power-sync',
        ]
        for app_path in app_paths:
            if (app_path / 'run.py').exists() and (app_path / 'venv').exists():
                run_migrations(app_path)
                break

    # Create server
    server = OCPPCentralSystem(args.host, args.port, db_path)

    # Handle shutdown signals
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        server.stop()
        loop.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        server.stop()
        loop.close()
        logger.info("OCPP server stopped")


if __name__ == '__main__':
    main()
