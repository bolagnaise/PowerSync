"""Helper functions and decorators for Flask routes

This module contains reusable decorators and utilities to reduce
duplication in route handlers.
"""

import logging
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

from flask import jsonify, flash, redirect, url_for, request, current_app
from flask_login import current_user

from app import db

logger = logging.getLogger(__name__)


def get_api_user():
    """
    Get the authenticated user from either session login or Bearer token.
    For API endpoints that need to support both web and mobile access.

    Returns the user if authenticated, None otherwise.
    """
    # First check session login
    if current_user.is_authenticated:
        return current_user

    # Then check Bearer token
    from app.models import User
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ', 1)[1]
        if token:
            user = User.query.filter_by(battery_health_api_token=token).first()
            if user:
                return user

    return None


# ============================================================================
# API Client Validation Decorators
# ============================================================================

def require_tesla_client(f):
    """Decorator to ensure Tesla client is available

    Validates that a Tesla API client can be created for the current user.
    If not available, returns appropriate error response based on request type.
    Injects tesla_client and api_user as keyword arguments to the decorated function.

    Supports both session login and Bearer token authentication.

    Usage:
        @bp.route('/api/tesla/something')
        @login_required  # or @api_login_required for token support
        @require_tesla_client
        def my_route(tesla_client, api_user=None):
            # tesla_client is guaranteed to be available here
            # api_user is the authenticated user (from session or token)
            pass
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app.api_clients import get_tesla_client

        # Get user from session or Bearer token
        user = get_api_user()
        if not user:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('main.login'))

        tesla_client = get_tesla_client(user)
        if not tesla_client:
            logger.warning(f"Tesla client not available for {f.__name__}")
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Tesla API not configured'}), 400
            flash('Please configure your Tesla API credentials first.')
            return redirect(url_for('main.settings'))

        kwargs['api_user'] = user
        return f(tesla_client=tesla_client, *args, **kwargs)

    return decorated_function


def require_amber_client(f):
    """Decorator to ensure Amber client is available

    Validates that an Amber API client can be created for the current user.
    If not available, returns appropriate error response based on request type.
    Injects amber_client and api_user as keyword arguments to the decorated function.

    Supports both session login and Bearer token authentication.

    Usage:
        @bp.route('/api/amber/something')
        @login_required  # or @api_login_required for token support
        @require_amber_client
        def my_route(amber_client, api_user=None):
            # amber_client is guaranteed to be available here
            # api_user is the authenticated user (from session or token)
            pass
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app.api_clients import get_amber_client

        # Get user from session or Bearer token
        user = get_api_user()
        if not user:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('main.login'))

        amber_client = get_amber_client(user)
        if not amber_client:
            logger.warning(f"Amber client not available for {f.__name__}")
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Amber API not configured'}), 400
            flash('Please configure your Amber API credentials first.')
            return redirect(url_for('main.settings'))

        kwargs['api_user'] = user
        return f(amber_client=amber_client, *args, **kwargs)

    return decorated_function


def require_tesla_site_id(f):
    """Decorator to ensure Tesla site ID is configured

    Validates that the current user has a Tesla energy site ID configured.
    If not available, returns appropriate error response based on request type.

    Supports both session login and Bearer token authentication.

    Usage:
        @bp.route('/api/tesla/something')
        @login_required  # or use require_tesla_client which handles auth
        @require_tesla_site_id
        def my_route(api_user=None):
            # site ID is guaranteed to be configured here
            pass
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get user from kwargs (set by require_tesla_client) or from session/token
        user = kwargs.get('api_user') or get_api_user()
        if not user:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('main.login'))

        if not user.tesla_energy_site_id:
            logger.warning(f"No Tesla site ID configured for {f.__name__}")
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'No Tesla site ID configured'}), 400
            flash('Please configure your Tesla energy site ID first.')
            return redirect(url_for('main.settings'))

        # Ensure api_user is in kwargs
        if 'api_user' not in kwargs:
            kwargs['api_user'] = user

        return f(*args, **kwargs)

    return decorated_function


# ============================================================================
# Database Transaction Helper
# ============================================================================

def db_commit_with_retry(max_retries=3, retry_delay=0.5):
    """Commit database session with retry logic for SQLite locking.

    SQLite can return "database is locked" errors when multiple processes
    try to write simultaneously. This function retries the commit with
    exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Initial delay between retries in seconds (default: 0.5)

    Raises:
        Exception: The last exception if all retries fail
    """
    import sqlite3

    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            db.session.commit()
            return  # Success
        except Exception as e:
            last_exception = e
            # Check if it's a SQLite locking error
            is_locked = (
                'database is locked' in str(e).lower() or
                (hasattr(e, 'orig') and isinstance(e.orig, sqlite3.OperationalError))
            )

            if is_locked and attempt < max_retries:
                wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(
                    f"Database locked on attempt {attempt + 1}/{max_retries + 1}, "
                    f"retrying in {wait_time}s..."
                )
                db.session.rollback()
                time.sleep(wait_time)
            else:
                # Not a locking error or out of retries
                db.session.rollback()
                raise

    # Should not reach here, but just in case
    raise last_exception


@contextmanager
def db_transaction(success_msg=None, error_msg=None, logger_context=None, retry_on_lock=True):
    """Context manager for database transactions with error handling

    Automatically commits on success, rolls back on error, handles logging
    and flash messages. Includes retry logic for SQLite database locking.

    Args:
        success_msg: Flash message to show on successful commit
        error_msg: Flash message to show on error
        logger_context: Context string for log messages
        retry_on_lock: Whether to retry on SQLite locking errors (default: True)

    Usage:
        try:
            with db_transaction(
                success_msg='Settings saved!',
                error_msg='Failed to save settings.',
                logger_context='Save user settings'
            ):
                current_user.some_field = new_value
                # ... more database operations
        except Exception:
            return redirect(url_for('main.error_page'))
    """
    try:
        yield
        if retry_on_lock:
            db_commit_with_retry()
        else:
            db.session.commit()
        if success_msg:
            flash(success_msg)
        if logger_context:
            logger.info(f"{logger_context}: Success")
    except Exception as e:
        db.session.rollback()
        error_detail = f"{logger_context or 'Database operation'} failed: {e}"
        logger.error(error_detail)
        if error_msg:
            flash(error_msg)
        raise


# ============================================================================
# Background Task Helpers
# ============================================================================

def start_background_task(target_func, *args, **kwargs):
    """Start a background thread with Flask app context

    Automatically passes the Flask app context as the first argument
    and starts a daemon thread.

    Args:
        target_func: Function to run in background (must accept app as first arg)
        *args: Additional positional arguments
        **kwargs: Additional keyword arguments

    Returns:
        threading.Thread: The started thread

    Usage:
        def my_background_task(app, user_id, data):
            with app.app_context():
                # ... do work
                pass

        start_background_task(my_background_task, current_user.id, some_data)
    """
    thread = threading.Thread(
        target=target_func,
        args=(current_app._get_current_object(),) + args,
        kwargs=kwargs
    )
    thread.daemon = True
    thread.start()
    logger.info(f"Started background task: {target_func.__name__}")
    return thread


def restore_tariff_background(app, user_id, site_id, tariff_data,
                               callback=None, profile_name="Tariff"):
    """Unified background task to restore tariff to Tesla Powerwall

    This function handles the complete flow of restoring a tariff:
    1. Switch Powerwall to self_consumption mode
    2. Upload the tariff
    3. Execute optional callback for database updates
    4. Wait 60 seconds for Tesla to process
    5. Switch back to autonomous mode

    Args:
        app: Flask app context
        user_id: User ID to restore tariff for
        site_id: Tesla energy site ID
        tariff_data: Complete tariff JSON dict to upload
        callback: Optional function(user, db) to update database after upload
        profile_name: Descriptive name for logging

    Usage:
        def my_callback(user, db):
            user.some_field = new_value
            # db.session.commit() is called automatically after callback

        restore_tariff_background(
            app, user_id, site_id, tariff_json,
            callback=my_callback,
            profile_name="My Custom Tariff"
        )
    """
    with app.app_context():
        try:
            from app.models import User
            from app.api_clients import get_tesla_client

            # Get user
            user = User.query.get(user_id)
            if not user:
                logger.error(f"Background restore [{profile_name}]: User {user_id} not found")
                return

            # Get Tesla client
            tesla_client = get_tesla_client(user)
            if not tesla_client:
                logger.error(f"Background restore [{profile_name}]: Failed to get Tesla client")
                return

            logger.info(f"========== Background Tariff Restore: {profile_name} ==========")

            # Step 1: Switch to self_consumption mode
            logger.info(f"Background restore [{profile_name}]: Step 1 - Switching to self_consumption mode")
            mode_result = tesla_client.set_operation_mode(site_id, 'self_consumption')
            if not mode_result:
                logger.error(f"Background restore [{profile_name}]: Failed to switch to self_consumption mode")
                return
            logger.info(f"Background restore [{profile_name}]: ✓ Switched to self_consumption mode")

            # Step 2: Upload tariff
            logger.info(f"Background restore [{profile_name}]: Step 2 - Uploading tariff to Tesla")
            upload_result = tesla_client.set_tariff_rate(site_id, tariff_data)
            if not upload_result:
                logger.error(f"Background restore [{profile_name}]: ✗ Tariff upload failed")
                # Try to switch back to autonomous even on failure
                logger.info(f"Background restore [{profile_name}]: Attempting to switch back to autonomous after failure")
                tesla_client.set_operation_mode(site_id, 'autonomous')
                return
            logger.info(f"Background restore [{profile_name}]: ✓ Tariff uploaded successfully")

            # Step 3: Execute callback for database updates
            if callback:
                logger.info(f"Background restore [{profile_name}]: Step 3 - Executing database callback")
                try:
                    callback(user, db)
                    db.session.commit()
                    logger.info(f"Background restore [{profile_name}]: ✓ Database updates completed")
                except Exception as e:
                    logger.error(f"Background restore [{profile_name}]: ✗ Database callback failed: {e}")
                    db.session.rollback()

            # Step 4: Wait for Tesla to process the tariff
            logger.info(f"Background restore [{profile_name}]: Step 4 - Waiting 60 seconds for Tesla to process tariff...")
            time.sleep(60)
            logger.info(f"Background restore [{profile_name}]: ✓ Wait completed")

            # Step 5: Switch back to autonomous mode
            logger.info(f"Background restore [{profile_name}]: Step 5 - Switching back to autonomous mode")
            autonomous_result = tesla_client.set_operation_mode(site_id, 'autonomous')
            if autonomous_result:
                logger.info(f"Background restore [{profile_name}]: ✓ Switched to autonomous mode")
                logger.info(f"========== Background Restore Complete: {profile_name} ==========")
            else:
                logger.error(f"Background restore [{profile_name}]: ✗ Failed to switch back to autonomous mode")
                logger.warning(f"Your Powerwall may still be in self_consumption mode. Please check Tesla app.")

        except Exception as e:
            logger.error(f"Background restore [{profile_name}]: Unexpected error: {e}")
            logger.error(f"Background restore [{profile_name}]: Traceback:\n{traceback.format_exc()}")
