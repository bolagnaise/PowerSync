# app/routes.py
from flask import render_template, flash, redirect, url_for, request, Blueprint, jsonify, session, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db, cache
from app.models import User, PriceRecord, SavedTOUProfile
from app.forms import LoginForm, RegistrationForm, SettingsForm, DemandChargeForm, AmberSettingsForm
from app.utils import encrypt_token, decrypt_token
from app.api_clients import get_amber_client, get_tesla_client
from app.scheduler import TOUScheduler
from app.route_helpers import (
    require_tesla_client,
    require_amber_client,
    require_tesla_site_id,
    db_transaction,
    start_background_task,
    restore_tariff_background
)
import os
import requests
import time
import logging
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode


# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


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
    # Check if we have a cached timezone in the session
    cache_key = f'powerwall_tz_{user.id}'
    if cache_key in session:
        return session[cache_key]

    # Try to fetch from Tesla API
    tesla_client = get_tesla_client(user)
    if tesla_client and user.tesla_energy_site_id:
        try:
            site_info = tesla_client.get_site_info(user.tesla_energy_site_id)
            if site_info:
                tz = site_info.get('installation_time_zone')
                if tz:
                    logger.info(f"Fetched Powerwall timezone from Tesla API: {tz}")
                    # Cache in session for this login session
                    session[cache_key] = tz
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
        login_user(user, remember=form.remember_me.data)
        return redirect(url_for('main.dashboard'))
    return render_template('login.html', title='Sign In', form=form, allow_registration=allow_registration)

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.login'))

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
    return render_template('dashboard.html', title='Dashboard', has_amber_token=has_amber_token,
                           solar_curtailment_enabled=current_user.solar_curtailment_enabled or False)


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

        if 'tesla_site_id' in submitted_fields and form.tesla_site_id.data:
            logger.info(f"Saving Tesla Site ID: {form.tesla_site_id.data}")
            current_user.tesla_energy_site_id = form.tesla_site_id.data

        # Handle Tesla API Provider selection
        if 'tesla_api_provider' in submitted_fields and form.tesla_api_provider.data:
            logger.info(f"Saving Tesla API provider: {form.tesla_api_provider.data}")
            current_user.tesla_api_provider = form.tesla_api_provider.data

        # Handle Teslemetry API key (only if field was in the submitted form)
        if 'teslemetry_api_key' in submitted_fields:
            if form.teslemetry_api_key.data:
                logger.info("Encrypting and saving Teslemetry API key")
                current_user.teslemetry_api_key_encrypted = encrypt_token(form.teslemetry_api_key.data)
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

        try:
            db.session.commit()
            logger.info("Settings saved successfully to database")
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

    form.tesla_site_id.data = current_user.tesla_energy_site_id
    logger.debug(f"Tesla Site ID: {form.tesla_site_id.data}")

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
        if len(amber_sites) == 1:
            current_user.amber_site_id = amber_sites[0]['id']
            logger.info(f"Auto-selected single Amber site: {current_user.amber_site_id}")
        elif form.amber_site_id.data:
            current_user.amber_site_id = form.amber_site_id.data
            logger.info(f"User selected Amber site: {current_user.amber_site_id}")

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


@bp.route('/api/amber/current-price')
@login_required
@require_amber_client
def amber_current_price(amber_client):
    """Get current Amber electricity price using WebSocket (real-time) with REST API fallback"""
    logger.info(f"Current price requested by user: {current_user.email}")

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
                user_id=current_user.id,
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
            user_tz = ZoneInfo(get_powerwall_timezone(current_user))

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


@bp.route('/api/amber/5min-forecast')
@login_required
def amber_5min_forecast():
    """Get 5-minute interval forecast for the next few hours"""
    # Allow requesting more hours for the 30-min forecast view (default: 1 hour)
    hours = request.args.get('hours', 1, type=int)
    hours = min(hours, 4)  # Cap at 4 hours to avoid excessive API calls
    logger.info(f"5-minute forecast requested by user: {current_user.email} (hours={hours})")

    amber_client = get_amber_client(current_user)
    if not amber_client:
        logger.warning("Amber client not available for 5-min forecast")
        return jsonify({'error': 'Amber API not configured'}), 400

    # Get forecast data at 5-minute resolution
    forecast = amber_client.get_price_forecast(next_hours=hours, resolution=5)
    if not forecast:
        logger.error("Failed to fetch 5-minute forecast")
        return jsonify({'error': 'Failed to fetch 5-minute forecast'}), 500

    # Convert nemTime to user's local timezone for each interval
    user_tz = ZoneInfo(get_powerwall_timezone(current_user))

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
        'forecast_type': current_user.amber_forecast_type or 'predicted',
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


@bp.route('/api/tesla/status')
@login_required
@require_tesla_client
@require_tesla_site_id
@cache.cached(timeout=60, key_prefix=lambda: f'tesla_status_{current_user.id}')
def tesla_status(tesla_client):
    """Get Tesla Powerwall status including firmware version"""
    logger.info(f"Tesla status requested by user: {current_user.email}")

    # Get live status
    site_status = tesla_client.get_site_status(current_user.tesla_energy_site_id)
    if not site_status:
        logger.error("Failed to fetch Tesla site status")
        return jsonify({'error': 'Failed to fetch site status'}), 500

    # Get site info for firmware version
    site_info = tesla_client.get_site_info(current_user.tesla_energy_site_id)

    # Add firmware version to response if available
    if site_info:
        site_status['firmware_version'] = site_info.get('version', 'Unknown')
        logger.info(f"Firmware version: {site_status['firmware_version']}")

    return jsonify(site_status)


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
@login_required
def energy_history():
    """Get historical energy usage data for graphing"""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    logger.info(f"Energy history requested by user: {current_user.email}")

    # Get user's timezone
    user_tz = ZoneInfo(get_powerwall_timezone(current_user))

    # Get timeframe parameter (default to 'day')
    timeframe = request.args.get('timeframe', 'day')

    # Calculate time range based on timeframe
    from app.models import EnergyRecord

    if timeframe == 'day':
        # Get today's data from midnight onwards in user's timezone
        now_local = datetime.now(user_tz)
        start_of_day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_day_utc = start_of_day_local.astimezone(timezone.utc)

        # Query records from midnight today onwards
        records = EnergyRecord.query.filter(
            EnergyRecord.user_id == current_user.id,
            EnergyRecord.timestamp >= start_of_day_utc
        ).order_by(
            EnergyRecord.timestamp.asc()
        ).all()

    elif timeframe == 'month':
        # Get last 30 days of data
        limit = 720  # 30 days * 24 hours
        records = EnergyRecord.query.filter_by(
            user_id=current_user.id
        ).order_by(
            EnergyRecord.timestamp.desc()
        ).limit(limit).all()

    else:  # year
        # Get last 365 days of data
        limit = 8760  # 365 days * 24 hours
        records = EnergyRecord.query.filter_by(
            user_id=current_user.id
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
        now_local = datetime.now(user_tz)
        start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

        response_data['metadata'] = {
            'start_of_day': start_of_day.isoformat(),
            'end_of_day': end_of_day.isoformat(),
            'timezone': str(user_tz)
        }

    return jsonify(response_data)


@bp.route('/api/energy-calendar-history')
@login_required
@require_tesla_client
@require_tesla_site_id
def energy_calendar_history(tesla_client):
    """Get historical energy summaries from Tesla calendar history API"""
    logger.info(f"Energy calendar history requested by user: {current_user.email}")

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
            user_tz = ZoneInfo(get_powerwall_timezone(current_user))
            dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = dt.replace(hour=23, minute=59, second=59, tzinfo=user_tz)
            end_date = end_dt.isoformat()
        except Exception as e:
            logger.warning(f"Invalid end_date format: {end_date_str}, using default: {e}")

    # Fetch calendar history
    history = tesla_client.get_calendar_history(
        site_id=current_user.tesla_energy_site_id,
        kind='energy',
        period=period,
        end_date=end_date,
        timezone=get_powerwall_timezone(current_user)
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

        user_tz = ZoneInfo(get_powerwall_timezone(current_user))
        now = datetime.now(user_tz)

        # Calculate cutoff date based on period
        if period == 'day':
            # Today only - filter to entries from today
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            # Last 7 days
            cutoff = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'month':
            # Last 30 days
            cutoff = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'year':
            # Last 365 days
            cutoff = (now - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)

        # Filter time_series to entries after cutoff
        filtered_series = []
        for entry in time_series:
            try:
                # Parse timestamp (format: 2025-01-15T00:00:00+10:00)
                ts_str = entry.get('timestamp', '')
                if ts_str:
                    entry_dt = datetime.fromisoformat(ts_str)
                    if entry_dt >= cutoff:
                        filtered_series.append(entry)
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse timestamp: {entry.get('timestamp')}: {e}")
                continue

        logger.info(f"Filtered calendar history from {len(time_series)} to {len(filtered_series)} records for period '{period}'")
        time_series = filtered_series

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
@login_required
@cache.cached(timeout=300, key_prefix=lambda: f'tou_schedule_{current_user.id}')
def tou_schedule():
    """Get the rolling 24-hour tariff schedule that will be sent to Tesla"""
    logger.info(f"TOU tariff schedule requested by user: {current_user.email}")

    amber_client = get_amber_client(current_user)
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
    actual_interval = None
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
    tesla_client = get_tesla_client(current_user)
    if tesla_client and current_user.tesla_energy_site_id:
        site_info = tesla_client.get_site_info(current_user.tesla_energy_site_id)
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
    from app.tariff_converter import AmberTariffConverter
    converter = AmberTariffConverter()
    tariff = converter.convert_amber_to_tesla_tariff(
        forecast_30min,
        user=current_user,
        powerwall_timezone=powerwall_timezone,
        current_actual_interval=actual_interval
    )

    if not tariff:
        logger.error("Failed to convert tariff")
        return jsonify({'error': 'Failed to convert tariff'}), 500

    # Extract tariff periods for display
    energy_rates = tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {})
    feedin_rates = tariff.get('sell_tariff', {}).get('energy_charges', {}).get('Summer', {}).get('rates', {})

    # Get current time in user's timezone to mark current period
    from datetime import datetime
    from zoneinfo import ZoneInfo
    user_tz = ZoneInfo(get_powerwall_timezone(current_user))
    now = datetime.now(user_tz)
    current_hour = now.hour
    current_minute_bucket = 0 if now.minute < 30 else 30

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

                periods.append({
                    'time': f"{hour:02d}:{minute:02d}",
                    'hour': hour,
                    'minute': minute,
                    'buy_price': energy_rates[period_key] * 100,  # Convert back to cents
                    'sell_price': feedin_rates.get(period_key, 0) * 100,
                    'is_current': is_current,
                    'uses_actual_interval': uses_actual_interval
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

    return jsonify({
        'periods': periods,
        'stats': stats,
        'tariff_name': tariff.get('name', 'Unknown')
    })


@bp.route('/api/sync-tesla-schedule', methods=['POST'])
@login_required
@require_amber_client
@require_tesla_client
@require_tesla_site_id
def sync_tesla_schedule(amber_client, tesla_client):
    """Apply the TOU schedule to Tesla Powerwall"""
    logger.info(f"Tesla schedule sync requested by user: {current_user.email}")

    site_id = current_user.tesla_energy_site_id

    try:
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
        from app.tariff_converter import AmberTariffConverter
        converter = AmberTariffConverter()
        tariff = converter.convert_amber_to_tesla_tariff(
            forecast,
            user=current_user,
            powerwall_timezone=powerwall_timezone
        )

        if not tariff:
            logger.error("Failed to convert tariff")
            return jsonify({'error': 'Failed to convert Amber prices to Tesla tariff format'}), 500

        num_periods = len(tariff.get('energy_charges', {}).get('Summer', {}).get('rates', {}))
        logger.info(f"Applying TESLA SYNC tariff with {num_periods} rate periods")

        # Apply tariff to Tesla
        result = tesla_client.set_tariff_rate(site_id, tariff)

        if not result:
            logger.error("Failed to apply tariff to Tesla")
            return jsonify({'error': 'Failed to apply tariff to Tesla Powerwall'}), 500

        logger.info("Successfully synced Amber tariff to Tesla Powerwall")

        return jsonify({
            'success': True,
            'message': 'TESLA SYNC tariff applied successfully',
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
def test_find_tesla_sites(tesla_client):
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
def test_aemo_spike(tesla_client):
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
# CURRENT TOU RATE MANAGEMENT
# ============================================

@bp.route('/current_tou_rate')
@login_required
@require_tesla_client
@require_tesla_site_id
def current_tou_rate(tesla_client):
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
def save_current_tou_rate(tesla_client):
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
def restore_tou_rate(profile_id, tesla_client):
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
def api_current_tou_rate_raw(tesla_client):
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
def api_debug_site_info(tesla_client):
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
        flash('✓ Successfully connected to Tesla Fleet API! Your account is now using direct Tesla API access.')

    except Exception as e:
        logger.error(f"Error during OAuth token exchange: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash(f'Error connecting to Tesla Fleet API: {str(e)}')
        db.session.rollback()

    return redirect(url_for('main.settings'))
