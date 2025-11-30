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
log_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'flask.log')

# Create handlers
file_handler = logging.FileHandler(log_file)
console_handler = logging.StreamHandler()

# Add filter to both handlers
sensitive_filter = SensitiveDataFilter()
file_handler.addFilter(sensitive_filter)
console_handler.addFilter(sensitive_filter)

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # Redirect to login page if user is not authenticated
cache = Cache()

def create_app(config_class=Config):
    logger.info("Creating Flask application")
    app = Flask(__name__)
    app.config.from_object(config_class)

    logger.info("Initializing database and extensions")
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)

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

    try:
        # Try to acquire exclusive lock (non-blocking)
        lock_file = open(lock_file_path, 'w')
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # If we got here, we acquired the lock - this worker will run the scheduler
        logger.info("🔒 This worker acquired the scheduler lock - initializing background scheduler")
        scheduler = BackgroundScheduler()

        # Add job to sync all users' TOU schedules every 5 minutes (aligned with Amber's update cycle)
        from app.tasks import sync_all_users, save_price_history, save_energy_usage, monitor_aemo_prices, solar_curtailment_check

        # Wrapper functions to run tasks within app context
        def run_sync_all_users():
            with app.app_context():
                sync_all_users()

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

        scheduler.add_job(
            func=run_sync_all_users,
            trigger=CronTrigger(minute='1-59/5', second='0'),  # Run at :01, :06, :11, etc. (60s after Amber price updates)
            id='sync_tou_schedules',
            name='Sync TOU schedules from Amber to Tesla',
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

        # Start the scheduler
        scheduler.start()
        logger.info("✅ Background scheduler started:")
        logger.info("  - TOU sync: WebSocket event-driven (primary) + REST API fallback every 5 minutes at :01")
        logger.info("  - Solar curtailment: WebSocket event-driven (primary) + REST API fallback every 5 minutes at :01")
        logger.info("  - Price history: WebSocket event-driven (primary) + REST API fallback every 5 minutes at :01")
        logger.info("  - Energy usage logging: every minute (Teslemetry allows 1/min)")
        logger.info("  - AEMO price monitoring: every 1 minute at :35 seconds for spike detection")

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
            EVENT-DRIVEN SYNC: WebSocket price arrival triggers immediate sync.
            This is the primary trigger - cron jobs are just fallback.
            """
            from app.tasks import get_sync_coordinator, sync_all_users_with_websocket_data, save_price_history_with_websocket_data, solar_curtailment_with_websocket_data

            # Notify coordinator (for period deduplication)
            coordinator = get_sync_coordinator()
            coordinator.notify_websocket_update(prices_data)

            # Check if we should sync this period (prevents duplicates)
            if not coordinator.should_sync_this_period():
                logger.info("⏭️  WebSocket price received but already synced this period, skipping")
                return

            # TRIGGER SYNC IMMEDIATELY with WebSocket data (event-driven!)
            logger.info("🚀 WebSocket price received - triggering immediate sync (event-driven)")

            # Run sync in app context (needed for database operations)
            with app.app_context():
                try:
                    # 1. Sync TOU to Tesla with WebSocket price
                    sync_all_users_with_websocket_data(prices_data)

                    # 2. Save price history with WebSocket price
                    save_price_history_with_websocket_data(prices_data)

                    # 3. Check solar curtailment with WebSocket price
                    solar_curtailment_with_websocket_data(prices_data)

                    logger.info("✅ Event-driven sync completed successfully")
                except Exception as e:
                    logger.error(f"❌ Error in event-driven sync: {e}", exc_info=True)

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

    logger.info("Flask application created successfully")
    return app

