# app/__init__.py
from flask import Flask, request
from config import Config
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_caching import Cache
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import atexit
import fcntl
import os
import re
import random
import time


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that obfuscates sensitive data like API keys and tokens.
    Shows first 4 and last 4 characters with asterisks in between.
    """

    # Patterns to match sensitive data
    PATTERNS = [
        # Bearer tokens (Amber PSK, Teslemetry, etc.)
        (re.compile(r'(Bearer\s+)([a-zA-Z0-9_-]{20,})', re.IGNORECASE), r'\1'),
        # Amber PSK keys (psk_...)
        (re.compile(r'(psk_)([a-zA-Z0-9]{20,})', re.IGNORECASE), r'\1'),
        # Generic API keys/tokens (32+ hex chars)
        (re.compile(r'(["\']?(?:api[_-]?key|token|secret|password|authorization)["\']?\s*[=:]\s*["\']?)([a-zA-Z0-9_-]{20,})(["\']?)', re.IGNORECASE), None),
    ]

    @staticmethod
    def obfuscate(value, show_chars=4):
        """Obfuscate a string showing only first and last N characters."""
        if len(value) <= show_chars * 2:
            return '*' * len(value)
        return f"{value[:show_chars]}{'*' * (len(value) - show_chars * 2)}{value[-show_chars:]}"

    def _obfuscate_string(self, text):
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

        # Handle psk_ tokens
        text = re.sub(
            r'(psk_)([a-zA-Z0-9]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle authorization headers in websocket logs
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

    def filter(self, record):
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._obfuscate_string(str(v)) if isinstance(v, str) else v
                              for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_string(str(a)) if isinstance(a, str) else a
                                   for a in record.args)

        return True


# Set up logging with sensitive data filter
# Use persistent log directory that survives container restarts
from logging.handlers import RotatingFileHandler

# Persistent log directory - /app/data/logs in Docker, or local data/logs for development
log_dir = os.environ.get('LOG_DIR', '/app/data/logs')
if not os.path.exists(log_dir):
    try:
        os.makedirs(log_dir, exist_ok=True)
    except (PermissionError, OSError):
        # Fallback to local directory if /app/data/logs is not writable
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'logs')
        os.makedirs(log_dir, exist_ok=True)

log_file = os.path.join(log_dir, 'flask.log')

# Create handlers with rotation (5MB max, keep 5 backup files)
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=5*1024*1024,  # 5MB
    backupCount=5,
    encoding='utf-8'
)
console_handler = logging.StreamHandler()

# Add filter to both handlers
sensitive_filter = SensitiveDataFilter()
file_handler.addFilter(sensitive_filter)
console_handler.addFilter(sensitive_filter)

# Set format for handlers
log_format = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(log_format)
console_handler.setFormatter(log_format)

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)
logger.info(f"Logging to persistent file: {log_file}")

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # Redirect to login page if user is not authenticated
cache = Cache()


@login.unauthorized_handler
def unauthorized_callback():
    """Handle unauthorized access - return JSON for API routes, redirect for web routes."""
    from flask import request, jsonify, redirect, url_for
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': 'Session expired. Please refresh the page and log in again.'}), 401
    return redirect(url_for('main.login'))


def _repair_missing_columns(db, logger):
    """
    Repair schema if columns are missing but alembic_version says migration is done.
    This handles edge cases where migrations were recorded but not actually applied.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_columns = {col['name'] for col in inspector.get_columns('user')}

    # Define columns that should exist with their SQL definitions
    # Format: (column_name, sql_type, default_value)
    required_columns = [
        # Export boost fields (i2b3c4d5e6f7)
        ('export_boost_enabled', 'BOOLEAN', '0'),
        ('export_price_offset', 'FLOAT', '0.0'),
        ('export_min_price', 'FLOAT', '0.0'),
        ('export_boost_start', 'VARCHAR(5)', "'17:00'"),
        ('export_boost_end', 'VARCHAR(5)', "'21:00'"),
        ('export_boost_threshold', 'FLOAT', '0.0'),  # Activation threshold
        # Flow Power PEA fields (546b2ceef0e5)
        ('flow_power_base_rate', 'FLOAT', 'NULL'),
        ('pea_enabled', 'BOOLEAN', 'NULL'),
        ('pea_custom_value', 'FLOAT', 'NULL'),
        # Enphase JWT token
        ('inverter_token', 'VARCHAR(2000)', 'NULL'),
    ]

    missing_columns = [col for col in required_columns if col[0] not in existing_columns]

    if missing_columns:
        logger.warning(f"🔧 Schema repair needed: {len(missing_columns)} missing column(s) detected")
        with db.engine.connect() as conn:
            for col_name, col_type, default_val in missing_columns:
                try:
                    sql = f"ALTER TABLE user ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info(f"  ✅ Added missing column: {col_name}")
                except Exception as e:
                    logger.error(f"  ❌ Failed to add column {col_name}: {e}")
        logger.info("🔧 Schema repair completed")


def create_app(config_class=Config):
    logger.info("Creating Flask application")
    app = Flask(__name__)
    app.config.from_object(config_class)

    logger.info("Initializing database and extensions")
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)

    # Automatically run database migrations on startup
    with app.app_context():
        try:
            from flask_migrate import upgrade
            from alembic.runtime.migration import MigrationContext
            from alembic.script import ScriptDirectory

            # Get current database revision
            conn = db.engine.connect()
            context = MigrationContext.configure(conn)
            current_rev = context.get_current_revision()

            # Get the head revision from migrations
            config = migrate.get_config()
            script = ScriptDirectory.from_config(config)
            head_rev = script.get_current_head()

            if current_rev != head_rev:
                logger.info(f"📦 Database migration needed: {current_rev} -> {head_rev}")
                upgrade()
                logger.info("✅ Database migrations applied successfully")
            else:
                logger.debug("Database schema is up to date")

            conn.close()
        except Exception as e:
            logger.warning(f"Auto-migration check skipped: {e}")

        # Always run schema repair - handles cases where migrations failed or were skipped
        try:
            _repair_missing_columns(db, logger)
        except Exception as e:
            logger.warning(f"Schema repair skipped: {e}")

    # Initialize Flask-Caching for API response caching
    app.config['CACHE_TYPE'] = 'SimpleCache'  # In-memory cache
    app.config['CACHE_DEFAULT_TIMEOUT'] = 300  # Default 5 minutes
    cache.init_app(app)
    logger.info("Flask-Caching initialized with SimpleCache backend")

    from app.routes import bp as main_bp
    app.register_blueprint(main_bp)
    logger.info("Main blueprint registered")

    from app.custom_tou_routes import custom_tou_bp
    app.register_blueprint(custom_tou_bp)
    logger.info("Custom TOU blueprint registered")

    # Add Jinja2 template filter for timezone conversion
    @app.template_filter('user_timezone')
    def user_timezone_filter(dt):
        """Convert UTC datetime to user's local timezone"""
        if dt is None:
            return None

        from flask_login import current_user
        from datetime import datetime
        import pytz

        # Get user's timezone (default to UTC if not set)
        user_tz = pytz.timezone(current_user.timezone if hasattr(current_user, 'timezone') and current_user.timezone else 'UTC')

        # If datetime is naive (no timezone), assume it's UTC
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)

        # Convert to user's timezone
        return dt.astimezone(user_tz)

    # Add context processor to inject version into all templates
    @app.context_processor
    def inject_version():
        """Make version available to all templates"""
        from config import get_version
        return {'app_version': get_version()}

    # Add request logging
    @app.before_request
    def log_request():
        logger.info(f"REQUEST: {request.method} {request.path} from {request.remote_addr}")

    @app.after_request
    def log_response(response):
        logger.info(f"RESPONSE: {request.method} {request.path} -> {response.status_code}")
        return response

    # Initialize background scheduler for automatic TOU syncing and price history
    # Use file locking to ensure only ONE worker (in multi-worker setup) runs the scheduler
    lock_file_path = os.path.join(app.instance_path, 'scheduler.lock')
    os.makedirs(app.instance_path, exist_ok=True)

    # Add random delay to prevent race condition when multiple workers start simultaneously
    # Without this, workers can race to acquire the lock before any has actually written to the file
    time.sleep(random.uniform(0.1, 0.5))

    try:
        # Try to acquire exclusive lock (non-blocking)
        lock_file = open(lock_file_path, 'w')
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # If we got here, we acquired the lock - this worker will run the scheduler
        logger.info("🔒 This worker acquired the scheduler lock - initializing background scheduler")
        scheduler = BackgroundScheduler()

        # Add jobs for smart TOU sync (3-stage approach)
        from app.tasks import sync_initial_forecast, sync_rest_api_check, save_price_history, save_energy_usage, monitor_aemo_prices, solar_curtailment_check, demand_period_grid_charging_check, check_manual_discharge_expiry, check_manual_charge_expiry

        # Wrapper functions to run tasks within app context
        def run_sync_initial_forecast():
            """Stage 1: Initial forecast sync at start of 5-min period."""
            with app.app_context():
                # Ensure WebSocket thread is alive (restart if it died)
                ws_client = app.config.get('AMBER_WEBSOCKET_CLIENT')
                if ws_client:
                    ws_client.ensure_running()
                sync_initial_forecast()

        def run_sync_rest_api_check_35s():
            """Stage 3: REST API fallback check at 35s if no WebSocket."""
            with app.app_context():
                sync_rest_api_check(check_name="35s check")

        def run_sync_rest_api_check_60s():
            """Stage 4: Final REST API check at 60s if still no update."""
            with app.app_context():
                sync_rest_api_check(check_name="60s final")

        def run_save_price_history():
            with app.app_context():
                save_price_history()

        def run_save_energy_usage():
            with app.app_context():
                save_energy_usage()

        def run_monitor_aemo_prices():
            with app.app_context():
                monitor_aemo_prices()

        def run_solar_curtailment_check():
            with app.app_context():
                solar_curtailment_check()

        def run_demand_period_grid_charging_check():
            with app.app_context():
                demand_period_grid_charging_check()

        def run_check_manual_discharge_expiry():
            with app.app_context():
                check_manual_discharge_expiry()

        def run_check_manual_charge_expiry():
            with app.app_context():
                check_manual_charge_expiry()

        # STAGE 1: Initial forecast sync at start of each 5-min period (0s)
        # Gets predicted price to Tesla ASAP
        scheduler.add_job(
            func=run_sync_initial_forecast,
            trigger=CronTrigger(minute='0-59/5', second='0'),  # Run at :00, :05, :10, etc. (start of period)
            id='sync_initial_forecast',
            name='Stage 1: Initial forecast sync at start of period',
            replace_existing=True
        )

        # STAGE 2: WebSocket-triggered sync (handled by callback, not scheduler)
        # Re-syncs only if price differs from forecast

        # STAGE 3: REST API fallback check at 35s if WebSocket hasn't delivered
        scheduler.add_job(
            func=run_sync_rest_api_check_35s,
            trigger=CronTrigger(minute='0-59/5', second='35'),  # Run at :00:35, :05:35, etc.
            id='sync_rest_api_35s',
            name='Stage 3: REST API check at 35s if no WebSocket',
            replace_existing=True
        )

        # STAGE 4: Final REST API check at 60s (1 minute into period)
        scheduler.add_job(
            func=run_sync_rest_api_check_60s,
            trigger=CronTrigger(minute='1-59/5', second='0'),  # Run at :01, :06, :11, etc.
            id='sync_rest_api_60s',
            name='Stage 4: Final REST API check at 60s',
            replace_existing=True
        )

        # Add job to save price history every 5 minutes (same timing as TOU sync - 60s after Amber price updates)
        scheduler.add_job(
            func=run_save_price_history,
            trigger=CronTrigger(minute='1-59/5', second='0'),  # Run at :01, :06, :11, etc. (same as TOU sync)
            id='save_price_history',
            name='Save Amber price history to database',
            replace_existing=True
        )

        # Add job to save energy usage every minute for granular tracking (within Teslemetry 1/min limit)
        scheduler.add_job(
            func=run_save_energy_usage,
            trigger=CronTrigger(minute='*'),
            id='save_energy_usage',
            name='Save Tesla energy usage to database',
            replace_existing=True
        )

        # Add job to monitor AEMO prices every 1 minute for spike detection (more responsive to price spikes)
        scheduler.add_job(
            func=run_monitor_aemo_prices,
            trigger=CronTrigger(minute='*', second='35'),
            id='monitor_aemo_prices',
            name='Monitor AEMO NEM prices for spike detection',
            replace_existing=True
        )

        # Add job to check solar curtailment every 5 minutes (prevent export at negative prices)
        # Same timing as TOU sync - 60s after Amber price updates
        scheduler.add_job(
            func=run_solar_curtailment_check,
            trigger=CronTrigger(minute='1-59/5', second='0'),  # Run at :01, :06, :11, etc. (same as TOU sync)
            id='solar_curtailment_check',
            name='Check Amber export prices for solar curtailment',
            replace_existing=True
        )

        # Add job to check for expired manual discharge modes every minute
        scheduler.add_job(
            func=run_check_manual_discharge_expiry,
            trigger=CronTrigger(minute='*', second='15'),  # Every minute at :15 seconds
            id='check_manual_discharge_expiry',
            name='Check for expired manual discharge modes',
            replace_existing=True
        )

        # Add job to check for expired manual charge modes every minute
        scheduler.add_job(
            func=run_check_manual_charge_expiry,
            trigger=CronTrigger(minute='*', second='30'),  # Every minute at :30 seconds (offset from discharge)
            id='check_manual_charge_expiry',
            name='Check for expired manual charge modes',
            replace_existing=True
        )

        # Add job to check demand period grid charging every minute
        # Disables grid charging during peak demand periods to prevent increasing demand charges
        scheduler.add_job(
            func=run_demand_period_grid_charging_check,
            trigger=CronTrigger(minute='*', second='45'),  # Run every minute at :45 seconds
            id='demand_period_grid_charging_check',
            name='Check demand period and toggle grid charging',
            replace_existing=True
        )

        # Start the scheduler
        scheduler.start()
        logger.info("✅ Background scheduler started with SMART SYNC:")
        logger.info("  - Stage 1 (0s): Initial forecast sync at :00, :05, :10, etc.")
        logger.info("  - Stage 2 (WebSocket): Re-sync on price change (event-driven)")
        logger.info("  - Stage 3 (35s): REST API fallback if no WebSocket")
        logger.info("  - Stage 4 (60s): Final REST API check at :01, :06, :11, etc.")
        logger.info("  - Solar curtailment: WebSocket event-driven + REST API fallback at :01")
        logger.info("  - Price history: WebSocket event-driven + REST API fallback at :01")
        logger.info("  - Energy usage: every minute (Teslemetry allows 1/min)")
        logger.info("  - AEMO monitoring: every minute at :35 seconds")
        logger.info("  - Demand period grid charging: every 1 minute at :45 seconds")

        # Shut down the scheduler and release lock when exiting the app
        def cleanup():
            scheduler.shutdown()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            logger.info("🔓 Scheduler shut down and lock released")

        atexit.register(cleanup)

    except IOError:
        # Lock already held by another worker - skip scheduler initialization
        logger.info("⏭️  Another worker is running the scheduler - skipping initialization in this worker")

    # Initialize WebSocket client for real-time price updates (ONLY ONE WORKER)
    # Use file locking to ensure only ONE worker (in multi-worker setup) runs WebSocket client
    # This prevents duplicate sync triggers when Amber pushes price updates to all clients
    websocket_lock_file_path = os.path.join(app.instance_path, 'websocket.lock')

    # Define function to create WebSocket sync callback (reusable for reinit)
    def create_websocket_sync_callback():
        """Create callback function for WebSocket price updates."""
        def websocket_sync_callback(prices_data):
            """
            STAGE 2: WebSocket price arrival triggers re-sync IF price differs.

            Smart sync flow:
            - Stage 1 (0s): Initial forecast already synced
            - Stage 2 (WebSocket): Re-sync only if price differs from forecast
            - Stage 3 (35s): REST API fallback if no WebSocket
            - Stage 4 (60s): Final REST API check
            """
            from app.tasks import get_sync_coordinator, sync_all_users_with_websocket_data, save_price_history_with_websocket_data, solar_curtailment_with_websocket_data

            # Notify coordinator that WebSocket delivered (for REST API fallback checks)
            coordinator = get_sync_coordinator()
            coordinator.notify_websocket_update(prices_data)

            # TRIGGER SYNC with price comparison (sync_all_users_with_websocket_data handles the comparison)
            logger.info("📡 Stage 2: WebSocket price received - checking if re-sync needed")

            # Run sync in app context (needed for database operations)
            with app.app_context():
                try:
                    # 1. Re-sync TOU to Tesla if price changed (handles comparison internally)
                    sync_all_users_with_websocket_data(prices_data)

                    # 2. Save price history with WebSocket price
                    save_price_history_with_websocket_data(prices_data)

                    # 3. Check solar curtailment with WebSocket price
                    solar_curtailment_with_websocket_data(prices_data)

                    logger.info("✅ Stage 2 WebSocket sync completed")
                except Exception as e:
                    logger.error(f"❌ Error in Stage 2 WebSocket sync: {e}", exc_info=True)

        return websocket_sync_callback

    # Define function to initialize/reinitialize WebSocket client
    def init_websocket_client(api_token, site_id):
        """
        Initialize or reinitialize WebSocket client with given credentials.
        Stops existing client before starting new one.

        Args:
            api_token: Decrypted Amber API token
            site_id: Amber site ID to subscribe to

        Returns:
            AmberWebSocketClient instance or None if failed
        """
        from app.websocket_client import AmberWebSocketClient

        # Stop existing client if running
        existing_client = app.config.get('AMBER_WEBSOCKET_CLIENT')
        if existing_client:
            logger.info("🔄 Stopping existing WebSocket client for reinit...")
            existing_client.stop()

        try:
            # Create new client with sync callback
            ws_client = AmberWebSocketClient(
                api_token,
                site_id,
                sync_callback=create_websocket_sync_callback()
            )
            ws_client.start()

            # Store in app config
            app.config['AMBER_WEBSOCKET_CLIENT'] = ws_client
            logger.info(f"🔌 WebSocket client (re)initialized for site {site_id}")
            return ws_client
        except Exception as e:
            logger.error(f"Failed to initialize WebSocket client: {e}", exc_info=True)
            return None

    # Store reinit function in app config so routes can access it
    app.config['WEBSOCKET_INIT_FUNCTION'] = init_websocket_client

    # Add random delay to prevent race condition when multiple workers start simultaneously
    # This ensures proper lock acquisition order across workers
    time.sleep(random.uniform(0.1, 0.5))

    try:
        # Try to acquire exclusive lock (non-blocking)
        websocket_lock_file = open(websocket_lock_file_path, 'w')
        fcntl.flock(websocket_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # If we got here, we acquired the lock - this worker will run the WebSocket client
        logger.info("🔒 This worker acquired the WebSocket lock - initializing WebSocket client")
        app.config['WEBSOCKET_LOCK_ACQUIRED'] = True  # Flag for routes to check

        try:
            from app.models import User

            # Query database within application context
            with app.app_context():
                # Get the first user's Amber credentials (assuming single-user setup)
                # In multi-user setups, each user would need their own WebSocket client
                user = User.query.first()
                if user and user.amber_api_token_encrypted:
                    from app.utils import decrypt_token

                    # Decrypt the Amber API token
                    decrypted_token = decrypt_token(user.amber_api_token_encrypted)

                    # Use stored site ID if available, otherwise fetch and auto-select first site
                    site_id = user.amber_site_id
                    if site_id:
                        logger.info(f"Using stored Amber site ID: {site_id}")
                    else:
                        # Backward compatibility: fetch sites and use first one
                        from app.api_clients import get_amber_client
                        amber_client = get_amber_client(user)
                        if amber_client:
                            sites = amber_client.get_sites()
                            if sites:
                                site_id = sites[0]['id']
                                logger.info(f"No stored site ID - auto-selected first Amber site: {site_id}")

                    if site_id:
                        # Use the init function to create client
                        ws_client = init_websocket_client(decrypted_token, site_id)

                        if ws_client:
                            # Register cleanup on app teardown
                            def cleanup_websocket():
                                logger.info("Cleaning up WebSocket client")
                                ws_client = app.config.get('AMBER_WEBSOCKET_CLIENT')
                                if ws_client:
                                    ws_client.stop()
                                fcntl.flock(websocket_lock_file.fileno(), fcntl.LOCK_UN)
                                websocket_lock_file.close()
                                logger.info("🔓 WebSocket client shut down and lock released")

                            atexit.register(cleanup_websocket)
                    else:
                        logger.warning("No Amber site ID available - WebSocket client not started")
                else:
                    logger.info("No user with Amber credentials found - WebSocket client not started")

        except Exception as e:
            logger.error(f"Failed to initialize WebSocket client: {e}", exc_info=True)
            logger.warning("WebSocket client not available - will use REST API fallback")

    except IOError:
        # Lock already held by another worker - skip WebSocket initialization
        logger.info("⏭️  Another worker is running the WebSocket client - skipping initialization in this worker")
        app.config['WEBSOCKET_LOCK_ACQUIRED'] = False

    # Auto-start named Cloudflare tunnel if configured
    if not app.config.get('TESTING'):
        with app.app_context():
            try:
                from app.models import User
                import subprocess

                user = User.query.filter_by(cloudflare_tunnel_enabled=True).first()
                if user and user.cloudflare_tunnel_token_encrypted and user.cloudflare_tunnel_domain:
                    from app.routes import CloudflareTunnel, get_cloudflared_path
                    import app.routes as routes_module

                    logger.info(f"🚇 Checking tunnel auto-start for {user.cloudflare_tunnel_domain}")

                    # Check if cloudflared tunnel is already running
                    tunnel_already_running = False
                    try:
                        result = subprocess.run(
                            ['ps', 'aux'],
                            capture_output=True, text=True, timeout=5
                        )
                        if 'cloudflared' in result.stdout and 'tunnel' in result.stdout:
                            tunnel_already_running = True
                            logger.info("Found existing cloudflared process")
                    except Exception as e:
                        logger.debug(f"Process check failed: {e}")

                    if tunnel_already_running:
                        # Tunnel already running - just set the reference
                        logger.info(f"🚇 Cloudflare tunnel already running for {user.cloudflare_tunnel_domain}")
                        tunnel = CloudflareTunnel()
                        tunnel.public_url = f"https://{user.cloudflare_tunnel_domain}"
                        tunnel.tunnel_type = 'named'
                        routes_module._cloudflare_tunnel = tunnel
                    else:
                        cloudflared_bin = get_cloudflared_path()
                        if cloudflared_bin:
                            logger.info(f"Starting cloudflared from {cloudflared_bin}")
                            try:
                                from app.utils import decrypt_token
                                token = decrypt_token(user.cloudflare_tunnel_token_encrypted)
                                tunnel = CloudflareTunnel()
                                tunnel.start_named_tunnel(token)
                                tunnel.public_url = f"https://{user.cloudflare_tunnel_domain}"
                                routes_module._cloudflare_tunnel = tunnel
                                logger.info(f"🚇 Auto-started named tunnel for {user.cloudflare_tunnel_domain}")
                            except Exception as e:
                                logger.error(f"Failed to auto-start named tunnel: {e}")
                        else:
                            logger.warning("cloudflared not found - cannot auto-start named tunnel")
                else:
                    logger.debug("No user with tunnel auto-start enabled")
            except Exception as e:
                logger.error(f"Error checking for auto-start tunnel: {e}")

    logger.info("Flask application created successfully")
    return app

