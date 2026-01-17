# app/routes.py
from flask import render_template, flash, redirect, url_for, request, Blueprint, jsonify, session, current_app, send_file
from flask_login import login_user, logout_user, current_user, login_required
from app import db, cache
from app.models import User, PriceRecord, SavedTOUProfile, BatteryHealthHistory
from app.forms import LoginForm, RegistrationForm, SettingsForm, DemandChargeForm, AmberSettingsForm, TwoFactorSetupForm, TwoFactorVerifyForm, TwoFactorDisableForm, ChangePasswordForm
from app.utils import encrypt_token, decrypt_token
from app.api_clients import get_amber_client, get_tesla_client, AEMOAPIClient
from app.scheduler import TOUScheduler
from app.route_helpers import (
    require_tesla_client,
    require_amber_client,
    require_tesla_site_id,
    db_transaction,
    db_commit_with_retry,
    start_background_task,
    restore_tariff_background,
    get_api_user
)
import os
import requests
import time
import logging
import secrets
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode


# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def run_async_with_cleanup(coro):
    """Run an async coroutine in a new event loop with proper cleanup.

    This prevents 'Task was destroyed but it is pending!' errors by
    cancelling all pending tasks before closing the loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Cancel all pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        # Wait for cancellation to complete
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def get_user_from_api_token():
    """
    Get user from Bearer token in Authorization header.
    Returns None if no valid token found.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return None

    token = auth_header.split(' ', 1)[1]
    if not token:
        return None

    # Find user by API token (battery_health_api_token is used for mobile app auth)
    user = User.query.filter_by(battery_health_api_token=token).first()
    return user


def get_authenticated_user():
    """
    Get the authenticated user from either session login or Bearer token.
    Returns the user if authenticated, None otherwise.
    """
    # First check session login
    if current_user.is_authenticated:
        return current_user

    # Then check Bearer token
    return get_user_from_api_token()


def api_auth_required(f):
    """
    Decorator that allows either session login OR Bearer token authentication.
    Use this for API endpoints that should be accessible from both web and mobile.
    """
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({'error': 'Authentication required'}), 401
        # Make the user available as 'api_user' in the function
        kwargs['api_user'] = user
        return f(*args, **kwargs)
    return decorated_function


# Cache for NEM region auto-detection (keyed by site_id)
_nem_region_cache = {}

# NEM region to timezone mapping (for automation time triggers)
NEM_REGION_TIMEZONES = {
    "NSW1": "Australia/Sydney",
    "VIC1": "Australia/Melbourne",
    "QLD1": "Australia/Brisbane",
    "SA1": "Australia/Adelaide",
    "TAS1": "Australia/Hobart",
}


def _get_timezone_from_nem_region(nem_region: str) -> str:
    """Get IANA timezone from NEM region code."""
    return NEM_REGION_TIMEZONES.get(nem_region, "Australia/Sydney")


def _get_nem_region_from_amber_site(amber_client, site_id):
    """Auto-detect NEM region from Amber site's network field.

    Args:
        amber_client: AmberAPIClient instance
        site_id: Amber site ID (optional - will auto-select if not provided)

    Returns:
        NEM region code (NSW1, VIC1, etc.) or None
    """
    # Network to NEM region mapping
    NETWORK_TO_NEM_REGION = {
        # NSW networks
        "Ausgrid": "NSW1",
        "Endeavour Energy": "NSW1",
        "Essential Energy": "NSW1",
        # ACT network (part of NSW1 NEM region)
        "Evoenergy": "NSW1",
        # VIC networks
        "AusNet Services": "VIC1",
        "CitiPower": "VIC1",
        "Jemena": "VIC1",
        "Powercor": "VIC1",
        "United Energy": "VIC1",
        # QLD networks
        "Energex": "QLD1",
        "Ergon Energy": "QLD1",
        # SA networks
        "SA Power Networks": "SA1",
        # TAS networks
        "TasNetworks": "TAS1",
    }

    # If no site_id provided, fetch all sites and auto-select first active one
    if not site_id:
        logger.debug("No Amber site ID provided, fetching sites list...")
        try:
            response = requests.get(
                f"{amber_client.base_url}/sites",
                headers=amber_client.headers,
                timeout=10
            )
            if response.status_code == 200:
                sites = response.json()
                # Prefer active site
                active_sites = [s for s in sites if s.get("status") == "active"]
                if active_sites:
                    site_id = active_sites[0]["id"]
                elif sites:
                    site_id = sites[0]["id"]
                if site_id:
                    logger.info(f"Auto-selected Amber site: {site_id}")
            else:
                logger.debug(f"Failed to fetch Amber sites: HTTP {response.status_code}")
        except Exception as e:
            logger.debug(f"Error fetching Amber sites: {e}")

    if not site_id:
        logger.debug("Could not determine Amber site ID for NEM region auto-detection")
        return None

    # Check cache first
    if site_id in _nem_region_cache:
        return _nem_region_cache[site_id]

    try:
        # Fetch site info from Amber
        response = requests.get(
            f"{amber_client.base_url}/sites/{site_id}",
            headers=amber_client.headers,
            timeout=10
        )

        if response.status_code == 200:
            site_info = response.json()
            network = site_info.get("network")

            if network:
                nem_region = NETWORK_TO_NEM_REGION.get(network)
                if nem_region:
                    logger.info(f"Auto-detected NEM region: {nem_region} (network: {network})")
                    _nem_region_cache[site_id] = nem_region
                    return nem_region
                else:
                    logger.warning(f"Unknown network '{network}' - cannot determine NEM region")
            else:
                logger.debug("Amber site info doesn't include network field")
        else:
            logger.debug(f"Failed to fetch Amber site info: HTTP {response.status_code}")

    except Exception as e:
        logger.debug(f"Error auto-detecting NEM region: {e}")

    return None


def get_powerwall_timezone(user, default='Australia/Brisbane'):
    """
    Get the Powerwall's timezone from Tesla API.

    Caches the timezone in the session to avoid repeated API calls.
    Falls back to default timezone if Tesla API is unavailable.

    Args:
        user: Current user object
        default: Default timezone if Tesla API fails

    Returns:
        IANA timezone string (e.g., 'Australia/Sydney')
    """
    # Check if we have a cached timezone in the session (only for web requests with session)
    cache_key = f'powerwall_tz_{user.id}'
    try:
        if cache_key in session:
            return session[cache_key]
    except RuntimeError:
        # No session context (API token auth) - skip cache check
        pass

    # Try to fetch from Tesla API
    tesla_client = get_tesla_client(user)
    if tesla_client and user.tesla_energy_site_id:
        try:
            site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
            if site_info:
                tz = site_info.get('installation_time_zone')
                if tz:
                    logger.info(f"Fetched Powerwall timezone from Tesla API: {tz}")
                    # Cache in session for this login session (only if session available)
                    try:
                        session[cache_key] = tz
                    except RuntimeError:
                        pass  # No session context
                    return tz
        except Exception as e:
            logger.warning(f"Failed to fetch Powerwall timezone from Tesla API: {e}")

    # Fallback to default
    logger.debug(f"Using default timezone: {default}")
    return default


bp = Blueprint('main', __name__)

@bp.route('/')
@bp.route('/index')
def index():
    logger.info("Index page accessed")
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.login'))

@bp.route('/login', methods=['GET', 'POST'])
def login():
    logger.info(f"Login page accessed - Method: {request.method}")
    if current_user.is_authenticated:
        logger.info(f"User already authenticated: {current_user.email}")
        return redirect(url_for('main.dashboard'))

    # Check if registration should be allowed (single-user mode)
    allow_registration = User.query.count() == 0

    form = LoginForm()
    if form.validate_on_submit():
        logger.info(f"Login form submitted for email: {form.email.data}")
        user = User.query.filter_by(email=form.email.data).first()
        if user is None or not user.check_password(form.password.data):
            logger.warning(f"Failed login attempt for email: {form.email.data}")
            flash('Invalid email or password')
            return redirect(url_for('main.login'))
        logger.info(f"Successful login for user: {user.email}")

        # Check if 2FA is enabled
        if user.two_factor_enabled:
            # Store user ID and remember_me preference in session for 2FA verification
            session['pending_2fa_user'] = user.id
            session['pending_2fa_remember'] = form.remember_me.data
            logger.info(f"2FA required for user: {user.email}")
            return redirect(url_for('main.two_factor_verify'))

        login_user(user, remember=form.remember_me.data)
        return redirect(url_for('main.dashboard'))
    return render_template('login.html', title='Sign In', form=form, allow_registration=allow_registration)

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.login'))


# ============================================================================
# Two-Factor Authentication Routes
# ============================================================================

@bp.route('/security-settings')
@login_required
def security_settings():
    """Security settings page - 2FA management"""
    form = ChangePasswordForm()
    return render_template('security_settings.html', title='Security Settings', form=form)


@bp.route('/2fa/setup', methods=['GET', 'POST'])
@login_required
def two_factor_setup():
    """Setup 2FA - show QR code and verify first TOTP code"""
    import qrcode
    import io
    import base64

    # If 2FA is already enabled, redirect to security settings
    if current_user.two_factor_enabled:
        flash('Two-factor authentication is already enabled')
        return redirect(url_for('main.security_settings'))

    form = TwoFactorSetupForm()

    # Generate a new TOTP secret if one doesn't exist
    if not current_user.totp_secret:
        current_user.generate_totp_secret()
        db.session.commit()

    if form.validate_on_submit():
        # Verify the token
        if current_user.verify_totp(form.token.data):
            current_user.two_factor_enabled = True
            db.session.commit()
            logger.info(f"2FA enabled for user: {current_user.email}")
            flash('Two-factor authentication has been enabled')
            return redirect(url_for('main.security_settings'))
        else:
            flash('Invalid verification code. Please try again.')

    # Generate QR code as base64 image
    totp_uri = current_user.get_totp_uri()
    qr = qrcode.make(totp_uri)
    buffer = io.BytesIO()
    qr.save(buffer, format='PNG')
    qr_code_base64 = base64.b64encode(buffer.getvalue()).decode()

    return render_template('2fa_setup.html',
                           title='Setup Two-Factor Authentication',
                           form=form,
                           qr_code=qr_code_base64,
                           totp_secret=current_user.totp_secret)


@bp.route('/2fa/verify', methods=['GET', 'POST'])
def two_factor_verify():
    """Verify 2FA code during login"""
    # Check if there's a pending 2FA user
    pending_user_id = session.get('pending_2fa_user')
    if not pending_user_id:
        return redirect(url_for('main.login'))

    user = User.query.get(pending_user_id)
    if not user:
        session.pop('pending_2fa_user', None)
        session.pop('pending_2fa_remember', None)
        return redirect(url_for('main.login'))

    form = TwoFactorVerifyForm()

    if form.validate_on_submit():
        if user.verify_totp(form.token.data):
            # Clear pending 2FA session data
            remember_me = session.pop('pending_2fa_remember', False)
            session.pop('pending_2fa_user', None)

            # Complete login
            login_user(user, remember=remember_me)
            logger.info(f"2FA verification successful for user: {user.email}")
            return redirect(url_for('main.dashboard'))
        else:
            logger.warning(f"Invalid 2FA code for user: {user.email}")
            flash('Invalid verification code. Please try again.')

    return render_template('2fa_verify.html',
                           title='Two-Factor Authentication',
                           form=form)


@bp.route('/2fa/disable', methods=['POST'])
@login_required
def two_factor_disable():
    """Disable 2FA - requires current TOTP code"""
    form = TwoFactorDisableForm()

    if form.validate_on_submit():
        if current_user.verify_totp(form.token.data):
            current_user.two_factor_enabled = False
            current_user.totp_secret = None
            db.session.commit()
            logger.info(f"2FA disabled for user: {current_user.email}")
            flash('Two-factor authentication has been disabled')
        else:
            flash('Invalid verification code. 2FA was not disabled.')

    return redirect(url_for('main.security_settings'))


@bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """Change user password - requires current password"""
    form = ChangePasswordForm()

    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash('Current password is incorrect')
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            logger.info(f"Password changed for user: {current_user.email}")
            flash('Your password has been changed successfully')

    return redirect(url_for('main.security_settings'))


def _mask_email(email: str) -> str:
    """Mask email for display to prevent exposure. e.g., 'user@example.com' -> 'us***@e***.com'"""
    if not email or '@' not in email:
        return '***@***.***'
    local, domain = email.rsplit('@', 1)
    # Mask local part: show first 2 chars, rest as ***
    masked_local = local[:2] + '***' if len(local) > 2 else '***'
    # Mask domain: show first char, then ***, then TLD
    if '.' in domain:
        domain_name, tld = domain.rsplit('.', 1)
        masked_domain = domain_name[0] + '***.' + tld if domain_name else '***.' + tld
    else:
        masked_domain = '***'
    return f"{masked_local}@{masked_domain}"


@bp.route('/reset-account', methods=['GET', 'POST'])
def reset_account():
    """Reset account - deletes user and all data, allows re-registration"""
    # Check if there's a user to reset
    user = User.query.first()
    if not user:
        flash('No account to reset')
        return redirect(url_for('main.register'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        confirm = request.form.get('confirm', '').strip()
        totp_code = request.form.get('totp_code', '').strip()

        if email != user.email:
            flash('Email does not match the registered account')
            return redirect(url_for('main.reset_account'))

        if confirm.lower() != 'reset':
            flash('You must type RESET to confirm')
            return redirect(url_for('main.reset_account'))

        # Verify 2FA code if enabled
        if user.two_factor_enabled:
            if not totp_code:
                flash('2FA code is required')
                return redirect(url_for('main.reset_account'))
            if not user.verify_totp(totp_code):
                flash('Invalid 2FA code')
                return redirect(url_for('main.reset_account'))

        # Log out if currently logged in
        if current_user.is_authenticated:
            logout_user()

        # Delete all user data
        logger.warning(f"Account reset initiated for user: {user.email}")

        # Delete related records first (due to foreign key constraints)
        PriceRecord.query.filter_by(user_id=user.id).delete()
        from app.models import EnergyRecord, SavedTOUProfile, CustomTOUSchedule
        EnergyRecord.query.filter_by(user_id=user.id).delete()
        SavedTOUProfile.query.filter_by(user_id=user.id).delete()
        CustomTOUSchedule.query.filter_by(user_id=user.id).delete()

        # Delete the user
        db.session.delete(user)
        db.session.commit()

        logger.warning("Account reset completed - all user data deleted")
        flash('Account has been reset. You can now register a new account.')
        return redirect(url_for('main.register'))

    from flask_wtf import FlaskForm
    form = FlaskForm()  # Just for CSRF token
    # Mask email to prevent exposure - user must know their full email to reset
    masked_email = _mask_email(user.email)
    return render_template(
        'reset_account.html',
        title='Reset Account',
        user_email=masked_email,
        two_factor_enabled=user.two_factor_enabled,
        form=form
    )


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    # Check if any users already exist (single-user mode)
    existing_user_count = User.query.count()
    if existing_user_count > 0:
        logger.warning(f"Registration attempt blocked - user already exists (count: {existing_user_count})")
        flash('Registration is disabled. This application only supports a single user account.')
        return redirect(url_for('main.login'))

    form = RegistrationForm()
    if form.validate_on_submit():
        # Double-check in case of race condition
        if User.query.count() > 0:
            logger.warning("Registration blocked during form submission - user already exists")
            flash('Registration is disabled. A user account already exists.')
            return redirect(url_for('main.login'))

        user = User(email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        logger.info(f"First user account created: {user.email}")
        flash('Congratulations, you are now a registered user!')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register', form=form)

@bp.route('/dashboard')
@login_required
def dashboard():
    logger.info(f"Dashboard accessed by user: {current_user.email}")
    has_amber_token = current_user.amber_api_token_encrypted is not None
    tesla_api_provider = current_user.tesla_api_provider or 'teslemetry'
    return render_template('dashboard.html', title='Dashboard', has_amber_token=has_amber_token,
                           solar_curtailment_enabled=current_user.solar_curtailment_enabled or False,
                           inverter_curtailment_enabled=current_user.inverter_curtailment_enabled or False,
                           tesla_api_provider=tesla_api_provider)


@bp.route('/api/curtailment-status')
@login_required
def api_curtailment_status():
    """Get current solar curtailment status"""
    from app.api_clients import get_amber_client

    # Check if curtailment is enabled
    if not current_user.solar_curtailment_enabled:
        return jsonify({'enabled': False})

    # Read cached export rule from user model (set by curtailment tasks)
    export_rule = current_user.current_export_rule

    # Get current feed-in price if Amber is configured
    feedin_price = None
    export_earnings = None
    if current_user.amber_api_token_encrypted and current_user.amber_site_id:
        amber_client = get_amber_client(current_user)
        if amber_client:
            # Try WebSocket first, then REST API
            ws_client = current_app.config.get('AMBER_WEBSOCKET_CLIENT')
            prices = amber_client.get_live_prices(ws_client=ws_client)
            if prices:
                for price in prices:
                    if price.get('channelType') == 'feedIn':
                        feedin_price = price.get('perKwh')
                        if feedin_price is not None:
                            export_earnings = -feedin_price
                        break

    # Determine curtailment status based on CURRENT price conditions
    # Curtailment should be active only when export earnings < 1c/kWh
    # This ensures dashboard shows correct state even if cache is stale
    if export_earnings is not None:
        # Use current price to determine if curtailment SHOULD be active
        is_curtailed = export_earnings < 1.0
    else:
        # No price data available, fall back to cached rule
        is_curtailed = export_rule == 'never'

    return jsonify({
        'enabled': True,
        'is_curtailed': is_curtailed,
        'export_rule': export_rule,
        'feedin_price': feedin_price,
        'export_earnings': export_earnings
    })


@bp.route('/api/config')
@login_required
def api_config():
    """
    Get backend configuration for mobile app auto-detection.

    Returns battery system type, electricity provider, and enabled features
    so the mobile app can auto-configure itself without manual selection.
    """
    battery_system = getattr(current_user, 'battery_system', 'tesla') or 'tesla'
    electricity_provider = getattr(current_user, 'electricity_provider', 'amber') or 'amber'

    # Build features dict based on user configuration
    features = {
        'solar_curtailment': getattr(current_user, 'solar_curtailment_enabled', False),
        'inverter_control': getattr(current_user, 'inverter_curtailment_enabled', False),
        'spike_protection': getattr(current_user, 'spike_protection_enabled', False),
        'export_boost': getattr(current_user, 'export_boost_enabled', False),
        'demand_charges': getattr(current_user, 'enable_demand_charges', False),
        'auto_sync': getattr(current_user, 'auto_sync_enabled', True),
    }

    # Add Sigenergy-specific info if applicable
    sigenergy_config = None
    if battery_system == 'sigenergy':
        sigenergy_config = {
            'station_id': getattr(current_user, 'sigenergy_station_id', None),
            'modbus_enabled': bool(getattr(current_user, 'sigenergy_modbus_host', None)),
        }

    return jsonify({
        'battery_system': battery_system,
        'electricity_provider': electricity_provider,
        'features': features,
        'sigenergy': sigenergy_config,
    })


# =============================================================================
# Inverter Curtailment API Endpoints
# =============================================================================

@bp.route('/api/inverter/status')
def api_inverter_status():
    """Get current inverter status for AC-coupled curtailment"""
    import asyncio

    # Support both session and Bearer token auth
    user = get_api_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if not user.inverter_curtailment_enabled:
        return jsonify({'enabled': False, 'message': 'Inverter curtailment not enabled'})

    if not user.inverter_host or not user.inverter_brand:
        return jsonify({'enabled': True, 'error': 'Inverter not configured'})

    try:
        from app.inverters import get_inverter_controller_from_user

        controller = get_inverter_controller_from_user(user)
        if not controller:
            return jsonify({'enabled': True, 'error': 'Failed to create inverter controller'})

        # Run async function in sync context
        async def get_status_and_disconnect():
            try:
                state = await controller.get_status()
            finally:
                await controller.disconnect()
            return state

        state = run_async_with_cleanup(get_status_and_disconnect())

        # Convert state to dict for response
        state_dict = state.to_dict()

        # Use tracked inverter_last_state as source of truth for is_curtailed
        # This fixes Fronius simple mode where power_limit_enabled is False
        # but the inverter is actually curtailed using soft export limit
        if getattr(user, 'inverter_last_state', None) == 'curtailed':
            state_dict['is_curtailed'] = True
            if state_dict.get('status') == 'online':
                state_dict['status'] = 'curtailed'
        elif getattr(user, 'inverter_last_state', None) == 'online':
            state_dict['is_curtailed'] = False

        # Check if it's nighttime for sleep detection
        import pytz
        is_night = False
        try:
            tz = pytz.timezone(user.timezone or 'Australia/Sydney')
            local_hour = datetime.now(tz).hour
            # Consider 6pm-6am as "night" when inverter sleep is expected
            is_night = local_hour >= 18 or local_hour < 6
            logger.debug(f"Sleep detection: timezone={user.timezone}, hour={local_hour}, is_night={is_night}")
        except Exception as e:
            logger.warning(f"Sleep detection timezone error: {e}")

        # Apply sleep detection at night if:
        # - Status is offline/error, OR
        # - Power output is very low (< 100W, e.g. Sungrow PID recovery mode)
        power_output = state_dict.get('power_output_w', 0) or 0
        status = state_dict.get('status')
        logger.debug(f"Sleep detection: is_night={is_night}, power={power_output}W, status={status}")

        if is_night:
            if status in ('offline', 'error') or power_output < 100:
                state_dict['status'] = 'sleep'
                state_dict['error_message'] = 'Inverter in sleep mode (night)'
                logger.info(f"Inverter status changed to sleep (night mode, power={power_output}W)")

        return jsonify({
            'enabled': True,
            'brand': user.inverter_brand,
            'model': user.inverter_model,
            'host': user.inverter_host,
            **state_dict
        })

    except Exception as e:
        logger.error(f"Error getting inverter status: {e}")
        # Determine if it's likely nighttime (inverter sleep) vs actual offline
        # Note: datetime is already imported at module level, don't re-import here
        import pytz
        try:
            tz = pytz.timezone(user.timezone or 'Australia/Sydney')
            local_hour = datetime.now(tz).hour
            # Consider 6pm-6am as "night" when inverter sleep is expected
            is_night = local_hour >= 18 or local_hour < 6
            status = 'sleep' if is_night else 'offline'
            description = 'Inverter in sleep mode (night)' if is_night else 'Cannot reach inverter'
        except Exception:
            status = 'offline'
            description = 'Cannot reach inverter'

        return jsonify({
            'enabled': True,
            'status': status,
            'is_curtailed': False,
            'power_output_w': None,
            'power_limit_percent': None,
            'brand': user.inverter_brand,
            'model': user.inverter_model,
            'host': user.inverter_host,
            'error_message': description
        })


@bp.route('/api/inverter/test', methods=['POST'])
@login_required
def api_inverter_test():
    """Test connection to inverter"""
    import asyncio

    data = request.get_json() or {}
    brand = data.get('brand')
    host = data.get('host')
    port = data.get('port', 502)
    slave_id = data.get('slave_id', 1)
    token = data.get('token')  # JWT token for Enphase
    enphase_username = data.get('enphase_username')  # Enlighten credentials for auto-refresh
    enphase_password = data.get('enphase_password')
    enphase_serial = data.get('enphase_serial')

    if not brand or not host:
        return jsonify({'success': False, 'error': 'Brand and host are required'})

    try:
        from app.inverters import get_inverter_controller

        controller = get_inverter_controller(
            brand=brand,
            host=host,
            port=port,
            slave_id=slave_id,
            token=token,
            enphase_username=enphase_username,
            enphase_password=enphase_password,
            enphase_serial=enphase_serial,
        )

        if not controller:
            return jsonify({'success': False, 'error': f'Unsupported inverter brand: {brand}'})

        # Run async function in sync context
        success, message = run_async_with_cleanup(controller.test_connection())

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        logger.error(f"Error testing inverter connection: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/inverter/curtail', methods=['POST'])
def api_inverter_curtail():
    """Manually trigger inverter curtailment.

    For Zeversolar, supports load-following mode via query param or JSON body:
    - ?load_following=true - Auto-detect home load and limit to that
    - {"power_limit_w": 1000} - Set specific power limit in watts
    """
    import asyncio
    from datetime import datetime

    # Support both session and Bearer token auth
    user = get_api_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if not user.inverter_curtailment_enabled:
        return jsonify({'success': False, 'error': 'Inverter curtailment not enabled'})

    # Check for mode and load-following parameters
    mode = request.args.get('mode', 'load_following')  # 'shutdown' or 'load_following'
    load_following = request.args.get('load_following', 'false').lower() == 'true'
    data = request.get_json(silent=True) or {}
    power_limit_w = data.get('power_limit_w')

    # Determine home_load_w based on mode
    home_load_w = None
    if mode == 'shutdown':
        # Explicit shutdown mode - 0% output
        home_load_w = 0
        logger.info(f"Manual shutdown requested for {user.inverter_brand}")
    elif power_limit_w is not None:
        # Explicit power limit provided
        home_load_w = int(power_limit_w)
        logger.info(f"Manual power limit set to {home_load_w}W")
    elif load_following:
        # Auto-detect home load from Tesla for load-following
        try:
            tesla_client = get_tesla_client(user)
            if tesla_client and user.tesla_energy_site_id:
                site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
                if site_status:
                    home_load_w = int(site_status.get('load_power', 0))
                    logger.info(f"Load-following curtailment: home load is {home_load_w}W for {user.inverter_brand}")
        except Exception as e:
            logger.warning(f"Failed to get home load for load-following: {e}")

    try:
        from app.inverters import get_inverter_controller_from_user

        controller = get_inverter_controller_from_user(user)
        if not controller:
            return jsonify({'success': False, 'error': 'Failed to create inverter controller'})

        # Run async function in sync context
        async def do_curtail():
            try:
                # Check if controller supports home_load_w parameter
                if home_load_w is not None and hasattr(controller.curtail, '__code__') and 'home_load_w' in controller.curtail.__code__.co_varnames:
                    result = await controller.curtail(home_load_w=home_load_w)
                else:
                    result = await controller.curtail()
                return result
            finally:
                await controller.disconnect()

        success = run_async_with_cleanup(do_curtail())

        if success:
            user.inverter_last_state = 'curtailed'
            user.inverter_last_state_updated = datetime.utcnow()
            user.inverter_power_limit_w = home_load_w
            db.session.commit()

        response = {'success': success, 'state': 'curtailed' if success else 'unknown'}
        if home_load_w is not None:
            response['power_limit_w'] = home_load_w
        return jsonify(response)

    except Exception as e:
        logger.error(f"Error curtailing inverter: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/inverter/set-power-limit', methods=['POST'])
@login_required
def api_inverter_set_power_limit():
    """Set inverter power limit in watts (Zeversolar only).

    JSON body:
    - power_limit_w: Power limit in watts (0 to inverter capacity)
    - percent: Power limit as percentage (0-100)
    """
    import asyncio
    from datetime import datetime

    if not current_user.inverter_curtailment_enabled:
        return jsonify({'success': False, 'error': 'Inverter curtailment not enabled'})

    if current_user.inverter_brand != 'zeversolar':
        return jsonify({'success': False, 'error': 'Power limit control only supported for Zeversolar'})

    data = request.get_json(silent=True) or {}
    power_limit_w = data.get('power_limit_w')
    percent = data.get('percent')

    if power_limit_w is None and percent is None:
        return jsonify({'success': False, 'error': 'Provide power_limit_w or percent'})

    try:
        from app.inverters import get_inverter_controller_from_user

        controller = get_inverter_controller_from_user(current_user)
        if not controller:
            return jsonify({'success': False, 'error': 'Failed to create inverter controller'})

        # Run async function in sync context with proper cleanup
        async def do_set_limit():
            if power_limit_w is not None:
                result = await controller.set_power_limit_watts(int(power_limit_w))
            else:
                result = await controller._set_power_limit(int(percent))
            await controller.disconnect()
            return result

        success = run_async_with_cleanup(do_set_limit())

        if success:
            current_user.inverter_last_state = 'curtailed' if (percent is not None and percent < 100) or (power_limit_w is not None and power_limit_w < controller.ac_capacity_w) else 'online'
            current_user.inverter_last_state_updated = datetime.utcnow()
            current_user.inverter_power_limit_w = power_limit_w
            db.session.commit()

        response = {'success': success}
        if power_limit_w is not None:
            response['power_limit_w'] = power_limit_w
        if percent is not None:
            response['percent'] = percent
        return jsonify(response)

    except Exception as e:
        logger.error(f"Error setting inverter power limit: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/inverter/restore', methods=['POST'])
def api_inverter_restore():
    """Manually restore inverter to normal operation"""
    import asyncio
    from datetime import datetime

    # Support both session and Bearer token auth
    user = get_api_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if not user.inverter_curtailment_enabled:
        return jsonify({'success': False, 'error': 'Inverter curtailment not enabled'})

    try:
        from app.inverters import get_inverter_controller_from_user

        controller = get_inverter_controller_from_user(user)
        if not controller:
            return jsonify({'success': False, 'error': 'Failed to create inverter controller'})

        # Run async function in sync context with proper cleanup
        async def do_restore():
            result = await controller.restore()
            await controller.disconnect()
            return result

        success = run_async_with_cleanup(do_restore())

        if success:
            user.inverter_last_state = 'online'
            user.inverter_last_state_updated = datetime.utcnow()
            user.inverter_power_limit_w = None  # Clear power limit
            db.session.commit()

        return jsonify({'success': success, 'state': 'online' if success else 'unknown'})

    except Exception as e:
        logger.error(f"Error restoring inverter: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Sigenergy API Routes
# ============================================================================

@bp.route('/api/sigenergy/validate', methods=['POST'])
@login_required
def api_sigenergy_validate():
    """Validate Sigenergy credentials and get stations list.

    Accepts both plain password (preferred) and pre-encoded pass_enc (backwards compatible).
    If plain password is provided, it's encoded automatically.
    """
    from app.sigenergy_client import SigenergyClient, encode_sigenergy_password
    from app.utils import encrypt_token

    data = request.get_json() or {}
    username = data.get('username')
    password = data.get('password')  # Plain password (preferred)
    pass_enc = data.get('pass_enc')  # Pre-encoded (backwards compat)
    device_id = data.get('device_id')

    # Determine which password to use
    # Priority: pass_enc (explicit override) > password (encode it)
    if pass_enc:
        final_pass_enc = pass_enc
    elif password:
        final_pass_enc = encode_sigenergy_password(password)
    else:
        final_pass_enc = None

    if not username or not final_pass_enc:
        return jsonify({'success': False, 'error': 'Username and password are required'})

    if device_id and len(device_id) != 13:
        return jsonify({'success': False, 'error': 'Device ID must be exactly 13 digits'})

    try:
        client = SigenergyClient(
            username=username,
            pass_enc=final_pass_enc,
            device_id=device_id,  # Will use default if None
        )

        # Authenticate
        auth_result = client.authenticate()
        if 'error' in auth_result:
            return jsonify({'success': False, 'error': auth_result['error']})

        # Get stations
        stations_result = client.get_stations()
        if 'error' in stations_result:
            return jsonify({'success': False, 'error': stations_result['error']})

        # Save credentials to user (use client.device_id which may be auto-generated)
        current_user.sigenergy_username = username
        current_user.sigenergy_pass_enc_encrypted = encrypt_token(final_pass_enc)
        current_user.sigenergy_device_id = client.device_id
        current_user.sigenergy_access_token_encrypted = encrypt_token(client.access_token)
        if client.refresh_token:
            current_user.sigenergy_refresh_token_encrypted = encrypt_token(client.refresh_token)
        current_user.sigenergy_token_expires_at = client.token_expires_at

        db.session.commit()
        logger.info(f"Sigenergy credentials validated and saved for user {current_user.email}")

        stations = stations_result.get('stations', [])
        return jsonify({
            'success': True,
            'stations': stations,
            'message': f'Authentication successful. Found {len(stations)} station(s).'
        })

    except Exception as e:
        logger.error(f"Error validating Sigenergy credentials: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/sigenergy/stations', methods=['GET'])
@login_required
def api_sigenergy_stations():
    """Get list of Sigenergy stations for current user."""
    from app.sigenergy_client import get_sigenergy_client

    try:
        client = get_sigenergy_client(current_user)
        if not client:
            return jsonify({'success': False, 'error': 'Sigenergy not configured'})

        result = client.get_stations()
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']})

        return jsonify({'success': True, 'stations': result.get('stations', [])})

    except Exception as e:
        logger.error(f"Error getting Sigenergy stations: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/sigenergy/save', methods=['POST'])
@login_required
def api_sigenergy_save():
    """Save Sigenergy station selection."""
    data = request.get_json() or {}
    station_id = data.get('station_id')

    if not station_id:
        return jsonify({'success': False, 'error': 'Station ID is required'})

    try:
        current_user.sigenergy_station_id = str(station_id)
        current_user.battery_system = 'sigenergy'  # Also set battery system
        db.session.commit()

        logger.info(f"Sigenergy station {station_id} saved for user {current_user.email}")
        return jsonify({'success': True, 'message': 'Station saved successfully'})

    except Exception as e:
        logger.error(f"Error saving Sigenergy station: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/sigenergy/disconnect', methods=['POST'])
@login_required
def api_sigenergy_disconnect():
    """Disconnect Sigenergy and clear credentials."""
    try:
        current_user.sigenergy_username = None
        current_user.sigenergy_pass_enc_encrypted = None
        current_user.sigenergy_device_id = None
        current_user.sigenergy_station_id = None
        current_user.sigenergy_access_token_encrypted = None
        current_user.sigenergy_refresh_token_encrypted = None
        current_user.sigenergy_token_expires_at = None

        # Reset battery system to Tesla if was Sigenergy
        if current_user.battery_system == 'sigenergy':
            current_user.battery_system = 'tesla'

        db.session.commit()

        logger.info(f"Sigenergy disconnected for user {current_user.email}")
        return jsonify({'success': True, 'message': 'Sigenergy disconnected'})

    except Exception as e:
        logger.error(f"Error disconnecting Sigenergy: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/sigenergy/status', methods=['GET'])
@login_required
def api_sigenergy_status():
    """Get current Sigenergy connection status."""
    try:
        connected = bool(
            current_user.sigenergy_username and
            current_user.sigenergy_access_token_encrypted and
            current_user.sigenergy_station_id
        )

        return jsonify({
            'success': True,
            'connected': connected,
            'username': current_user.sigenergy_username,
            'station_id': current_user.sigenergy_station_id,
            'battery_system': current_user.battery_system,
        })

    except Exception as e:
        logger.error(f"Error getting Sigenergy status: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/sigenergy/tariff', methods=['GET'])
@api_auth_required
def api_sigenergy_tariff(api_user=None, **kwargs):
    """Get current Sigenergy tariff schedule (buy/sell prices for all 48 half-hour slots).

    Returns the tariff that would be synced to Sigenergy, useful for mobile app dashboard display.
    Supports both session login and Bearer token authentication.
    """
    from app.sigenergy_client import convert_amber_prices_to_sigenergy
    from app.api_clients import AmberAPIClient

    user = api_user or current_user

    try:
        # Check if user has Sigenergy configured
        if user.battery_system != 'sigenergy':
            return jsonify({
                'success': False,
                'error': 'Not a Sigenergy system',
                'battery_system': user.battery_system
            })

        # Get Amber forecast data
        amber_client = AmberAPIClient(user.amber_api_token)
        forecast_data = amber_client.get_prices(
            user.amber_site_id,
            resolution=30  # 30-min intervals for tariff
        )

        if not forecast_data:
            return jsonify({
                'success': True,
                'message': 'No Amber price data available',
                'buy_prices': [],
                'sell_prices': []
            })

        # Get forecast type preference
        forecast_type = user.amber_forecast_type or 'predicted'

        # Split by channel type
        general_prices = [p for p in forecast_data if p.get('channelType') == 'general']
        feedin_prices = [p for p in forecast_data if p.get('channelType') == 'feedIn']

        # Get NEM region for timezone selection
        # Priority: 1) Explicit aemo_region, 2) Auto-detect from Amber site network
        nem_region = user.aemo_region

        if not nem_region:
            # Auto-detect from Amber site info
            nem_region = _get_nem_region_from_amber_site(amber_client, user.amber_site_id)

        # Convert to Sigenergy format
        buy_prices = convert_amber_prices_to_sigenergy(
            general_prices, price_type='buy', forecast_type=forecast_type, nem_region=nem_region
        )
        sell_prices = convert_amber_prices_to_sigenergy(
            feedin_prices, price_type='sell', forecast_type=forecast_type, nem_region=nem_region
        )

        return jsonify({
            'success': True,
            'buy_prices': buy_prices,
            'sell_prices': sell_prices if sell_prices else buy_prices,
            'forecast_type': forecast_type,
            'generated_at': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error getting Sigenergy tariff: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/aemo-price')
@login_required
@cache.cached(timeout=60, key_prefix=lambda: f'aemo_price_{current_user.id}')
def api_aemo_price():
    """Get current AEMO wholesale price and spike status"""
    from app.api_clients import AEMOAPIClient

    # Check if AEMO spike detection is enabled
    if not current_user.aemo_spike_detection_enabled:
        return jsonify({'enabled': False, 'message': 'AEMO spike detection not enabled'})

    if not current_user.aemo_region:
        return jsonify({'enabled': True, 'error': 'AEMO region not configured'})

    # Fetch current AEMO price
    aemo_client = AEMOAPIClient()
    price_data = aemo_client.get_region_price(current_user.aemo_region)

    if not price_data:
        return jsonify({'enabled': True, 'error': 'Failed to fetch AEMO price'})

    # Build response
    current_price = price_data['price']
    threshold = current_user.aemo_spike_threshold or 300.0
    is_spike = current_price >= threshold

    response = {
        'enabled': True,
        'region': current_user.aemo_region,
        'current_price': current_price,
        'threshold': threshold,
        'is_spike': is_spike,
        'in_spike_mode': current_user.aemo_in_spike_mode,
        'last_check': current_user.aemo_last_check.isoformat() if current_user.aemo_last_check else None,
        'spike_start_time': current_user.aemo_spike_start_time.isoformat() if current_user.aemo_spike_start_time else None,
        'timestamp': price_data.get('timestamp')
    }

    logger.info(f"AEMO price API: {current_user.aemo_region} = ${current_price}/MWh (threshold: ${threshold})")
    return jsonify(response)


@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    logger.info(f"Settings page accessed by user: {current_user.email} - Method: {request.method}")
    form = SettingsForm()

    # Debug: log form validation details
    if request.method == 'POST':
        logger.debug(f"POST data keys: {list(request.form.keys())}")
        logger.debug(f"tesla_api_provider in form: {request.form.get('tesla_api_provider')}")
        if not form.validate():
            logger.warning(f"Form validation failed: {form.errors}")

    if form.validate_on_submit():
        logger.info(f"Settings form submitted by user: {current_user.email}")

        # Check which fields were actually submitted in the form
        # This handles partial form submissions (e.g., just the provider dropdown)
        submitted_fields = set(request.form.keys())
        logger.debug(f"Submitted form fields: {submitted_fields}")

        # Handle Amber API token (only if field was in the submitted form)
        if 'amber_token' in submitted_fields:
            if form.amber_token.data:
                logger.info("Encrypting and saving Amber API token")
                current_user.amber_api_token_encrypted = encrypt_token(form.amber_token.data)
            else:
                logger.info("Clearing Amber API token")
                current_user.amber_api_token_encrypted = None

        # Tesla Site ID is now auto-detected when connecting Tesla account
        # No longer manually configurable

        # Handle Battery System selection
        if 'battery_system' in submitted_fields and form.battery_system.data:
            logger.info(f"Saving battery system: {form.battery_system.data}")
            current_user.battery_system = form.battery_system.data

        # Handle Tesla API Provider selection
        if 'tesla_api_provider' in submitted_fields and form.tesla_api_provider.data:
            logger.info(f"Saving Tesla API provider: {form.tesla_api_provider.data}")
            current_user.tesla_api_provider = form.tesla_api_provider.data

        # Handle Teslemetry API key (only if field was in the submitted form)
        if 'teslemetry_api_key' in submitted_fields:
            if form.teslemetry_api_key.data:
                logger.info("Encrypting and saving Teslemetry API key")
                current_user.teslemetry_api_key_encrypted = encrypt_token(form.teslemetry_api_key.data)

                # Auto-detect energy site ID
                try:
                    from app.api_clients import TeslemetryAPIClient
                    teslemetry_client = TeslemetryAPIClient(form.teslemetry_api_key.data)
                    energy_sites = teslemetry_client.get_energy_sites()
                    if energy_sites:
                        if len(energy_sites) == 1:
                            # Single site - auto-select it
                            site_id = str(energy_sites[0].get('energy_site_id'))
                            current_user.tesla_energy_site_id = site_id
                            logger.info(f"Auto-detected Tesla energy site ID via Teslemetry: {site_id}")

                            # Fetch and store timezone for automation time triggers
                            try:
                                site_info = teslemetry_client.get_site_info(site_id)
                                if site_info:
                                    site_timezone = site_info.get('installation_time_zone')
                                    if site_timezone:
                                        current_user.timezone = site_timezone
                                        logger.info(f"Updated user timezone from Teslemetry site info: {site_timezone}")
                            except Exception as tz_err:
                                logger.warning(f"Could not fetch site timezone via Teslemetry: {tz_err}")
                        else:
                            # Multiple sites - user needs to choose
                            logger.info(f"Found {len(energy_sites)} energy sites via Teslemetry - user needs to select one")
                            flash(f'Found {len(energy_sites)} energy sites - please select one below.')
                    else:
                        logger.warning("No energy sites found via Teslemetry")
                except Exception as site_err:
                    logger.error(f"Error auto-detecting energy site via Teslemetry: {site_err}")
            else:
                logger.info("Clearing Teslemetry API key")
                current_user.teslemetry_api_key_encrypted = None

        # Handle Fleet API OAuth credentials (only if fields were in the submitted form)
        if 'fleet_api_client_id' in submitted_fields:
            if form.fleet_api_client_id.data:
                logger.info("Encrypting and saving Fleet API Client ID")
                current_user.fleet_api_client_id_encrypted = encrypt_token(form.fleet_api_client_id.data)
            else:
                logger.info("Clearing Fleet API Client ID")
                current_user.fleet_api_client_id_encrypted = None

        if 'fleet_api_client_secret' in submitted_fields:
            if form.fleet_api_client_secret.data:
                logger.info("Encrypting and saving Fleet API Client Secret")
                current_user.fleet_api_client_secret_encrypted = encrypt_token(form.fleet_api_client_secret.data)
            else:
                logger.info("Clearing Fleet API Client Secret")
                current_user.fleet_api_client_secret_encrypted = None

        if 'fleet_api_redirect_uri' in submitted_fields:
            if form.fleet_api_redirect_uri.data:
                logger.info(f"Saving Fleet API Redirect URI: {form.fleet_api_redirect_uri.data}")
                current_user.fleet_api_redirect_uri = form.fleet_api_redirect_uri.data
            else:
                logger.info("Clearing Fleet API Redirect URI")
                current_user.fleet_api_redirect_uri = None

        # AEMO Spike Detection settings (only if fields were in the submitted form)
        if 'aemo_spike_detection_enabled' in submitted_fields:
            current_user.aemo_spike_detection_enabled = form.aemo_spike_detection_enabled.data
        if 'aemo_region' in submitted_fields and form.aemo_region.data:
            logger.info(f"Saving AEMO region: {form.aemo_region.data}")
            current_user.aemo_region = form.aemo_region.data
        if 'aemo_spike_threshold' in submitted_fields and form.aemo_spike_threshold.data:
            logger.info(f"Saving AEMO spike threshold: ${form.aemo_spike_threshold.data}/MWh")
            current_user.aemo_spike_threshold = float(form.aemo_spike_threshold.data)

        # Sigenergy Modbus settings
        if 'sigenergy_modbus_host' in submitted_fields:
            if form.sigenergy_modbus_host.data:
                current_user.sigenergy_modbus_host = form.sigenergy_modbus_host.data.strip()
                logger.info(f"Saving Sigenergy Modbus host: {current_user.sigenergy_modbus_host}")
            else:
                current_user.sigenergy_modbus_host = None
        if 'sigenergy_modbus_port' in submitted_fields:
            current_user.sigenergy_modbus_port = form.sigenergy_modbus_port.data or 502
        if 'sigenergy_modbus_slave_id' in submitted_fields:
            current_user.sigenergy_modbus_slave_id = form.sigenergy_modbus_slave_id.data or 1

        # Flow Power / Electricity Provider settings
        if 'electricity_provider' in submitted_fields:
            provider = request.form.get('electricity_provider')
            if provider in ['amber', 'flow_power', 'globird']:
                logger.info(f"Saving electricity provider: {provider}")
                current_user.electricity_provider = provider

        # Amber Spike Protection (checkbox - saved from either electricity provider form or Amber settings form)
        if 'spike_protection_enabled' in submitted_fields or 'electricity_provider' in submitted_fields:
            current_user.spike_protection_enabled = 'spike_protection_enabled' in request.form
            logger.info(f"Saving spike protection enabled: {current_user.spike_protection_enabled}")

        if 'flow_power_state' in submitted_fields:
            state = request.form.get('flow_power_state')
            if state in ['NSW1', 'VIC1', 'QLD1', 'SA1']:
                logger.info(f"Saving Flow Power state: {state}")
                current_user.flow_power_state = state

        if 'flow_power_price_source' in submitted_fields:
            source = request.form.get('flow_power_price_source')
            if source in ['amber', 'aemo']:
                logger.info(f"Saving Flow Power price source: {source}")
                current_user.flow_power_price_source = source

        # Flow Power PEA (Price Efficiency Adjustment) settings
        # Checkbox - pea_enabled defaults to True if not present in form
        current_user.pea_enabled = 'pea_enabled' in request.form
        logger.info(f"Saving PEA enabled: {current_user.pea_enabled}")

        if 'flow_power_base_rate' in submitted_fields:
            try:
                base_rate = float(request.form.get('flow_power_base_rate', 34.0))
                current_user.flow_power_base_rate = base_rate
                logger.info(f"Saving Flow Power base rate: {base_rate} c/kWh")
            except (ValueError, TypeError):
                pass

        if 'pea_custom_value' in submitted_fields:
            pea_custom = request.form.get('pea_custom_value', '').strip()
            if pea_custom:
                try:
                    current_user.pea_custom_value = float(pea_custom)
                    logger.info(f"Saving custom PEA: {current_user.pea_custom_value} c/kWh")
                except (ValueError, TypeError):
                    current_user.pea_custom_value = None
            else:
                current_user.pea_custom_value = None
                logger.info("Clearing custom PEA (auto-calculate from wholesale)")

        # Network Tariff Configuration (for Flow Power + AEMO)
        # Distributor and tariff code for aemo_to_tariff library
        if 'network_distributor' in submitted_fields:
            distributor = request.form.get('network_distributor', 'energex')
            current_user.network_distributor = distributor
            logger.info(f"Saving network distributor: {distributor}")

        if 'network_tariff_code' in submitted_fields:
            tariff_code = request.form.get('network_tariff_code', 'NTC6900')
            current_user.network_tariff_code = tariff_code
            logger.info(f"Saving network tariff code: {tariff_code}")

        # Checkbox - use manual rates instead of library
        current_user.network_use_manual_rates = 'network_use_manual_rates' in request.form
        logger.info(f"Using manual rates: {current_user.network_use_manual_rates}")

        if 'network_tariff_type' in submitted_fields:
            tariff_type = request.form.get('network_tariff_type')
            if tariff_type in ['flat', 'tou']:
                logger.info(f"Saving network tariff type: {tariff_type}")
                current_user.network_tariff_type = tariff_type

        if 'network_flat_rate' in submitted_fields:
            try:
                rate = float(request.form.get('network_flat_rate', 8.0))
                current_user.network_flat_rate = rate
                logger.info(f"Saving network flat rate: {rate} c/kWh")
            except (ValueError, TypeError):
                pass

        if 'network_peak_rate' in submitted_fields:
            try:
                current_user.network_peak_rate = float(request.form.get('network_peak_rate', 15.0))
            except (ValueError, TypeError):
                pass

        if 'network_shoulder_rate' in submitted_fields:
            try:
                current_user.network_shoulder_rate = float(request.form.get('network_shoulder_rate', 5.0))
            except (ValueError, TypeError):
                pass

        if 'network_offpeak_rate' in submitted_fields:
            try:
                current_user.network_offpeak_rate = float(request.form.get('network_offpeak_rate', 2.0))
            except (ValueError, TypeError):
                pass

        if 'network_peak_start' in submitted_fields:
            current_user.network_peak_start = request.form.get('network_peak_start', '16:00')

        if 'network_peak_end' in submitted_fields:
            current_user.network_peak_end = request.form.get('network_peak_end', '21:00')

        if 'network_offpeak_start' in submitted_fields:
            current_user.network_offpeak_start = request.form.get('network_offpeak_start', '10:00')

        if 'network_offpeak_end' in submitted_fields:
            current_user.network_offpeak_end = request.form.get('network_offpeak_end', '15:00')

        if 'network_other_fees' in submitted_fields:
            try:
                current_user.network_other_fees = float(request.form.get('network_other_fees', 1.5))
            except (ValueError, TypeError):
                pass

        # Checkbox - only present if checked
        current_user.network_include_gst = 'network_include_gst' in request.form

        try:
            db.session.commit()
            logger.info("Settings saved successfully to database")

            # Clear TOU schedule cache so new settings take effect immediately
            cache_key = f'tou_schedule_{current_user.id}'
            cache.delete(cache_key)
            logger.info("Cleared TOU schedule cache after settings update")

            flash('Your settings have been saved.')
        except Exception as e:
            logger.error(f"Error saving settings to database: {e}")
            flash('Error saving settings. Please try again.')
            db.session.rollback()

        return redirect(url_for('main.settings'))

    # Pre-populate form with existing data (decrypted)
    logger.debug("Decrypting and pre-populating form data")
    try:
        form.amber_token.data = decrypt_token(current_user.amber_api_token_encrypted)
        logger.debug(f"Amber token decrypted: {'Yes' if form.amber_token.data else 'No'}")
    except Exception as e:
        logger.error(f"Error decrypting amber token: {e}")
        form.amber_token.data = None

    # Tesla Site ID is now auto-detected and displayed directly in template

    # Pre-populate Tesla API provider selection
    form.tesla_api_provider.data = current_user.tesla_api_provider or 'teslemetry'
    logger.debug(f"Tesla API Provider: {form.tesla_api_provider.data}")

    try:
        form.teslemetry_api_key.data = decrypt_token(current_user.teslemetry_api_key_encrypted)
        logger.debug(f"Teslemetry API key decrypted: {'Yes' if form.teslemetry_api_key.data else 'No'}")
    except Exception as e:
        logger.error(f"Error decrypting teslemetry api key: {e}")
        form.teslemetry_api_key.data = None

    # Pre-populate Fleet API OAuth credentials
    try:
        form.fleet_api_client_id.data = decrypt_token(current_user.fleet_api_client_id_encrypted)
        logger.debug(f"Fleet API Client ID decrypted: {'Yes' if form.fleet_api_client_id.data else 'No'}")
    except Exception as e:
        logger.error(f"Error decrypting Fleet API client ID: {e}")
        form.fleet_api_client_id.data = None

    try:
        form.fleet_api_client_secret.data = decrypt_token(current_user.fleet_api_client_secret_encrypted)
        logger.debug(f"Fleet API Client Secret decrypted: {'Yes' if form.fleet_api_client_secret.data else 'No'}")
    except Exception as e:
        logger.error(f"Error decrypting Fleet API client secret: {e}")
        form.fleet_api_client_secret.data = None

    # Pre-populate Fleet API Redirect URI (with sensible default)
    form.fleet_api_redirect_uri.data = current_user.fleet_api_redirect_uri or os.getenv('TESLA_REDIRECT_URI', '')
    logger.debug(f"Fleet API Redirect URI: {form.fleet_api_redirect_uri.data}")

    # Pre-populate AEMO settings
    form.aemo_spike_detection_enabled.data = current_user.aemo_spike_detection_enabled
    form.aemo_region.data = current_user.aemo_region or ''
    form.aemo_spike_threshold.data = current_user.aemo_spike_threshold or 300.0
    logger.debug(f"AEMO enabled: {form.aemo_spike_detection_enabled.data}, Region: {form.aemo_region.data}, Threshold: ${form.aemo_spike_threshold.data}")

    # Pre-populate Sigenergy Modbus settings
    form.sigenergy_modbus_host.data = current_user.sigenergy_modbus_host or ''
    form.sigenergy_modbus_port.data = current_user.sigenergy_modbus_port or 502
    form.sigenergy_modbus_slave_id.data = current_user.sigenergy_modbus_slave_id or 1

    logger.info(f"Rendering settings page - Has Amber token: {bool(current_user.amber_api_token_encrypted)}, Has Teslemetry key: {bool(current_user.teslemetry_api_key_encrypted)}, Tesla Site ID: {current_user.tesla_energy_site_id}")

    return render_template('settings.html', title='Settings', form=form)


@bp.route('/demand-charges', methods=['GET', 'POST'])
@login_required
def demand_charges():
    """Configure demand charge periods and rates"""
    logger.info(f"Demand charges page accessed by user: {current_user.email} - Method: {request.method}")
    form = DemandChargeForm()

    if form.validate_on_submit():
        logger.info(f"Demand charge form submitted by user: {current_user.email}")

        # Update user's demand charge configuration
        current_user.enable_demand_charges = form.enable_demand_charges.data
        current_user.peak_demand_rate = form.peak_rate.data if form.peak_rate.data else 0.0
        current_user.peak_start_hour = form.peak_start_hour.data if form.peak_start_hour.data is not None else 14
        current_user.peak_start_minute = form.peak_start_minute.data if form.peak_start_minute.data is not None else 0
        current_user.peak_end_hour = form.peak_end_hour.data if form.peak_end_hour.data is not None else 20
        current_user.peak_end_minute = form.peak_end_minute.data if form.peak_end_minute.data is not None else 0
        current_user.peak_days = form.peak_days.data
        current_user.demand_charge_apply_to = form.demand_charge_apply_to.data
        current_user.offpeak_demand_rate = form.offpeak_rate.data if form.offpeak_rate.data else 0.0
        current_user.shoulder_demand_rate = form.shoulder_rate.data if form.shoulder_rate.data else 0.0
        current_user.shoulder_start_hour = form.shoulder_start_hour.data if form.shoulder_start_hour.data is not None else 7
        current_user.shoulder_start_minute = form.shoulder_start_minute.data if form.shoulder_start_minute.data is not None else 0
        current_user.shoulder_end_hour = form.shoulder_end_hour.data if form.shoulder_end_hour.data is not None else 14
        current_user.shoulder_end_minute = form.shoulder_end_minute.data if form.shoulder_end_minute.data is not None else 0
        current_user.daily_supply_charge = form.daily_supply_charge.data if form.daily_supply_charge.data else 0.0
        current_user.monthly_supply_charge = form.monthly_supply_charge.data if form.monthly_supply_charge.data else 0.0
        current_user.demand_artificial_price_enabled = form.demand_artificial_price_enabled.data

        try:
            db.session.commit()
            logger.info("Demand charge settings saved successfully to database")
            flash('Demand charge settings have been saved.')
        except Exception as e:
            logger.error(f"Error saving demand charge settings to database: {e}")
            flash('Error saving demand charge settings. Please try again.')
            db.session.rollback()

        return redirect(url_for('main.demand_charges'))

    # Pre-populate form with existing data
    logger.debug("Pre-populating demand charge form data")
    form.enable_demand_charges.data = current_user.enable_demand_charges
    form.peak_rate.data = current_user.peak_demand_rate
    form.peak_start_hour.data = current_user.peak_start_hour
    form.peak_start_minute.data = current_user.peak_start_minute
    form.peak_end_hour.data = current_user.peak_end_hour
    form.peak_end_minute.data = current_user.peak_end_minute
    form.peak_days.data = current_user.peak_days
    form.demand_charge_apply_to.data = current_user.demand_charge_apply_to or 'buy'
    form.offpeak_rate.data = current_user.offpeak_demand_rate
    form.shoulder_rate.data = current_user.shoulder_demand_rate
    form.shoulder_start_hour.data = current_user.shoulder_start_hour
    form.shoulder_start_minute.data = current_user.shoulder_start_minute
    form.shoulder_end_hour.data = current_user.shoulder_end_hour
    form.shoulder_end_minute.data = current_user.shoulder_end_minute
    form.daily_supply_charge.data = current_user.daily_supply_charge
    form.monthly_supply_charge.data = current_user.monthly_supply_charge
    form.demand_artificial_price_enabled.data = current_user.demand_artificial_price_enabled

    logger.info(f"Rendering demand charges page - Enabled: {current_user.enable_demand_charges}, Peak rate: {current_user.peak_demand_rate}")
    return render_template('demand_charges.html', title='Demand Charges', form=form)


@bp.route('/amber-settings', methods=['GET', 'POST'])
@login_required
def amber_settings():
    """Configure Amber Electric specific settings"""
    logger.info(f"Amber settings page accessed by user: {current_user.email} - Method: {request.method}")
    form = AmberSettingsForm()

    # Fetch Amber sites to populate dropdown (if Amber token is configured)
    amber_sites = []
    site_choices = []
    if current_user.amber_api_token_encrypted:
        try:
            amber_client = get_amber_client(current_user)
            if amber_client:
                amber_sites = amber_client.get_sites()
                if amber_sites:
                    # Sort sites: active first, then pending, then closed
                    status_order = {'active': 0, 'pending': 1, 'closed': 2}
                    amber_sites.sort(key=lambda s: status_order.get(s.get('status', 'unknown'), 3))

                    # Create dropdown choices using NMI (user-friendly) as label, site ID as value
                    # Append status label for closed/pending sites to help users identify the right one
                    def format_site_label(site):
                        label = site.get('nmi', site['id'])
                        status = site.get('status', 'unknown')
                        if status == 'closed':
                            return f"{label} (Closed)"
                        elif status == 'pending':
                            return f"{label} (Pending)"
                        return label  # Active sites don't need a label

                    site_choices = [(site['id'], format_site_label(site)) for site in amber_sites]
                    logger.info(f"Found {len(amber_sites)} Amber sites for dropdown")
                else:
                    logger.warning("No Amber sites found for this API token")
        except Exception as e:
            logger.error(f"Error fetching Amber sites: {e}")

    # Populate dropdown choices
    form.amber_site_id.choices = site_choices

    if form.validate_on_submit():
        logger.info(f"Amber settings form submitted by user: {current_user.email}")

        # Update Amber-specific settings
        current_user.amber_forecast_type = form.amber_forecast_type.data

        # Check if curtailment is being disabled (was enabled, now disabled)
        was_curtailment_enabled = current_user.solar_curtailment_enabled
        current_user.solar_curtailment_enabled = form.solar_curtailment_enabled.data

        if was_curtailment_enabled and not form.solar_curtailment_enabled.data:
            # Restore Tesla export rule to battery_ok when disabling curtailment
            from app.api_clients import get_tesla_client
            tesla_client = get_tesla_client(current_user)
            if tesla_client and current_user.tesla_energy_site_id:
                result = tesla_client.set_grid_export_rule(current_user.tesla_energy_site_id, 'battery_ok')
                if result:
                    current_user.current_export_rule = 'battery_ok'
                    current_user.current_export_rule_updated = datetime.utcnow()
                    logger.info(f"✅ Solar curtailment disabled - restored export rule to 'battery_ok'")
                else:
                    logger.error(f"Failed to restore export rule when disabling curtailment")

        # Save selected site ID (auto-select if only 1 site)
        selected_amber_site_id = None
        if len(amber_sites) == 1:
            current_user.amber_site_id = amber_sites[0]['id']
            selected_amber_site_id = amber_sites[0]['id']
            logger.info(f"Auto-selected single Amber site: {current_user.amber_site_id}")
        elif form.amber_site_id.data:
            current_user.amber_site_id = form.amber_site_id.data
            selected_amber_site_id = form.amber_site_id.data
            logger.info(f"User selected Amber site: {current_user.amber_site_id}")

        # Set timezone from Amber NEM region for Sigenergy users (who don't have Tesla to get timezone from)
        if selected_amber_site_id and current_user.battery_system == 'sigenergy':
            try:
                nem_region = _get_nem_region_from_amber_site(amber_client, selected_amber_site_id)
                if nem_region:
                    tz = _get_timezone_from_nem_region(nem_region)
                    current_user.timezone = tz
                    logger.info(f"Updated user timezone from Amber NEM region ({nem_region}): {tz}")
            except Exception as e:
                logger.warning(f"Could not set timezone from Amber NEM region: {e}")

        # Export Price Boost settings
        current_user.export_boost_enabled = 'export_boost_enabled' in request.form
        logger.info(f"Saving export boost enabled: {current_user.export_boost_enabled}")

        if 'export_price_offset' in request.form:
            try:
                current_user.export_price_offset = float(request.form.get('export_price_offset', 0))
            except (ValueError, TypeError):
                pass

        if 'export_min_price' in request.form:
            try:
                current_user.export_min_price = float(request.form.get('export_min_price', 0))
            except (ValueError, TypeError):
                pass

        if 'export_boost_start' in request.form:
            current_user.export_boost_start = request.form.get('export_boost_start', '17:00')

        if 'export_boost_end' in request.form:
            current_user.export_boost_end = request.form.get('export_boost_end', '21:00')

        if 'export_boost_threshold' in request.form:
            try:
                current_user.export_boost_threshold = float(request.form.get('export_boost_threshold', 0))
            except (ValueError, TypeError):
                pass

        # Chip Mode settings (inverse of export boost - suppress exports unless above threshold)
        current_user.chip_mode_enabled = 'chip_mode_enabled' in request.form
        logger.info(f"Saving Chip Mode enabled: {current_user.chip_mode_enabled}")

        if 'chip_mode_start' in request.form:
            current_user.chip_mode_start = request.form.get('chip_mode_start', '22:00')

        if 'chip_mode_end' in request.form:
            current_user.chip_mode_end = request.form.get('chip_mode_end', '06:00')

        if 'chip_mode_threshold' in request.form:
            try:
                current_user.chip_mode_threshold = float(request.form.get('chip_mode_threshold', 30.0))
            except (ValueError, TypeError):
                pass

        # AC-Coupled Inverter Curtailment settings
        current_user.inverter_curtailment_enabled = 'inverter_curtailment_enabled' in request.form
        logger.info(f"Saving inverter curtailment enabled: {current_user.inverter_curtailment_enabled}")

        if 'inverter_brand' in request.form:
            current_user.inverter_brand = request.form.get('inverter_brand', '') or None

        if 'inverter_model' in request.form:
            current_user.inverter_model = request.form.get('inverter_model', '') or None

        if 'inverter_host' in request.form:
            current_user.inverter_host = request.form.get('inverter_host', '') or None

        if 'inverter_port' in request.form:
            try:
                current_user.inverter_port = int(request.form.get('inverter_port', 502))
            except (ValueError, TypeError):
                current_user.inverter_port = 502

        if 'inverter_slave_id' in request.form:
            try:
                current_user.inverter_slave_id = int(request.form.get('inverter_slave_id', 1))
            except (ValueError, TypeError):
                current_user.inverter_slave_id = 1

        if 'inverter_token' in request.form:
            current_user.inverter_token = request.form.get('inverter_token', '') or None

        # Enphase Enlighten credentials for automatic JWT token refresh
        if 'enphase_username' in request.form:
            current_user.enphase_username = request.form.get('enphase_username', '') or None
        if 'enphase_password' in request.form:
            current_user.enphase_password = request.form.get('enphase_password', '') or None
        if 'enphase_serial' in request.form:
            current_user.enphase_serial = request.form.get('enphase_serial', '') or None

        if 'inverter_restore_soc' in request.form:
            try:
                restore_soc = int(request.form.get('inverter_restore_soc', 98))
                current_user.inverter_restore_soc = max(50, min(100, restore_soc))  # Clamp 50-100
            except (ValueError, TypeError):
                current_user.inverter_restore_soc = 98

        # Fronius-specific: load following mode (for users without 0W export profile)
        current_user.fronius_load_following = 'fronius_load_following' in request.form

        # Spike Protection setting
        current_user.spike_protection_enabled = 'spike_protection_enabled' in request.form
        logger.info(f"Saving spike protection enabled: {current_user.spike_protection_enabled}")

        # Settled Prices Only setting
        current_user.settled_prices_only = 'settled_prices_only' in request.form
        logger.info(f"Saving settled prices only: {current_user.settled_prices_only}")

        # Alpha: Force tariff mode toggle setting
        current_user.force_tariff_mode_toggle = 'force_tariff_mode_toggle' in request.form
        logger.info(f"Saving force tariff mode toggle: {current_user.force_tariff_mode_toggle}")

        # Sigenergy Modbus settings (for DC curtailment when battery_system is sigenergy)
        if current_user.battery_system == 'sigenergy':
            if 'sigenergy_modbus_host' in request.form:
                current_user.sigenergy_modbus_host = request.form.get('sigenergy_modbus_host', '') or None

            if 'sigenergy_modbus_port' in request.form:
                try:
                    current_user.sigenergy_modbus_port = int(request.form.get('sigenergy_modbus_port', 502))
                except (ValueError, TypeError):
                    current_user.sigenergy_modbus_port = 502

            if 'sigenergy_modbus_slave_id' in request.form:
                try:
                    current_user.sigenergy_modbus_slave_id = int(request.form.get('sigenergy_modbus_slave_id', 1))
                except (ValueError, TypeError):
                    current_user.sigenergy_modbus_slave_id = 1

            logger.info(f"Saving Sigenergy Modbus: host={current_user.sigenergy_modbus_host}, port={current_user.sigenergy_modbus_port}")

        try:
            db.session.commit()
            logger.info(f"Amber settings saved successfully: forecast_type={form.amber_forecast_type.data}, site_id={current_user.amber_site_id}")

            # Reinitialize WebSocket client with new site_id
            # In single-worker Docker deployments, always try to reinit (no lock check needed)
            if current_user.amber_site_id and current_user.amber_api_token_encrypted:
                init_fn = current_app.config.get('WEBSOCKET_INIT_FUNCTION')
                logger.info(f"WebSocket reinit check: init_fn={init_fn is not None}, site_id={current_user.amber_site_id}")

                if init_fn:
                    try:
                        from app.utils import decrypt_token
                        decrypted_token = decrypt_token(current_user.amber_api_token_encrypted)
                        init_fn(decrypted_token, current_user.amber_site_id)
                        logger.info(f"✅ WebSocket client reinitialized with site: {current_user.amber_site_id}")
                    except Exception as e:
                        logger.error(f"❌ Failed to reinitialize WebSocket: {e}", exc_info=True)
                else:
                    logger.warning("WebSocket init function not available - restart Flask to enable WebSocket")

            flash('Amber settings have been saved.')
        except Exception as e:
            logger.error(f"Error saving Amber settings to database: {e}")
            flash('Error saving Amber settings. Please try again.')
            db.session.rollback()

        return redirect(url_for('main.amber_settings'))
    elif request.method == 'POST':
        # Log validation errors if form didn't validate on POST
        logger.warning(f"Form validation failed. Errors: {form.errors}")
        logger.debug(f"Form data received: amber_site_id={form.amber_site_id.data}, forecast_type={form.amber_forecast_type.data}, curtailment={form.solar_curtailment_enabled.data}")
        logger.debug(f"Available choices for amber_site_id: {site_choices}")

    # Pre-populate form with existing data
    logger.debug("Pre-populating Amber settings form data")
    form.amber_forecast_type.data = current_user.amber_forecast_type or 'predicted'
    form.solar_curtailment_enabled.data = current_user.solar_curtailment_enabled or False
    form.amber_site_id.data = current_user.amber_site_id

    logger.info(f"Rendering Amber settings page - Forecast type: {form.amber_forecast_type.data}, Site ID: {current_user.amber_site_id}")
    return render_template('amber_settings.html', title='Amber Settings', form=form, amber_sites=amber_sites)


@bp.route('/logs')
@login_required
def logs():
    """Display application logs viewer"""
    logger.info(f"Logs page accessed by user: {current_user.email}")
    return render_template('logs.html', title='Application Logs')


# API Status and Data Routes
@bp.route('/api/status')
@login_required
@cache.cached(timeout=180, key_prefix=lambda: f'api_status_{current_user.id}')
def api_status():
    """Get connection status for both Amber and Tesla APIs"""
    logger.info(f"API status check requested by user: {current_user.email}")

    status = {
        'amber': {'connected': False, 'message': 'Not configured'},
        'tesla': {'connected': False, 'message': 'Not configured'}
    }

    # Check Amber connection
    amber_client = get_amber_client(current_user)
    if amber_client:
        connected, message = amber_client.test_connection()
        status['amber'] = {'connected': connected, 'message': message}
    else:
        status['amber']['message'] = 'No API token configured'

    # Check Tesla connection
    tesla_client = get_tesla_client(current_user)
    if tesla_client:
        connected, message = tesla_client.test_connection()
        status['tesla'] = {'connected': connected, 'message': message}
    else:
        status['tesla']['message'] = 'No access token configured'

    logger.info(f"API status: Amber={status['amber']['connected']}, Tesla={status['tesla']['connected']}")
    return jsonify(status)


@bp.route('/api/websocket/status')
@login_required
def websocket_status():
    """Get WebSocket connection health status"""
    from flask import current_app

    ws_client = current_app.config.get('AMBER_WEBSOCKET_CLIENT')

    if not ws_client:
        return jsonify({
            'available': False,
            'status': 'not_initialized',
            'message': 'WebSocket client not initialized (no Amber credentials or site configured)'
        })

    # Get health status from WebSocket client
    health = ws_client.get_health_status()

    return jsonify({
        'available': True,
        'status': health.get('status', 'unknown'),
        'connected': health.get('connected', False),
        'last_update': health.get('last_update'),
        'age_seconds': round(health.get('age_seconds', 0), 1) if health.get('age_seconds') else None,
        'message_count': health.get('message_count', 0),
        'error_count': health.get('error_count', 0),
        'last_error': health.get('last_error'),
        'has_cached_data': health.get('has_cached_data', False),
        'message': f"Status: {health.get('status', 'unknown')}, Messages: {health.get('message_count', 0)}"
    })


@bp.route('/api/tesla/energy-sites')
@login_required
def get_tesla_energy_sites():
    """Get list of Tesla energy sites for site selection dropdown"""
    tesla_client = get_tesla_client(current_user)
    if not tesla_client:
        return jsonify({'error': 'Tesla not connected', 'sites': []}), 400

    try:
        energy_sites = tesla_client.get_energy_sites()
        sites = []
        for site in energy_sites:
            site_id = str(site.get('energy_site_id', ''))
            site_name = site.get('site_name', f'Energy Site {site_id}')
            # Try to get more details
            resource_type = site.get('resource_type', 'unknown')
            sites.append({
                'id': site_id,
                'name': site_name,
                'type': resource_type,
                'selected': site_id == current_user.tesla_energy_site_id
            })
        return jsonify({'sites': sites, 'current': current_user.tesla_energy_site_id})
    except Exception as e:
        logger.error(f"Error fetching energy sites: {e}")
        return jsonify({'error': str(e), 'sites': []}), 500


@bp.route('/api/tesla/select-site', methods=['POST'])
@login_required
def select_tesla_energy_site():
    """Select a Tesla energy site"""
    data = request.get_json()
    site_id = data.get('site_id')

    if not site_id:
        return jsonify({'success': False, 'error': 'No site_id provided'}), 400

    # Verify the site exists
    tesla_client = get_tesla_client(current_user)
    if tesla_client:
        try:
            energy_sites = tesla_client.get_energy_sites()
            valid_ids = [str(s.get('energy_site_id', '')) for s in energy_sites]
            if site_id not in valid_ids:
                return jsonify({'success': False, 'error': 'Invalid site ID'}), 400
        except Exception as e:
            logger.error(f"Error verifying site ID: {e}")

    current_user.tesla_energy_site_id = site_id

    # Fetch and store the site's timezone for automation time triggers
    if tesla_client:
        try:
            site_info = tesla_client.get_site_info(site_id)
            if site_info:
                site_timezone = site_info.get('installation_time_zone')
                if site_timezone:
                    current_user.timezone = site_timezone
                    logger.info(f"Updated user timezone from Tesla site info: {site_timezone}")
        except Exception as e:
            logger.warning(f"Could not fetch site timezone: {e}")

    db.session.commit()
    logger.info(f"User {current_user.email} selected energy site: {site_id}")

    return jsonify({'success': True, 'site_id': site_id})


@bp.route('/api/amber/current-price')
@require_amber_client
def amber_current_price(amber_client, api_user=None, **kwargs):
    """Get current Amber electricity price using WebSocket (real-time) with REST API fallback

    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    logger.info(f"Current price requested by user: {user.email}")

    # Get WebSocket client from Flask app config
    from flask import current_app
    ws_client = current_app.config.get('AMBER_WEBSOCKET_CLIENT')

    # Try WebSocket first, fall back to REST API
    prices = amber_client.get_live_prices(ws_client=ws_client)

    if not prices:
        logger.error("No current price data available from WebSocket or REST API")
        return jsonify({'error': 'No current price data available'}), 500

    logger.info(f"Retrieved {len(prices)} price channels (WebSocket-first approach)")

    # Store prices in database and add display times
    try:
        for price_data in prices:
            # Check if we already have this price record
            nem_time = datetime.fromisoformat(price_data['nemTime'].replace('Z', '+00:00'))

            record = PriceRecord(
                user_id=user.id,
                per_kwh=price_data.get('perKwh'),
                spot_per_kwh=price_data.get('spotPerKwh'),
                wholesale_kwh_price=price_data.get('wholesaleKWHPrice'),
                network_kwh_price=price_data.get('networkKWHPrice'),
                market_kwh_price=price_data.get('marketKWHPrice'),
                green_kwh_price=price_data.get('greenKWHPrice'),
                channel_type=price_data.get('channelType'),
                forecast=price_data.get('forecast', False),
                nem_time=nem_time,
                spike_status=price_data.get('spikeStatus'),
                timestamp=datetime.utcnow()
            )
            db.session.add(record)

            # Add display time for the interval using Powerwall's timezone
            # For ActualInterval: use the actual interval's time range (nemTime - duration)
            # For CurrentInterval: use current browser time bucket
            user_tz = ZoneInfo(get_powerwall_timezone(user))

            if price_data.get('type') == 'ActualInterval':
                # Use actual interval's time range from the API
                # nemTime is END of interval, duration tells us the length
                duration = price_data.get('duration', 5)
                from datetime import timedelta
                interval_end_time = nem_time.astimezone(user_tz)
                interval_start_time = interval_end_time - timedelta(minutes=duration)

                price_data['displayIntervalStart'] = interval_start_time.strftime('%H:%M')
                price_data['displayIntervalEnd'] = interval_end_time.strftime('%H:%M')
            else:
                # CurrentInterval: calculate from current browser time
                current_time = datetime.now(user_tz)
                minute = current_time.minute
                hour = current_time.hour
                interval_start = (minute // 5) * 5
                interval_end = interval_start + 5

                # Handle interval crossing hour boundary (e.g., 17:55 - 18:00)
                if interval_end >= 60:
                    end_hour = (hour + 1) % 24
                    end_minute = interval_end - 60
                    price_data['displayIntervalStart'] = f"{hour:02d}:{interval_start:02d}"
                    price_data['displayIntervalEnd'] = f"{end_hour:02d}:{end_minute:02d}"
                else:
                    price_data['displayIntervalStart'] = f"{hour:02d}:{interval_start:02d}"
                    price_data['displayIntervalEnd'] = f"{hour:02d}:{interval_end:02d}"

        db.session.commit()
        logger.info(f"Saved {len(prices)} price records to database")
    except Exception as e:
        logger.error(f"Error saving price records: {e}")
        db.session.rollback()

    return jsonify(prices)


@bp.route('/api/current-price')
@login_required
def current_price():
    """Get current electricity price - unified endpoint for Amber and AEMO users"""
    logger.info(f"Current price requested by user: {current_user.email}")

    # Determine price source based on user settings
    use_aemo = (
        current_user.electricity_provider == 'flow_power' and
        current_user.flow_power_price_source == 'aemo'
    )

    if use_aemo:
        # Use AEMO data for Flow Power AEMO-only mode
        aemo_region = current_user.flow_power_state
        if not aemo_region:
            logger.error("AEMO price source selected but no region configured")
            return jsonify({'error': 'AEMO region not configured'}), 400

        logger.info(f"Fetching AEMO current price for region: {aemo_region}")
        aemo_client = AEMOAPIClient()
        price_data = aemo_client.get_region_price(aemo_region)

        if not price_data:
            logger.error(f"Failed to fetch AEMO price for {aemo_region}")
            return jsonify({'error': 'Failed to fetch AEMO price'}), 500

        # Get network tariff settings for price adjustment
        # Normalize rates - if < 0.1, assume entered in dollars and convert to cents
        # Threshold 0.1 catches dollar mistakes while preserving low off-peak rates (~0.4c/kWh)
        def normalize_rate(rate, default):
            if rate is None:
                return default
            if rate < 0.1:
                return rate * 100  # Convert dollars to cents
            return rate

        network_tariff_type = current_user.network_tariff_type or 'flat'
        network_flat_rate = normalize_rate(current_user.network_flat_rate, 8.0)
        network_other_fees = normalize_rate(current_user.network_other_fees, 1.5)
        network_include_gst = current_user.network_include_gst if current_user.network_include_gst is not None else True

        # Calculate network charges for current time
        now = datetime.now()
        hour, minute = now.hour, now.minute

        if network_tariff_type == 'flat':
            network_charge_cents = network_flat_rate
        else:
            # TOU - determine which rate applies
            time_minutes = hour * 60 + minute
            peak_start = current_user.network_peak_start or '16:00'
            peak_end = current_user.network_peak_end or '21:00'
            offpeak_start = current_user.network_offpeak_start or '10:00'
            offpeak_end = current_user.network_offpeak_end or '15:00'

            peak_start_mins = int(peak_start.split(':')[0]) * 60 + int(peak_start.split(':')[1])
            peak_end_mins = int(peak_end.split(':')[0]) * 60 + int(peak_end.split(':')[1])
            offpeak_start_mins = int(offpeak_start.split(':')[0]) * 60 + int(offpeak_start.split(':')[1])
            offpeak_end_mins = int(offpeak_end.split(':')[0]) * 60 + int(offpeak_end.split(':')[1])

            if peak_start_mins <= time_minutes < peak_end_mins:
                network_charge_cents = normalize_rate(current_user.network_peak_rate, 15.0)
            elif offpeak_start_mins <= time_minutes < offpeak_end_mins:
                network_charge_cents = normalize_rate(current_user.network_offpeak_rate, 2.0)
            else:
                network_charge_cents = normalize_rate(current_user.network_shoulder_rate, 5.0)

        # Add other fees and GST
        total_network_cents = network_charge_cents + network_other_fees
        if network_include_gst:
            total_network_cents = total_network_cents * 1.10

        # AEMO price is in $/MWh, convert to c/kWh and add network charges
        wholesale_cents = price_data.get('price', 0) / 10  # $/MWh to c/kWh
        total_price_cents = wholesale_cents + total_network_cents

        # Calculate 5-minute interval times
        interval_start = (minute // 5) * 5
        interval_end = interval_start + 5
        if interval_end >= 60:
            end_hour = (hour + 1) % 24
            end_minute = interval_end - 60
            display_end = f"{end_hour:02d}:{end_minute:02d}"
        else:
            display_end = f"{hour:02d}:{interval_end:02d}"
        display_start = f"{hour:02d}:{interval_start:02d}"

        # Format response to match Amber format expected by dashboard
        prices = [
            {
                'channelType': 'general',
                'perKwh': round(total_price_cents, 2),
                'wholesalePerKwh': round(wholesale_cents, 2),
                'networkPerKwh': round(total_network_cents, 2),
                'type': 'CurrentInterval',
                'displayIntervalStart': display_start,
                'displayIntervalEnd': display_end,
                'renewables': 0,  # AEMO doesn't provide this
                'source': 'AEMO',
                'region': aemo_region,
            },
            {
                'channelType': 'feedIn',
                'perKwh': 0,  # Flow Power: 0c outside Happy Hour
                'type': 'CurrentInterval',
                'displayIntervalStart': display_start,
                'displayIntervalEnd': display_end,
                'source': 'AEMO',
            }
        ]

        # Check if we're in Happy Hour (5:30pm - 7:30pm) for feed-in
        if (17 * 60 + 30) <= (hour * 60 + minute) < (19 * 60 + 30):
            # Happy Hour export rate
            from app.tariff_converter import FLOW_POWER_EXPORT_RATES
            export_rate = FLOW_POWER_EXPORT_RATES.get(aemo_region, 0.45)
            prices[1]['perKwh'] = round(export_rate * 100, 2)  # Convert $/kWh to c/kWh

        logger.info(f"AEMO price for {aemo_region}: wholesale={wholesale_cents:.2f}c + network={total_network_cents:.2f}c = {total_price_cents:.2f}c/kWh")
        return jsonify({'prices': prices, 'source': 'AEMO'})

    else:
        # Use Amber API (default) - redirect to existing endpoint
        amber_client = get_amber_client(current_user)
        if not amber_client:
            logger.warning("Amber client not available for current price")
            return jsonify({'error': 'Amber API not configured'}), 400

        # Get WebSocket client from Flask app config
        from flask import current_app
        ws_client = current_app.config.get('AMBER_WEBSOCKET_CLIENT')

        # Try WebSocket first, fall back to REST API
        prices = amber_client.get_live_prices(ws_client=ws_client)

        if not prices:
            logger.error("No current price data available from WebSocket or REST API")
            return jsonify({'error': 'No current price data available'}), 500

        # Add display times for Amber prices
        user_tz = ZoneInfo(get_powerwall_timezone(current_user))
        for price_data in prices:
            nem_time = datetime.fromisoformat(price_data['nemTime'].replace('Z', '+00:00'))

            if price_data.get('type') == 'ActualInterval':
                duration = price_data.get('duration', 5)
                interval_end_time = nem_time.astimezone(user_tz)
                interval_start_time = interval_end_time - timedelta(minutes=duration)
                price_data['displayIntervalStart'] = interval_start_time.strftime('%H:%M')
                price_data['displayIntervalEnd'] = interval_end_time.strftime('%H:%M')
            else:
                current_time = datetime.now(user_tz)
                minute = current_time.minute
                hour = current_time.hour
                interval_start = (minute // 5) * 5
                interval_end = interval_start + 5
                if interval_end >= 60:
                    end_hour = (hour + 1) % 24
                    end_minute = interval_end - 60
                    price_data['displayIntervalStart'] = f"{hour:02d}:{interval_start:02d}"
                    price_data['displayIntervalEnd'] = f"{end_hour:02d}:{end_minute:02d}"
                else:
                    price_data['displayIntervalStart'] = f"{hour:02d}:{interval_start:02d}"
                    price_data['displayIntervalEnd'] = f"{hour:02d}:{interval_end:02d}"

        logger.info(f"Amber prices: {len(prices)} channels")
        return jsonify({'prices': prices, 'source': 'Amber'})


@bp.route('/api/amber/5min-forecast')
@api_auth_required
def amber_5min_forecast(api_user=None, **kwargs):
    """Get 5-minute interval forecast for the next few hours

    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    # Allow requesting more hours for the 30-min forecast view (default: 1 hour)
    hours = request.args.get('hours', 1, type=int)
    hours = min(hours, 4)  # Cap at 4 hours to avoid excessive API calls
    logger.info(f"5-minute forecast requested by user: {user.email} (hours={hours})")

    amber_client = get_amber_client(user)
    if not amber_client:
        logger.warning("Amber client not available for 5-min forecast")
        return jsonify({'error': 'Amber API not configured'}), 400

    # Get forecast data at 5-minute resolution
    forecast = amber_client.get_price_forecast(next_hours=hours, resolution=5)
    if not forecast:
        logger.error("Failed to fetch 5-minute forecast")
        return jsonify({'error': 'Failed to fetch 5-minute forecast'}), 500

    # Convert nemTime to user's local timezone for each interval
    user_tz = ZoneInfo(get_powerwall_timezone(user))

    # Get current time in user's timezone to filter out past intervals
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(user_tz)
    # Round down to start of current 30-min block (so we include all intervals in current block)
    current_30min_start = now_local.replace(minute=(now_local.minute // 30) * 30, second=0, microsecond=0)

    logger.info(f"Current time in user timezone: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}, filtering from start of 30-min block: {current_30min_start.strftime('%H:%M')}")

    filtered_forecast = []
    for interval in forecast:
        if 'nemTime' in interval:
            try:
                # Parse nemTime (already in Australian Eastern Time with timezone)
                # Example: "2025-11-12T17:05:00+10:00"
                nem_dt = datetime.fromisoformat(interval['nemTime'])

                # Convert to user's timezone
                local_dt = nem_dt.astimezone(user_tz)

                # Skip intervals before the start of the current 30-min block
                # This ensures we show all 6 intervals in the current block (past ones will be greyed in UI)
                if local_dt < current_30min_start:
                    continue

                # Add localTime field (naive datetime string in user's timezone)
                interval['localTime'] = local_dt.strftime('%Y-%m-%dT%H:%M:%S')
                interval['localHour'] = local_dt.hour
                interval['localMinute'] = local_dt.minute
                filtered_forecast.append(interval)
            except Exception as e:
                logger.error(f"Error converting nemTime to local timezone: {e}")

    logger.info(f"Filtered forecast: {len(forecast)} -> {len(filtered_forecast)} intervals (kept from current 30-min block onwards)")

    # Group by channel type and return
    general_intervals = [i for i in filtered_forecast if i.get('channelType') == 'general']
    feedin_intervals = [i for i in filtered_forecast if i.get('channelType') == 'feedIn']

    result = {
        'fetch_time': datetime.utcnow().isoformat(),
        'total_intervals': len(forecast),
        'forecast_type': user.amber_forecast_type or 'predicted',
        'general': general_intervals,
        'feedIn': feedin_intervals
    }

    logger.info(f"5-min forecast: {len(general_intervals)} general, {len(feedin_intervals)} feedIn intervals (using {result['forecast_type']} prices)")
    return jsonify(result)


@bp.route('/api/amber/30min-forecast')
@login_required
def amber_30min_forecast():
    """Get 30-minute interval forecast for extended hours (48 hours available)"""
    # Allow requesting up to 48 hours for the 30-min forecast view
    hours = request.args.get('hours', 8, type=int)
    hours = min(hours, 48)  # Cap at 48 hours
    logger.info(f"30-minute forecast requested by user: {current_user.email} (hours={hours})")

    amber_client = get_amber_client(current_user)
    if not amber_client:
        logger.warning("Amber client not available for 30-min forecast")
        return jsonify({'error': 'Amber API not configured'}), 400

    # Get forecast data at 30-minute resolution (has data for full 48 hours)
    forecast = amber_client.get_price_forecast(next_hours=hours, resolution=30)
    if not forecast:
        logger.error("Failed to fetch 30-minute forecast")
        return jsonify({'error': 'Failed to fetch 30-minute forecast'}), 500

    # Convert nemTime to user's local timezone for each interval
    user_tz = ZoneInfo(get_powerwall_timezone(current_user))

    # Get current time in user's timezone to filter out past intervals
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(user_tz)
    # Round down to start of current 30-min block
    current_30min_start = now_local.replace(minute=(now_local.minute // 30) * 30, second=0, microsecond=0)

    logger.info(f"30-min forecast: Current time in user timezone: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}, filtering from: {current_30min_start.strftime('%H:%M')}")

    filtered_forecast = []
    for interval in forecast:
        if 'nemTime' in interval:
            try:
                # Parse nemTime (already in Australian Eastern Time with timezone)
                nem_dt = datetime.fromisoformat(interval['nemTime'])

                # Convert to user's timezone
                local_dt = nem_dt.astimezone(user_tz)

                # Skip intervals before the start of the current 30-min block
                if local_dt < current_30min_start:
                    continue

                # Add localTime field (naive datetime string in user's timezone)
                interval['localTime'] = local_dt.strftime('%Y-%m-%dT%H:%M:%S')
                interval['localHour'] = local_dt.hour
                interval['localMinute'] = local_dt.minute
                filtered_forecast.append(interval)
            except Exception as e:
                logger.error(f"Error converting nemTime to local timezone: {e}")

    logger.info(f"30-min forecast: {len(forecast)} -> {len(filtered_forecast)} intervals (kept from current 30-min block onwards)")

    # Group by channel type and return
    general_intervals = [i for i in filtered_forecast if i.get('channelType') == 'general']
    feedin_intervals = [i for i in filtered_forecast if i.get('channelType') == 'feedIn']

    result = {
        'fetch_time': datetime.utcnow().isoformat(),
        'total_intervals': len(forecast),
        'forecast_type': current_user.amber_forecast_type or 'predicted',
        'general': general_intervals,
        'feedIn': feedin_intervals
    }

    logger.info(f"30-min forecast: {len(general_intervals)} general, {len(feedin_intervals)} feedIn intervals (using {result['forecast_type']} prices)")
    return jsonify(result)


@bp.route('/api/amber/debug-forecast')
@login_required
def amber_debug_forecast():
    """
    Debug endpoint to fetch raw Amber forecast data.
    Returns all available price fields from Amber API for the next 48 hours.
    """
    logger.info(f"Debug forecast requested by user: {current_user.email}")

    amber_client = get_amber_client(current_user)
    if not amber_client:
        logger.warning("Amber client not available")
        return jsonify({'error': 'Amber API not configured'}), 400

    # Get 48 hours of forecast data
    forecast = amber_client.get_price_forecast(next_hours=48)
    if not forecast:
        logger.error("Failed to fetch price forecast")
        return jsonify({'error': 'Failed to fetch price forecast'}), 500

    # Format the data for easy comparison
    debug_data = {
        'total_intervals': len(forecast),
        'fetch_time': datetime.utcnow().isoformat(),
        'intervals': []
    }

    for interval in forecast:
        # Extract all available fields
        interval_data = {
            'nemTime': interval.get('nemTime'),
            'startTime': interval.get('startTime'),
            'endTime': interval.get('endTime'),
            'duration': interval.get('duration'),
            'channelType': interval.get('channelType'),
            'descriptor': interval.get('descriptor'),

            # All price fields
            'perKwh': interval.get('perKwh'),
            'spotPerKwh': interval.get('spotPerKwh'),
            'wholesaleKWHPrice': interval.get('wholesaleKWHPrice'),
            'networkKWHPrice': interval.get('networkKWHPrice'),
            'marketKWHPrice': interval.get('marketKWHPrice'),
            'greenKWHPrice': interval.get('greenKWHPrice'),
            'lossFactor': interval.get('lossFactor'),

            # Metadata
            'spikeStatus': interval.get('spikeStatus'),
            'forecast': interval.get('forecast'),
            'renewables': interval.get('renewables'),
            'estimate': interval.get('estimate')
        }
        debug_data['intervals'].append(interval_data)

    # Group by channel type for easier analysis
    general_intervals = [i for i in debug_data['intervals'] if i['channelType'] == 'general']
    feedin_intervals = [i for i in debug_data['intervals'] if i['channelType'] == 'feedIn']

    summary = {
        'total_intervals': debug_data['total_intervals'],
        'fetch_time': debug_data['fetch_time'],
        'general_channel_count': len(general_intervals),
        'feedin_channel_count': len(feedin_intervals),
        'general_intervals': general_intervals,
        'feedin_intervals': feedin_intervals,
        'sample_fields': list(debug_data['intervals'][0].keys()) if debug_data['intervals'] else []
    }

    logger.info(f"Debug forecast: {len(general_intervals)} general, {len(feedin_intervals)} feedIn intervals")
    return jsonify(summary)


@bp.route('/api/validate-token')
def validate_token():
    """Validate API token without requiring Tesla credentials.

    This endpoint only checks if the Bearer token is valid,
    allowing mobile apps to verify authentication before making
    other API calls that require Tesla credentials.
    """
    from app.route_helpers import get_api_user

    user = get_api_user()
    if not user:
        return jsonify({
            'valid': False,
            'error': 'Invalid or missing API token'
        }), 401

    return jsonify({
        'valid': True,
        'email': user.email,
        'has_tesla_credentials': bool(user.teslemetry_api_key_encrypted or user.fleet_api_refresh_token_encrypted),
        'has_site_id': bool(user.tesla_energy_site_id)
    })


@bp.route('/api/tesla/status')
@require_tesla_client
@require_tesla_site_id
def tesla_status(tesla_client, api_user=None, **kwargs):
    """Get Tesla Powerwall status including firmware version

    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    logger.info(f"Tesla status requested by user: {user.email}")

    # Get live status
    site_status = tesla_client.get_site_status(user.tesla_energy_site_id)
    if not site_status:
        logger.error("Failed to fetch Tesla site status")
        return jsonify({'error': 'Failed to fetch site status'}), 500

    # Get site info for firmware version
    site_info = tesla_client.get_site_info(user.tesla_energy_site_id)

    # Add firmware version to response if available
    if site_info:
        site_status['firmware_version'] = site_info.get('version', 'Unknown')
        logger.info(f"Firmware version: {site_status['firmware_version']}")

    return jsonify(site_status)


@bp.route('/api/tesla/powerwall-type')
@require_tesla_client
@require_tesla_site_id
def powerwall_type(tesla_client, api_user=None, **kwargs):
    """Get Powerwall type (PW2 or PW3) from Tesla site info.

    Returns the gateway part_name which indicates Powerwall version.
    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    logger.info(f"Powerwall type requested by user: {user.email}")

    site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
    if not site_info:
        logger.error("Failed to fetch Tesla site info")
        return jsonify({'error': 'Failed to fetch site info'}), 500

    # Extract gateway info - gateways array contains part_name
    gateways = site_info.get('components', {}).get('gateways', [])
    if not gateways:
        # Try top-level gateways
        gateways = site_info.get('gateways', [])

    powerwall_type = 'unknown'
    part_name = None

    if gateways and len(gateways) > 0:
        gateway = gateways[0]  # Primary gateway
        part_name = gateway.get('part_name', '')

        # Detect type from part_name
        if 'Powerwall 3' in part_name:
            powerwall_type = 'PW3'
        elif 'Powerwall 2' in part_name or 'Powerwall+' in part_name:
            powerwall_type = 'PW2'
        elif 'Powerwall' in part_name:
            # Generic Powerwall, try to determine from part_number
            part_number = gateway.get('part_number', '')
            if part_number.startswith('170'):  # PW3 part numbers start with 170
                powerwall_type = 'PW3'
            else:
                powerwall_type = 'PW2'  # Default to PW2 for older units

    logger.info(f"Powerwall type: {powerwall_type} (part_name: {part_name})")

    return jsonify({
        'powerwall_type': powerwall_type,
        'part_name': part_name,
        'gateway_count': len(gateways)
    })


@bp.route('/api/sigenergy/live-status')
@api_auth_required
def sigenergy_live_status(api_user=None, **kwargs):
    """Get Sigenergy inverter live status via Modbus TCP.

    Returns power flow data in same format as Tesla status endpoint.
    Supports both session login and Bearer token authentication.
    """
    from app.sigenergy_modbus import get_sigenergy_modbus_client

    user = api_user or current_user
    logger.info(f"Sigenergy live status requested by user: {user.email}")

    # Check if user has Sigenergy configured
    if user.battery_system != 'sigenergy':
        return jsonify({'error': 'Sigenergy not configured as battery system'}), 400

    # Check if Modbus is configured
    if not user.sigenergy_modbus_host:
        return jsonify({'error': 'Sigenergy Modbus not configured. Please set IP address in settings.'}), 400

    # Get Modbus client
    client = get_sigenergy_modbus_client(user)
    if not client:
        return jsonify({'error': 'Failed to create Sigenergy Modbus client'}), 500

    # Get live status
    status = client.get_live_status()
    if 'error' in status:
        logger.error(f"Sigenergy status error: {status['error']}")
        return jsonify(status), 500

    return jsonify(status)


@bp.route('/api/sigenergy/battery-health')
@api_auth_required
def sigenergy_battery_health(api_user=None, **kwargs):
    """Get Sigenergy battery health information via Modbus TCP.

    Returns battery State of Health (SOH) and rated capacity.
    Supports both session login and Bearer token authentication.
    """
    from app.sigenergy_modbus import get_sigenergy_modbus_client

    user = api_user or current_user
    logger.info(f"Sigenergy battery health requested by user: {user.email}")

    # Check if user has Sigenergy configured
    if user.battery_system != 'sigenergy':
        return jsonify({'error': 'Sigenergy not configured as battery system'}), 400

    # Check if Modbus is configured
    if not user.sigenergy_modbus_host:
        return jsonify({'error': 'Sigenergy Modbus not configured. Please set IP address in settings.'}), 400

    # Get Modbus client
    client = get_sigenergy_modbus_client(user)
    if not client:
        return jsonify({'error': 'Failed to create Sigenergy Modbus client'}), 500

    # Get battery health
    health = client.get_battery_health()
    if 'error' in health:
        logger.error(f"Sigenergy battery health error: {health['error']}")
        return jsonify(health), 500

    return jsonify(health)


@bp.route('/api/sigenergy/energy-summary')
@api_auth_required
def sigenergy_energy_summary(api_user=None, **kwargs):
    """Get Sigenergy accumulated energy totals via Modbus TCP.

    Returns lifetime and daily energy statistics directly from Modbus registers.
    Supports both session login and Bearer token authentication.
    """
    from app.sigenergy_modbus import get_sigenergy_modbus_client

    user = api_user or current_user
    logger.info(f"Sigenergy energy summary requested by user: {user.email}")

    # Check if user has Sigenergy configured
    if user.battery_system != 'sigenergy':
        return jsonify({'error': 'Sigenergy not configured as battery system'}), 400

    # Check if Modbus is configured
    if not user.sigenergy_modbus_host:
        return jsonify({'error': 'Sigenergy Modbus not configured. Please set IP address in settings.'}), 400

    # Get Modbus client
    client = get_sigenergy_modbus_client(user)
    if not client:
        return jsonify({'error': 'Failed to create Sigenergy Modbus client'}), 500

    # Get energy summary
    summary = client.get_energy_summary()
    if 'error' in summary:
        logger.error(f"Sigenergy energy summary error: {summary['error']}")
        return jsonify(summary), 500

    return jsonify(summary)


@bp.route('/api/sigenergy/calendar-history')
@api_auth_required
def sigenergy_calendar_history(api_user=None, **kwargs):
    """Get historical energy summaries for Sigenergy users.

    Aggregates data from EnergyRecord table since Sigenergy Cloud API
    doesn't provide historical energy data like Tesla does.

    Supports both session login and Bearer token authentication.
    """
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import func
    from app.models import EnergyRecord

    user = api_user or current_user
    logger.info(f"Sigenergy calendar history requested by user: {user.email}")

    # Check if user has Sigenergy configured
    if user.battery_system != 'sigenergy':
        return jsonify({'error': 'Sigenergy not configured as battery system'}), 400

    # Get parameters
    period = request.args.get('period', 'day')  # day, week, month, year
    end_date_str = request.args.get('end_date')

    # Get user's timezone
    user_tz = ZoneInfo(get_powerwall_timezone(user))
    now_local = datetime.now(user_tz)

    # Parse end_date or use now
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=user_tz)
        except ValueError:
            end_date = now_local
    else:
        end_date = now_local

    # Calculate time range based on period
    if period == 'day':
        # Today's data
        start_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
        group_format = '%Y-%m-%d %H:00'  # Group by hour
    elif period == 'week':
        # Last 7 days
        start_date = end_date - timedelta(days=7)
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        group_format = '%Y-%m-%d'  # Group by day
    elif period == 'month':
        # Last 30 days
        start_date = end_date - timedelta(days=30)
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        group_format = '%Y-%m-%d'  # Group by day
    elif period == 'year':
        # Last 365 days
        start_date = end_date - timedelta(days=365)
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        group_format = '%Y-%m'  # Group by month
    else:
        start_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
        group_format = '%Y-%m-%d %H:00'

    # Convert to UTC for query
    start_utc = start_date.astimezone(timezone.utc)
    end_utc = end_date.astimezone(timezone.utc)

    # Query EnergyRecord for this period
    records = EnergyRecord.query.filter(
        EnergyRecord.user_id == user.id,
        EnergyRecord.timestamp >= start_utc,
        EnergyRecord.timestamp <= end_utc
    ).order_by(EnergyRecord.timestamp.asc()).all()

    if not records:
        logger.info(f"No energy records found for period {period}")
        return jsonify({
            'period': period,
            'time_series': [],
            'totals': {
                'solar_energy_exported': 0,
                'battery_energy_exported': 0,
                'battery_energy_imported_from_grid': 0,
                'battery_energy_imported_from_solar': 0,
                'grid_energy_imported': 0,
                'grid_energy_exported_from_solar': 0,
                'grid_energy_exported_from_battery': 0,
                'consumer_energy_imported_from_grid': 0,
                'consumer_energy_imported_from_solar': 0,
                'consumer_energy_imported_from_battery': 0,
            }
        })

    # Aggregate records into time buckets
    # Power is in watts, we need to convert to Wh (energy)
    # Since records are typically 5 minutes apart, each record represents ~5 min of energy
    # Energy (Wh) = Power (W) * Time (h) = Power (W) * (5/60)

    time_buckets = {}
    sample_interval_hours = 5 / 60  # Assume 5-minute sampling

    for record in records:
        # Convert timestamp to local time and format for grouping
        local_ts = record.timestamp.replace(tzinfo=timezone.utc).astimezone(user_tz)
        bucket_key = local_ts.strftime(group_format)

        if bucket_key not in time_buckets:
            time_buckets[bucket_key] = {
                'timestamp': bucket_key,
                'solar_wh': 0,
                'battery_discharge_wh': 0,
                'battery_charge_wh': 0,
                'grid_import_wh': 0,
                'grid_export_wh': 0,
                'home_wh': 0,
            }

        bucket = time_buckets[bucket_key]

        # Convert power (W) to energy (Wh) for this interval
        solar_wh = max(0, record.solar_power or 0) * sample_interval_hours
        home_wh = max(0, record.load_power or 0) * sample_interval_hours

        # Battery: positive = discharging, negative = charging
        battery_power = record.battery_power or 0
        if battery_power > 0:
            bucket['battery_discharge_wh'] += battery_power * sample_interval_hours
        else:
            bucket['battery_charge_wh'] += abs(battery_power) * sample_interval_hours

        # Grid: positive = importing, negative = exporting
        grid_power = record.grid_power or 0
        if grid_power > 0:
            bucket['grid_import_wh'] += grid_power * sample_interval_hours
        else:
            bucket['grid_export_wh'] += abs(grid_power) * sample_interval_hours

        bucket['solar_wh'] += solar_wh
        bucket['home_wh'] += home_wh

    # Build time series in Tesla calendar history format
    time_series = []
    for bucket_key in sorted(time_buckets.keys()):
        bucket = time_buckets[bucket_key]
        time_series.append({
            'timestamp': bucket['timestamp'],
            'solar_energy_exported': bucket['solar_wh'],
            'battery_energy_exported': bucket['battery_discharge_wh'],
            'battery_energy_imported_from_grid': bucket['battery_charge_wh'] * 0.5,  # Approximate split
            'battery_energy_imported_from_solar': bucket['battery_charge_wh'] * 0.5,
            'grid_energy_imported': bucket['grid_import_wh'],
            'grid_energy_exported_from_solar': bucket['grid_export_wh'] * 0.8,  # Approximate
            'grid_energy_exported_from_battery': bucket['grid_export_wh'] * 0.2,
            'consumer_energy_imported_from_grid': bucket['grid_import_wh'] * 0.7,
            'consumer_energy_imported_from_solar': bucket['home_wh'] * 0.5,
            'consumer_energy_imported_from_battery': bucket['battery_discharge_wh'] * 0.5,
        })

    # Calculate totals
    totals = {
        'solar_energy_exported': sum(b['solar_wh'] for b in time_buckets.values()),
        'battery_energy_exported': sum(b['battery_discharge_wh'] for b in time_buckets.values()),
        'battery_energy_imported_from_grid': sum(b['battery_charge_wh'] for b in time_buckets.values()) * 0.5,
        'battery_energy_imported_from_solar': sum(b['battery_charge_wh'] for b in time_buckets.values()) * 0.5,
        'grid_energy_imported': sum(b['grid_import_wh'] for b in time_buckets.values()),
        'grid_energy_exported_from_solar': sum(b['grid_export_wh'] for b in time_buckets.values()) * 0.8,
        'grid_energy_exported_from_battery': sum(b['grid_export_wh'] for b in time_buckets.values()) * 0.2,
        'consumer_energy_imported_from_grid': sum(b['grid_import_wh'] for b in time_buckets.values()) * 0.7,
        'consumer_energy_imported_from_solar': sum(b['home_wh'] for b in time_buckets.values()) * 0.5,
        'consumer_energy_imported_from_battery': sum(b['battery_discharge_wh'] for b in time_buckets.values()) * 0.5,
    }

    logger.info(f"Returning {len(time_series)} time series records for period '{period}'")

    return jsonify({
        'period': period,
        'time_series': time_series,
        'totals': totals,
    })


@bp.route('/api/price-history')
@login_required
def price_history():
    """Get historical price data"""
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo

    logger.info(f"Price history requested by user: {current_user.email}")

    # Get user's timezone
    user_tz = ZoneInfo(get_powerwall_timezone(current_user))

    # Get day parameter (default to 'today')
    day = request.args.get('day', 'today')

    # Calculate date range based on day parameter
    now_local = datetime.now(user_tz)

    if day == 'today':
        target_date = now_local
    elif day == 'yesterday':
        target_date = now_local - timedelta(days=1)
    else:
        # Try to parse as integer (days ago)
        try:
            days_ago = int(day)
            target_date = now_local - timedelta(days=days_ago)
        except ValueError:
            # Default to today if invalid
            target_date = now_local

    # Get start and end of target day in user's timezone
    start_of_day_local = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day_local = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Convert to UTC for database query
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    end_of_day_utc = end_of_day_local.astimezone(timezone.utc)

    logger.info(f"Fetching price history for {day}: {start_of_day_local.date()} (UTC range: {start_of_day_utc} to {end_of_day_utc})")

    # Get import price data for target day (general channel, only actual prices, not forecasts)
    import_records = PriceRecord.query.filter(
        PriceRecord.user_id == current_user.id,
        PriceRecord.channel_type == 'general',
        PriceRecord.forecast == False,
        PriceRecord.timestamp >= start_of_day_utc,
        PriceRecord.timestamp <= end_of_day_utc
    ).order_by(
        PriceRecord.timestamp.asc()
    ).all()

    # Get export price data for target day (feedIn channel, only actual prices, not forecasts)
    export_records = PriceRecord.query.filter(
        PriceRecord.user_id == current_user.id,
        PriceRecord.channel_type == 'feedIn',
        PriceRecord.forecast == False,
        PriceRecord.timestamp >= start_of_day_utc,
        PriceRecord.timestamp <= end_of_day_utc
    ).order_by(
        PriceRecord.timestamp.asc()
    ).all()

    import_data = []
    for record in import_records:
        # Convert UTC timestamp to user's timezone
        if record.timestamp.tzinfo is None:
            # Assume UTC if no timezone info
            utc_time = record.timestamp.replace(tzinfo=timezone.utc)
        else:
            utc_time = record.timestamp

        local_time = utc_time.astimezone(user_tz)

        import_data.append({
            'timestamp': local_time.isoformat(),
            'per_kwh': record.per_kwh,
            'spike_status': record.spike_status,
            'forecast': record.forecast
        })

    export_data = []
    for record in export_records:
        # Convert UTC timestamp to user's timezone
        if record.timestamp.tzinfo is None:
            # Assume UTC if no timezone info
            utc_time = record.timestamp.replace(tzinfo=timezone.utc)
        else:
            utc_time = record.timestamp

        local_time = utc_time.astimezone(user_tz)

        export_data.append({
            'timestamp': local_time.isoformat(),
            'per_kwh': record.per_kwh,
            'spike_status': record.spike_status,
            'forecast': record.forecast
        })

    # Calculate max prices from all records for the day
    max_import_price = max([record.per_kwh for record in import_records], default=0)
    max_export_price = max([record.per_kwh for record in export_records], default=0)

    logger.info(f"Returning {len(import_data)} import and {len(export_data)} export price history records for {day}")
    logger.info(f"Max prices for {day}: Import={max_import_price}¢/kWh, Export={max_export_price}¢/kWh")

    # Include metadata for chart configuration (midnight-to-midnight display)
    response_data = {
        'import': import_data,
        'export': export_data,
        'metadata': {
            'start_of_day': start_of_day_local.isoformat(),
            'end_of_day': end_of_day_local.isoformat(),
            'day': day,
            'date': target_date.strftime('%Y-%m-%d'),
            'timezone': str(user_tz),
            'max_import_price': max_import_price,
            'max_export_price': max_export_price
        }
    }

    return jsonify(response_data)


@bp.route('/api/energy-history')
@api_auth_required
def energy_history(api_user=None, **kwargs):
    """Get historical energy usage data for graphing

    Supports both session login and Bearer token authentication.
    """
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo

    user = api_user or current_user
    logger.info(f"Energy history requested by user: {user.email}")

    # Get user's timezone
    user_tz = ZoneInfo(get_powerwall_timezone(user))

    # Get timeframe parameter (default to 'day')
    timeframe = request.args.get('timeframe', 'day')

    # Get optional date parameter (YYYY-MM-DD format) for historical queries
    date_param = request.args.get('date')

    # Calculate time range based on timeframe
    from app.models import EnergyRecord

    if timeframe == 'day':
        # If date provided, use that date; otherwise use today
        if date_param:
            try:
                target_date = datetime.strptime(date_param, '%Y-%m-%d')
                start_of_day_local = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
            except ValueError:
                return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        else:
            now_local = datetime.now(user_tz)
            start_of_day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        # Calculate end of day
        end_of_day_local = start_of_day_local + timedelta(days=1) - timedelta(microseconds=1)
        start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
        end_of_day_utc = end_of_day_local.astimezone(timezone.utc)

        # Query records for the specified day
        records = EnergyRecord.query.filter(
            EnergyRecord.user_id == user.id,
            EnergyRecord.timestamp >= start_of_day_utc,
            EnergyRecord.timestamp <= end_of_day_utc
        ).order_by(
            EnergyRecord.timestamp.asc()
        ).all()

    elif timeframe == 'month':
        # Get last 30 days of data
        limit = 720  # 30 days * 24 hours
        records = EnergyRecord.query.filter_by(
            user_id=user.id
        ).order_by(
            EnergyRecord.timestamp.desc()
        ).limit(limit).all()

    else:  # year
        # Get last 365 days of data
        limit = 8760  # 365 days * 24 hours
        records = EnergyRecord.query.filter_by(
            user_id=user.id
        ).order_by(
            EnergyRecord.timestamp.desc()
        ).limit(limit).all()

    data = []
    # For 'day' view, records are already in ascending order
    # For 'month' and 'year', we need to reverse them (they're in desc order)
    records_to_process = records if timeframe == 'day' else reversed(records)

    for record in records_to_process:
        # Convert UTC timestamp to user's timezone
        if record.timestamp.tzinfo is None:
            # Assume UTC if no timezone info
            utc_time = record.timestamp.replace(tzinfo=timezone.utc)
        else:
            utc_time = record.timestamp

        local_time = utc_time.astimezone(user_tz)

        data.append({
            'timestamp': local_time.isoformat(),
            'solar_power': record.solar_power,
            'battery_power': record.battery_power,
            'grid_power': record.grid_power,
            'load_power': record.load_power,
            'battery_level': record.battery_level
        })

    logger.info(f"Returning {len(data)} energy history records for timeframe: {timeframe}")

    # For 'day' timeframe, include date range metadata for frontend chart configuration
    response_data = {
        'records': data,
        'timeframe': timeframe
    }

    if timeframe == 'day':
        # Send start/end of day in user's timezone for chart x-axis configuration
        # Use the requested date if provided, otherwise use today
        if date_param:
            try:
                target_date = datetime.strptime(date_param, '%Y-%m-%d')
                start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
            except ValueError:
                start_of_day = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_of_day = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day.replace(hour=23, minute=59, second=59, microsecond=999999)

        response_data['metadata'] = {
            'start_of_day': start_of_day.isoformat(),
            'end_of_day': end_of_day.isoformat(),
            'timezone': str(user_tz)
        }

    return jsonify(response_data)


@bp.route('/api/energy-calendar-history')
@api_auth_required
def energy_calendar_history_unified(api_user=None, **kwargs):
    """Get historical energy summaries - routes to appropriate backend.

    For Sigenergy users: Uses Modbus energy registers
    For Tesla users: Uses Tesla calendar history API

    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    logger.info(f"Energy calendar history requested by user: {user.email}")

    # Route Sigenergy users to Modbus energy summary
    if user.battery_system == 'sigenergy':
        if not user.sigenergy_modbus_host:
            return jsonify({'error': 'Sigenergy Modbus not configured'}), 400

        from app.sigenergy_modbus import get_sigenergy_modbus_client
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({'error': 'Failed to create Sigenergy Modbus client'}), 500

        summary = client.get_energy_summary()
        if 'error' in summary:
            return jsonify(summary), 500

        # Format as calendar history response for app compatibility
        from datetime import datetime
        now = datetime.now().isoformat()
        return jsonify({
            'period': request.args.get('period', 'day'),
            'time_series': [{
                'timestamp': now,
                'solar_energy_exported': summary.get('total_pv_energy_kwh', 0) * 1000,  # kWh to Wh
                'grid_energy_imported': summary.get('total_grid_import_kwh', 0) * 1000,
                'grid_energy_exported_from_solar': summary.get('total_grid_export_kwh', 0) * 1000,
                'battery_energy_exported': summary.get('total_battery_discharged_kwh', 0) * 1000,
                'battery_energy_imported_from_grid': summary.get('total_battery_charged_kwh', 0) * 500,
                'battery_energy_imported_from_solar': summary.get('total_battery_charged_kwh', 0) * 500,
                'consumer_energy_imported_from_grid': summary.get('total_load_energy_kwh', 0) * 1000,
            }],
            'totals': {
                'total_solar_wh': summary.get('total_pv_energy_kwh', 0) * 1000,
                'total_grid_import_wh': summary.get('total_grid_import_kwh', 0) * 1000,
                'total_grid_export_wh': summary.get('total_grid_export_kwh', 0) * 1000,
                'total_battery_charge_wh': summary.get('total_battery_charged_kwh', 0) * 1000,
                'total_battery_discharge_wh': summary.get('total_battery_discharged_kwh', 0) * 1000,
                'total_home_wh': summary.get('total_load_energy_kwh', 0) * 1000,
                'daily_load_wh': summary.get('daily_load_energy_kwh', 0) * 1000,
            },
            'source': 'sigenergy_modbus'
        })

    # Tesla users - need Tesla client
    tesla_client = get_tesla_client(user)
    if not tesla_client:
        return jsonify({'error': 'Tesla API not configured'}), 400

    if not user.tesla_energy_site_id:
        return jsonify({'error': 'Tesla Energy Site ID not configured'}), 400

    # Get parameters
    period = request.args.get('period', 'month')  # day, week, month, year, lifetime
    end_date_str = request.args.get('end_date')  # Optional: datetime with timezone

    # Convert end_date to proper format if provided
    # Otherwise, get_calendar_history will use current time
    end_date = None
    if end_date_str:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            # Parse YYYY-MM-DD and convert to datetime with user's timezone
            user_tz = ZoneInfo(get_powerwall_timezone(user))
            dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = dt.replace(hour=23, minute=59, second=59, tzinfo=user_tz)
            end_date = end_dt.isoformat()
        except Exception as e:
            logger.warning(f"Invalid end_date format: {end_date_str}, using default: {e}")

    # Fetch calendar history
    history = tesla_client.get_calendar_history(
        site_id=user.tesla_energy_site_id,
        kind='energy',
        period=period,
        end_date=end_date,
        timezone=get_powerwall_timezone(user)
    )

    if not history:
        logger.error("Failed to fetch calendar history")
        return jsonify({'error': 'Failed to fetch calendar history'}), 500

    # Extract time series data
    time_series = history.get('time_series', [])

    # Tesla API seems to ignore the period parameter and returns all data
    # Filter client-side based on requested period
    if time_series and period in ['day', 'week', 'month', 'year']:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        user_tz = ZoneInfo(get_powerwall_timezone(user))

        # Use the requested end_date for filtering, not current time
        # This ensures historical data is correctly filtered when viewing past periods
        if end_date_str:
            try:
                reference_date = datetime.strptime(end_date_str, '%Y-%m-%d').replace(tzinfo=user_tz)
            except ValueError:
                reference_date = datetime.now(user_tz)
        else:
            reference_date = datetime.now(user_tz)

        # Calculate cutoff date based on period, relative to reference_date
        if period == 'day':
            # Single day - filter to entries from that day only
            cutoff = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            # Last 7 days from reference date
            cutoff = (reference_date - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'month':
            # Last 30 days from reference date
            cutoff = (reference_date - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'year':
            # Last 365 days from reference date
            cutoff = (reference_date - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)

        # Filter time_series to entries within the date range (cutoff to reference_date)
        # End of reference day (23:59:59) as upper bound
        end_of_day = reference_date.replace(hour=23, minute=59, second=59, microsecond=999999)

        filtered_series = []
        for entry in time_series:
            try:
                # Parse timestamp (format: 2025-01-15T00:00:00+10:00)
                ts_str = entry.get('timestamp', '')
                if ts_str:
                    entry_dt = datetime.fromisoformat(ts_str)
                    # Filter to entries within the date range
                    if cutoff <= entry_dt <= end_of_day:
                        filtered_series.append(entry)
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse timestamp: {entry.get('timestamp')}: {e}")
                continue

        logger.info(f"Filtered calendar history from {len(time_series)} to {len(filtered_series)} records for period '{period}' (cutoff={cutoff.date()}, end={end_of_day.date()})")
        time_series = filtered_series

    # Debug: Log sample of time series data to diagnose identical values issue
    if time_series and len(time_series) > 0:
        sample_entries = time_series[:min(5, len(time_series))]
        for i, entry in enumerate(sample_entries):
            solar = entry.get('solar_energy_exported', 0)
            logger.debug(f"Calendar entry {i}: ts={entry.get('timestamp')}, solar={solar:.1f}Wh")

    # Format response
    data = {
        'period': period,
        'time_series': time_series,
        'serial_number': history.get('serial_number'),
        'installation_date': history.get('installation_date')
    }

    logger.info(f"Returning calendar history: {len(time_series)} records for period '{period}'")
    return jsonify(data)


@bp.route('/api/tou-schedule')
@api_auth_required
def tou_schedule(api_user=None, **kwargs):
    """Get the rolling 24-hour tariff schedule that will be sent to Tesla

    Supports both session login and Bearer token authentication.
    """
    user = api_user or current_user
    # Check for cache bypass (used after settings changes)
    bypass_cache = request.args.get('refresh') == '1'

    if not bypass_cache:
        # Try to get from cache
        cache_key = f'tou_schedule_{user.id}'
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.debug(f"TOU schedule served from cache for user: {user.email}")
            return cached_result

    logger.info(f"TOU tariff schedule requested by user: {user.email} (bypass_cache={bypass_cache})")

    # Determine price source based on user settings
    use_aemo = (
        user.electricity_provider == 'flow_power' and
        user.flow_power_price_source == 'aemo'
    )

    actual_interval = None
    forecast_30min = None

    if use_aemo:
        # Use AEMO data for Flow Power AEMO-only mode
        aemo_region = user.flow_power_state
        if not aemo_region:
            logger.error("AEMO price source selected but no region configured")
            return jsonify({'error': 'AEMO region not configured. Please set your Flow Power state in settings.'}), 400

        logger.info(f"TOU Schedule - Using AEMO price source for region: {aemo_region}")
        aemo_client = AEMOAPIClient()
        # Request 96 periods (48 hours) to ensure coverage for rolling 24h window
        # AEMO pre-dispatch provides ~40 hours of forecast, so 96 ensures full coverage
        forecast_30min = aemo_client.get_price_forecast(aemo_region, periods=96)
        if not forecast_30min:
            logger.error(f"Failed to fetch AEMO price forecast for {aemo_region}")
            return jsonify({'error': 'Failed to fetch AEMO price forecast'}), 500

        logger.info(f"Using AEMO forecast for TOU schedule: {len(forecast_30min)} intervals")
    else:
        # Use Amber API (default)
        amber_client = get_amber_client(user)
        if not amber_client:
            logger.warning("Amber client not available for tariff schedule")
            return jsonify({'error': 'Amber API not configured'}), 400

        # Step 1: Get current interval prices from WebSocket (real-time) with REST API fallback
        # This ensures we have the most up-to-date pricing for the current period
        from flask import current_app
        ws_client = current_app.config.get('AMBER_WEBSOCKET_CLIENT')

        # Get live prices (WebSocket first, REST API fallback)
        current_prices = amber_client.get_live_prices(ws_client=ws_client)

        # Convert to actual_interval format for tariff converter
        if current_prices:
            actual_interval = {'general': None, 'feedIn': None}
            for price in current_prices:
                channel = price.get('channelType')
                if channel in ['general', 'feedIn']:
                    actual_interval[channel] = price

            logger.info(f"TOU Schedule - Live prices from WebSocket: general={actual_interval.get('general', {}).get('perKwh')}¢/kWh, feedIn={actual_interval.get('feedIn', {}).get('perKwh')}¢/kWh")
        else:
            logger.warning("TOU Schedule - No live price data available from WebSocket or REST API")

        # Step 2: Fetch full 48-hour forecast with 30-min resolution for TOU schedule building
        # (The Amber API doesn't provide 48 hours of 5-min data, so we must use 30-min for full schedule)
        forecast_30min = amber_client.get_price_forecast(next_hours=48, resolution=30)
        if not forecast_30min:
            logger.error("Failed to fetch 48-hour forecast for TOU schedule")
            return jsonify({'error': 'Failed to fetch price forecast'}), 500

        logger.info(f"Using 30-min forecast for TOU schedule: {len(forecast_30min)} intervals")

    # Debug logging to compare with live price display
    if actual_interval:
        general_price = actual_interval.get('general', {}).get('perKwh')
        feedin_price = actual_interval.get('feedIn', {}).get('perKwh')
        logger.info(f"TOU Schedule - ActualInterval extracted: general={general_price}¢/kWh, feedIn={feedin_price}¢/kWh")
    else:
        logger.warning("TOU Schedule - No ActualInterval found in forecast data")

    # Fetch Powerwall timezone from Tesla API (most accurate)
    # This ensures correct timezone handling for TOU schedule alignment
    powerwall_timezone = None
    tesla_client = get_tesla_client(user)
    if tesla_client and user.tesla_energy_site_id:
        site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
        if site_info:
            powerwall_timezone = site_info.get('installation_time_zone')
            if powerwall_timezone:
                logger.info(f"Using Powerwall timezone from Tesla API: {powerwall_timezone}")
            else:
                logger.warning("No installation_time_zone in site_info, will auto-detect from Amber data")
        else:
            logger.warning("Failed to fetch site_info from Tesla API, will auto-detect timezone from Amber data")
    else:
        logger.warning("Tesla API not configured, will auto-detect timezone from Amber data")

    # Convert to Tesla tariff format using 30-min forecast data
    # The actual_interval (from 5-min data) will be injected for the current period only
    from app.tariff_converter import AmberTariffConverter, apply_flow_power_export, apply_network_tariff, apply_flow_power_pea, get_wholesale_lookup
    converter = AmberTariffConverter()
    tariff = converter.convert_amber_to_tesla_tariff(
        forecast_30min,
        user=user,
        powerwall_timezone=powerwall_timezone,
        current_actual_interval=actual_interval
    )

    if not tariff:
        logger.error("Failed to convert tariff")
        return jsonify({'error': 'Failed to convert tariff'}), 500

    # Apply Flow Power PEA pricing (works with both AEMO and Amber price sources)
    if user.electricity_provider == 'flow_power':
        # Check if PEA (Price Efficiency Adjustment) is enabled
        pea_enabled = getattr(user, 'pea_enabled', True)  # Default True for Flow Power

        if pea_enabled:
            # Use Flow Power PEA pricing model: Base Rate + PEA
            # Works with both AEMO (raw wholesale) and Amber (wholesaleKWHPrice forecast)
            base_rate = getattr(user, 'flow_power_base_rate', 34.0) or 34.0
            custom_pea = getattr(user, 'pea_custom_value', None)
            wholesale_prices = get_wholesale_lookup(forecast_30min)
            price_source = user.flow_power_price_source or 'amber'
            logger.info(f"Applying Flow Power PEA ({price_source}): base_rate={base_rate}c, custom_pea={custom_pea}")
            tariff = apply_flow_power_pea(tariff, wholesale_prices, base_rate, custom_pea)
        elif user.flow_power_price_source == 'aemo':
            # PEA disabled + AEMO: fall back to network tariff calculation
            # (Amber prices already include network fees, no fallback needed)
            logger.info("Applying network tariff to AEMO wholesale prices (PEA disabled)")
            tariff = apply_network_tariff(tariff, user)

    # Apply Flow Power export rates if user is on Flow Power (for preview display)
    if user.electricity_provider == 'flow_power' and user.flow_power_state:
        logger.info(f"Preview: Applying Flow Power export rates for state: {user.flow_power_state}")
        tariff = apply_flow_power_export(tariff, user.flow_power_state)

    # Apply export price boost for Amber users (if enabled)
    if user.electricity_provider == 'amber' and getattr(user, 'export_boost_enabled', False):
        from app.tariff_converter import apply_export_boost
        offset = getattr(user, 'export_price_offset', 0) or 0
        min_price = getattr(user, 'export_min_price', 0) or 0
        boost_start = getattr(user, 'export_boost_start', '17:00') or '17:00'
        boost_end = getattr(user, 'export_boost_end', '21:00') or '21:00'
        threshold = getattr(user, 'export_boost_threshold', 0) or 0
        logger.info(f"Preview: Applying export boost: offset={offset}c, min={min_price}c, threshold={threshold}c, window={boost_start}-{boost_end}")
        tariff = apply_export_boost(tariff, offset, min_price, boost_start, boost_end, threshold)

    # Apply Chip Mode for Amber users (if enabled) - suppress exports unless above threshold
    if user.electricity_provider == 'amber' and getattr(user, 'chip_mode_enabled', False):
        from app.tariff_converter import apply_chip_mode
        chip_start = getattr(user, 'chip_mode_start', '22:00') or '22:00'
        chip_end = getattr(user, 'chip_mode_end', '06:00') or '06:00'
        chip_threshold = getattr(user, 'chip_mode_threshold', 30.0) or 30.0
        logger.info(f"Preview: Applying Chip Mode: window={chip_start}-{chip_end}, threshold={chip_threshold}c")
        tariff = apply_chip_mode(tariff, chip_start, chip_end, chip_threshold)

    # Extract tariff periods for display
    energy_rates = tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {})
    feedin_rates = tariff.get('sell_tariff', {}).get('energy_charges', {}).get('Summer', {}).get('rates', {})

    # Get current time in user's timezone to mark current period
    from datetime import datetime
    from zoneinfo import ZoneInfo
    user_tz = ZoneInfo(get_powerwall_timezone(user))
    now = datetime.now(user_tz)
    current_hour = now.hour
    current_minute_bucket = 0 if now.minute < 30 else 30

    # Get Chip Mode settings for display indication
    chip_mode_enabled = user.electricity_provider == 'amber' and getattr(user, 'chip_mode_enabled', False)
    chip_start = getattr(user, 'chip_mode_start', '22:00') or '22:00'
    chip_end = getattr(user, 'chip_mode_end', '06:00') or '06:00'
    chip_threshold = getattr(user, 'chip_mode_threshold', 30.0) or 30.0

    # Get Export Boost settings for display indication
    export_boost_enabled = user.electricity_provider == 'amber' and getattr(user, 'export_boost_enabled', False)
    boost_start = getattr(user, 'export_boost_start', '17:00') or '17:00'
    boost_end = getattr(user, 'export_boost_end', '21:00') or '21:00'
    boost_threshold = getattr(user, 'export_boost_threshold', 0) or 0

    def is_in_time_window(hour: int, minute: int, start_time: str, end_time: str) -> bool:
        """Check if a time falls within a time window"""
        try:
            start_h, start_m = map(int, start_time.split(':'))
            end_h, end_m = map(int, end_time.split(':'))
            start_mins = start_h * 60 + start_m
            end_mins = end_h * 60 + end_m
            current_mins = hour * 60 + minute

            if start_mins <= end_mins:
                # Same day window (e.g., 09:00 to 17:00)
                return start_mins <= current_mins < end_mins
            else:
                # Overnight window (e.g., 22:00 to 06:00)
                return current_mins >= start_mins or current_mins < end_mins
        except (ValueError, AttributeError):
            return False

    def is_in_chip_window(hour: int, minute: int) -> bool:
        """Check if a time falls within the chip mode window"""
        if not chip_mode_enabled:
            return False
        return is_in_time_window(hour, minute, chip_start, chip_end)

    def is_in_boost_window(hour: int, minute: int) -> bool:
        """Check if a time falls within the export boost window"""
        if not export_boost_enabled:
            return False
        return is_in_time_window(hour, minute, boost_start, boost_end)

    # Build periods for display
    periods = []
    for hour in range(24):
        for minute in [0, 30]:
            period_key = f"PERIOD_{hour:02d}_{minute:02d}"
            if period_key in energy_rates:
                # Check if this is the current period
                is_current = (hour == current_hour and minute == current_minute_bucket)

                # Check if current period is using ActualInterval pricing
                uses_actual_interval = is_current and actual_interval is not None

                # Check if chip mode is suppressing exports for this period
                sell_price_cents = feedin_rates.get(period_key, 0) * 100
                chip_mode_suppressed = is_in_chip_window(hour, minute) and sell_price_cents < 1.0

                # Check if export boost is active for this period
                # Export boost is active when in the boost window and sell price has been boosted
                export_boost_active = is_in_boost_window(hour, minute) and sell_price_cents >= boost_threshold

                periods.append({
                    'time': f"{hour:02d}:{minute:02d}",
                    'hour': hour,
                    'minute': minute,
                    'buy_price': energy_rates[period_key] * 100,  # Convert back to cents
                    'sell_price': sell_price_cents,
                    'is_current': is_current,
                    'uses_actual_interval': uses_actual_interval,
                    'chip_mode_suppressed': chip_mode_suppressed,
                    'export_boost_active': export_boost_active
                })

    # Calculate stats
    buy_prices = [p['buy_price'] for p in periods if p['buy_price'] > 0]
    sell_prices = [p['sell_price'] for p in periods if p['sell_price'] > 0]

    stats = {
        'buy': {
            'min': min(buy_prices) if buy_prices else 0,
            'max': max(buy_prices) if buy_prices else 0,
            'avg': sum(buy_prices) / len(buy_prices) if buy_prices else 0
        },
        'sell': {
            'min': min(sell_prices) if sell_prices else 0,
            'max': max(sell_prices) if sell_prices else 0,
            'avg': sum(sell_prices) / len(sell_prices) if sell_prices else 0
        },
        'total_periods': len(periods)
    }

    logger.info(f"Generated tariff schedule with {len(periods)} periods")

    result = jsonify({
        'periods': periods,
        'stats': stats,
        'tariff_name': tariff.get('name', 'Unknown')
    })

    # Cache the result for 5 minutes (unless bypassed)
    cache_key = f'tou_schedule_{user.id}'
    cache.set(cache_key, result, timeout=300)

    return result


@bp.route('/api/sync-tesla-schedule', methods=['POST'])
@login_required
@require_tesla_client
@require_tesla_site_id
def sync_tesla_schedule(tesla_client, api_user=None):
    """Apply the TOU schedule to Tesla Powerwall"""
    logger.info(f"Tesla schedule sync requested by user: {current_user.email}")

    site_id = current_user.tesla_energy_site_id

    try:
        # Determine price source based on user settings
        use_aemo = (
            current_user.electricity_provider == 'flow_power' and
            current_user.flow_power_price_source == 'aemo'
        )

        if use_aemo:
            # Use AEMO data for import prices (Flow Power AEMO-only mode)
            aemo_region = current_user.flow_power_state
            if not aemo_region:
                logger.error("AEMO price source selected but no region configured")
                return jsonify({'error': 'AEMO region not configured. Please set your Flow Power state in settings.'}), 400

            logger.info(f"Using AEMO price source for region: {aemo_region}")
            aemo_client = AEMOAPIClient()
            # Request 96 periods (48 hours) to ensure coverage for rolling 24h window
            forecast = aemo_client.get_price_forecast(aemo_region, periods=96)
            if not forecast:
                logger.error(f"Failed to fetch AEMO price forecast for {aemo_region}")
                return jsonify({'error': 'Failed to fetch AEMO price forecast'}), 500
        else:
            # Use Amber API (default)
            amber_client = get_amber_client(current_user)
            if not amber_client:
                logger.error("Amber client not available")
                return jsonify({'error': 'Amber API not configured. Please set up Amber or use AEMO price source.'}), 400

            # Get price forecast (48 hours for better coverage)
            # Request 30-minute resolution - Amber pre-averages 5-min intervals for us
            forecast = amber_client.get_price_forecast(next_hours=48, resolution=30)
            if not forecast:
                logger.error("Failed to fetch price forecast for sync")
                return jsonify({'error': 'Failed to fetch price forecast'}), 500

        # Fetch Powerwall timezone from Tesla API (most accurate)
        # This ensures correct timezone handling for TOU schedule alignment
        powerwall_timezone = None
        site_info = tesla_client.get_site_info(site_id)
        if site_info:
            powerwall_timezone = site_info.get('installation_time_zone')
            if powerwall_timezone:
                logger.info(f"Using Powerwall timezone from Tesla API: {powerwall_timezone}")
            else:
                logger.warning("No installation_time_zone in site_info, will auto-detect from Amber data")
        else:
            logger.warning("Failed to fetch site_info from Tesla API, will auto-detect timezone from Amber data")

        # Convert Amber prices to Tesla tariff format
        from app.tariff_converter import AmberTariffConverter, apply_flow_power_export, apply_network_tariff, apply_flow_power_pea, get_wholesale_lookup
        converter = AmberTariffConverter()
        tariff = converter.convert_amber_to_tesla_tariff(
            forecast,
            user=current_user,
            powerwall_timezone=powerwall_timezone
        )

        if not tariff:
            logger.error("Failed to convert tariff")
            return jsonify({'error': 'Failed to convert Amber prices to Tesla tariff format'}), 500

        # Apply Flow Power PEA pricing (works with both AEMO and Amber price sources)
        if current_user.electricity_provider == 'flow_power':
            # Check if PEA (Price Efficiency Adjustment) is enabled
            pea_enabled = getattr(current_user, 'pea_enabled', True)  # Default True for Flow Power

            if pea_enabled:
                # Use Flow Power PEA pricing model: Base Rate + PEA
                # Works with both AEMO (raw wholesale) and Amber (wholesaleKWHPrice forecast)
                base_rate = getattr(current_user, 'flow_power_base_rate', 34.0) or 34.0
                custom_pea = getattr(current_user, 'pea_custom_value', None)
                wholesale_prices = get_wholesale_lookup(forecast)
                price_source = current_user.flow_power_price_source or 'amber'
                logger.info(f"Applying Flow Power PEA ({price_source}): base_rate={base_rate}c, custom_pea={custom_pea}")
                tariff = apply_flow_power_pea(tariff, wholesale_prices, base_rate, custom_pea)
            elif use_aemo:
                # PEA disabled + AEMO: fall back to network tariff calculation
                # (Amber prices already include network fees, no fallback needed)
                logger.info("Applying network tariff to AEMO wholesale prices (PEA disabled)")
                tariff = apply_network_tariff(tariff, current_user)

        # Apply Flow Power export rates if user is on Flow Power
        if current_user.electricity_provider == 'flow_power' and current_user.flow_power_state:
            logger.info(f"Applying Flow Power export rates for state: {current_user.flow_power_state}")
            tariff = apply_flow_power_export(tariff, current_user.flow_power_state)

        # Apply export price boost for Amber users (if enabled)
        if current_user.electricity_provider == 'amber' and getattr(current_user, 'export_boost_enabled', False):
            from app.tariff_converter import apply_export_boost
            offset = getattr(current_user, 'export_price_offset', 0) or 0
            min_price = getattr(current_user, 'export_min_price', 0) or 0
            boost_start = getattr(current_user, 'export_boost_start', '17:00') or '17:00'
            boost_end = getattr(current_user, 'export_boost_end', '21:00') or '21:00'
            threshold = getattr(current_user, 'export_boost_threshold', 0) or 0
            logger.info(f"Applying export boost: offset={offset}c, min={min_price}c, threshold={threshold}c, window={boost_start}-{boost_end}")
            tariff = apply_export_boost(tariff, offset, min_price, boost_start, boost_end, threshold)

        # Apply Chip Mode for Amber users (if enabled) - suppress exports unless above threshold
        if current_user.electricity_provider == 'amber' and getattr(current_user, 'chip_mode_enabled', False):
            from app.tariff_converter import apply_chip_mode
            chip_start = getattr(current_user, 'chip_mode_start', '22:00') or '22:00'
            chip_end = getattr(current_user, 'chip_mode_end', '06:00') or '06:00'
            chip_threshold = getattr(current_user, 'chip_mode_threshold', 30.0) or 30.0
            logger.info(f"Applying Chip Mode: window={chip_start}-{chip_end}, threshold={chip_threshold}c")
            tariff = apply_chip_mode(tariff, chip_start, chip_end, chip_threshold)

        num_periods = len(tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {}))
        logger.info(f"Applying POWER SYNC tariff with {num_periods} rate periods")

        # Apply tariff to Tesla
        result = tesla_client.set_tariff_rate(site_id, tariff)

        if not result:
            logger.error("Failed to apply tariff to Tesla")
            return jsonify({'error': 'Failed to apply tariff to Tesla Powerwall'}), 500

        logger.info("Successfully synced Amber tariff to Tesla Powerwall")

        return jsonify({
            'success': True,
            'message': 'POWER SYNC tariff applied successfully',
            'rate_periods': num_periods,
            'tariff_name': tariff.get('name', 'Unknown')
        })

    except Exception as e:
        logger.error(f"Error syncing schedule to Tesla: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': f'Error syncing schedule: {str(e)}'}), 500


@bp.route('/api/toggle-sync', methods=['POST'])
@login_required
def toggle_sync():
    """Toggle automatic Tesla syncing on/off"""
    try:
        # Toggle the sync_enabled flag
        current_user.sync_enabled = not current_user.sync_enabled
        db.session.commit()

        status = "enabled" if current_user.sync_enabled else "disabled"
        logger.info(f"User {current_user.email} {status} automatic Tesla syncing")

        return jsonify({
            'success': True,
            'sync_enabled': current_user.sync_enabled,
            'message': f'Automatic syncing {status}'
        })

    except Exception as e:
        logger.error(f"Error toggling sync: {e}")
        db.session.rollback()
        return jsonify({'error': f'Error toggling sync: {str(e)}'}), 500


# API Testing Routes

@bp.route('/api-testing')
@login_required
def api_testing():
    """API Testing interface page"""
    logger.info(f"API Testing page accessed by user: {current_user.email}")
    return render_template('api_testing.html', title='API Testing')


@bp.route('/api/test/amber/sites')
@login_required
def test_amber_sites():
    """Test GET /sites endpoint"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        sites = amber_client.get_sites()
        return jsonify({
            'success': True,
            'endpoint': 'GET /v1/sites',
            'data': sites
        })
    except Exception as e:
        logger.error(f"Error testing sites endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/amber/current-prices')
@login_required
def test_amber_current_prices():
    """Test GET /sites/{site_id}/prices/current endpoint"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        site_id = request.args.get('site_id')
        prices = amber_client.get_current_prices(site_id)

        return jsonify({
            'success': True,
            'endpoint': f'GET /v1/sites/{site_id or "auto"}/prices/current',
            'data': prices
        })
    except Exception as e:
        logger.error(f"Error testing current prices endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/amber/price-forecast')
@login_required
def test_amber_price_forecast():
    """Test GET /sites/{site_id}/prices endpoint with various parameters"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        site_id = request.args.get('site_id')
        next_hours = int(request.args.get('next_hours', 24))
        resolution = request.args.get('resolution')  # 5 or 30

        if resolution:
            resolution = int(resolution)

        forecast = amber_client.get_price_forecast(
            site_id=site_id,
            next_hours=next_hours,
            resolution=resolution
        )

        endpoint = f'GET /v1/sites/{site_id or "auto"}/prices?next_hours={next_hours}'
        if resolution:
            endpoint += f'&resolution={resolution}'

        return jsonify({
            'success': True,
            'endpoint': endpoint,
            'data': forecast,
            'count': len(forecast) if forecast else 0
        })
    except Exception as e:
        logger.error(f"Error testing price forecast endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/amber/usage')
@login_required
def test_amber_usage():
    """Test GET /sites/{site_id}/usage endpoint"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        site_id = request.args.get('site_id')
        usage = amber_client.get_usage(site_id=site_id)

        return jsonify({
            'success': True,
            'endpoint': f'GET /v1/sites/{site_id or "auto"}/usage',
            'data': usage,
            'count': len(usage) if usage else 0
        })
    except Exception as e:
        logger.error(f"Error testing usage endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/amber/raw', methods=['GET', 'POST'])
@login_required
def test_amber_raw():
    """Test raw API call to any Amber endpoint"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        if request.method == 'POST':
            data = request.get_json()
            endpoint = data.get('endpoint', '/sites')
            method = data.get('method', 'GET')
            params = data.get('params', {})
            json_data = data.get('json_data', None)
        else:
            endpoint = request.args.get('endpoint', '/sites')
            method = request.args.get('method', 'GET')
            params = {}
            json_data = None

        success, response_data, status_code = amber_client.raw_api_call(
            endpoint=endpoint,
            method=method,
            params=params,
            json_data=json_data
        )

        return jsonify({
            'success': success,
            'endpoint': f'{method} /v1{endpoint}',
            'status_code': status_code,
            'data': response_data
        })
    except Exception as e:
        logger.error(f"Error testing raw endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/amber/advanced-price-schema')
@login_required
def test_amber_advanced_price_schema():
    """Test advanced price schema with 30-minute resolution forecast"""
    try:
        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        # Get 48-hour forecast with 30-minute resolution to see advanced price structure
        forecast = amber_client.get_price_forecast(
            site_id=None,
            next_hours=48,
            resolution=30
        )

        if not forecast:
            return jsonify({'error': 'Failed to fetch forecast data'}), 500

        # Extract a sample record to highlight the structure
        sample = None
        if isinstance(forecast, list) and len(forecast) > 0:
            sample_raw = forecast[0]
            # Parse the sample to show key fields
            sample = {
                'period': sample_raw.get('period'),
                'channelType': sample_raw.get('channelType'),
                'spikeStatus': sample_raw.get('spikeStatus'),
                'perKwh': sample_raw.get('perKwh'),
                'spotPerKwh': sample_raw.get('spotPerKwh'),
                'advancedPrice': sample_raw.get('advancedPrice', {})
            }

        return jsonify({
            'success': True,
            'endpoint': 'GET /v1/sites/{site_id}/prices?next_hours=48&resolution=30',
            'data': forecast,
            'count': len(forecast) if forecast else 0,
            'sample': sample,
            'description': 'This shows the advanced price structure including ML predictions used by SmartShift'
        })
    except Exception as e:
        logger.error(f"Error testing advanced price schema: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/tariff-comparison')
@login_required
def test_tariff_comparison():
    """Compare different tariff implementations to debug price differences"""
    try:
        from app.tariff_converter import AmberTariffConverter

        amber_client = get_amber_client(current_user)
        if not amber_client:
            return jsonify({'error': 'Amber API client not configured'}), 400

        # Get 48-hour forecast
        forecast = amber_client.get_price_forecast(
            site_id=None,
            next_hours=48,
            resolution=30
        )

        if not forecast:
            return jsonify({'error': 'Failed to fetch forecast data'}), 500

        # Build actual tariff using current implementation
        converter = AmberTariffConverter()
        actual_tariff = converter.convert_amber_to_tesla_tariff(forecast, user=current_user)

        # Extract first 5 periods from buy prices for comparison
        actual_periods = {}
        if actual_tariff and 'energy_charges' in actual_tariff:
            summer_rates = actual_tariff['energy_charges'].get('Summer', {}).get('rates', {})
            # Get first 5 periods for debugging
            for i, (period, price) in enumerate(list(summer_rates.items())[:5]):
                actual_periods[period] = price

        # Build a "no-shift" version for comparison
        no_shift_periods = {}
        from datetime import datetime
        now = datetime.now()

        # Parse forecast to show what "no shift" would look like
        general_lookup = {}
        for point in forecast:
            try:
                nem_time = point.get('nemTime', '')
                timestamp = datetime.fromisoformat(nem_time.replace('Z', '+00:00'))
                channel_type = point.get('channelType', '')

                if channel_type == 'general':
                    # Get price (same logic as tariff converter)
                    advanced_price = point.get('advancedPrice')
                    if advanced_price and isinstance(advanced_price, dict):
                        if 'predicted' in advanced_price:
                            predicted = advanced_price['predicted']
                            # Check if predicted is a number or object
                            if isinstance(predicted, dict):
                                per_kwh_cents = predicted.get('perKwh', 0)
                            else:
                                per_kwh_cents = predicted
                        else:
                            per_kwh_cents = point.get('perKwh', 0)
                    else:
                        per_kwh_cents = point.get('perKwh', 0)

                    per_kwh_dollars = per_kwh_cents / 100

                    # Round to 30-min bucket
                    minute_bucket = 0 if timestamp.minute < 30 else 30
                    hour = timestamp.hour

                    period_key = f"PERIOD_{hour:02d}_{minute_bucket:02d}"

                    # NO SHIFT - use current slot's price
                    if period_key not in general_lookup:
                        general_lookup[period_key] = []
                    general_lookup[period_key].append(per_kwh_dollars)

            except Exception as e:
                logger.error(f"Error processing: {e}")
                continue

        # Average prices for no-shift version
        for period, prices in list(general_lookup.items())[:5]:
            no_shift_periods[period] = sum(prices) / len(prices)

        # Get raw API data for first few periods
        raw_samples = []
        for i, point in enumerate(forecast[:10]):
            if point.get('channelType') == 'general':
                raw_samples.append({
                    'nemTime': point.get('nemTime'),
                    'type': point.get('type'),
                    'perKwh': point.get('perKwh'),
                    'advancedPrice': point.get('advancedPrice')
                })

        return jsonify({
            'success': True,
            'current_time': now.isoformat(),
            'implementation_notes': {
                'actual': 'Current implementation with 30-min shift',
                'no_shift': 'Hypothetical - no shift applied',
                'difference': 'Shows how 30-min advance notice affects prices'
            },
            'comparison': {
                'actual_tariff_first_5_periods': actual_periods,
                'no_shift_first_5_periods': no_shift_periods
            },
            'raw_forecast_samples': raw_samples,
            'advancedPrice_structure': 'Check if predicted is a number or object with perKwh field'
        })
    except Exception as e:
        logger.error(f"Error in tariff comparison: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@bp.route('/api/test/find-tesla-sites')
@login_required
@require_tesla_client
def test_find_tesla_sites(tesla_client, api_user=None):
    """Helper to find Tesla energy site IDs"""
    try:
        # Get all energy sites
        sites = tesla_client.get_energy_sites()

        if not sites:
            return jsonify({
                'error': 'No energy sites found',
                'help': 'Make sure your Teslemetry API key is correct and you have a Powerwall registered'
            }), 404

        # Format site information
        site_info = []
        for site in sites:
            site_info.append({
                'site_id': site.get('energy_site_id'),
                'site_name': site.get('site_name', 'Unnamed Site'),
                'resource_type': site.get('resource_type', 'unknown')
            })

        return jsonify({
            'success': True,
            'sites': site_info,
            'instructions': 'Copy the site_id value and paste it into Settings > Tesla Site ID'
        })

    except Exception as e:
        logger.error(f"Error finding Tesla sites: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# Teslemetry Routes
@bp.route('/teslemetry/disconnect', methods=['POST'])
@login_required
def teslemetry_disconnect():
    """Disconnect Teslemetry"""
    try:
        logger.info(f"Teslemetry disconnect requested by user: {current_user.email}")

        # Clear Teslemetry API key
        current_user.teslemetry_api_key_encrypted = None

        db.session.commit()

        logger.info(f"Teslemetry API key cleared for user: {current_user.email}")
        flash('Teslemetry disconnected successfully')
        return redirect(url_for('main.dashboard'))

    except Exception as e:
        logger.error(f"Error disconnecting Teslemetry: {e}")
        flash('Error disconnecting Teslemetry. Please try again.')
        return redirect(url_for('main.dashboard'))


# ============================================
# LOGS API
# ============================================

def get_log_file_path():
    """Get the path to the persistent log file"""
    # Use same logic as __init__.py for consistency
    log_dir = os.environ.get('LOG_DIR', '/app/data/logs')
    if not os.path.exists(log_dir):
        # Fallback to local directory
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'logs')
    return os.path.join(log_dir, 'flask.log')


@bp.route('/api/logs')
@login_required
def get_logs():
    """
    Fetch application logs with optional filtering by log level
    Query params:
        - level: Filter by log level (DEBUG, INFO, WARNING, ERROR) - can specify multiple comma-separated
        - limit: Maximum number of log lines to return (default: 1000)
    """
    try:
        # Get query parameters
        levels_param = request.args.get('level', '')
        limit = int(request.args.get('limit', 1000))

        # Parse levels filter
        if levels_param:
            requested_levels = [level.strip().upper() for level in levels_param.split(',')]
        else:
            requested_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

        # Read log file from persistent location
        log_file_path = get_log_file_path()

        if not os.path.exists(log_file_path):
            return jsonify({
                'success': False,
                'error': 'Log file not found'
            }), 404

        logs = []
        with open(log_file_path, 'r') as f:
            # Read all lines (most recent last)
            all_lines = f.readlines()

            # Process lines in reverse to get most recent first
            for line in reversed(all_lines):
                if len(logs) >= limit:
                    break

                # Parse log line format: "2025-11-12 18:16:31 [INFO] app: Creating Flask application"
                line = line.strip()
                if not line:
                    continue

                # Extract log level from line
                log_level = None
                for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                    if f'[{level}]' in line:
                        log_level = level
                        break

                # Filter by level if specified
                if log_level and log_level in requested_levels:
                    logs.append({
                        'line': line,
                        'level': log_level
                    })

        return jsonify({
            'success': True,
            'logs': logs,
            'total': len(logs),
            'filters': {
                'levels': requested_levels,
                'limit': limit
            }
        })

    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/logs/download')
@login_required
def download_logs():
    """Download the complete log file"""
    try:
        from flask import send_file
        log_file_path = get_log_file_path()

        if not os.path.exists(log_file_path):
            flash('Log file not found')
            return redirect(url_for('main.settings'))

        return send_file(
            log_file_path,
            as_attachment=True,
            download_name=f'tesla-amber-sync-logs-{datetime.now().strftime("%Y%m%d-%H%M%S")}.log',
            mimetype='text/plain'
        )

    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        flash(f'Error downloading logs: {str(e)}')
        return redirect(url_for('main.settings'))


# ============================================
# AEMO SPIKE DETECTION TESTING
# ============================================

@bp.route('/test-aemo-spike', methods=['POST'])
@login_required
@require_tesla_client
@require_tesla_site_id
def test_aemo_spike(tesla_client, api_user=None):
    """Test/simulate AEMO price spike mode"""
    from app.tasks import create_spike_tariff
    from app.models import SavedTOUProfile
    import json

    try:
        logger.info(f"AEMO spike simulation requested by user: {current_user.email}")

        # Validate configuration
        if not current_user.aemo_spike_detection_enabled:
            flash('AEMO spike detection is not enabled. Please enable it in settings first.')
            return redirect(url_for('main.settings'))

        if not current_user.aemo_region:
            flash('AEMO region not configured. Please set it in settings first.')
            return redirect(url_for('main.settings'))

        # Use the user's configured spike threshold for simulation
        simulated_price = current_user.aemo_spike_threshold or 300.0
        logger.info(f"Simulating spike with user's threshold: ${simulated_price}/MWh for user {current_user.email}")

        # Check if battery is already exporting - if so, don't interfere
        logger.info(f"Checking battery status to avoid disrupting existing export for {current_user.email}")
        site_status = tesla_client.get_site_status(current_user.tesla_energy_site_id)

        if site_status:
            solar_power = site_status.get('solar_power', 0.0)
            battery_power = site_status.get('battery_power', 0.0)
            load_power = site_status.get('load_power', 0.0)
            grid_power = site_status.get('grid_power', 0.0)

            logger.info(f"Current power flow: Solar={solar_power}W, Battery={battery_power}W, Load={load_power}W, Grid={grid_power}W")

            # Check if BATTERY is exporting to grid (not just solar)
            # Battery exports when: battery_power > (load - solar)
            # This accounts for solar already covering some/all of the load
            net_load_after_solar = max(0, load_power - solar_power)
            battery_export = battery_power - net_load_after_solar

            logger.info(f"Net load after solar: {net_load_after_solar}W, Battery export: {battery_export}W")

            # If battery is already exporting >100W to grid, skip spike tariff upload
            if battery_export > 100:
                logger.info(f"⚡ Battery already exporting {battery_export}W to grid - skipping spike tariff upload to avoid disruption")
                flash(f'⚡ Battery already exporting {battery_export/1000:.2f} kW to grid. Skipping spike tariff upload to avoid disrupting optimal operation.')

                # Still mark as in spike mode for tracking
                if not current_user.aemo_in_spike_mode:
                    current_user.aemo_in_spike_mode = True
                    current_user.aemo_spike_test_mode = True  # Prevent automatic restore during manual test
                    current_user.aemo_spike_start_time = datetime.utcnow()
                    current_user.aemo_last_price = simulated_price
                    current_user.aemo_last_check = datetime.utcnow()
                    db.session.commit()

                return redirect(url_for('main.settings'))

        # Check for default tariff or save current tariff as backup (if not already in spike mode)
        if not current_user.aemo_in_spike_mode:
            # First check if a default tariff already exists
            default_profile = SavedTOUProfile.query.filter_by(
                user_id=current_user.id,
                is_default=True
            ).first()

            if default_profile:
                # Use existing default tariff as backup reference
                current_user.aemo_saved_tariff_id = default_profile.id
                logger.info(f"✅ Using existing default tariff ID {default_profile.id} ({default_profile.name}) as backup reference")
            else:
                # No default exists - save current tariff and mark as default
                logger.info(f"No default tariff found - saving current Tesla tariff as default for {current_user.email}")
                current_tariff = tesla_client.get_current_tariff(current_user.tesla_energy_site_id)

                if current_tariff:
                    backup_profile = SavedTOUProfile(
                        user_id=current_user.id,
                        name=f"Default Tariff (Saved {datetime.utcnow().strftime('%Y-%m-%d %H:%M')})",
                        description=f"Automatically saved as default before first spike test at ${simulated_price}/MWh",
                        source_type='tesla',
                        tariff_name=current_tariff.get('name', 'Unknown'),
                        utility=current_tariff.get('utility', 'Unknown'),
                        tariff_json=json.dumps(current_tariff),
                        created_at=datetime.utcnow(),
                        fetched_from_tesla_at=datetime.utcnow(),
                        is_default=True  # Mark as default
                    )
                    db.session.add(backup_profile)
                    db.session.flush()
                    current_user.aemo_saved_tariff_id = backup_profile.id
                    logger.info(f"✅ Saved current tariff as default with ID {backup_profile.id}")
                else:
                    flash('Failed to fetch current tariff for backup. Cannot enter spike mode.')
                    return redirect(url_for('main.settings'))

        # Create and upload spike tariff
        logger.info(f"Creating test spike tariff with price ${simulated_price}/MWh")
        spike_tariff = create_spike_tariff(simulated_price)

        result = tesla_client.set_tariff_rate(current_user.tesla_energy_site_id, spike_tariff)

        if result:
            current_user.aemo_in_spike_mode = True
            current_user.aemo_spike_test_mode = True  # Prevent automatic restore during manual test
            current_user.aemo_spike_start_time = datetime.utcnow()
            current_user.aemo_last_price = simulated_price
            current_user.aemo_last_check = datetime.utcnow()
            db.session.commit()

            logger.info(f"✅ Successfully entered test spike mode for {current_user.email}")

            # Force Powerwall to immediately apply the spike tariff
            from app.tasks import force_tariff_refresh
            logger.info(f"Forcing Powerwall to apply spike tariff for {current_user.email}")
            force_tariff_refresh(tesla_client, current_user.tesla_energy_site_id)

            flash(f'🚨 Spike mode activated! Simulated ${simulated_price}/MWh spike. High sell-rate tariff uploaded to Tesla.')
        else:
            logger.error(f"Failed to upload spike tariff for {current_user.email}")
            flash('Error uploading spike tariff to Tesla. Please check logs.')

        return redirect(url_for('main.settings'))

    except Exception as e:
        logger.error(f"Error in AEMO spike simulation: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash('Error simulating spike. Please check logs.')
        db.session.rollback()
        return redirect(url_for('main.settings'))



@bp.route('/test-aemo-restore', methods=['POST'])
@login_required
def test_aemo_restore():
    """Test/simulate restoring from AEMO spike mode (async)"""
    from app.models import SavedTOUProfile
    import json
    import threading

    try:
        logger.info(f"AEMO restore simulation requested by user: {current_user.email}")

        if not current_user.aemo_in_spike_mode:
            flash('Not currently in spike mode. Nothing to restore.')
            return redirect(url_for('main.settings'))

        # Get Tesla client
        tesla_client = get_tesla_client(current_user)
        if not tesla_client:
            flash('Tesla API not configured.')
            return redirect(url_for('main.settings'))

        # Restore saved tariff
        if current_user.aemo_saved_tariff_id:
            logger.info(f"Restoring backup tariff ID {current_user.aemo_saved_tariff_id} for {current_user.email}")
            backup_profile = SavedTOUProfile.query.get(current_user.aemo_saved_tariff_id)

            if backup_profile:
                tariff = json.loads(backup_profile.tariff_json)

                # Database update callback for AEMO spike restore
                def aemo_spike_callback(user, db):
                    """Clear AEMO spike mode flags and update profile timestamp"""
                    user.aemo_in_spike_mode = False
                    user.aemo_spike_test_mode = False
                    user.aemo_spike_start_time = None
                    backup_profile_obj = SavedTOUProfile.query.get(backup_profile.id)
                    if backup_profile_obj:
                        backup_profile_obj.last_restored_at = datetime.utcnow()

                # Start background task to restore tariff
                start_background_task(
                    restore_tariff_background,
                    current_user.id,
                    current_user.tesla_energy_site_id,
                    tariff,
                    callback=aemo_spike_callback,
                    profile_name="AEMO Spike Restore"
                )

                logger.info(f"Spike restore initiated in background for {current_user.email}")
                flash('⏳ Restoring original tariff from spike mode. This will take ~60 seconds. You can navigate away.')
            else:
                logger.error(f"Backup tariff ID {current_user.aemo_saved_tariff_id} not found")
                flash('Backup tariff not found. Exiting spike mode anyway.')
                current_user.aemo_in_spike_mode = False
                current_user.aemo_spike_test_mode = False  # Clear test mode
                db.session.commit()
        else:
            logger.warning(f"No backup tariff saved for {current_user.email}")
            flash('No backup tariff found. Exiting spike mode anyway.')
            current_user.aemo_in_spike_mode = False
            current_user.aemo_spike_test_mode = False  # Clear test mode
            db.session.commit()

        return redirect(url_for('main.settings'))

    except Exception as e:
        logger.error(f"Error in AEMO restore simulation: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash('Error restoring from spike mode. Please check logs.')
        db.session.rollback()
        return redirect(url_for('main.settings'))


# ============================================
# MANUAL DISCHARGE CONTROL
# ============================================

@bp.route('/api/force-discharge', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_force_discharge(tesla_client, api_user=None):
    """
    Force discharge mode - switches to autonomous mode with high export tariff.

    Supports both session login and Bearer token authentication.

    Request JSON:
    {
        "duration_minutes": 30  // Optional: 15, 30, 45, 60, 75, 90, 105, 120. Default: 30
    }

    Returns JSON:
    {
        "success": true,
        "message": "Force discharge activated for 30 minutes",
        "expires_at": "2024-01-01T12:30:00Z"
    }
    """
    user = api_user or current_user
    from app.tasks import create_spike_tariff
    from app.models import SavedTOUProfile
    import json

    try:
        # Get duration from request (default 30 minutes)
        data = request.get_json() or {}
        duration_minutes = data.get('duration_minutes', 30)

        # Convert to int if string (for API compatibility)
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            duration_minutes = 30

        # Validate duration (must be in 15-minute intervals, max 2 hours)
        valid_durations = [15, 30, 45, 60, 75, 90, 105, 120]
        if duration_minutes not in valid_durations:
            return jsonify({
                'success': False,
                'error': f'Invalid duration. Must be one of: {valid_durations}'
            }), 400

        logger.info(f"Force discharge requested by {user.email} for {duration_minutes} minutes")

        # Check if already in discharge mode
        if user.manual_discharge_active:
            # Extend the duration
            new_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
            user.manual_discharge_expires_at = new_expires_at
            db_commit_with_retry()
            logger.info(f"Extended discharge mode to {new_expires_at}")
            return jsonify({
                'success': True,
                'message': f'Discharge mode extended for {duration_minutes} minutes',
                'expires_at': new_expires_at.isoformat() + 'Z',
                'already_active': True
            })

        # Save current tariff as backup (if not already saved)
        default_profile = SavedTOUProfile.query.filter_by(
            user_id=user.id,
            is_default=True
        ).first()

        if default_profile:
            user.manual_discharge_saved_tariff_id = default_profile.id
            logger.info(f"Using existing default tariff ID {default_profile.id} as backup")
        else:
            # Save current tariff
            current_tariff = tesla_client.get_current_tariff(user.tesla_energy_site_id)
            if current_tariff:
                backup_profile = SavedTOUProfile(
                    user_id=user.id,
                    name=f"Auto-saved before discharge ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})",
                    description="Automatically saved before manual discharge mode",
                    source_type='tesla',
                    tariff_name=current_tariff.get('name', 'Unknown'),
                    utility=current_tariff.get('utility', 'Unknown'),
                    tariff_json=json.dumps(current_tariff),
                    created_at=datetime.utcnow(),
                    fetched_from_tesla_at=datetime.utcnow(),
                    is_default=True
                )
                db.session.add(backup_profile)
                db.session.flush()
                user.manual_discharge_saved_tariff_id = backup_profile.id
                logger.info(f"Saved current tariff as backup with ID {backup_profile.id}")

        # Create discharge tariff (high sell rate to encourage export)
        # Uses $10/kWh sell rate, 30c/kWh buy rate
        from app.tasks import create_discharge_tariff
        discharge_tariff = create_discharge_tariff(duration_minutes)

        # Upload tariff to Tesla
        result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, discharge_tariff)

        if result:
            # Calculate expiry time
            expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)

            # Update user state
            user.manual_discharge_active = True
            user.manual_discharge_expires_at = expires_at
            # Clear tariff hash so restore normal will force a re-sync
            # (prevents deduplication from skipping the restore)
            user.last_tariff_hash = None
            db_commit_with_retry()

            # Force Powerwall to apply the tariff immediately
            from app.tasks import force_tariff_refresh
            force_tariff_refresh(tesla_client, user.tesla_energy_site_id)

            logger.info(f"Force discharge activated for {user.email} until {expires_at}")

            return jsonify({
                'success': True,
                'message': f'Force discharge activated for {duration_minutes} minutes',
                'expires_at': expires_at.isoformat() + 'Z',
                'duration_minutes': duration_minutes
            })
        else:
            logger.error(f"Failed to upload discharge tariff for {user.email}")
            return jsonify({
                'success': False,
                'error': 'Failed to upload discharge tariff to Tesla'
            }), 500

    except Exception as e:
        logger.error(f"Error in force discharge: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/force-charge', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_force_charge(tesla_client, api_user=None):
    """
    Force charge mode - switches to autonomous mode with free import tariff.

    Supports both session login and Bearer token authentication.

    Request JSON:
    {
        "duration_minutes": 30  // Optional: 15, 30, 45, 60, 75, 90, 105, 120. Default: 30
    }

    Returns JSON:
    {
        "success": true,
        "message": "Force charge activated for 30 minutes",
        "expires_at": "2024-01-01T12:30:00Z"
    }
    """
    user = api_user or current_user
    from app.models import SavedTOUProfile
    import json

    try:
        # Get duration from request (default 30 minutes)
        data = request.get_json() or {}
        duration_minutes = data.get('duration_minutes', 30)

        # Convert to int if string (for API compatibility)
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            duration_minutes = 30

        # Validate duration (must be in 15-minute intervals, max 2 hours)
        valid_durations = [15, 30, 45, 60, 75, 90, 105, 120]
        if duration_minutes not in valid_durations:
            return jsonify({
                'success': False,
                'error': f'Invalid duration. Must be one of: {valid_durations}'
            }), 400

        logger.info(f"Force charge requested by {user.email} for {duration_minutes} minutes")

        # Check if already in charge mode
        if user.manual_charge_active:
            # Extend the duration
            new_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
            user.manual_charge_expires_at = new_expires_at
            db_commit_with_retry()
            logger.info(f"Extended charge mode to {new_expires_at}")
            return jsonify({
                'success': True,
                'message': f'Charge mode extended for {duration_minutes} minutes',
                'expires_at': new_expires_at.isoformat() + 'Z',
                'already_active': True
            })

        # If currently in discharge mode, cancel it first
        if user.manual_discharge_active:
            logger.info(f"Canceling active discharge mode to enable charge mode")
            user.manual_discharge_active = False
            user.manual_discharge_expires_at = None

        # Save current tariff as backup (if not already saved)
        default_profile = SavedTOUProfile.query.filter_by(
            user_id=user.id,
            is_default=True
        ).first()

        if default_profile:
            user.manual_charge_saved_tariff_id = default_profile.id
            logger.info(f"Using existing default tariff ID {default_profile.id} as backup")
        else:
            # Save current tariff
            current_tariff = tesla_client.get_current_tariff(user.tesla_energy_site_id)
            if current_tariff:
                backup_profile = SavedTOUProfile(
                    user_id=user.id,
                    name=f"Auto-saved before charge ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})",
                    description="Automatically saved before manual charge mode",
                    source_type='tesla',
                    tariff_name=current_tariff.get('name', 'Unknown'),
                    utility=current_tariff.get('utility', 'Unknown'),
                    tariff_json=json.dumps(current_tariff),
                    created_at=datetime.utcnow(),
                    fetched_from_tesla_at=datetime.utcnow(),
                    is_default=True
                )
                db.session.add(backup_profile)
                db.session.flush()
                user.manual_charge_saved_tariff_id = backup_profile.id
                logger.info(f"Saved current tariff as backup with ID {backup_profile.id}")

        # Save current backup reserve and set to 100% to force charging
        # Only save if not already in charge mode (prevents overwriting on double-click)
        already_in_charge = getattr(user, 'manual_charge_active', False)
        if not already_in_charge:
            site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
            if site_info:
                current_backup_reserve = site_info.get('backup_reserve_percent')
                if current_backup_reserve is not None:
                    user.manual_charge_saved_backup_reserve = current_backup_reserve
                    logger.info(f"Saved current backup reserve: {current_backup_reserve}%")
        else:
            logger.info(f"Force charge already active - keeping original saved backup reserve: {getattr(user, 'manual_charge_saved_backup_reserve', 'unknown')}%")

        # Set backup reserve to 100% to force charging from grid
        logger.info("Setting backup reserve to 100% to force charging...")
        backup_result = tesla_client.set_backup_reserve(user.tesla_energy_site_id, 100)
        if backup_result:
            logger.info("Set backup reserve to 100%")
        else:
            logger.warning("Failed to set backup reserve to 100%")

        # Create charge tariff (free buy rate to encourage charging)
        # Uses $0/kWh buy rate during window, $10/kWh outside, $0/kWh sell
        from app.tasks import create_charge_tariff
        charge_tariff = create_charge_tariff(duration_minutes)

        # Upload tariff to Tesla
        result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, charge_tariff)

        if result:
            # Calculate expiry time
            expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)

            # Update user state
            user.manual_charge_active = True
            user.manual_charge_expires_at = expires_at
            # Clear tariff hash so restore normal will force a re-sync
            # (prevents deduplication from skipping the restore)
            user.last_tariff_hash = None
            db_commit_with_retry()

            # Force Powerwall to apply the tariff immediately
            from app.tasks import force_tariff_refresh
            force_tariff_refresh(tesla_client, user.tesla_energy_site_id)

            logger.info(f"Force charge activated for {user.email} until {expires_at}")

            return jsonify({
                'success': True,
                'message': f'Force charge activated for {duration_minutes} minutes',
                'expires_at': expires_at.isoformat() + 'Z',
                'duration_minutes': duration_minutes
            })
        else:
            logger.error(f"Failed to upload charge tariff for {user.email}")
            return jsonify({
                'success': False,
                'error': 'Failed to upload charge tariff to Tesla'
            }), 500

    except Exception as e:
        logger.error(f"Error in force charge: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/restore-normal', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_restore_normal(tesla_client, api_user=None):
    """
    Restore normal operation - restores saved tariff or triggers Amber sync.
    Supports both session login and Bearer token authentication.

    Returns JSON:
    {
        "success": true,
        "message": "Normal operation restored",
        "method": "tariff_restore" | "amber_sync"
    }
    """
    import json
    user = api_user or current_user

    try:
        logger.info(f"Restore normal requested by {user.email}")

        # Check what mode we're in
        was_in_discharge = user.manual_discharge_active
        was_in_charge = getattr(user, 'manual_charge_active', False)
        was_in_spike = user.aemo_in_spike_mode

        if not was_in_discharge and not was_in_charge and not was_in_spike:
            return jsonify({
                'success': True,
                'message': 'Already in normal operation',
                'method': 'none'
            })

        restore_method = 'none'

        # IMMEDIATELY switch to self_consumption to stop any ongoing export/import
        # This ensures discharge/charge stops right away, before tariff restoration completes
        logger.info("Immediately switching to self_consumption to stop forced charge/discharge")
        tesla_client.set_operation_mode(user.tesla_energy_site_id, 'self_consumption')

        # Clear all discharge/charge/spike states FIRST (before sync, so sync doesn't skip this user)
        user.manual_discharge_active = False
        user.manual_discharge_expires_at = None
        user.manual_charge_active = False
        user.manual_charge_expires_at = None
        user.aemo_in_spike_mode = False
        user.aemo_spike_test_mode = False
        user.aemo_spike_start_time = None
        db_commit_with_retry()
        logger.info(f"Cleared force charge/discharge flags for {user.email}")

        # Check if user has Amber configured (should sync instead of restore static tariff)
        use_amber_sync = bool(user.amber_api_token_encrypted and user.sync_enabled)

        if use_amber_sync:
            # For Amber users, trigger a sync to get fresh prices
            logger.info(f"Amber user - triggering price sync for {user.email}")
            from app.tasks import _sync_all_users_internal
            # Run sync - flags are now cleared so user won't be skipped
            _sync_all_users_internal(None, sync_mode='initial_forecast')
            # Switch back to autonomous mode after sync completes
            tesla_client.set_operation_mode(user.tesla_energy_site_id, 'autonomous')
            restore_method = 'amber_sync'
        else:
            # Restore saved tariff
            backup_profile = None

            # Check manual discharge backup first
            if was_in_discharge and user.manual_discharge_saved_tariff_id:
                backup_profile = SavedTOUProfile.query.get(user.manual_discharge_saved_tariff_id)
            # Check manual charge backup
            elif was_in_charge and getattr(user, 'manual_charge_saved_tariff_id', None):
                backup_profile = SavedTOUProfile.query.get(user.manual_charge_saved_tariff_id)
            # Fall back to AEMO backup
            elif was_in_spike and user.aemo_saved_tariff_id:
                backup_profile = SavedTOUProfile.query.get(user.aemo_saved_tariff_id)
            # Fall back to default profile
            else:
                backup_profile = SavedTOUProfile.query.filter_by(
                    user_id=user.id,
                    is_default=True
                ).first()

            if backup_profile:
                tariff = json.loads(backup_profile.tariff_json)
                result = tesla_client.set_tariff_rate(user.tesla_energy_site_id, tariff)

                if result:
                    # Force Powerwall to apply
                    from app.tasks import force_tariff_refresh
                    force_tariff_refresh(tesla_client, user.tesla_energy_site_id)
                    restore_method = 'tariff_restore'
                    logger.info(f"Restored tariff from profile {backup_profile.id}")
                else:
                    logger.error(f"Failed to restore tariff for {user.email}")
                    return jsonify({
                        'success': False,
                        'error': 'Failed to upload restored tariff to Tesla'
                    }), 500
            else:
                logger.warning(f"No backup tariff found for {user.email}")
                restore_method = 'no_backup'

        # Restore saved backup reserve if it was saved during force charge
        saved_backup_reserve = getattr(user, 'manual_charge_saved_backup_reserve', None)
        if was_in_charge and saved_backup_reserve is not None:
            logger.info(f"Restoring backup reserve to {saved_backup_reserve}%")
            backup_result = tesla_client.set_backup_reserve(user.tesla_energy_site_id, saved_backup_reserve)
            if backup_result:
                logger.info(f"Restored backup reserve to {saved_backup_reserve}%")
            else:
                logger.warning(f"Failed to restore backup reserve to {saved_backup_reserve}%")
            # Clear the saved backup reserve
            user.manual_charge_saved_backup_reserve = None
            db_commit_with_retry()

        message = 'Normal operation restored'
        if restore_method == 'amber_sync':
            message = 'Normal operation restored via Amber sync'
        elif restore_method == 'tariff_restore':
            message = 'Normal operation restored from saved tariff'
        elif restore_method == 'no_backup':
            message = 'Discharge mode cleared (no backup tariff to restore)'

        logger.info(f"Restore complete for {user.email}: {message}")

        return jsonify({
            'success': True,
            'message': message,
            'method': restore_method,
            'was_in_discharge': was_in_discharge,
            'was_in_spike': was_in_spike
        })

    except Exception as e:
        logger.error(f"Error in restore normal: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/discharge-status')
def api_discharge_status():
    """
    Get current discharge and charge mode status.
    Supports both session login and Bearer token authentication.

    Returns JSON:
    {
        "active": true,
        "expires_at": "2024-01-01T12:30:00Z",
        "remaining_minutes": 15,
        "in_spike_mode": false,
        "charge_active": false,
        "charge_expires_at": null,
        "charge_remaining_minutes": 0
    }
    """
    # Get user from Bearer token or session
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    now = datetime.utcnow()

    # Check if discharge has expired
    if user.manual_discharge_active and user.manual_discharge_expires_at:
        if now >= user.manual_discharge_expires_at:
            # Auto-expire the discharge mode
            logger.info(f"Manual discharge expired for {user.email}")
            user.manual_discharge_active = False
            user.manual_discharge_expires_at = None
            db.session.commit()

    # Check if charge has expired
    charge_active = getattr(user, 'manual_charge_active', False)
    charge_expires_at = getattr(user, 'manual_charge_expires_at', None)
    if charge_active and charge_expires_at:
        if now >= charge_expires_at:
            # Auto-expire the charge mode
            logger.info(f"Manual charge expired for {user.email}")
            user.manual_charge_active = False
            user.manual_charge_expires_at = None
            charge_active = False
            charge_expires_at = None
            db.session.commit()

    # Calculate remaining minutes for discharge
    discharge_remaining_minutes = 0
    if user.manual_discharge_active and user.manual_discharge_expires_at:
        remaining_seconds = (user.manual_discharge_expires_at - now).total_seconds()
        discharge_remaining_minutes = max(0, int(remaining_seconds / 60))

    # Calculate remaining minutes for charge
    charge_remaining_minutes = 0
    if charge_active and charge_expires_at:
        charge_remaining_seconds = (charge_expires_at - now).total_seconds()
        charge_remaining_minutes = max(0, int(charge_remaining_seconds / 60))

    # For mobile app: return the active mode's expiry in the main expires_at/remaining_minutes fields
    # Priority: charge > discharge (if both somehow active, charge takes precedence)
    if charge_active and charge_expires_at:
        active_expires_at = charge_expires_at.isoformat() + 'Z'
        active_remaining_minutes = charge_remaining_minutes
    elif user.manual_discharge_active and user.manual_discharge_expires_at:
        active_expires_at = user.manual_discharge_expires_at.isoformat() + 'Z'
        active_remaining_minutes = discharge_remaining_minutes
    else:
        active_expires_at = None
        active_remaining_minutes = 0

    return jsonify({
        'active': user.manual_discharge_active,  # Legacy key for backwards compatibility
        'discharge_active': user.manual_discharge_active,  # New key matching mobile app expectation
        'expires_at': active_expires_at,  # Active mode's expiry for mobile countdown
        'remaining_minutes': active_remaining_minutes,  # Active mode's remaining time
        'in_spike_mode': user.aemo_in_spike_mode,
        'charge_active': charge_active,
        'charge_expires_at': charge_expires_at.isoformat() + 'Z' if charge_expires_at else None,
        'charge_remaining_minutes': charge_remaining_minutes,
        'discharge_expires_at': user.manual_discharge_expires_at.isoformat() + 'Z' if user.manual_discharge_expires_at else None,
        'discharge_remaining_minutes': discharge_remaining_minutes
    })


# ============================================
# POWERWALL SETTINGS API (for mobile app Controls)
# ============================================

@bp.route('/api/powerwall-settings')
@require_tesla_client
@require_tesla_site_id
def api_powerwall_settings(tesla_client, api_user=None):
    """
    Get current Powerwall settings for mobile app Controls.

    Supports both session login and Bearer token authentication.

    Returns JSON:
    {
        "backup_reserve": 20,
        "operation_mode": "autonomous",
        "grid_export_rule": "pv_only",
        "grid_charging_enabled": true,
        "solar_curtailment_enabled": false
    }
    """
    user = api_user or current_user
    try:
        site_id = user.tesla_energy_site_id

        # Get site info for backup reserve and operation mode
        site_info = tesla_client.get_site_info(site_id)
        if not site_info:
            return jsonify({'success': False, 'error': 'Failed to get site info'}), 500

        # Get grid import/export settings from Tesla API
        grid_settings = tesla_client.get_grid_import_export(site_id)
        api_export_rule = grid_settings.get('customer_preferred_export_rule', 'pv_only') if grid_settings else 'pv_only'

        # If solar curtailment is enabled, use server's target rule (more accurate than stale API)
        # The server updates this every 5 mins based on price
        if user.solar_curtailment_enabled and user.current_export_rule:
            export_rule = user.current_export_rule
            logger.debug(f"Using server's target export rule '{export_rule}' (API reported '{api_export_rule}')")
        else:
            export_rule = api_export_rule

        return jsonify({
            'success': True,
            'backup_reserve': site_info.get('backup_reserve_percent', 0),
            'operation_mode': site_info.get('default_real_mode', 'autonomous'),
            'grid_export_rule': export_rule,
            'grid_charging_enabled': not grid_settings.get('disallow_charge_from_grid_with_solar_installed', False) if grid_settings else True,
            'solar_curtailment_enabled': user.solar_curtailment_enabled or False,
            'manual_export_override': getattr(user, 'manual_export_override', False) or False
        })
    except Exception as e:
        logger.error(f"Error getting Powerwall settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backup-reserve', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_set_backup_reserve(tesla_client, api_user=None):
    """
    Set the Powerwall backup reserve percentage.

    Supports both session login and Bearer token authentication.

    Request JSON: {"percent": 20}
    Returns JSON: {"success": true, "backup_reserve": 20}
    """
    user = api_user or current_user
    try:
        data = request.get_json() or {}
        percent = data.get('percent', 20)

        # Validate range
        percent = max(0, min(100, int(percent)))

        site_id = user.tesla_energy_site_id
        result = tesla_client.set_backup_reserve(site_id, percent)

        if result:
            logger.info(f"Set backup reserve to {percent}% for {user.email}")
            return jsonify({'success': True, 'backup_reserve': percent})
        else:
            return jsonify({'success': False, 'error': 'Failed to set backup reserve'}), 500
    except Exception as e:
        logger.error(f"Error setting backup reserve: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/operation-mode', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_set_operation_mode(tesla_client, api_user=None):
    """
    Set the Powerwall operation mode.

    Supports both session login and Bearer token authentication.

    Request JSON: {"mode": "autonomous"} or {"mode": "self_consumption"}
    Returns JSON: {"success": true, "operation_mode": "autonomous"}
    """
    user = api_user or current_user
    try:
        data = request.get_json() or {}
        mode = data.get('mode', 'autonomous')

        # Validate mode
        valid_modes = ['autonomous', 'self_consumption', 'backup']
        if mode not in valid_modes:
            return jsonify({'success': False, 'error': f'Invalid mode. Must be one of: {valid_modes}'}), 400

        site_id = user.tesla_energy_site_id
        result = tesla_client.set_operation_mode(site_id, mode)

        if result:
            logger.info(f"Set operation mode to '{mode}' for {user.email}")
            return jsonify({'success': True, 'operation_mode': mode})
        else:
            return jsonify({'success': False, 'error': 'Failed to set operation mode'}), 500
    except Exception as e:
        logger.error(f"Error setting operation mode: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/grid-export', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_set_grid_export(tesla_client, api_user=None):
    """
    Set the grid export rule.

    Supports both session login and Bearer token authentication.

    Request JSON: {"rule": "pv_only"}
    Valid rules: "never", "pv_only", "battery_ok"
    Returns JSON: {"success": true, "grid_export_rule": "pv_only"}
    """
    user = api_user or current_user
    try:
        data = request.get_json() or {}
        rule = data.get('rule', 'pv_only')

        # Validate rule
        valid_rules = ['never', 'pv_only', 'battery_ok']
        if rule not in valid_rules:
            return jsonify({'success': False, 'error': f'Invalid rule. Must be one of: {valid_rules}'}), 400

        site_id = user.tesla_energy_site_id
        result = tesla_client.set_grid_export_rule(site_id, rule)

        if result:
            # If solar curtailment is enabled, mark this as a manual override
            # so the 5-minute scheduler respects the user's choice
            if getattr(user, 'solar_curtailment_enabled', False):
                user.manual_export_override = True
                user.manual_export_rule = rule
                db.session.commit()
                logger.info(f"Set grid export rule to '{rule}' for {user.email} (manual override enabled)")
            else:
                logger.info(f"Set grid export rule to '{rule}' for {user.email}")
            return jsonify({'success': True, 'grid_export_rule': rule})
        else:
            return jsonify({'success': False, 'error': 'Failed to set grid export rule'}), 500
    except Exception as e:
        logger.error(f"Error setting grid export rule: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/grid-export/auto', methods=['POST'])
@login_required
@require_tesla_client
@require_tesla_site_id
def api_set_grid_export_auto(tesla_client, api_user=None):
    """
    Clear manual export override and return to automatic control.

    This allows the 5-minute solar curtailment scheduler to resume
    automatic control of the grid export setting.

    Returns JSON: {"success": true, "manual_override": false}
    """
    user = api_user or current_user
    try:
        user.manual_export_override = False
        user.manual_export_rule = None
        db.session.commit()
        logger.info(f"Cleared manual export override for {user.email} - returning to auto control")
        return jsonify({
            'success': True,
            'manual_override': False,
            'message': 'Grid export returned to automatic control'
        })
    except Exception as e:
        logger.error(f"Error clearing manual export override: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/grid-charging', methods=['POST'])
@require_tesla_client
@require_tesla_site_id
def api_set_grid_charging(tesla_client, api_user=None):
    """
    Enable or disable grid charging.

    Supports both session login and Bearer token authentication.

    Request JSON: {"enabled": true}
    Returns JSON: {"success": true, "grid_charging_enabled": true}
    """
    user = api_user or current_user
    try:
        data = request.get_json() or {}
        enabled = data.get('enabled', True)

        # Ensure boolean
        enabled = bool(enabled)

        site_id = user.tesla_energy_site_id
        result = tesla_client.set_grid_charging_enabled(site_id, enabled)

        if result:
            logger.info(f"Set grid charging to {'enabled' if enabled else 'disabled'} for {user.email}")
            return jsonify({'success': True, 'grid_charging_enabled': enabled})
        else:
            return jsonify({'success': False, 'error': 'Failed to set grid charging'}), 500
    except Exception as e:
        logger.error(f"Error setting grid charging: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# CURRENT TOU RATE MANAGEMENT
# ============================================

@bp.route('/current_tou_rate')
@login_required
@require_tesla_client
@require_tesla_site_id
def current_tou_rate(tesla_client, api_user=None):
    """View current TOU rate from Tesla and manage saved profiles"""
    logger.info(f"User {current_user.email} accessing Current TOU Rate page")

    site_id = current_user.tesla_energy_site_id

    # Fetch current tariff from Tesla
    current_tariff = None
    try:
        current_tariff = tesla_client.get_current_tariff(site_id)
        if current_tariff:
            logger.info(f"Successfully fetched current tariff: {current_tariff.get('name', 'Unknown')}")
        else:
            logger.warning("No tariff data returned from Tesla")
    except Exception as e:
        logger.error(f"Error fetching current tariff: {e}")
        flash(f'Error fetching current tariff from Tesla: {str(e)}')

    # Get all saved profiles for this user
    saved_profiles = SavedTOUProfile.query.filter_by(user_id=current_user.id).order_by(SavedTOUProfile.created_at.desc()).all()

    return render_template(
        'current_tou_rate.html',
        title='Current TOU Rate',
        current_tariff=current_tariff,
        saved_profiles=saved_profiles
    )


@bp.route('/current_tou_rate/save', methods=['POST'])
@login_required
@require_tesla_client
@require_tesla_site_id
def save_current_tou_rate(tesla_client, api_user=None):
    """Save the current TOU rate from Tesla to database"""
    import json

    logger.info(f"User {current_user.email} saving current TOU rate")

    # Get form data
    profile_name = request.form.get('profile_name', '').strip()
    description = request.form.get('description', '').strip()

    if not profile_name:
        flash('Please provide a name for this profile.')
        return redirect(url_for('main.current_tou_rate'))

    site_id = current_user.tesla_energy_site_id

    # Fetch current tariff from Tesla
    try:
        current_tariff = tesla_client.get_current_tariff(site_id)
        if not current_tariff:
            flash('Could not fetch current tariff from Tesla. Please try again.')
            return redirect(url_for('main.current_tou_rate'))

        # Mark all existing profiles as not current
        SavedTOUProfile.query.filter_by(user_id=current_user.id, is_current=True).update({'is_current': False})

        # Create new saved profile
        new_profile = SavedTOUProfile(
            user_id=current_user.id,
            name=profile_name,
            description=description,
            source_type='tesla',
            tariff_name=current_tariff.get('name', 'Unknown'),
            utility=current_tariff.get('utility', ''),
            tariff_json=json.dumps(current_tariff),
            fetched_from_tesla_at=datetime.utcnow(),
            is_current=True
        )

        db.session.add(new_profile)
        db.session.commit()

        logger.info(f"Saved TOU profile: {profile_name}")
        flash(f'✓ Successfully saved TOU rate profile: {profile_name}')

    except Exception as e:
        logger.error(f"Error saving TOU rate: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash(f'Error saving TOU rate: {str(e)}')
        db.session.rollback()

    return redirect(url_for('main.current_tou_rate'))



@bp.route('/current_tou_rate/restore/<int:profile_id>', methods=['POST'])
@login_required
@require_tesla_client
@require_tesla_site_id
def restore_tou_rate(profile_id, tesla_client, api_user=None):
    """Restore a saved TOU rate profile to Tesla (async)"""
    import json
    import threading

    logger.info(f"User {current_user.email} restoring TOU profile {profile_id}")

    # Get the profile
    profile = SavedTOUProfile.query.filter_by(id=profile_id, user_id=current_user.id).first()
    if not profile:
        flash('Profile not found.')
        return redirect(url_for('main.current_tou_rate'))

    site_id = current_user.tesla_energy_site_id

    try:
        # Parse the saved tariff JSON
        tariff_data = json.loads(profile.tariff_json)

        # Database update callback for TOU rate restore
        def tou_restore_callback(user, db):
            """Update profile timestamps and mark as current"""
            profile_obj = SavedTOUProfile.query.get(profile_id)
            if profile_obj:
                profile_obj.last_restored_at = datetime.utcnow()
                # Mark this profile as current
                SavedTOUProfile.query.filter_by(user_id=user.id, is_current=True).update({'is_current': False})
                profile_obj.is_current = True

        # Start background task to restore tariff
        start_background_task(
            restore_tariff_background,
            current_user.id,
            site_id,
            tariff_data,
            callback=tou_restore_callback,
            profile_name=profile.name
        )

        logger.info(f"Restore initiated in background for profile: {profile.name}")
        flash(f'⏳ Restoring TOU rate: {profile.name}. This will take ~60 seconds. You can navigate away.')

    except Exception as e:
        logger.error(f"Error initiating TOU rate restore: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash(f'Error restoring TOU rate: {str(e)}')

    return redirect(url_for('main.current_tou_rate'))


@bp.route('/current_tou_rate/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_tou_profile(profile_id):
    """Delete a saved TOU rate profile"""
    logger.info(f"User {current_user.email} deleting TOU profile {profile_id}")

    # Get the profile
    profile = SavedTOUProfile.query.filter_by(id=profile_id, user_id=current_user.id).first()
    if not profile:
        flash('Profile not found.')
        return redirect(url_for('main.current_tou_rate'))

    try:
        profile_name = profile.name
        db.session.delete(profile)
        db.session.commit()

        logger.info(f"Deleted TOU profile: {profile_name}")
        flash(f'✓ Deleted TOU rate profile: {profile_name}')

    except Exception as e:
        logger.error(f"Error deleting TOU profile: {e}")
        flash(f'Error deleting TOU profile: {str(e)}')
        db.session.rollback()

    return redirect(url_for('main.current_tou_rate'))


@bp.route('/current_tou_rate/set_default/<int:profile_id>', methods=['POST'])
@login_required
def set_default_tou_profile(profile_id):
    """Set a TOU profile as the default tariff to restore to"""
    logger.info(f"User {current_user.email} setting TOU profile {profile_id} as default")

    # Get the profile
    profile = SavedTOUProfile.query.filter_by(id=profile_id, user_id=current_user.id).first()
    if not profile:
        flash('Profile not found.')
        return redirect(url_for('main.current_tou_rate'))

    try:
        # Clear any existing default
        SavedTOUProfile.query.filter_by(user_id=current_user.id, is_default=True).update({'is_default': False})

        # Set this profile as default
        profile.is_default = True
        db.session.commit()

        logger.info(f"Set TOU profile as default: {profile.name}")
        flash(f'✓ Set as default tariff: {profile.name}. This will be restored after spike events.')

    except Exception as e:
        logger.error(f"Error setting default TOU profile: {e}")
        flash(f'Error setting default: {str(e)}')
        db.session.rollback()

    return redirect(url_for('main.current_tou_rate'))


@bp.route('/api/current_tou_rate/raw')
@login_required
@require_tesla_client
@require_tesla_site_id
def api_current_tou_rate_raw(tesla_client, api_user=None):
    """API endpoint to get the raw current TOU tariff JSON"""
    site_id = current_user.tesla_energy_site_id

    try:
        current_tariff = tesla_client.get_current_tariff(site_id)
        if current_tariff:
            return jsonify(current_tariff)
        else:
            return jsonify({'error': 'No tariff data available'}), 404
    except Exception as e:
        logger.error(f"Error fetching current tariff: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/debug/site_info')
@login_required
@require_tesla_client
@require_tesla_site_id
def api_debug_site_info(tesla_client, api_user=None):
    """Debug endpoint to see full site_info response from Tesla"""
    site_id = current_user.tesla_energy_site_id

    try:
        site_info = tesla_client.get_site_info(site_id)
        if site_info:
            # Return the full site_info with a note about tariff field
            # Teslemetry uses 'tariff_content_v2', not 'utility_tariff_content_v2'
            has_tariff = 'tariff_content_v2' in site_info
            return jsonify({
                'has_tariff_field': has_tariff,
                'tariff_field_name': 'tariff_content_v2',
                'full_site_info': site_info
            })
        else:
            return jsonify({'error': 'No site info returned from Tesla'}), 404
    except Exception as e:
        logger.error(f"Error fetching site info: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================
# TESLA FLEET API OAUTH ROUTES
# ============================================

@bp.route('/fleet-api/connect')
@login_required
def fleet_api_oauth_start():
    """Start Tesla Fleet API OAuth flow"""
    logger.info(f"Fleet API OAuth flow initiated by user: {current_user.email}")

    # Get OAuth configuration - prefer user's saved credentials, fall back to environment
    client_id = None
    if current_user.fleet_api_client_id_encrypted:
        client_id = decrypt_token(current_user.fleet_api_client_id_encrypted)
    if not client_id:
        client_id = os.getenv('TESLA_CLIENT_ID')

    # Redirect URI - prefer user's saved value, fall back to environment
    redirect_uri = current_user.fleet_api_redirect_uri or os.getenv('TESLA_REDIRECT_URI')

    if not client_id:
        logger.error("Fleet API OAuth not configured - missing Client ID")
        flash('Tesla Fleet API Client ID not configured. Please enter your Client ID in the API Configuration section below and save settings.')
        return redirect(url_for('main.settings'))

    if not redirect_uri:
        logger.error("Fleet API OAuth not configured - missing Redirect URI")
        flash('Tesla Fleet API Redirect URI not configured. Please enter it in the API Configuration section below (e.g., http://localhost:5001/fleet-api/callback).')
        return redirect(url_for('main.settings'))

    # Generate random state parameter for CSRF protection
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    logger.debug(f"Generated OAuth state: {state[:10]}...")

    # Build authorization URL
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': 'openid offline_access energy_device_data energy_cmds',
        'state': state
    }

    auth_url = f"https://auth.tesla.com/oauth2/v3/authorize?{urlencode(params)}"
    logger.info(f"Redirecting to Tesla OAuth: {auth_url[:100]}...")

    return redirect(auth_url)


@bp.route('/fleet-api/callback')
@login_required
def fleet_api_oauth_callback():
    """Handle Tesla Fleet API OAuth callback"""
    logger.info(f"Fleet API OAuth callback received for user: {current_user.email}")

    # Get authorization code and state from query parameters
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')

    # Check for OAuth errors
    if error:
        logger.error(f"OAuth error: {error} - {error_description}")
        flash(f'OAuth authorization failed: {error_description or error}')
        return redirect(url_for('main.settings'))

    # Validate state parameter (CSRF protection)
    expected_state = session.get('oauth_state')
    if not state or state != expected_state:
        logger.error(f"OAuth state mismatch: expected {expected_state}, got {state}")
        flash('OAuth security validation failed. Please try again.')
        return redirect(url_for('main.settings'))

    # Clear state from session
    session.pop('oauth_state', None)

    if not code:
        logger.error("No authorization code received")
        flash('No authorization code received from Tesla.')
        return redirect(url_for('main.settings'))

    # Exchange authorization code for access token
    try:
        # Get credentials - prefer user's saved credentials, fall back to environment
        client_id = None
        client_secret = None
        if current_user.fleet_api_client_id_encrypted:
            client_id = decrypt_token(current_user.fleet_api_client_id_encrypted)
        if current_user.fleet_api_client_secret_encrypted:
            client_secret = decrypt_token(current_user.fleet_api_client_secret_encrypted)

        # Fall back to environment variables if not in database
        if not client_id:
            client_id = os.getenv('TESLA_CLIENT_ID')
        if not client_secret:
            client_secret = os.getenv('TESLA_CLIENT_SECRET')

        # Redirect URI - prefer user's saved value, fall back to environment
        redirect_uri = current_user.fleet_api_redirect_uri or os.getenv('TESLA_REDIRECT_URI')

        if not client_id or not client_secret or not redirect_uri:
            logger.error("Fleet API OAuth not configured - missing credentials")
            flash('Tesla Fleet API credentials not configured. Please enter Client ID, Client Secret, and Redirect URI in Settings.')
            return redirect(url_for('main.settings'))

        # Make token request
        token_url = "https://auth.tesla.com/oauth2/v3/token"
        token_data = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': redirect_uri
        }

        logger.info("Exchanging authorization code for access token")
        response = requests.post(token_url, data=token_data)

        if response.status_code != 200:
            logger.error(f"Token exchange failed: {response.status_code} - {response.text}")
            flash(f'Failed to exchange authorization code: {response.text}')
            return redirect(url_for('main.settings'))

        token_response = response.json()
        access_token = token_response.get('access_token')
        refresh_token = token_response.get('refresh_token')
        expires_in = token_response.get('expires_in', 28800)  # Default 8 hours

        if not access_token:
            logger.error("No access token in response")
            flash('Failed to obtain access token from Tesla.')
            return redirect(url_for('main.settings'))

        logger.info("Successfully obtained access and refresh tokens")

        # Calculate token expiry time
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Encrypt and save tokens to database
        current_user.fleet_api_access_token_encrypted = encrypt_token(access_token)
        if refresh_token:
            current_user.fleet_api_refresh_token_encrypted = encrypt_token(refresh_token)
        current_user.fleet_api_token_expires_at = expires_at
        current_user.tesla_api_provider = 'fleet_api'  # Set provider to Fleet API

        db.session.commit()

        logger.info(f"Successfully saved Fleet API tokens for user {current_user.email}")

        # Auto-detect energy site ID
        try:
            from app.api_clients import FleetAPIClient
            fleet_client = FleetAPIClient(
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret
            )
            energy_sites = fleet_client.get_energy_sites()
            if energy_sites:
                if len(energy_sites) == 1:
                    # Single site - auto-select it
                    site_id = str(energy_sites[0].get('energy_site_id'))
                    current_user.tesla_energy_site_id = site_id

                    # Fetch and store timezone for automation time triggers
                    try:
                        site_info = fleet_client.get_site_info(site_id)
                        if site_info:
                            site_timezone = site_info.get('installation_time_zone')
                            if site_timezone:
                                current_user.timezone = site_timezone
                                logger.info(f"Updated user timezone from Fleet API site info: {site_timezone}")
                    except Exception as tz_err:
                        logger.warning(f"Could not fetch site timezone via Fleet API: {tz_err}")

                    db.session.commit()
                    logger.info(f"Auto-detected Tesla energy site ID: {site_id}")
                    flash('✓ Successfully connected to Tesla Fleet API!')
                else:
                    # Multiple sites - user needs to choose
                    logger.info(f"Found {len(energy_sites)} energy sites - user needs to select one")
                    flash(f'✓ Connected to Tesla Fleet API! Found {len(energy_sites)} energy sites - please select one in Settings.')
            else:
                logger.warning("No energy sites found in Tesla account")
                flash('✓ Connected to Tesla Fleet API, but no energy sites (Powerwall/Solar) found in your Tesla account.')
        except Exception as site_err:
            logger.error(f"Error auto-detecting energy site: {site_err}")
            flash('✓ Connected to Tesla Fleet API!')

    except Exception as e:
        logger.error(f"Error during OAuth token exchange: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash(f'Error connecting to Tesla Fleet API: {str(e)}')
        db.session.rollback()

    return redirect(url_for('main.settings'))


@bp.route('/fleet-api/disconnect', methods=['POST'])
@login_required
def fleet_api_disconnect():
    """Disconnect Tesla Fleet API - clear OAuth tokens but keep client credentials."""
    try:
        logger.info(f"Fleet API disconnect requested by user: {current_user.email}")

        # Clear Fleet API OAuth tokens only
        current_user.fleet_api_access_token_encrypted = None
        current_user.fleet_api_refresh_token_encrypted = None
        current_user.fleet_api_token_expires_at = None

        # Keep client ID, secret, and redirect URI - they're reusable
        # Only the tunnel URL changes, not the app credentials

        # Clear Tesla site ID since it was obtained via Fleet API
        current_user.tesla_energy_site_id = None

        db.session.commit()

        logger.info(f"Fleet API disconnected for user: {current_user.email}")
        flash('Tesla Fleet API disconnected. Client credentials preserved.')
        return redirect(url_for('main.settings'))

    except Exception as e:
        logger.error(f"Error disconnecting Fleet API: {e}")
        flash('Error disconnecting Fleet API. Please try again.')
        return redirect(url_for('main.settings'))


@bp.route('/fleet-api/delete-keys', methods=['POST'])
@login_required
def fleet_api_delete_keys():
    """Delete Tesla Fleet API domain keys."""
    logger.info(f"Delete keys requested by user: {current_user.email}")

    # Define key directory and paths
    keys_dir = os.path.join(current_app.root_path, '..', 'data', 'tesla-keys')
    private_key_path = os.path.join(keys_dir, 'private-key.pem')
    public_key_path = os.path.join(keys_dir, 'com.tesla.3p.public-key.pem')

    deleted_files = []
    errors = []

    try:
        if os.path.exists(private_key_path):
            os.remove(private_key_path)
            deleted_files.append('private-key.pem')
            logger.info(f"Deleted private key: {private_key_path}")

        if os.path.exists(public_key_path):
            os.remove(public_key_path)
            deleted_files.append('com.tesla.3p.public-key.pem')
            logger.info(f"Deleted public key: {public_key_path}")

        if deleted_files:
            return jsonify({
                'success': True,
                'message': f'Deleted keys: {", ".join(deleted_files)}'
            })
        else:
            return jsonify({
                'success': True,
                'message': 'No keys found to delete'
            })

    except Exception as e:
        logger.error(f"Error deleting keys: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/fleet-api/generate-keys', methods=['POST'])
@login_required
def fleet_api_generate_keys():
    """
    Generate EC key pair for Tesla Fleet API domain verification.

    Creates:
    - data/tesla-keys/private-key.pem (keep secret!)
    - data/tesla-keys/com.tesla.3p.public-key.pem (served publicly)
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend

    logger.info(f"Key generation initiated by user: {current_user.email}")

    # Define key directory and paths
    keys_dir = os.path.join(current_app.root_path, '..', 'data', 'tesla-keys')
    private_key_path = os.path.join(keys_dir, 'private-key.pem')
    public_key_path = os.path.join(keys_dir, 'com.tesla.3p.public-key.pem')

    try:
        # Create directory if it doesn't exist
        os.makedirs(keys_dir, exist_ok=True)

        # Check if keys already exist
        keys_exist = os.path.exists(private_key_path) and os.path.exists(public_key_path)

        # Generate EC key pair using prime256v1 (secp256r1) curve
        # This is required by Tesla Fleet API
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        public_key = private_key.public_key()

        # Serialize private key to PEM format
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        )

        # Serialize public key to PEM format
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        # Write private key with restricted permissions
        with open(private_key_path, 'wb') as f:
            f.write(private_pem)
        os.chmod(private_key_path, 0o600)  # Owner read/write only

        # Write public key
        with open(public_key_path, 'wb') as f:
            f.write(public_pem)

        logger.info(f"Successfully generated Tesla Fleet API keys at {keys_dir}")

        return jsonify({
            'success': True,
            'message': 'Keys generated successfully',
            'keys_replaced': keys_exist,
            'public_key_path': '/.well-known/appspecific/com.tesla.3p.public-key.pem'
        })

    except PermissionError as e:
        logger.error(f"Permission error generating keys: {e}")
        return jsonify({
            'success': False,
            'error': f'Permission denied: {str(e)}. Check directory permissions.'
        }), 500
    except Exception as e:
        logger.error(f"Error generating keys: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': f'Failed to generate keys: {str(e)}'
        }), 500


@bp.route('/fleet-api/key-status')
@login_required
def fleet_api_key_status():
    """Check if Tesla Fleet API keys exist and are accessible."""
    keys_dir = os.path.join(current_app.root_path, '..', 'data', 'tesla-keys')
    private_key_path = os.path.join(keys_dir, 'private-key.pem')
    public_key_path = os.path.join(keys_dir, 'com.tesla.3p.public-key.pem')

    private_exists = os.path.exists(private_key_path)
    public_exists = os.path.exists(public_key_path)

    return jsonify({
        'keys_exist': private_exists and public_exists,
        'private_key_exists': private_exists,
        'public_key_exists': public_exists,
        'public_key_url': '/.well-known/appspecific/com.tesla.3p.public-key.pem'
    })


@bp.route('/.well-known/appspecific/com.tesla.3p.public-key.pem')
def tesla_public_key():
    """
    Serve Tesla Fleet API public key for domain verification.

    Tesla requires this key to be publicly accessible (no auth) at this exact path
    for partner registration to work.
    """
    key_path = os.path.join(current_app.root_path, '..', 'data', 'tesla-keys', 'com.tesla.3p.public-key.pem')

    if os.path.exists(key_path):
        logger.info(f"Serving Tesla public key from {key_path}")
        return send_file(key_path, mimetype='application/x-pem-file')
    else:
        logger.warning(f"Tesla public key not found at {key_path}")
        return 'Public key not found. Generate keys first: openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem && openssl ec -in private-key.pem -pubout -out com.tesla.3p.public-key.pem', 404


@bp.route('/fleet-api/register-partner', methods=['POST'])
@login_required
def fleet_api_register_partner():
    """
    Register this app's domain with Tesla Fleet API.

    This calls POST /api/1/partner_accounts to register the domain.
    Required before any Fleet API calls will work (fixes 412 Precondition Failed).

    Uses client_credentials flow to get a partner token (different from user token).
    """
    logger.info(f"Partner registration initiated by user: {current_user.email}")

    # Get client credentials - needed for partner token
    client_id = None
    client_secret = None

    if current_user.fleet_api_client_id_encrypted:
        client_id = decrypt_token(current_user.fleet_api_client_id_encrypted)
    if current_user.fleet_api_client_secret_encrypted:
        client_secret = decrypt_token(current_user.fleet_api_client_secret_encrypted)

    # Fall back to environment variables
    if not client_id:
        client_id = os.getenv('TESLA_CLIENT_ID')
    if not client_secret:
        client_secret = os.getenv('TESLA_CLIENT_SECRET')

    if not client_id or not client_secret:
        logger.error("Missing client credentials for partner token")
        return jsonify({
            'success': False,
            'error': 'Client ID and Client Secret are required. Please configure them in settings.'
        }), 400

    # Get the domain from the redirect URI or environment
    redirect_uri = current_user.fleet_api_redirect_uri or os.getenv('TESLA_REDIRECT_URI')
    if not redirect_uri:
        return jsonify({
            'success': False,
            'error': 'No redirect URI configured. Please set it in settings.'
        }), 400

    # Extract domain from redirect URI (e.g., https://example.com/callback -> example.com)
    from urllib.parse import urlparse
    parsed = urlparse(redirect_uri)
    domain = parsed.netloc

    if not domain:
        return jsonify({
            'success': False,
            'error': f'Could not extract domain from redirect URI: {redirect_uri}'
        }), 400

    logger.info(f"Registering domain: {domain}")

    # Step 1: Get a partner token using client_credentials flow
    # This is different from the user token obtained via authorization_code flow
    token_url = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    fleet_api_base = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    token_data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'openid user_data vehicle_device_data vehicle_cmds vehicle_charging_cmds energy_device_data energy_cmds',
        'audience': fleet_api_base
    }

    try:
        logger.info("Requesting partner token via client_credentials flow")
        token_response = requests.post(token_url, data=token_data, timeout=30)

        if token_response.status_code != 200:
            logger.error(f"Failed to get partner token: {token_response.status_code} - {token_response.text}")
            return jsonify({
                'success': False,
                'error': f'Failed to get partner token: {token_response.text}'
            }), token_response.status_code

        token_json = token_response.json()
        partner_token = token_json.get('access_token')

        if not partner_token:
            logger.error("No access_token in partner token response")
            return jsonify({
                'success': False,
                'error': 'No access token received from Tesla'
            }), 500

        logger.info("Successfully obtained partner token")

        # Step 2: Register the domain using the partner token
        register_url = f"{fleet_api_base}/api/1/partner_accounts"

        headers = {
            'Authorization': f'Bearer {partner_token}',
            'Content-Type': 'application/json'
        }

        payload = {
            'domain': domain
        }

        logger.info(f"Calling Tesla partner registration: POST {register_url}")
        response = requests.post(register_url, json=payload, headers=headers, timeout=30)

        logger.info(f"Partner registration response: {response.status_code}")
        logger.debug(f"Response body: {response.text}")

        if response.status_code == 200:
            result = response.json()
            logger.info(f"Partner registration successful: {result}")
            return jsonify({
                'success': True,
                'message': f'Successfully registered domain: {domain}',
                'response': result
            })
        elif response.status_code == 409:
            # Already registered
            logger.info(f"Domain already registered: {domain}")
            return jsonify({
                'success': True,
                'message': f'Domain already registered: {domain}',
                'already_registered': True
            })
        else:
            error_text = response.text
            logger.error(f"Partner registration failed: {response.status_code} - {error_text}")
            return jsonify({
                'success': False,
                'error': f'Registration failed: {response.status_code} - {error_text}'
            }), response.status_code

    except requests.exceptions.Timeout:
        logger.error("Partner registration request timed out")
        return jsonify({
            'success': False,
            'error': 'Request timed out. Please try again.'
        }), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Partner registration request failed: {e}")
        return jsonify({
            'success': False,
            'error': f'Request failed: {str(e)}'
        }), 500


# ============================================================================
# Cloudflare Tunnel Management (for Tesla Fleet API registration)
# ============================================================================

import subprocess
import threading
import re
import platform
import urllib.request
import stat


def get_cloudflared_path():
    """Get the path to cloudflared binary, checking both PATH and local data directory."""
    # Check if in PATH first
    try:
        result = subprocess.run(['which', 'cloudflared'], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # Check local data directory
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    local_path = os.path.join(data_dir, 'cloudflared')
    if os.path.exists(local_path) and os.access(local_path, os.X_OK):
        return local_path

    return None


def download_cloudflared():
    """Download cloudflared binary for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    # Map architecture names
    if machine in ('x86_64', 'amd64'):
        arch = 'amd64'
    elif machine in ('arm64', 'aarch64'):
        arch = 'arm64'
    elif machine in ('armv7l', 'armhf'):
        arch = 'arm'
    else:
        raise ValueError(f'Unsupported architecture: {machine}')

    # Determine download URL
    if system == 'darwin':
        if arch == 'arm64':
            filename = 'cloudflared-darwin-arm64.tgz'
        else:
            filename = 'cloudflared-darwin-amd64.tgz'
        is_tarball = True
    elif system == 'linux':
        filename = f'cloudflared-linux-{arch}'
        is_tarball = False
    else:
        raise ValueError(f'Unsupported platform: {system}')

    url = f'https://github.com/cloudflare/cloudflared/releases/latest/download/{filename}'

    # Download to data directory
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    os.makedirs(data_dir, exist_ok=True)
    cloudflared_path = os.path.join(data_dir, 'cloudflared')

    logger.info(f'Downloading cloudflared from {url}')

    if is_tarball:
        # Download and extract tarball (macOS)
        import tarfile
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.tgz', delete=False) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            with tarfile.open(tmp.name, 'r:gz') as tar:
                # Extract cloudflared binary
                for member in tar.getmembers():
                    if member.name == 'cloudflared':
                        tar.extract(member, data_dir)
                        break
            os.unlink(tmp.name)
    else:
        # Direct binary download (Linux)
        urllib.request.urlretrieve(url, cloudflared_path)

    # Make executable
    os.chmod(cloudflared_path, os.stat(cloudflared_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    logger.info(f'cloudflared installed to {cloudflared_path}')
    return cloudflared_path


class CloudflareTunnel:
    """Manages a Cloudflare tunnel subprocess for exposing the local server."""

    def __init__(self):
        self.process = None
        self.public_url = None
        self.tunnel_type = None  # 'quick' or 'named'
        self._output_lines = []
        self._reader_thread = None

    def start_quick_tunnel(self, port=5001, timeout=30):
        """
        Start a Cloudflare quick tunnel and wait for the URL.

        Args:
            port: Local port to tunnel (default 5001)
            timeout: Max seconds to wait for URL (default 30)

        Returns:
            The public tunnel URL (https://xxx.trycloudflare.com)

        Raises:
            FileNotFoundError: If cloudflared is not installed
            TimeoutError: If URL not received within timeout
        """
        cloudflared_bin = get_cloudflared_path()
        if not cloudflared_bin:
            raise FileNotFoundError("cloudflared not installed")

        cmd = [cloudflared_bin, 'tunnel', '--url', f'http://localhost:{port}']

        logger.info(f"Starting cloudflared quick tunnel on port {port}")

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # Read output in background thread
        self._output_lines = []
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

        # Wait for URL to appear in output
        import time as time_module
        start_time = time_module.time()
        url_pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')

        while time_module.time() - start_time < timeout:
            for line in self._output_lines:
                match = url_pattern.search(line)
                if match:
                    self.public_url = match.group(0)
                    self.tunnel_type = 'quick'
                    logger.info(f"Cloudflare tunnel started: {self.public_url}")
                    return self.public_url
            time_module.sleep(0.5)

        # Timeout - kill process
        self.stop()
        raise TimeoutError("Timed out waiting for tunnel URL")

    def start_named_tunnel(self, token, timeout=30):
        """
        Start a named Cloudflare tunnel using a tunnel token.

        Args:
            token: Cloudflare tunnel token from dashboard
            timeout: Max seconds to wait for connection

        Returns:
            True if connected successfully

        Raises:
            FileNotFoundError: If cloudflared is not installed
            TimeoutError: If connection not established within timeout
        """
        cloudflared_bin = get_cloudflared_path()
        if not cloudflared_bin:
            raise FileNotFoundError("cloudflared not installed")

        cmd = [cloudflared_bin, 'tunnel', 'run', '--token', token]

        logger.info("Starting cloudflared named tunnel")

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # Read output in background thread
        self._output_lines = []
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

        # Wait for "Connection" or "Registered tunnel connection" message
        import time as time_module
        start_time = time_module.time()

        while time_module.time() - start_time < timeout:
            for line in self._output_lines:
                if 'INF Connection' in line or 'Registered tunnel connection' in line:
                    self.tunnel_type = 'named'
                    logger.info("Cloudflare named tunnel connected")
                    return True
            time_module.sleep(0.5)

        # Timeout - kill process
        self.stop()
        raise TimeoutError("Timed out waiting for tunnel connection")

    def _read_output(self):
        """Read process output in background thread."""
        try:
            for line in self.process.stdout:
                self._output_lines.append(line)
                logger.debug(f"cloudflared: {line.strip()}")
        except Exception:
            pass

    def stop(self):
        """Stop the tunnel process."""
        if self.process:
            logger.info("Stopping cloudflared tunnel")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        self.public_url = None
        self.tunnel_type = None
        self._output_lines = []

    def is_running(self):
        """Check if the tunnel process is still running."""
        return self.process is not None and self.process.poll() is None


# Global tunnel instance
_cloudflare_tunnel = None


@bp.route('/fleet-api/check-cloudflared')
@login_required
def check_cloudflared_installed():
    """Check if cloudflared CLI is available on the system."""
    cloudflared_bin = get_cloudflared_path()
    if not cloudflared_bin:
        return jsonify({
            'installed': False,
            'error': 'cloudflared not found'
        })

    try:
        result = subprocess.run(
            [cloudflared_bin, '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        version = result.stdout.strip() or result.stderr.strip()
        # Extract just the version string
        version_match = re.search(r'cloudflared version (\S+)', version)
        if version_match:
            version = version_match.group(1)
        return jsonify({
            'installed': True,
            'version': version
        })
    except subprocess.TimeoutExpired:
        return jsonify({
            'installed': False,
            'error': 'cloudflared check timed out'
        })
    except Exception as e:
        return jsonify({
            'installed': False,
            'error': str(e)
        })


@bp.route('/fleet-api/install-cloudflared', methods=['POST'])
@login_required
def install_cloudflared():
    """Download and install cloudflared binary."""
    try:
        # Check if already installed
        existing = get_cloudflared_path()
        if existing:
            return jsonify({
                'success': True,
                'message': 'cloudflared already installed',
                'path': existing
            })

        # Download cloudflared
        path = download_cloudflared()
        return jsonify({
            'success': True,
            'message': 'cloudflared installed successfully',
            'path': path
        })
    except Exception as e:
        logger.error(f"Failed to install cloudflared: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/fleet-api/start-tunnel', methods=['POST'])
@login_required
def fleet_api_start_tunnel():
    """
    Start a Cloudflare quick tunnel for Tesla Fleet API registration.

    This creates a temporary public URL that Tesla's servers can reach
    to verify the public key and complete domain registration.
    """
    global _cloudflare_tunnel

    logger.info(f"Tunnel start requested by user: {current_user.email}")

    # Check if tunnel is already running
    if _cloudflare_tunnel and _cloudflare_tunnel.is_running():
        public_url = _cloudflare_tunnel.public_url
        logger.info(f"Tunnel already active: {public_url}")
        return jsonify({
            'success': True,
            'public_url': public_url,
            'public_key_url': f"{public_url}/.well-known/appspecific/com.tesla.3p.public-key.pem",
            'callback_url': f"{public_url}/fleet-api/callback",
            'already_running': True
        })

    try:
        # Start new tunnel
        _cloudflare_tunnel = CloudflareTunnel()
        public_url = _cloudflare_tunnel.start_quick_tunnel(port=5001, timeout=30)

        # Store in app config
        current_app.config['TUNNEL_URL'] = public_url

        logger.info(f"Tunnel started successfully: {public_url}")

        return jsonify({
            'success': True,
            'public_url': public_url,
            'public_key_url': f"{public_url}/.well-known/appspecific/com.tesla.3p.public-key.pem",
            'callback_url': f"{public_url}/fleet-api/callback",
            'already_running': False
        })

    except FileNotFoundError:
        logger.error("cloudflared not installed")
        return jsonify({
            'success': False,
            'error': 'cloudflared is not installed. Install with: brew install cloudflared (macOS) or download from https://github.com/cloudflare/cloudflared/releases'
        }), 500

    except TimeoutError:
        logger.error("Timed out waiting for tunnel URL")
        return jsonify({
            'success': False,
            'error': 'Timed out waiting for tunnel to start. Please try again.'
        }), 500

    except Exception as e:
        logger.error(f"Failed to start tunnel: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': f'Failed to start tunnel: {str(e)}'
        }), 500


@bp.route('/fleet-api/stop-tunnel', methods=['POST'])
@login_required
def fleet_api_stop_tunnel():
    """Stop the Cloudflare tunnel."""
    global _cloudflare_tunnel

    logger.info(f"Tunnel stop requested by user: {current_user.email}")

    try:
        if _cloudflare_tunnel:
            _cloudflare_tunnel.stop()
            _cloudflare_tunnel = None

        current_app.config.pop('TUNNEL_URL', None)

        logger.info("Tunnel stopped")

        return jsonify({
            'success': True,
            'message': 'Tunnel stopped'
        })

    except Exception as e:
        logger.error(f"Failed to stop tunnel: {e}")
        return jsonify({
            'success': False,
            'error': f'Failed to stop tunnel: {str(e)}'
        }), 500


@bp.route('/fleet-api/tunnel-status')
@login_required
def fleet_api_tunnel_status():
    """Check if a Cloudflare tunnel is currently running."""
    global _cloudflare_tunnel

    try:
        if _cloudflare_tunnel and _cloudflare_tunnel.is_running():
            public_url = _cloudflare_tunnel.public_url
            return jsonify({
                'active': True,
                'tunnel_type': _cloudflare_tunnel.tunnel_type,
                'public_url': public_url,
                'public_key_url': f"{public_url}/.well-known/appspecific/com.tesla.3p.public-key.pem",
                'callback_url': f"{public_url}/fleet-api/callback"
            })

        return jsonify({
            'active': False,
            'public_url': None
        })

    except Exception as e:
        logger.error(f"Failed to check tunnel status: {e}")
        return jsonify({
            'active': False,
            'public_url': None,
            'error': str(e)
        })


@bp.route('/fleet-api/save-tunnel-config', methods=['POST'])
@login_required
def save_tunnel_config():
    """Save named tunnel configuration."""
    data = request.get_json()

    tunnel_token = data.get('tunnel_token')
    tunnel_domain = data.get('tunnel_domain')
    tunnel_enabled = data.get('tunnel_enabled', False)

    if tunnel_token:
        current_user.cloudflare_tunnel_token_encrypted = encrypt_token(tunnel_token)
    current_user.cloudflare_tunnel_domain = tunnel_domain
    current_user.cloudflare_tunnel_enabled = tunnel_enabled

    # Also update the redirect URI to use the custom domain
    if tunnel_domain:
        current_user.fleet_api_redirect_uri = f"https://{tunnel_domain}/fleet-api/callback"

    db.session.commit()

    return jsonify({'success': True})


@bp.route('/fleet-api/start-named-tunnel', methods=['POST'])
@login_required
def fleet_api_start_named_tunnel():
    """Start a named Cloudflare tunnel."""
    global _cloudflare_tunnel

    if not current_user.cloudflare_tunnel_token_encrypted:
        return jsonify({'success': False, 'error': 'No tunnel token configured'}), 400

    token = decrypt_token(current_user.cloudflare_tunnel_token_encrypted)

    if _cloudflare_tunnel and _cloudflare_tunnel.is_running():
        return jsonify({
            'success': True,
            'message': 'Tunnel already running',
            'tunnel_type': _cloudflare_tunnel.tunnel_type,
            'domain': current_user.cloudflare_tunnel_domain
        })

    try:
        _cloudflare_tunnel = CloudflareTunnel()
        _cloudflare_tunnel.start_named_tunnel(token)
        _cloudflare_tunnel.public_url = f"https://{current_user.cloudflare_tunnel_domain}"

        return jsonify({
            'success': True,
            'tunnel_type': 'named',
            'domain': current_user.cloudflare_tunnel_domain,
            'public_url': _cloudflare_tunnel.public_url
        })
    except Exception as e:
        logger.error(f"Failed to start named tunnel: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/fleet-api/get-tunnel-config')
@login_required
def get_tunnel_config():
    """Get the current named tunnel configuration."""
    return jsonify({
        'has_token': current_user.cloudflare_tunnel_token_encrypted is not None,
        'domain': current_user.cloudflare_tunnel_domain,
        'auto_start': current_user.cloudflare_tunnel_enabled
    })


# ============================================================================
# Battery Health API (for mobile app sync)
# ============================================================================

@bp.route('/api/battery-health', methods=['POST'])
def api_battery_health_update():
    """
    Receive battery health data from mobile app.

    Authentication: Bearer token in Authorization header
    The token is generated per-user via /api/battery-health/generate-token

    Request body:
    {
        "originalCapacityWh": 27000,
        "currentCapacityWh": 26100,
        "degradationPercent": 3.33,
        "batteryCount": 2,
        "scannedAt": "2025-01-15T10:30:00Z"
    }
    """
    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid Authorization header'}), 401

    token = auth_header.split(' ', 1)[1]
    if not token:
        return jsonify({'error': 'Empty token'}), 401

    # Find user by API token
    user = User.query.filter_by(battery_health_api_token=token).first()
    if not user:
        return jsonify({'error': 'Invalid API token'}), 401

    # Parse request data
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Update battery health fields
    try:
        original_capacity = data.get('originalCapacityWh')
        current_capacity = data.get('currentCapacityWh')
        degradation = data.get('degradationPercent')
        battery_count = data.get('batteryCount', 1)
        scanned_at_str = data.get('scannedAt')
        pack_data = data.get('packData')  # Optional per-pack data
        individual_batteries = data.get('individualBatteries')  # Per-battery data for HA display

        # Parse scanned_at timestamp or use current time
        if scanned_at_str:
            try:
                scanned_at = datetime.fromisoformat(scanned_at_str.replace('Z', '+00:00'))
            except ValueError:
                scanned_at = datetime.utcnow()
        else:
            scanned_at = datetime.utcnow()

        # Update user's current battery health
        user.battery_original_capacity_wh = original_capacity
        user.battery_current_capacity_wh = current_capacity
        user.battery_degradation_percent = degradation
        user.battery_count = battery_count
        user.battery_health_updated = datetime.utcnow()

        # Calculate health percent
        health_percent = (current_capacity / original_capacity * 100) if original_capacity else 100

        # Save to history
        import json
        # Store individual_batteries in pack_data if provided (for HA display)
        combined_pack_data = pack_data or {}
        if individual_batteries:
            combined_pack_data['individual_batteries'] = individual_batteries
        history_entry = BatteryHealthHistory(
            user_id=user.id,
            scanned_at=scanned_at,
            rated_capacity_wh=original_capacity,
            actual_capacity_wh=current_capacity,
            health_percent=health_percent,
            degradation_percent=degradation or 0,
            battery_count=battery_count,
            pack_data=json.dumps(combined_pack_data) if combined_pack_data else None
        )
        db.session.add(history_entry)
        db.session.commit()

        logger.info(f"Battery health updated for user {user.email}: {degradation}% degradation, saved to history")

        return jsonify({
            'status': 'ok',
            'message': 'Battery health updated and saved to history'
        })

    except Exception as e:
        logger.error(f"Error updating battery health: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/battery-health', methods=['GET'])
@api_auth_required
def api_battery_health_get(api_user=None, **kwargs):
    """
    Get current battery health data for the logged-in user.

    Supports both session login and Bearer token authentication.
    For Sigenergy users, fetches SOH from Modbus registers.
    """
    user = api_user or current_user

    # For Sigenergy users, get battery health from Modbus
    if user.battery_system == 'sigenergy' and user.sigenergy_modbus_host:
        from app.sigenergy_modbus import get_sigenergy_modbus_client
        client = get_sigenergy_modbus_client(user)
        if client:
            health = client.get_battery_health()
            if health and 'error' not in health:
                soh = health.get('battery_soh', 100)
                capacity_kwh = health.get('battery_capacity_kwh', 0)
                return jsonify({
                    'has_data': True,
                    'originalCapacityWh': capacity_kwh * 1000,  # Convert kWh to Wh
                    'currentCapacityWh': capacity_kwh * 1000 * (soh / 100),  # Estimate current from SOH
                    'degradationPercent': round(100 - soh, 2),
                    'batteryCount': 1,  # Sigenergy typically 1 unit
                    'healthPercent': soh,
                    'individualBatteries': None,
                    'updatedAt': None,
                    'installDate': None,
                    'source': 'sigenergy_modbus'
                })
        # Fall through to no data if Modbus fails

    if not user.battery_health_updated:
        return jsonify({
            'has_data': False,
            'message': 'No battery health data available'
        })

    # Auto-fetch install date from Tesla API if not already set
    install_date = user.powerwall_install_date
    if not install_date and user.tesla_energy_site_id:
        try:
            tesla_client = get_tesla_client(user)
            if tesla_client:
                # Get install date from site_info (more reliable than calendar_history)
                site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
                if site_info and site_info.get('installation_date'):
                    from datetime import date
                    install_date_str = site_info['installation_date']
                    # Parse ISO date format (e.g., "2025-02-21T10:28:47+10:00")
                    install_date = date.fromisoformat(install_date_str[:10])
                    user.powerwall_install_date = install_date
                    db.session.commit()
                    logger.info(f"Auto-fetched install date from site_info for {user.email}: {install_date}")
        except Exception as e:
            logger.warning(f"Could not auto-fetch install date: {e}")

    # Get individual batteries from latest history entry
    individual_batteries = None
    latest_history = BatteryHealthHistory.query.filter_by(
        user_id=user.id
    ).order_by(BatteryHealthHistory.scanned_at.desc()).first()
    if latest_history and latest_history.pack_data:
        try:
            import json
            pack_data = json.loads(latest_history.pack_data)
            individual_batteries = pack_data.get('individual_batteries')
        except (json.JSONDecodeError, TypeError):
            pass

    return jsonify({
        'has_data': True,
        'originalCapacityWh': user.battery_original_capacity_wh,
        'currentCapacityWh': user.battery_current_capacity_wh,
        'degradationPercent': user.battery_degradation_percent,
        'batteryCount': user.battery_count,
        'individualBatteries': individual_batteries,
        'updatedAt': user.battery_health_updated.isoformat() if user.battery_health_updated else None,
        'installDate': install_date.isoformat() if install_date else None
    })


@bp.route('/api/battery-health/history', methods=['GET'])
@login_required
def api_battery_health_history():
    """
    Get battery health history for degradation graph.

    Returns all historical readings sorted by scan date.
    """
    history = BatteryHealthHistory.query.filter_by(
        user_id=current_user.id
    ).order_by(BatteryHealthHistory.scanned_at.asc()).all()

    # Build data points for the graph
    data_points = []
    for record in history:
        data_points.append({
            'date': record.scanned_at.isoformat(),
            'healthPercent': round(record.health_percent, 2),
            'degradationPercent': round(record.degradation_percent, 2),
            'actualCapacityWh': record.actual_capacity_wh,
            'ratedCapacityWh': record.rated_capacity_wh,
            'batteryCount': record.battery_count
        })

    # Add install date as the starting point (100% health)
    install_date = current_user.powerwall_install_date
    rated_capacity = current_user.battery_original_capacity_wh
    battery_count = current_user.battery_count or 1

    return jsonify({
        'installDate': install_date.isoformat() if install_date else None,
        'ratedCapacityWh': rated_capacity,
        'batteryCount': battery_count,
        'history': data_points
    })


@bp.route('/api/battery-health/install-date', methods=['POST'])
@login_required
def api_battery_health_set_install_date():
    """
    Set the Powerwall installation date.

    Request body:
    {
        "installDate": "2024-01-15"
    }
    """
    data = request.get_json()
    if not data or not data.get('installDate'):
        return jsonify({'error': 'installDate is required'}), 400

    try:
        from datetime import date
        install_date = date.fromisoformat(data['installDate'])
        current_user.powerwall_install_date = install_date
        db.session.commit()

        logger.info(f"Set Powerwall install date for {current_user.email}: {install_date}")

        return jsonify({
            'status': 'ok',
            'message': 'Install date saved',
            'installDate': install_date.isoformat()
        })
    except ValueError as e:
        return jsonify({'error': f'Invalid date format: {e}'}), 400
    except Exception as e:
        logger.error(f"Error setting install date: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/battery-health/generate-token', methods=['POST'])
@login_required
def api_battery_health_generate_token():
    """
    Generate a new API token for battery health sync from mobile app.
    This replaces any existing token.
    """
    try:
        # Generate a secure random token
        new_token = secrets.token_urlsafe(32)
        current_user.battery_health_api_token = new_token
        db.session.commit()

        logger.info(f"Generated new battery health API token for user {current_user.email}")

        return jsonify({
            'success': True,
            'token': new_token,
            'message': 'New API token generated. Save this token - it will only be shown once.'
        })

    except Exception as e:
        logger.error(f"Error generating battery health token: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/battery-health/revoke-token', methods=['POST'])
@login_required
def api_battery_health_revoke_token():
    """
    Revoke the current API token for battery health sync.
    """
    try:
        current_user.battery_health_api_token = None
        db.session.commit()

        logger.info(f"Revoked battery health API token for user {current_user.email}")

        return jsonify({
            'success': True,
            'message': 'API token revoked'
        })

    except Exception as e:
        logger.error(f"Error revoking battery health token: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/battery-health/from-cloud', methods=['GET'])
def api_battery_health_from_cloud():
    """
    Fetch actual battery capacity from Tesla Fleet API / Teslemetry.

    This endpoint provides the real battery capacity data that the local
    TEDAPI cannot access (requires signed payloads).

    Authentication: Bearer token in Authorization header (same as battery-health POST)

    Response:
    {
        "success": true,
        "totalPackEnergyWh": 43200,      # total_pack_energy from live_status
        "energyLeftWh": 41500,            # energy_left from live_status
        "percentageCharged": 96.1,        # percentage_charged
        "batteryCount": 3,                # from site_info
        "nominalSystemEnergyWh": 40500,   # from site_info (rated capacity)
        "degradationPercent": 0.0,        # calculated: (1 - energyLeft/totalPack) * 100
        "dataSource": "teslemetry"        # or "fleet_api"
    }
    """
    from app.api_clients import get_tesla_client

    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401

    token = auth_header.split(' ', 1)[1]
    if not token:
        return jsonify({'success': False, 'error': 'Empty token'}), 401

    # Find user by API token
    user = User.query.filter_by(battery_health_api_token=token).first()
    if not user:
        return jsonify({'success': False, 'error': 'Invalid API token'}), 401

    # Get Tesla API client for this user
    tesla_client = get_tesla_client(user)
    if not tesla_client:
        return jsonify({
            'success': False,
            'error': 'Tesla API not configured. Please set up Fleet API or Teslemetry in settings.'
        }), 400

    try:
        # Get energy sites
        energy_sites = tesla_client.get_energy_sites()
        if not energy_sites:
            return jsonify({
                'success': False,
                'error': 'No energy sites found in Tesla account'
            }), 404

        # Use first energy site (or user's selected site if configured)
        site_id = user.tesla_energy_site_id or energy_sites[0].get('energy_site_id')

        # Get live status for battery capacity data
        live_status = tesla_client.get_site_status(site_id)
        if not live_status:
            return jsonify({
                'success': False,
                'error': 'Failed to fetch site status from Tesla API'
            }), 500

        # Get site info for additional details
        site_info = tesla_client.get_site_info(site_id)

        # Extract capacity data from live_status
        total_pack_energy = live_status.get('total_pack_energy', 0)  # Wh
        energy_left = live_status.get('energy_left', 0)  # Wh
        percentage_charged = live_status.get('percentage_charged', 0)

        # Get battery count from site_info if available
        battery_count = 1
        nominal_system_energy = 0
        if site_info:
            # Count powerwalls from components or backup capability
            battery_count = site_info.get('backup_reserve_percent', 0) > 0 and 1 or 1
            # Get nominal capacity from site_info
            nominal_system_energy = site_info.get('nominal_system_energy_kWh', 0) * 1000  # Convert to Wh
            if nominal_system_energy == 0:
                nominal_system_energy = site_info.get('backup_capability_kWh', 0) * 1000

            # Try to get battery count from components
            components = site_info.get('components', {})
            if components:
                battery_count = components.get('battery_count', 1) or 1

        # Calculate degradation if we have total pack energy
        degradation_percent = 0.0
        if total_pack_energy > 0 and energy_left > 0:
            # At 100% charge, energy_left should equal total_pack_energy
            # If percentage_charged is 100 and energy_left < total_pack_energy, that's degradation
            # But normally we calculate based on nominal vs actual capacity
            if nominal_system_energy > 0:
                # Compare actual capacity to nominal (rated) capacity
                actual_capacity = total_pack_energy
                degradation_percent = max(0, (1 - actual_capacity / nominal_system_energy) * 100)

        # Determine data source
        data_source = 'fleet_api' if hasattr(tesla_client, 'refresh_token') else 'teslemetry'

        logger.info(f"Battery health from cloud for user {user.email}: total={total_pack_energy}Wh, left={energy_left}Wh, charged={percentage_charged}%")

        return jsonify({
            'success': True,
            'totalPackEnergyWh': total_pack_energy,
            'energyLeftWh': energy_left,
            'percentageCharged': percentage_charged,
            'batteryCount': battery_count,
            'nominalSystemEnergyWh': nominal_system_energy,
            'degradationPercent': round(degradation_percent, 2),
            'dataSource': data_source,
            'siteId': site_id
        })

    except Exception as e:
        logger.error(f"Error fetching battery health from cloud: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# ============================================================================
# Push Notification Endpoints
# ============================================================================

@bp.route('/api/push/register', methods=['POST'])
def api_push_register():
    """
    Register device for push notifications.
    Supports both Expo Push tokens and native APNs tokens.

    Authentication: Bearer token in Authorization header

    Request body:
    {
        "push_token": "ExponentPushToken[...] or native-device-token",
        "platform": "ios" or "android",
        "device_name": "optional device name"
    }

    Response:
    {
        "success": true,
        "message": "Device registered for push notifications"
    }
    """
    from app.route_helpers import get_api_user

    user = get_api_user()
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    # Accept both 'push_token' (new) and 'deviceToken' (old) for backwards compatibility
    device_token = data.get('push_token') or data.get('deviceToken')
    platform = data.get('platform', 'ios')
    device_name = data.get('device_name', 'Unknown device')

    if not device_token:
        return jsonify({'success': False, 'error': 'push_token is required'}), 400

    try:
        user.apns_device_token = device_token
        user.push_notifications_enabled = True
        db.session.commit()

        token_type = "Expo" if device_token.startswith("ExponentPushToken") else "native"
        logger.info(f"Registered {token_type} push token for user {user.email} ({platform}/{device_name}): {device_token[:30]}...")

        return jsonify({
            'success': True,
            'message': 'Device registered for push notifications'
        })

    except Exception as e:
        logger.error(f"Error registering push token: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/push/settings', methods=['GET', 'POST'])
def api_push_settings():
    """
    Get or update push notification settings.

    Authentication: Bearer token in Authorization header

    GET Response:
    {
        "enabled": true,
        "firmwareUpdates": true
    }

    POST Request:
    {
        "enabled": true,
        "firmwareUpdates": true
    }
    """
    from app.route_helpers import get_api_user

    user = get_api_user()
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        return jsonify({
            'enabled': user.push_notifications_enabled if user.push_notifications_enabled is not None else True,
            'firmwareUpdates': user.notify_firmware_updates if user.notify_firmware_updates is not None else True
        })

    # POST - update settings
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    try:
        if 'enabled' in data:
            user.push_notifications_enabled = bool(data['enabled'])
        if 'firmwareUpdates' in data:
            user.notify_firmware_updates = bool(data['firmwareUpdates'])

        db.session.commit()

        logger.info(f"Updated push settings for {user.email}: enabled={user.push_notifications_enabled}, firmware={user.notify_firmware_updates}")

        return jsonify({
            'success': True,
            'enabled': user.push_notifications_enabled,
            'firmwareUpdates': user.notify_firmware_updates
        })

    except Exception as e:
        logger.error(f"Error updating push settings: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/firmware/status', methods=['GET'])
def api_firmware_status():
    """
    Get current Powerwall firmware version.

    Authentication: Bearer token in Authorization header

    Response:
    {
        "version": "25.10.1",
        "lastChecked": "2024-12-21T12:00:00Z"
    }
    """
    from app.route_helpers import get_api_user
    from app.api_clients import get_tesla_client

    user = get_api_user()
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    # Return cached version first
    cached_version = user.powerwall_firmware_version
    last_checked = user.powerwall_firmware_updated

    # Try to get fresh version from Tesla API
    if user.tesla_energy_site_id:
        try:
            tesla_client = get_tesla_client(user)
            if tesla_client:
                site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
                if site_info and site_info.get('version'):
                    current_version = site_info.get('version')

                    # Check for change and notify
                    from app.push_notifications import check_and_notify_firmware_change
                    check_and_notify_firmware_change(user, current_version)

                    return jsonify({
                        'version': current_version,
                        'lastChecked': datetime.utcnow().isoformat() + 'Z'
                    })
        except Exception as e:
            logger.warning(f"Error fetching firmware version: {e}")

    # Fall back to cached version
    return jsonify({
        'version': cached_version,
        'lastChecked': last_checked.isoformat() + 'Z' if last_checked else None
    })


@bp.route('/api/powerwall/register-key', methods=['POST'])
def api_powerwall_register_key():
    """
    Register mobile app's RSA public key with Powerwall via Tesla Fleet API.

    This is required for Firmware 25.10+ to authenticate local TEDAPI requests.
    The mobile app generates an RSA keypair and sends the public key to this endpoint.
    This endpoint then calls Tesla Fleet API to register the key with the Powerwall.

    Authentication: Bearer token in Authorization header (same as battery-health POST)

    Request body:
    {
        "publicKey": "base64-encoded-DER-public-key"
    }

    Response:
    {
        "success": true,
        "message": "Key registration initiated",
        "requiresAcceptance": true  # User may need to accept on Powerwall
    }
    """
    from app.api_clients import get_tesla_client

    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401

    token = auth_header.split(' ', 1)[1]
    if not token:
        return jsonify({'success': False, 'error': 'Empty token'}), 401

    # Find user by API token
    user = User.query.filter_by(battery_health_api_token=token).first()
    if not user:
        return jsonify({'success': False, 'error': 'Invalid API token'}), 401

    # Get request data
    data = request.get_json()
    if not data or not data.get('publicKey'):
        return jsonify({'success': False, 'error': 'Missing publicKey in request body'}), 400

    public_key_base64 = data.get('publicKey')

    # Get Tesla API client for this user
    tesla_client = get_tesla_client(user)
    if not tesla_client:
        return jsonify({
            'success': False,
            'error': 'Tesla API not configured. Please set up Fleet API or Teslemetry in settings.'
        }), 400

    try:
        # Get energy site ID
        site_id = user.tesla_energy_site_id
        if not site_id:
            energy_sites = tesla_client.get_energy_sites()
            if not energy_sites:
                return jsonify({
                    'success': False,
                    'error': 'No energy sites found in Tesla account'
                }), 404
            site_id = energy_sites[0].get('energy_site_id')

        logger.info(f"Registering public key for user {user.email}, site {site_id}")
        logger.info(f"Public key (first 50 chars): {public_key_base64[:50]}...")

        # Call Tesla Fleet API to register the key with the Powerwall
        result = tesla_client.add_authorized_client(site_id, public_key_base64)

        if result and result.get('success'):
            return jsonify({
                'success': True,
                'message': 'Key registration request sent to Powerwall.',
                'requiresAcceptance': True,
                'acceptanceInstructions': 'To accept the key on your Powerwall 3: Toggle the power switch on the Gateway OFF, wait 5 seconds, then turn it back ON. This must be done within 30 seconds of registration.',
                'response': result.get('response', {})
            })
        else:
            error_msg = result.get('error', 'Unknown error') if result else 'No response from Tesla API'
            return jsonify({
                'success': False,
                'error': f'Failed to register key: {error_msg}'
            }), 400

    except Exception as e:
        logger.error(f"Error registering public key: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/powerwall/authorized-clients', methods=['GET'])
def list_authorized_clients():
    """
    List authorized clients registered with the Powerwall.
    Shows key state: 1 = pending acceptance, 3 = accepted

    Authentication: Bearer token in Authorization header
    """
    from app.api_clients import get_tesla_client

    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401

    token = auth_header.split(' ', 1)[1]
    if not token:
        return jsonify({'success': False, 'error': 'Empty token'}), 401

    # Find user by API token
    user = User.query.filter_by(battery_health_api_token=token).first()
    if not user:
        return jsonify({'success': False, 'error': 'Invalid API token'}), 401

    tesla_client = get_tesla_client(user)
    if not tesla_client:
        return jsonify({
            'success': False,
            'error': 'Tesla API not configured'
        }), 400

    try:
        site_id = user.tesla_energy_site_id
        if not site_id:
            energy_sites = tesla_client.get_energy_sites()
            if not energy_sites:
                return jsonify({
                    'success': False,
                    'error': 'No energy sites found'
                }), 404
            site_id = energy_sites[0].get('energy_site_id')

        result = tesla_client.list_authorized_clients(site_id)

        if result and result.get('success'):
            clients = result.get('clients', [])
            return jsonify({
                'success': True,
                'clients': clients,
                'keyStates': {
                    1: 'pending acceptance (toggle Powerwall power switch)',
                    3: 'accepted and ready to use'
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Failed to list clients') if result else 'No response'
            }), 400

    except Exception as e:
        logger.error(f"Error listing authorized clients: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/health')
def api_health():
    """
    Simple health check endpoint for the mobile app to test connectivity.
    No authentication required.
    """
    return jsonify({
        'status': 'ok',
        'service': 'PowerSync',
        'timestamp': datetime.utcnow().isoformat()
    })


# ============================================
# SIGENERGY CONTROL API (for mobile app)
# ============================================

@bp.route('/api/sigenergy/settings')
@login_required
def api_sigenergy_settings():
    """
    Get current Sigenergy control settings/limits.

    Returns JSON:
    {
        "success": true,
        "charge_rate_limit_kw": 10.0,
        "discharge_rate_limit_kw": 10.0,
        "export_limit_kw": null,  // null = unlimited
        "solar_curtailment_enabled": false
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    # Check if user has Sigenergy configured
    if user.battery_system != 'sigenergy':
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured as battery system'
        }), 400

    if not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy Modbus host not configured'
        }), 400

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        limits = client.get_current_limits()
        if 'error' in limits:
            return jsonify({
                'success': False,
                'error': limits['error']
            }), 500

        return jsonify({
            'success': True,
            'charge_rate_limit_kw': limits.get('charge_rate_limit_kw'),
            'discharge_rate_limit_kw': limits.get('discharge_rate_limit_kw'),
            'export_limit_kw': limits.get('export_limit_kw'),
            'solar_curtailment_enabled': user.solar_curtailment_enabled or False
        })

    except Exception as e:
        logger.error(f"Error getting Sigenergy settings: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/set-charge-rate', methods=['POST'])
@login_required
def api_sigenergy_set_charge_rate():
    """
    Set Sigenergy battery charge rate limit.

    Request JSON:
    {
        "limit_kw": 5.0  // 0-10 kW
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    data = request.get_json() or {}
    limit_kw = data.get('limit_kw')

    if limit_kw is None:
        return jsonify({
            'success': False,
            'error': 'limit_kw is required'
        }), 400

    try:
        limit_kw = float(limit_kw)
        if limit_kw < 0 or limit_kw > 15:
            return jsonify({
                'success': False,
                'error': 'limit_kw must be between 0 and 15'
            }), 400
    except (ValueError, TypeError):
        return jsonify({
            'success': False,
            'error': 'limit_kw must be a number'
        }), 400

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        success = client.set_charge_rate_limit(limit_kw)

        if success:
            logger.info(f"Set Sigenergy charge rate to {limit_kw} kW for {user.email}")
            return jsonify({
                'success': True,
                'message': f'Charge rate limit set to {limit_kw} kW'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to set charge rate limit via Modbus'
            }), 500

    except Exception as e:
        logger.error(f"Error setting Sigenergy charge rate: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/set-discharge-rate', methods=['POST'])
@login_required
def api_sigenergy_set_discharge_rate():
    """
    Set Sigenergy battery discharge rate limit.

    Request JSON:
    {
        "limit_kw": 5.0  // 0-10 kW
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    data = request.get_json() or {}
    limit_kw = data.get('limit_kw')

    if limit_kw is None:
        return jsonify({
            'success': False,
            'error': 'limit_kw is required'
        }), 400

    try:
        limit_kw = float(limit_kw)
        if limit_kw < 0 or limit_kw > 15:
            return jsonify({
                'success': False,
                'error': 'limit_kw must be between 0 and 15'
            }), 400
    except (ValueError, TypeError):
        return jsonify({
            'success': False,
            'error': 'limit_kw must be a number'
        }), 400

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        success = client.set_discharge_rate_limit(limit_kw)

        if success:
            logger.info(f"Set Sigenergy discharge rate to {limit_kw} kW for {user.email}")
            return jsonify({
                'success': True,
                'message': f'Discharge rate limit set to {limit_kw} kW'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to set discharge rate limit via Modbus'
            }), 500

    except Exception as e:
        logger.error(f"Error setting Sigenergy discharge rate: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/set-export-limit', methods=['POST'])
@login_required
def api_sigenergy_set_export_limit():
    """
    Set Sigenergy grid export limit.

    Request JSON:
    {
        "limit_kw": 5.0,  // 0-10 kW, or null for unlimited
        "unlimited": false  // If true, sets unlimited export
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    data = request.get_json() or {}
    unlimited = data.get('unlimited', False)
    limit_kw = data.get('limit_kw')

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        if unlimited:
            success = client.restore_export_limit()
            message = 'Export limit set to unlimited'
        else:
            if limit_kw is None:
                return jsonify({
                    'success': False,
                    'error': 'limit_kw is required when unlimited is false'
                }), 400

            try:
                limit_kw = float(limit_kw)
                if limit_kw < 0 or limit_kw > 15:
                    return jsonify({
                        'success': False,
                        'error': 'limit_kw must be between 0 and 15'
                    }), 400
            except (ValueError, TypeError):
                return jsonify({
                    'success': False,
                    'error': 'limit_kw must be a number'
                }), 400

            success = client.set_export_limit(limit_kw)
            message = f'Export limit set to {limit_kw} kW'

        if success:
            logger.info(f"Set Sigenergy export limit for {user.email}: {message}")
            return jsonify({
                'success': True,
                'message': message
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to set export limit via Modbus'
            }), 500

    except Exception as e:
        logger.error(f"Error setting Sigenergy export limit: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/force-charge', methods=['POST'])
@login_required
def api_sigenergy_force_charge():
    """
    Force charge mode for Sigenergy - maximizes grid charging.

    Strategy:
    - Set export limit to 0 kW (prevent export)
    - Set charge rate to max (10 kW)
    - Set discharge rate to 0 kW (prevent discharge)

    Request JSON:
    {
        "duration_minutes": 30  // Optional: 15, 30, 45, 60, 75, 90, 105, 120. Default: 30
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    data = request.get_json() or {}
    duration_minutes = data.get('duration_minutes', 30)

    try:
        duration_minutes = int(duration_minutes)
    except (ValueError, TypeError):
        duration_minutes = 30

    valid_durations = [15, 30, 45, 60, 75, 90, 105, 120]
    if duration_minutes not in valid_durations:
        return jsonify({
            'success': False,
            'error': f'Invalid duration. Must be one of: {valid_durations}'
        }), 400

    logger.info(f"Sigenergy force charge requested by {user.email} for {duration_minutes} minutes")

    # Check if already in charge mode
    if user.manual_charge_active:
        new_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
        user.manual_charge_expires_at = new_expires_at
        db_commit_with_retry()
        logger.info(f"Extended Sigenergy charge mode to {new_expires_at}")
        return jsonify({
            'success': True,
            'message': f'Charge mode extended for {duration_minutes} minutes',
            'expires_at': new_expires_at.isoformat() + 'Z',
            'already_active': True
        })

    from app.sigenergy_modbus import get_sigenergy_modbus_client, SigenergyModbusClient

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        # Force charge strategy:
        # 1. Set export to 0 (prevent solar export)
        # 2. Set charge rate to max
        # 3. Set discharge rate to 0 (prevent battery discharge)

        export_success = client.set_export_limit(0)
        if not export_success:
            return jsonify({
                'success': False,
                'error': 'Failed to set export limit to 0'
            }), 500

        charge_success = client.set_charge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)
        if not charge_success:
            # Try to restore export limit
            client.restore_export_limit()
            return jsonify({
                'success': False,
                'error': 'Failed to set charge rate to max'
            }), 500

        discharge_success = client.set_discharge_rate_limit(0)
        if not discharge_success:
            # Try to restore defaults
            client.restore_export_limit()
            client.set_charge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)
            return jsonify({
                'success': False,
                'error': 'Failed to set discharge rate to 0'
            }), 500

        # All successful - update user state
        expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
        user.manual_charge_active = True
        user.manual_charge_expires_at = expires_at
        user.manual_discharge_active = False  # Clear any discharge mode
        user.manual_discharge_expires_at = None
        db_commit_with_retry()

        logger.info(f"Sigenergy force charge activated for {user.email} until {expires_at}")

        return jsonify({
            'success': True,
            'message': f'Force charge activated for {duration_minutes} minutes',
            'expires_at': expires_at.isoformat() + 'Z',
            'duration_minutes': duration_minutes
        })

    except Exception as e:
        logger.error(f"Error in Sigenergy force charge: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/force-discharge', methods=['POST'])
@login_required
def api_sigenergy_force_discharge():
    """
    Force discharge mode for Sigenergy - maximizes battery discharge to grid.

    Strategy:
    - Set export limit to unlimited
    - Set discharge rate to max (10 kW)
    - Set charge rate to 0 kW (prevent charging)

    Request JSON:
    {
        "duration_minutes": 30  // Optional: 15, 30, 45, 60, 75, 90, 105, 120. Default: 30
    }
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    data = request.get_json() or {}
    duration_minutes = data.get('duration_minutes', 30)

    try:
        duration_minutes = int(duration_minutes)
    except (ValueError, TypeError):
        duration_minutes = 30

    valid_durations = [15, 30, 45, 60, 75, 90, 105, 120]
    if duration_minutes not in valid_durations:
        return jsonify({
            'success': False,
            'error': f'Invalid duration. Must be one of: {valid_durations}'
        }), 400

    logger.info(f"Sigenergy force discharge requested by {user.email} for {duration_minutes} minutes")

    # Check if already in discharge mode
    if user.manual_discharge_active:
        new_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
        user.manual_discharge_expires_at = new_expires_at
        db_commit_with_retry()
        logger.info(f"Extended Sigenergy discharge mode to {new_expires_at}")
        return jsonify({
            'success': True,
            'message': f'Discharge mode extended for {duration_minutes} minutes',
            'expires_at': new_expires_at.isoformat() + 'Z',
            'already_active': True
        })

    from app.sigenergy_modbus import get_sigenergy_modbus_client, SigenergyModbusClient

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        # Force discharge strategy:
        # 1. Set export to unlimited (allow all export)
        # 2. Set discharge rate to max
        # 3. Set charge rate to 0 (prevent charging from grid)

        export_success = client.restore_export_limit()  # Unlimited
        if not export_success:
            return jsonify({
                'success': False,
                'error': 'Failed to set export limit to unlimited'
            }), 500

        discharge_success = client.set_discharge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)
        if not discharge_success:
            return jsonify({
                'success': False,
                'error': 'Failed to set discharge rate to max'
            }), 500

        charge_success = client.set_charge_rate_limit(0)
        if not charge_success:
            # Try to restore discharge rate
            client.set_discharge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)
            return jsonify({
                'success': False,
                'error': 'Failed to set charge rate to 0'
            }), 500

        # All successful - update user state
        expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
        user.manual_discharge_active = True
        user.manual_discharge_expires_at = expires_at
        user.manual_charge_active = False  # Clear any charge mode
        user.manual_charge_expires_at = None
        db_commit_with_retry()

        logger.info(f"Sigenergy force discharge activated for {user.email} until {expires_at}")

        return jsonify({
            'success': True,
            'message': f'Force discharge activated for {duration_minutes} minutes',
            'expires_at': expires_at.isoformat() + 'Z',
            'duration_minutes': duration_minutes
        })

    except Exception as e:
        logger.error(f"Error in Sigenergy force discharge: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/api/sigenergy/restore-normal', methods=['POST'])
@login_required
def api_sigenergy_restore_normal():
    """
    Restore normal operation for Sigenergy - sets all limits to defaults.

    Restores:
    - Export limit to unlimited
    - Charge rate to max (10 kW)
    - Discharge rate to max (10 kW)
    """
    from app.route_helpers import get_api_user
    user = get_api_user()
    if not user and current_user.is_authenticated:
        user = current_user
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    if user.battery_system != 'sigenergy' or not user.sigenergy_modbus_host:
        return jsonify({
            'success': False,
            'error': 'Sigenergy not configured'
        }), 400

    logger.info(f"Sigenergy restore normal requested by {user.email}")

    # Check if already in normal mode
    was_in_discharge = user.manual_discharge_active
    was_in_charge = getattr(user, 'manual_charge_active', False)

    if not was_in_discharge and not was_in_charge:
        return jsonify({
            'success': True,
            'message': 'Already in normal operation',
            'method': 'none'
        })

    from app.sigenergy_modbus import get_sigenergy_modbus_client, SigenergyModbusClient

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            return jsonify({
                'success': False,
                'error': 'Failed to create Sigenergy client'
            }), 500

        # Restore all limits to defaults
        export_success = client.restore_export_limit()
        charge_success = client.set_charge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)
        discharge_success = client.set_discharge_rate_limit(SigenergyModbusClient.DEFAULT_MAX_RATE_KW)

        if not all([export_success, charge_success, discharge_success]):
            return jsonify({
                'success': False,
                'error': 'Failed to restore some limits via Modbus',
                'export_restored': export_success,
                'charge_restored': charge_success,
                'discharge_restored': discharge_success
            }), 500

        # Clear all manual mode flags
        user.manual_discharge_active = False
        user.manual_discharge_expires_at = None
        user.manual_charge_active = False
        user.manual_charge_expires_at = None
        db_commit_with_retry()

        logger.info(f"Sigenergy normal operation restored for {user.email}")

        return jsonify({
            'success': True,
            'message': 'Normal operation restored',
            'method': 'modbus_restore',
            'was_in_discharge': was_in_discharge,
            'was_in_charge': was_in_charge
        })

    except Exception as e:
        logger.error(f"Error in Sigenergy restore normal: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# ============================================================================
# AUTOMATIONS API ENDPOINTS
# ============================================================================

@bp.route('/api/automations', methods=['GET'])
@api_auth_required
def api_get_automations(api_user=None):
    """
    Get all automations for the authenticated user.

    Returns:
        JSON array of automations with triggers and actions
    """
    from app.models import Automation
    import json

    user = api_user or current_user

    automations = Automation.query.filter_by(user_id=user.id).order_by(
        Automation.group_name,
        Automation.priority.desc(),
        Automation.created_at.desc()
    ).all()

    result = []
    for auto in automations:
        auto_data = {
            'id': auto.id,
            'name': auto.name,
            'group_name': auto.group_name,
            'priority': auto.priority,
            'enabled': auto.enabled,
            'run_once': auto.run_once,
            'paused': auto.paused,
            'notification_only': auto.notification_only,
            'created_at': auto.created_at.isoformat() if auto.created_at else None,
            'updated_at': auto.updated_at.isoformat() if auto.updated_at else None,
            'last_triggered_at': auto.last_triggered_at.isoformat() if auto.last_triggered_at else None,
            'trigger': None,
            'actions': []
        }

        # Add trigger
        if auto.trigger:
            t = auto.trigger
            auto_data['trigger'] = {
                'trigger_type': t.trigger_type,
                'time_of_day': t.time_of_day.strftime('%H:%M') if t.time_of_day else None,
                'repeat_days': t.repeat_days,
                'battery_condition': t.battery_condition,
                'battery_threshold': t.battery_threshold,
                'flow_source': t.flow_source,
                'flow_transition': t.flow_transition,
                'flow_threshold_kw': t.flow_threshold_kw,
                'price_type': t.price_type,
                'price_transition': t.price_transition,
                'price_threshold': t.price_threshold,
                'grid_condition': t.grid_condition,
                'weather_condition': t.weather_condition,
                'time_window_start': t.time_window_start.strftime('%H:%M') if t.time_window_start else None,
                'time_window_end': t.time_window_end.strftime('%H:%M') if t.time_window_end else None,
            }

        # Add actions
        for action in auto.actions.order_by('execution_order').all():
            auto_data['actions'].append({
                'id': action.id,
                'action_type': action.action_type,
                'parameters': json.loads(action.parameters) if action.parameters else {},
                'execution_order': action.execution_order
            })

        result.append(auto_data)

    return jsonify(result)


@bp.route('/api/automations', methods=['POST'])
@api_auth_required
def api_create_automation(api_user=None):
    """
    Create a new automation.

    Request body:
    {
        "name": "My Automation",
        "group_name": "Default Group",
        "priority": 50,
        "enabled": true,
        "run_once": false,
        "notification_only": false,
        "trigger": {
            "trigger_type": "price",
            "price_type": "import",
            "price_transition": "rises_above",
            "price_threshold": 0.30,
            "time_window_start": "14:00",
            "time_window_end": "21:00"
        },
        "actions": [
            {
                "action_type": "force_discharge",
                "parameters": {"duration_minutes": 30}
            }
        ]
    }
    """
    from app.models import Automation, AutomationTrigger, AutomationAction
    import json

    user = api_user or current_user
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    # Create automation
    automation = Automation(
        user_id=user.id,
        name=data.get('name', 'Unnamed Automation'),
        group_name=data.get('group_name', 'Default Group'),
        priority=data.get('priority', 50),
        enabled=data.get('enabled', True),
        run_once=data.get('run_once', False),
        notification_only=data.get('notification_only', False),
    )
    db.session.add(automation)
    db.session.flush()  # Get the ID

    # Create trigger
    trigger_data = data.get('trigger', {})
    if trigger_data:
        trigger = AutomationTrigger(
            automation_id=automation.id,
            trigger_type=trigger_data.get('trigger_type', 'time'),
        )

        # Time trigger fields
        if trigger_data.get('time_of_day'):
            try:
                trigger.time_of_day = datetime.strptime(trigger_data['time_of_day'], '%H:%M').time()
            except ValueError:
                pass
        trigger.repeat_days = trigger_data.get('repeat_days')

        # Battery trigger fields
        trigger.battery_condition = trigger_data.get('battery_condition')
        trigger.battery_threshold = trigger_data.get('battery_threshold')

        # Flow trigger fields
        trigger.flow_source = trigger_data.get('flow_source')
        trigger.flow_transition = trigger_data.get('flow_transition')
        trigger.flow_threshold_kw = trigger_data.get('flow_threshold_kw')

        # Price trigger fields
        trigger.price_type = trigger_data.get('price_type')
        trigger.price_transition = trigger_data.get('price_transition')
        trigger.price_threshold = trigger_data.get('price_threshold')

        # Grid trigger fields
        trigger.grid_condition = trigger_data.get('grid_condition')

        # Weather trigger fields
        trigger.weather_condition = trigger_data.get('weather_condition')

        # Time window
        if trigger_data.get('time_window_start'):
            try:
                trigger.time_window_start = datetime.strptime(trigger_data['time_window_start'], '%H:%M').time()
            except ValueError:
                pass
        if trigger_data.get('time_window_end'):
            try:
                trigger.time_window_end = datetime.strptime(trigger_data['time_window_end'], '%H:%M').time()
            except ValueError:
                pass

        db.session.add(trigger)

    # Create actions
    actions_data = data.get('actions', [])
    for idx, action_data in enumerate(actions_data):
        action = AutomationAction(
            automation_id=automation.id,
            action_type=action_data.get('action_type'),
            parameters=json.dumps(action_data.get('parameters', {})),
            execution_order=action_data.get('execution_order', idx)
        )
        db.session.add(action)

    db_commit_with_retry()

    logger.info(f"Created automation '{automation.name}' (id={automation.id}) for user {user.email}")

    return jsonify({
        'success': True,
        'id': automation.id,
        'message': f"Automation '{automation.name}' created"
    }), 201


@bp.route('/api/automations/<int:automation_id>', methods=['GET'])
@api_auth_required
def api_get_automation(automation_id, api_user=None):
    """Get a specific automation by ID."""
    from app.models import Automation
    import json

    user = api_user or current_user

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    result = {
        'id': automation.id,
        'name': automation.name,
        'group_name': automation.group_name,
        'priority': automation.priority,
        'enabled': automation.enabled,
        'run_once': automation.run_once,
        'paused': automation.paused,
        'notification_only': automation.notification_only,
        'created_at': automation.created_at.isoformat() if automation.created_at else None,
        'updated_at': automation.updated_at.isoformat() if automation.updated_at else None,
        'last_triggered_at': automation.last_triggered_at.isoformat() if automation.last_triggered_at else None,
        'trigger': None,
        'actions': []
    }

    if automation.trigger:
        t = automation.trigger
        result['trigger'] = {
            'trigger_type': t.trigger_type,
            'time_of_day': t.time_of_day.strftime('%H:%M') if t.time_of_day else None,
            'repeat_days': t.repeat_days,
            'battery_condition': t.battery_condition,
            'battery_threshold': t.battery_threshold,
            'flow_source': t.flow_source,
            'flow_transition': t.flow_transition,
            'flow_threshold_kw': t.flow_threshold_kw,
            'price_type': t.price_type,
            'price_transition': t.price_transition,
            'price_threshold': t.price_threshold,
            'grid_condition': t.grid_condition,
            'weather_condition': t.weather_condition,
            'time_window_start': t.time_window_start.strftime('%H:%M') if t.time_window_start else None,
            'time_window_end': t.time_window_end.strftime('%H:%M') if t.time_window_end else None,
        }

    for action in automation.actions.order_by('execution_order').all():
        result['actions'].append({
            'id': action.id,
            'action_type': action.action_type,
            'parameters': json.loads(action.parameters) if action.parameters else {},
            'execution_order': action.execution_order
        })

    return jsonify(result)


@bp.route('/api/automations/<int:automation_id>', methods=['PUT'])
@api_auth_required
def api_update_automation(automation_id, api_user=None):
    """Update an existing automation."""
    from app.models import Automation, AutomationTrigger, AutomationAction
    import json

    user = api_user or current_user
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required'}), 400

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    # Update automation fields
    if 'name' in data:
        automation.name = data['name']
    if 'group_name' in data:
        automation.group_name = data['group_name']
    if 'priority' in data:
        automation.priority = data['priority']
    if 'enabled' in data:
        automation.enabled = data['enabled']
    if 'run_once' in data:
        automation.run_once = data['run_once']
    if 'notification_only' in data:
        automation.notification_only = data['notification_only']

    # Update trigger
    trigger_data = data.get('trigger')
    if trigger_data is not None:
        # Delete existing trigger
        if automation.trigger:
            db.session.delete(automation.trigger)

        if trigger_data:
            trigger = AutomationTrigger(
                automation_id=automation.id,
                trigger_type=trigger_data.get('trigger_type', 'time'),
            )

            if trigger_data.get('time_of_day'):
                try:
                    trigger.time_of_day = datetime.strptime(trigger_data['time_of_day'], '%H:%M').time()
                except ValueError:
                    pass
            trigger.repeat_days = trigger_data.get('repeat_days')
            trigger.battery_condition = trigger_data.get('battery_condition')
            trigger.battery_threshold = trigger_data.get('battery_threshold')
            trigger.flow_source = trigger_data.get('flow_source')
            trigger.flow_transition = trigger_data.get('flow_transition')
            trigger.flow_threshold_kw = trigger_data.get('flow_threshold_kw')
            trigger.price_type = trigger_data.get('price_type')
            trigger.price_transition = trigger_data.get('price_transition')
            trigger.price_threshold = trigger_data.get('price_threshold')
            trigger.grid_condition = trigger_data.get('grid_condition')
            trigger.weather_condition = trigger_data.get('weather_condition')

            if trigger_data.get('time_window_start'):
                try:
                    trigger.time_window_start = datetime.strptime(trigger_data['time_window_start'], '%H:%M').time()
                except ValueError:
                    pass
            if trigger_data.get('time_window_end'):
                try:
                    trigger.time_window_end = datetime.strptime(trigger_data['time_window_end'], '%H:%M').time()
                except ValueError:
                    pass

            db.session.add(trigger)

    # Update actions
    actions_data = data.get('actions')
    if actions_data is not None:
        # Delete existing actions
        for action in automation.actions.all():
            db.session.delete(action)

        # Add new actions
        for idx, action_data in enumerate(actions_data):
            action = AutomationAction(
                automation_id=automation.id,
                action_type=action_data.get('action_type'),
                parameters=json.dumps(action_data.get('parameters', {})),
                execution_order=action_data.get('execution_order', idx)
            )
            db.session.add(action)

    db_commit_with_retry()

    logger.info(f"Updated automation '{automation.name}' (id={automation.id}) for user {user.email}")

    return jsonify({
        'success': True,
        'message': f"Automation '{automation.name}' updated"
    })


@bp.route('/api/automations/<int:automation_id>', methods=['DELETE'])
@api_auth_required
def api_delete_automation(automation_id, api_user=None):
    """Delete an automation."""
    from app.models import Automation

    user = api_user or current_user

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    name = automation.name
    db.session.delete(automation)
    db_commit_with_retry()

    logger.info(f"Deleted automation '{name}' (id={automation_id}) for user {user.email}")

    return jsonify({
        'success': True,
        'message': f"Automation '{name}' deleted"
    })


@bp.route('/api/automations/<int:automation_id>/toggle', methods=['POST'])
@api_auth_required
def api_toggle_automation(automation_id, api_user=None):
    """Toggle automation enabled/disabled state."""
    from app.models import Automation

    user = api_user or current_user

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    automation.enabled = not automation.enabled
    db_commit_with_retry()

    state = 'enabled' if automation.enabled else 'disabled'
    logger.info(f"Automation '{automation.name}' {state} for user {user.email}")

    return jsonify({
        'success': True,
        'enabled': automation.enabled,
        'message': f"Automation '{automation.name}' {state}"
    })


@bp.route('/api/automations/<int:automation_id>/pause', methods=['POST'])
@api_auth_required
def api_pause_automation(automation_id, api_user=None):
    """Pause an automation (for run_once automations that have triggered)."""
    from app.models import Automation

    user = api_user or current_user

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    automation.paused = True
    db_commit_with_retry()

    return jsonify({
        'success': True,
        'message': f"Automation '{automation.name}' paused"
    })


@bp.route('/api/automations/<int:automation_id>/resume', methods=['POST'])
@api_auth_required
def api_resume_automation(automation_id, api_user=None):
    """Resume a paused automation."""
    from app.models import Automation

    user = api_user or current_user

    automation = Automation.query.filter_by(id=automation_id, user_id=user.id).first()
    if not automation:
        return jsonify({'error': 'Automation not found'}), 404

    automation.paused = False
    db_commit_with_retry()

    return jsonify({
        'success': True,
        'message': f"Automation '{automation.name}' resumed"
    })


@bp.route('/api/automations/groups', methods=['GET'])
@api_auth_required
def api_get_automation_groups(api_user=None):
    """Get list of unique automation group names for the user."""
    from app.models import Automation

    user = api_user or current_user

    groups = db.session.query(Automation.group_name).filter_by(
        user_id=user.id
    ).distinct().order_by(Automation.group_name).all()

    group_names = [g[0] for g in groups if g[0]]

    # Always include "Default Group"
    if 'Default Group' not in group_names:
        group_names.insert(0, 'Default Group')

    return jsonify(group_names)


@bp.route('/api/weather/current', methods=['GET'])
@api_auth_required
def api_get_current_weather(api_user=None):
    """Get current weather for the user's location."""
    from app.automations.weather import get_current_weather

    user = api_user or current_user

    weather = get_current_weather(user)
    if not weather:
        return jsonify({'error': 'Weather data unavailable'}), 503

    return jsonify(weather)
