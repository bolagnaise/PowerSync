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

# Set up logging
log_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'flask.log')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()  # Also log to console
    ]
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
            trigger=CronTrigger(minute='*/5', second='0'),  # Fallback if WebSocket fails (waits 60s for WebSocket)
            id='sync_tou_schedules',
            name='Sync TOU schedules from Amber to Tesla',
            replace_existing=True
        )

        # Add job to save price history every 5 minutes for continuous tracking
        scheduler.add_job(
            func=run_save_price_history,
            trigger=CronTrigger(minute='*/5', second='35'),
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
        # Aligned with WebSocket price updates at :35 seconds
        scheduler.add_job(
            func=run_solar_curtailment_check,
            trigger=CronTrigger(minute='*/5', second='35'),
            id='solar_curtailment_check',
            name='Check Amber export prices for solar curtailment',
            replace_existing=True
        )

        # Start the scheduler
        scheduler.start()
        logger.info("✅ Background scheduler started:")
        logger.info("  - TOU sync: WebSocket event-driven (primary) + cron fallback every 5 minutes at :00 (waits 60s for WebSocket)")
        logger.info("  - Price history collection will run every 5 minutes at :35 seconds")
        logger.info("  - Energy usage logging will run every minute (Teslemetry allows 1/min)")
        logger.info("  - Solar curtailment check will run every 5 minutes at :35 seconds (aligned with WebSocket prices)")
        logger.info("  - AEMO price monitoring will run every 1 minute at :35 seconds for spike detection")

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

    try:
        # Try to acquire exclusive lock (non-blocking)
        websocket_lock_file = open(websocket_lock_file_path, 'w')
        fcntl.flock(websocket_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # If we got here, we acquired the lock - this worker will run the WebSocket client
        logger.info("🔒 This worker acquired the WebSocket lock - initializing WebSocket client")

        try:
            from app.websocket_client import AmberWebSocketClient
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
                        # Create callback function to TRIGGER SYNC when WebSocket receives price update
                        # This is the PRIMARY sync trigger - WebSocket price arrival starts everything
                        def websocket_sync_callback(prices_data):
                            """
                            EVENT-DRIVEN SYNC: WebSocket price arrival triggers immediate sync.
                            This is the primary trigger - cron jobs are just fallback.
                            """
                            from app.tasks import get_sync_coordinator, sync_all_users_with_websocket_data, save_price_history_with_websocket_data

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

                                    logger.info("✅ Event-driven sync completed successfully")
                                except Exception as e:
                                    logger.error(f"❌ Error in event-driven sync: {e}", exc_info=True)

                        # Initialize and start WebSocket client with enhanced callback
                        ws_client = AmberWebSocketClient(decrypted_token, site_id, sync_callback=websocket_sync_callback)
                        ws_client.start()

                        # Store in app config for access by routes and tasks
                        app.config['AMBER_WEBSOCKET_CLIENT'] = ws_client

                        # Register cleanup on app teardown
                        def cleanup_websocket():
                            logger.info("Cleaning up WebSocket client")
                            ws_client.stop()
                            fcntl.flock(websocket_lock_file.fileno(), fcntl.LOCK_UN)
                            websocket_lock_file.close()
                            logger.info("🔓 WebSocket client shut down and lock released")

                        atexit.register(cleanup_websocket)

                        logger.info("🔌 Amber WebSocket client initialized and started")
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

    logger.info("Flask application created successfully")
    return app

