# app/models.py
from app import db, login
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from datetime import datetime
import pyotp

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(128))

    # Encrypted Credentials
    amber_api_token_encrypted = db.Column(db.LargeBinary)
    amber_site_id = db.Column(db.String(100))  # Amber Electric site ID (user-selected)
    tesla_energy_site_id = db.Column(db.String(50))
    teslemetry_api_key_encrypted = db.Column(db.LargeBinary)

    # Tesla API Provider Selection
    tesla_api_provider = db.Column(db.String(20), default='teslemetry')  # 'teslemetry' or 'fleet_api'

    # Battery System Selection
    battery_system = db.Column(db.String(20), default='tesla')  # 'tesla' or 'sigenergy'

    # Sigenergy Cloud API Credentials
    sigenergy_username = db.Column(db.String(255))  # Sigenergy account email
    sigenergy_pass_enc_encrypted = db.Column(db.LargeBinary)  # Encrypted password (from browser dev tools)
    sigenergy_device_id = db.Column(db.String(20))  # 13-digit device identifier
    sigenergy_station_id = db.Column(db.String(50))  # Selected station ID
    sigenergy_access_token_encrypted = db.Column(db.LargeBinary)  # OAuth access token
    sigenergy_refresh_token_encrypted = db.Column(db.LargeBinary)  # OAuth refresh token
    sigenergy_token_expires_at = db.Column(db.DateTime)  # Token expiry timestamp
    # Sigenergy Modbus settings (for DC curtailment and live status)
    sigenergy_modbus_host = db.Column(db.String(50))  # IP address of Sigenergy system
    sigenergy_modbus_port = db.Column(db.Integer, default=502)  # Modbus TCP port
    sigenergy_modbus_slave_id = db.Column(db.Integer, default=1)  # Modbus slave/unit ID
    sigenergy_curtailment_state = db.Column(db.String(20))  # 'curtailed' or 'normal'
    sigenergy_curtailment_updated = db.Column(db.DateTime)  # When curtailment state last changed
    sigenergy_export_limit_kw = db.Column(db.Float)  # Current export limit in kW (for load-following)

    # Fleet API Credentials (for direct Tesla Fleet API)
    fleet_api_client_id_encrypted = db.Column(db.LargeBinary)
    fleet_api_client_secret_encrypted = db.Column(db.LargeBinary)
    fleet_api_redirect_uri = db.Column(db.String(255))  # OAuth redirect URI (not encrypted, just a URL)
    fleet_api_access_token_encrypted = db.Column(db.LargeBinary)
    fleet_api_refresh_token_encrypted = db.Column(db.LargeBinary)
    fleet_api_token_expires_at = db.Column(db.DateTime)

    # Named Cloudflare Tunnel (for stable custom domain)
    cloudflare_tunnel_token_encrypted = db.Column(db.LargeBinary)  # Tunnel token (encrypted)
    cloudflare_tunnel_domain = db.Column(db.String(255))  # Custom domain (e.g., tesla.mydomain.com)
    cloudflare_tunnel_enabled = db.Column(db.Boolean, default=False)  # Auto-start on app startup

    # Two-Factor Authentication
    totp_secret = db.Column(db.String(32))  # Base32 encoded secret for TOTP
    two_factor_enabled = db.Column(db.Boolean, default=False)

    # Status Tracking
    last_update_status = db.Column(db.String(255))
    last_update_time = db.Column(db.DateTime)

    # User Preferences
    timezone = db.Column(db.String(50), default='Australia/Brisbane')  # IANA timezone string
    sync_enabled = db.Column(db.Boolean, default=True)  # Enable/disable automatic Tesla syncing

    # Weather/Location settings (for weather-based automations and dashboard display)
    weather_location = db.Column(db.String(100), nullable=True)  # City name or postcode (e.g., "Brisbane" or "4000")
    weather_latitude = db.Column(db.Float, nullable=True)  # Cached geocoded latitude
    weather_longitude = db.Column(db.Float, nullable=True)  # Cached geocoded longitude
    openweathermap_api_key = db.Column(db.String(64), nullable=True)  # Optional user-specific API key

    # Solcast Solar Forecasting
    solcast_api_key_encrypted = db.Column(db.LargeBinary, nullable=True)  # Encrypted Solcast API key
    solcast_resource_id = db.Column(db.String(50), nullable=True)  # Rooftop site resource ID from Solcast
    solcast_enabled = db.Column(db.Boolean, default=False)  # Enable Solcast solar forecasting
    solcast_capacity_kw = db.Column(db.Float, nullable=True)  # System capacity in kW (for validation)

    # Amber Electric Preferences
    amber_forecast_type = db.Column(db.String(20), default='predicted')  # 'low', 'predicted', 'high'
    solar_curtailment_enabled = db.Column(db.Boolean, default=False)  # Enable solar curtailment when export price <= 0
    current_export_rule = db.Column(db.String(20))  # Cached export rule: 'never', 'pv_only', 'battery_ok'
    current_export_rule_updated = db.Column(db.DateTime)  # When the export rule was last updated
    manual_export_override = db.Column(db.Boolean, default=False)  # User manually set export rule, skip auto-restore
    manual_export_rule = db.Column(db.String(20))  # The rule the user manually selected
    last_tariff_hash = db.Column(db.String(32))  # MD5 hash of last synced tariff for deduplication

    # Alpha: Force mode toggle after tariff sync
    # Toggle to self_consumption then back to TOU after tariff upload for faster PW response
    force_tariff_mode_toggle = db.Column(db.Boolean, default=False)

    # Export Price Boost Configuration
    # Artificially increase export prices to trigger Powerwall exports at lower price points
    export_boost_enabled = db.Column(db.Boolean, default=False)
    export_price_offset = db.Column(db.Float, default=0.0)  # c/kWh offset to add to export prices
    export_min_price = db.Column(db.Float, default=0.0)  # Minimum export price floor in c/kWh
    export_boost_start = db.Column(db.String(5), default='17:00')  # Start time for boost (HH:MM)
    export_boost_end = db.Column(db.String(5), default='21:00')  # End time for boost (HH:MM)
    export_boost_threshold = db.Column(db.Float, default=0.0)  # Min price to activate boost (c/kWh) - boost skipped if actual price below this

    # Chip Mode Configuration
    # Prevents Powerwall from exporting during configured hours unless price exceeds threshold
    # Inverse of Export Boost - sets export price to 0 to suppress exports, except on price spikes
    chip_mode_enabled = db.Column(db.Boolean, default=False)
    chip_mode_start = db.Column(db.String(5), default='22:00')  # Start time for anti-export (HH:MM)
    chip_mode_end = db.Column(db.String(5), default='06:00')  # End time for anti-export (HH:MM)
    chip_mode_threshold = db.Column(db.Float, default=30.0)  # Price threshold (c/kWh) - allow export only above this

    # AC-Coupled Inverter Curtailment Configuration
    # Direct control of solar inverters via Modbus TCP for AC-coupled systems
    inverter_curtailment_enabled = db.Column(db.Boolean, default=False)
    inverter_brand = db.Column(db.String(50))  # 'sungrow', 'fronius', etc.
    inverter_model = db.Column(db.String(50))  # 'sg10', 'sg5', etc.
    inverter_host = db.Column(db.String(100))  # IP address of inverter/gateway
    inverter_port = db.Column(db.Integer, default=502)  # Modbus TCP port
    inverter_slave_id = db.Column(db.Integer, default=1)  # Modbus slave ID
    inverter_token = db.Column(db.String(2000))  # JWT token for Enphase (firmware 7.x+)
    enphase_username = db.Column(db.String(200))  # Enlighten username/email for auto token refresh
    enphase_password = db.Column(db.String(200))  # Enlighten password for auto token refresh
    enphase_serial = db.Column(db.String(50))  # Envoy serial number (optional, auto-detected)
    inverter_restore_soc = db.Column(db.Integer, default=98)  # Restore inverter when battery drops below this %
    fronius_load_following = db.Column(db.Boolean, default=False)  # Fronius: use calculated limits instead of 0W profile
    inverter_last_state = db.Column(db.String(20))  # Last known state: 'online', 'curtailed', 'offline'
    inverter_last_state_updated = db.Column(db.DateTime)  # When state was last updated
    inverter_power_limit_w = db.Column(db.Integer)  # Current power limit in watts (for load-following)

    # Demand Charge Configuration
    enable_demand_charges = db.Column(db.Boolean, default=False)
    peak_demand_rate = db.Column(db.Float, default=0.0)
    peak_start_hour = db.Column(db.Integer, default=14)
    peak_start_minute = db.Column(db.Integer, default=0)
    peak_end_hour = db.Column(db.Integer, default=20)
    peak_end_minute = db.Column(db.Integer, default=0)
    peak_days = db.Column(db.String(20), default='weekdays')  # 'weekdays', 'all', 'weekends'
    demand_charge_apply_to = db.Column(db.String(20), default='buy')  # 'buy', 'sell', 'both'
    offpeak_demand_rate = db.Column(db.Float, default=0.0)
    shoulder_demand_rate = db.Column(db.Float, default=0.0)
    shoulder_start_hour = db.Column(db.Integer, default=7)
    shoulder_start_minute = db.Column(db.Integer, default=0)
    shoulder_end_hour = db.Column(db.Integer, default=14)
    shoulder_end_minute = db.Column(db.Integer, default=0)

    # Daily Supply Charge Configuration
    daily_supply_charge = db.Column(db.Float, default=0.0)  # Daily supply charge ($)
    monthly_supply_charge = db.Column(db.Float, default=0.0)  # Monthly fixed charge ($)

    # Demand Period Grid Charging State
    grid_charging_disabled_for_demand = db.Column(db.Boolean, default=False)  # True when grid charging disabled during peak

    # Demand Period Artificial Price Increase (ALPHA)
    demand_artificial_price_enabled = db.Column(db.Boolean, default=False)  # Add $2/kWh to import prices during demand periods

    # AEMO Spike Detection Configuration
    aemo_region = db.Column(db.String(10))  # NEM region: NSW1, QLD1, VIC1, SA1, TAS1
    aemo_spike_threshold = db.Column(db.Float, default=300.0)  # Spike threshold in $/MWh
    aemo_spike_detection_enabled = db.Column(db.Boolean, default=False)  # Enable spike monitoring
    aemo_in_spike_mode = db.Column(db.Boolean, default=False)  # Currently in spike mode
    aemo_spike_test_mode = db.Column(db.Boolean, default=False)  # Manual test mode - prevents auto-restore
    aemo_last_check = db.Column(db.DateTime)  # Last time AEMO was checked
    aemo_last_price = db.Column(db.Float)  # Last observed price in $/MWh
    aemo_spike_start_time = db.Column(db.DateTime)  # When current spike started
    aemo_saved_tariff_id = db.Column(db.Integer, db.ForeignKey('saved_tou_profile.id'))  # Tariff to restore after spike
    aemo_pre_spike_operation_mode = db.Column(db.String(20))  # Operation mode before spike (self_consumption, autonomous, backup)

    # Amber Spike Protection (anti-arbitrage)
    # When Amber reports spikeStatus='potential' or 'spike', inflate buy prices to max(sell)+$1
    # This prevents Powerwall from charging from grid during spikes to arbitrage
    spike_protection_enabled = db.Column(db.Boolean, default=False)

    # Settled Prices Only Mode
    # When enabled, skips the initial forecast sync at :00 seconds and only syncs when:
    # - WebSocket delivers actual/settled prices, OR
    # - REST API check at :35/:60 seconds (when prices are settled)
    # This avoids syncing predicted prices that may differ from actual settled prices
    settled_prices_only = db.Column(db.Boolean, default=False)

    # Manual Discharge Mode (Force Discharge button)
    manual_discharge_active = db.Column(db.Boolean, default=False)  # Currently in manual discharge mode
    manual_discharge_expires_at = db.Column(db.DateTime, nullable=True)  # When to auto-restore normal operation
    manual_discharge_saved_tariff_id = db.Column(db.Integer, db.ForeignKey('saved_tou_profile.id', use_alter=True), nullable=True)

    # Manual Charge Mode (Force Charge button)
    manual_charge_active = db.Column(db.Boolean, default=False)  # Currently in manual charge mode
    manual_charge_expires_at = db.Column(db.DateTime, nullable=True)  # When to auto-restore normal operation
    manual_charge_saved_tariff_id = db.Column(db.Integer, db.ForeignKey('saved_tou_profile.id', use_alter=True), nullable=True)
    manual_charge_saved_backup_reserve = db.Column(db.Integer, nullable=True)  # Backup reserve % to restore after charge

    # Electricity Provider Configuration
    electricity_provider = db.Column(db.String(20), default='amber')  # 'amber', 'flow_power', 'globird'
    flow_power_state = db.Column(db.String(10))  # NEM region: NSW1, VIC1, QLD1, SA1
    flow_power_price_source = db.Column(db.String(20), default='amber')  # 'amber', 'aemo'

    # Flow Power PEA (Price Efficiency Adjustment) Configuration
    # PEA adjusts pricing based on wholesale prices: Final Rate = Base Rate + (wholesale - 9.7c)
    flow_power_base_rate = db.Column(db.Float, default=34.0)  # Flow Power base rate in c/kWh
    pea_enabled = db.Column(db.Boolean, default=True)  # Enable PEA calculation for Flow Power
    pea_custom_value = db.Column(db.Float, nullable=True)  # Optional fixed PEA override in c/kWh

    # Network Tariff Configuration (for Flow Power + AEMO)
    # Primary: Use aemo_to_tariff library with distributor + tariff code
    # Fallback: Manual rate entry when use_manual_rates is True
    network_distributor = db.Column(db.String(20), default='energex')  # DNSP: energex, ausgrid, endeavour, etc.
    network_tariff_code = db.Column(db.String(20), default='6900')  # Tariff code: 6900, EA025, etc. (NTC prefix auto-stripped)
    network_use_manual_rates = db.Column(db.Boolean, default=False)  # True = use manual rates below instead of library

    # Manual rate entry (used when network_use_manual_rates is True)
    network_tariff_type = db.Column(db.String(10), default='flat')  # 'flat' or 'tou'
    network_flat_rate = db.Column(db.Float, default=8.0)  # Flat network rate in c/kWh
    network_peak_rate = db.Column(db.Float, default=15.0)  # Peak network rate in c/kWh
    network_shoulder_rate = db.Column(db.Float, default=5.0)  # Shoulder network rate in c/kWh
    network_offpeak_rate = db.Column(db.Float, default=2.0)  # Off-peak network rate in c/kWh
    network_peak_start = db.Column(db.String(5), default='16:00')  # Peak period start HH:MM
    network_peak_end = db.Column(db.String(5), default='21:00')  # Peak period end HH:MM
    network_offpeak_start = db.Column(db.String(5), default='10:00')  # Off-peak period start HH:MM
    network_offpeak_end = db.Column(db.String(5), default='15:00')  # Off-peak period end HH:MM
    network_other_fees = db.Column(db.Float, default=1.5)  # Environmental/market fees in c/kWh
    network_include_gst = db.Column(db.Boolean, default=True)  # Include 10% GST in calculations

    # Battery Health Data (from mobile app)
    battery_original_capacity_wh = db.Column(db.Float, nullable=True)  # Original battery capacity in Wh
    battery_current_capacity_wh = db.Column(db.Float, nullable=True)  # Current usable capacity in Wh
    battery_degradation_percent = db.Column(db.Float, nullable=True)  # Calculated degradation percentage
    battery_count = db.Column(db.Integer, nullable=True)  # Number of Powerwall units
    battery_health_updated = db.Column(db.DateTime, nullable=True)  # When health was last updated
    battery_health_api_token = db.Column(db.String(64), nullable=True)  # API token for mobile app sync
    powerwall_install_date = db.Column(db.Date, nullable=True)  # When Powerwall was installed

    # Firmware Tracking
    powerwall_firmware_version = db.Column(db.String(50), nullable=True)  # Current firmware version
    powerwall_firmware_updated = db.Column(db.DateTime, nullable=True)  # When firmware was last checked

    # Push Notifications (for mobile app)
    apns_device_token = db.Column(db.String(200), nullable=True)  # iOS APNs device token
    push_notifications_enabled = db.Column(db.Boolean, default=True)  # Enable/disable push notifications
    notify_firmware_updates = db.Column(db.Boolean, default=True)  # Notify on firmware changes

    # Relationships
    price_records = db.relationship('PriceRecord', backref='user', lazy='dynamic')
    energy_records = db.relationship('EnergyRecord', backref='user', lazy='dynamic')
    saved_tou_profiles = db.relationship('SavedTOUProfile', backref='user', lazy='dynamic', cascade='all, delete-orphan', foreign_keys='SavedTOUProfile.user_id')
    aemo_saved_tariff = db.relationship('SavedTOUProfile', foreign_keys=[aemo_saved_tariff_id], post_update=True)
    manual_discharge_saved_tariff = db.relationship('SavedTOUProfile', foreign_keys=[manual_discharge_saved_tariff_id], post_update=True)
    manual_charge_saved_tariff = db.relationship('SavedTOUProfile', foreign_keys=[manual_charge_saved_tariff_id], post_update=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_totp_secret(self):
        """Generate a new TOTP secret for 2FA setup"""
        self.totp_secret = pyotp.random_base32()
        return self.totp_secret

    def get_totp_uri(self):
        """Generate the provisioning URI for authenticator apps"""
        if not self.totp_secret:
            return None
        totp = pyotp.TOTP(self.totp_secret)
        return totp.provisioning_uri(
            name=self.email,
            issuer_name="PowerSync"
        )

    def verify_totp(self, token):
        """Verify a TOTP token"""
        if not self.totp_secret:
            return False
        totp = pyotp.TOTP(self.totp_secret)
        return totp.verify(token)


class PriceRecord(db.Model):
    """Stores historical Amber electricity pricing data"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Timestamp
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    # Amber pricing data
    per_kwh = db.Column(db.Float)  # Price per kWh in cents
    spot_per_kwh = db.Column(db.Float)  # Spot price per kWh
    wholesale_kwh_price = db.Column(db.Float)  # Wholesale price
    network_kwh_price = db.Column(db.Float)  # Network price
    market_kwh_price = db.Column(db.Float)  # Market price
    green_kwh_price = db.Column(db.Float)  # Green/renewable price

    # Price type (general usage, controlled load, feed-in)
    channel_type = db.Column(db.String(50))

    # Forecast or actual
    forecast = db.Column(db.Boolean, default=False)

    # Period start/end
    nem_time = db.Column(db.DateTime)
    period_start = db.Column(db.DateTime)
    period_end = db.Column(db.DateTime)

    # Spike status
    spike_status = db.Column(db.String(20))

    def __repr__(self):
        return f'<PriceRecord {self.timestamp} - {self.per_kwh}c/kWh>'


class EnergyRecord(db.Model):
    """Stores historical energy usage data from Tesla Powerwall"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Timestamp
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    # Energy data in watts (W)
    solar_power = db.Column(db.Float, default=0.0)  # Solar generation (W)
    battery_power = db.Column(db.Float, default=0.0)  # Battery power (+ discharge, - charge) (W)
    grid_power = db.Column(db.Float, default=0.0)  # Grid power (+ import, - export) (W)
    load_power = db.Column(db.Float, default=0.0)  # Home/load consumption (W)

    # Battery state
    battery_level = db.Column(db.Float)  # Battery percentage (0-100)

    def __repr__(self):
        return f'<EnergyRecord {self.timestamp} - Solar:{self.solar_power}W Grid:{self.grid_power}W>'


class CustomTOUSchedule(db.Model):
    """Custom Time-of-Use electricity rate schedules for fixed-rate providers"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Schedule metadata
    name = db.Column(db.String(100), nullable=False)  # e.g., "Origin Energy Single Rate"
    utility = db.Column(db.String(100), nullable=False)  # e.g., "Origin Energy"
    code = db.Column(db.String(100))  # Tariff code e.g., "EA205"
    currency = db.Column(db.String(3), default='AUD')

    # Charges
    daily_charge = db.Column(db.Float, default=0.0)  # Daily supply charge ($)
    monthly_charge = db.Column(db.Float, default=0.0)  # Monthly fixed charge ($)

    # Status
    active = db.Column(db.Boolean, default=False)  # Only one schedule can be active
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_synced = db.Column(db.DateTime)  # Last time synced to Tesla

    # Relationships
    seasons = db.relationship('TOUSeason', backref='schedule', lazy='dynamic', cascade='all, delete-orphan')
    user = db.relationship('User', backref='custom_tou_schedules')

    def __repr__(self):
        return f'<CustomTOUSchedule {self.name}>'


class TOUSeason(db.Model):
    """Seasonal periods within a TOU schedule"""
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey('custom_tou_schedule.id'), nullable=False)

    # Season definition
    name = db.Column(db.String(50), nullable=False)  # e.g., "Summer", "Winter", "All Year"
    from_month = db.Column(db.Integer, nullable=False)  # 1-12
    to_month = db.Column(db.Integer, nullable=False)  # 1-12
    from_day = db.Column(db.Integer, nullable=False)  # 1-31
    to_day = db.Column(db.Integer, nullable=False)  # 1-31

    # Relationships
    periods = db.relationship('TOUPeriod', backref='season', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<TOUSeason {self.name} {self.from_month}/{self.from_day}-{self.to_month}/{self.to_day}>'


class TOUPeriod(db.Model):
    """Individual time period with specific rates"""
    id = db.Column(db.Integer, primary_key=True)
    season_id = db.Column(db.Integer, db.ForeignKey('tou_season.id'), nullable=False)

    # Period name and display order
    name = db.Column(db.String(50), nullable=False)  # e.g., "Peak", "Shoulder", "Off-Peak 1"
    display_order = db.Column(db.Integer, default=0)  # For UI sorting

    # Time range
    from_hour = db.Column(db.Integer, nullable=False)  # 0-23
    from_minute = db.Column(db.Integer, nullable=False)  # 0 or 30
    to_hour = db.Column(db.Integer, nullable=False)  # 0-23
    to_minute = db.Column(db.Integer, nullable=False)  # 0 or 30

    # Day of week (0=Monday, 6=Sunday)
    from_day_of_week = db.Column(db.Integer, default=0)  # 0-6
    to_day_of_week = db.Column(db.Integer, default=6)  # 0-6

    # Rates (in $/kWh)
    energy_rate = db.Column(db.Float, nullable=False)  # Buy rate (import from grid)
    sell_rate = db.Column(db.Float, nullable=False)  # Sell rate (export to grid / feed-in)
    demand_rate = db.Column(db.Float, default=0.0)  # Demand charge ($/kW)

    def __repr__(self):
        return f'<TOUPeriod {self.name} {self.from_hour}:{self.from_minute:02d}-{self.to_hour}:{self.to_minute:02d}>'


class SavedTOUProfile(db.Model):
    """Saved TOU tariff profiles from Tesla - allows backup and restore"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Profile metadata
    name = db.Column(db.String(200), nullable=False)  # User-provided name for this saved profile
    description = db.Column(db.Text)  # Optional description

    # Source information
    source_type = db.Column(db.String(50), default='tesla')  # 'tesla', 'custom', 'amber'
    tariff_name = db.Column(db.String(200))  # Name from the tariff (e.g., "PGE-EV2-A")
    utility = db.Column(db.String(100))  # Utility name from tariff

    # Complete Tesla tariff JSON (stored as Text - will be JSON serialized)
    tariff_json = db.Column(db.Text, nullable=False)  # Complete Tesla tariff structure

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    fetched_from_tesla_at = db.Column(db.DateTime)  # When it was retrieved from Tesla
    last_restored_at = db.Column(db.DateTime)  # When it was last restored to Tesla

    # Metadata
    is_current = db.Column(db.Boolean, default=False)  # Is this the current tariff on Tesla?
    is_default = db.Column(db.Boolean, default=False)  # Is this the default tariff to restore to?

    def __repr__(self):
        return f'<SavedTOUProfile {self.name} - {self.tariff_name}>'


class BatteryHealthHistory(db.Model):
    """Stores historical battery health readings from mobile app scans"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Scan timestamp
    scanned_at = db.Column(db.DateTime, index=True, nullable=False)

    # Capacity data (in Wh)
    rated_capacity_wh = db.Column(db.Float, nullable=False)  # Rated capacity (13.5 kWh per PW3)
    actual_capacity_wh = db.Column(db.Float, nullable=False)  # Actual measured capacity

    # Calculated metrics
    health_percent = db.Column(db.Float, nullable=False)  # (actual/rated) * 100
    degradation_percent = db.Column(db.Float, nullable=False)  # (1 - actual/rated) * 100

    # Battery configuration
    battery_count = db.Column(db.Integer, nullable=False)

    # Per-pack data (JSON array of individual pack readings)
    pack_data = db.Column(db.Text, nullable=True)  # JSON: [{packId, capacityWh, healthPercent}, ...]

    # Record metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref=db.backref('battery_health_history', lazy='dynamic'))

    def __repr__(self):
        return f'<BatteryHealthHistory {self.scanned_at} - {self.health_percent}%>'


class Automation(db.Model):
    """User-defined automations for battery/energy management"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Automation metadata
    name = db.Column(db.String(100))  # User-provided name
    group_name = db.Column(db.String(100), default='Default Group')  # Grouping for UI
    priority = db.Column(db.Integer, default=50)  # 1-100, higher wins conflicts

    # State
    enabled = db.Column(db.Boolean, default=True)  # Enable/disable automation
    run_once = db.Column(db.Boolean, default=False)  # Pause after first trigger
    paused = db.Column(db.Boolean, default=False)  # Paused (set true after run_once executes)
    notification_only = db.Column(db.Boolean, default=False)  # Only send notification, no actions

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_triggered_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    trigger = db.relationship('AutomationTrigger', backref='automation', uselist=False, cascade='all, delete-orphan')
    actions = db.relationship('AutomationAction', backref='automation', lazy='dynamic', cascade='all, delete-orphan')
    user = db.relationship('User', backref=db.backref('automations', lazy='dynamic'))

    def __repr__(self):
        return f'<Automation {self.name} (priority={self.priority}, enabled={self.enabled})>'


class AutomationTrigger(db.Model):
    """Trigger conditions for an automation (one per automation)"""
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey('automation.id'), nullable=False)

    # Trigger type: time, battery, flow, price, grid, weather
    trigger_type = db.Column(db.String(50), nullable=False)

    # ========== Time Trigger Fields ==========
    time_of_day = db.Column(db.Time, nullable=True)  # When to trigger (HH:MM)
    repeat_days = db.Column(db.String(20), nullable=True)  # "0,1,2,3,4,5,6" for Sun-Sat

    # ========== Battery Trigger Fields ==========
    # battery_condition: charged_up_to, discharged_down_to, discharged_to_reserve
    battery_condition = db.Column(db.String(50), nullable=True)
    battery_threshold = db.Column(db.Integer, nullable=True)  # 0-100%

    # ========== Flow Trigger Fields ==========
    # flow_source: home_usage, solar, grid_import, grid_export, battery_charge, battery_discharge
    flow_source = db.Column(db.String(50), nullable=True)
    # flow_transition: rises_above, drops_below
    flow_transition = db.Column(db.String(20), nullable=True)
    flow_threshold_kw = db.Column(db.Float, nullable=True)  # Threshold in kW

    # ========== Price Trigger Fields ==========
    # price_type: import, export
    price_type = db.Column(db.String(20), nullable=True)
    # price_transition: rises_above, drops_below
    price_transition = db.Column(db.String(20), nullable=True)
    price_threshold = db.Column(db.Float, nullable=True)  # Threshold in $/kWh

    # ========== Grid Trigger Fields ==========
    # grid_condition: off_grid, on_grid
    grid_condition = db.Column(db.String(20), nullable=True)

    # ========== Weather Trigger Fields ==========
    # weather_condition: sunny, partly_sunny, cloudy
    weather_condition = db.Column(db.String(20), nullable=True)

    # ========== EV Trigger Fields ==========
    # ev_vehicle_id: Specific vehicle ID (null = any vehicle)
    ev_vehicle_id = db.Column(db.Integer, db.ForeignKey('tesla_vehicle.id'), nullable=True)
    # ev_condition: connected, disconnected, charging_starts, charging_stops, soc_reaches
    ev_condition = db.Column(db.String(30), nullable=True)
    # ev_soc_threshold: For soc_reaches trigger (0-100%)
    ev_soc_threshold = db.Column(db.Integer, nullable=True)

    # ========== OCPP Charger Trigger Fields ==========
    # ocpp_charger_id: Specific charger ID (null = any charger)
    ocpp_charger_id = db.Column(db.Integer, db.ForeignKey('ocpp_charger.id'), nullable=True)
    # ocpp_condition: connected, disconnected, charging_starts, charging_stops, energy_reaches, available, faulted
    ocpp_condition = db.Column(db.String(30), nullable=True)
    # ocpp_energy_threshold: For energy_reaches trigger (kWh)
    ocpp_energy_threshold = db.Column(db.Float, nullable=True)

    # ========== Time Window (optional constraint for all triggers) ==========
    time_window_start = db.Column(db.Time, nullable=True)  # Only trigger after this time
    time_window_end = db.Column(db.Time, nullable=True)  # Only trigger before this time

    # ========== State Tracking (for edge detection) ==========
    # Tracks last known state to detect transitions (rises_above, drops_below)
    last_evaluated_value = db.Column(db.Float, nullable=True)
    last_evaluated_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<AutomationTrigger type={self.trigger_type}>'


class AutomationAction(db.Model):
    """Actions to execute when automation triggers (one or more per automation)"""
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey('automation.id'), nullable=False)

    # Action type: set_backup_reserve, preserve_charge, set_operation_mode,
    #              force_discharge, force_charge, curtail_inverter, send_notification
    action_type = db.Column(db.String(50), nullable=False)

    # Action parameters as JSON
    # Examples:
    #   set_backup_reserve: {"reserve_percent": 50}
    #   preserve_charge: {} (no params, sets export to "never")
    #   set_operation_mode: {"mode": "self_consumption"} or {"mode": "autonomous"} or {"mode": "backup"}
    #   force_discharge: {"duration_minutes": 30}
    #   force_charge: {"duration_minutes": 60, "target_percent": 100}
    #   curtail_inverter: {} or {"power_limit_w": 2000}
    #   send_notification: {"message": "Price spike detected!"}
    parameters = db.Column(db.Text, nullable=True)  # JSON string

    # Execution order within the automation
    execution_order = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<AutomationAction type={self.action_type}>'


class TeslaVehicle(db.Model):
    """Tesla vehicle linked via Fleet API for EV charging control."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Vehicle identification
    vehicle_id = db.Column(db.String(50), nullable=False)  # Tesla vehicle ID
    vin = db.Column(db.String(20))  # Vehicle Identification Number

    # Vehicle info
    display_name = db.Column(db.String(100))  # User-assigned name (e.g., "TESSY")
    model = db.Column(db.String(50))  # Model name (e.g., "Model Y", "Model 3")
    year = db.Column(db.Integer)  # Model year
    color = db.Column(db.String(50))  # Exterior color

    # Charging state (cached from last update)
    charging_state = db.Column(db.String(30))  # Charging, Complete, Disconnected, Stopped, etc.
    battery_level = db.Column(db.Integer)  # Current battery percentage (0-100)
    battery_range = db.Column(db.Float)  # Estimated range in miles/km
    charge_limit_soc = db.Column(db.Integer)  # Charge limit percentage
    charge_current_request = db.Column(db.Integer)  # Requested charge amps
    charge_current_actual = db.Column(db.Float)  # Actual charge amps
    charger_voltage = db.Column(db.Integer)  # Charger voltage
    charger_power = db.Column(db.Float)  # Charging power in kW
    time_to_full_charge = db.Column(db.Float)  # Hours until full charge
    charge_port_door_open = db.Column(db.Boolean)  # Is charge port open
    charge_port_latch = db.Column(db.String(20))  # Engaged, Disengaged, etc.

    # Vehicle state
    is_online = db.Column(db.Boolean, default=False)  # Vehicle is online/awake
    is_plugged_in = db.Column(db.Boolean, default=False)  # Vehicle is plugged in

    # Location (optional, if user grants permission)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    location_name = db.Column(db.String(200))  # Reverse geocoded location

    # Timestamps
    last_seen = db.Column(db.DateTime)  # Last time vehicle was online
    data_updated_at = db.Column(db.DateTime)  # Last data refresh
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # User settings for this vehicle
    enable_automations = db.Column(db.Boolean, default=True)  # Enable EV automations for this vehicle
    prioritize_powerwall = db.Column(db.Boolean, default=False)  # Charge Powerwall before vehicle
    powerwall_min_soc = db.Column(db.Integer, default=80)  # Min Powerwall SoC before vehicle charging

    # Relationships
    user = db.relationship('User', backref=db.backref('tesla_vehicles', lazy='dynamic'))

    def __repr__(self):
        return f'<TeslaVehicle {self.display_name or self.vin}>'

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'vehicle_id': self.vehicle_id,
            'vin': self.vin,
            'display_name': self.display_name,
            'model': self.model,
            'year': self.year,
            'color': self.color,
            'charging_state': self.charging_state,
            'battery_level': self.battery_level,
            'battery_range': self.battery_range,
            'charge_limit_soc': self.charge_limit_soc,
            'charge_current_request': self.charge_current_request,
            'charge_current_actual': self.charge_current_actual,
            'charger_voltage': self.charger_voltage,
            'charger_power': self.charger_power,
            'time_to_full_charge': self.time_to_full_charge,
            'is_online': self.is_online,
            'is_plugged_in': self.is_plugged_in,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'data_updated_at': self.data_updated_at.isoformat() if self.data_updated_at else None,
            'enable_automations': self.enable_automations,
            'prioritize_powerwall': self.prioritize_powerwall,
            'powerwall_min_soc': self.powerwall_min_soc,
        }


class OCPPCharger(db.Model):
    """OCPP-compliant EV charger registered with the central system."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Charger identification (from BootNotification)
    charger_id = db.Column(db.String(50), nullable=False, unique=True)  # Unique ID used in OCPP connection
    vendor = db.Column(db.String(50))  # Charge point vendor (e.g., "Grizzl-E", "Wallbox")
    model = db.Column(db.String(50))  # Charge point model
    serial_number = db.Column(db.String(50))  # Serial number
    firmware_version = db.Column(db.String(50))  # Firmware version

    # Connection state
    is_connected = db.Column(db.Boolean, default=False)  # WebSocket connected
    last_seen = db.Column(db.DateTime)  # Last heartbeat/message
    last_boot = db.Column(db.DateTime)  # Last BootNotification

    # Connector status (for single-connector chargers; multi-connector uses OCPPConnector)
    # Status: Available, Preparing, Charging, SuspendedEVSE, SuspendedEV, Finishing, Reserved, Unavailable, Faulted
    status = db.Column(db.String(20), default='Unavailable')
    error_code = db.Column(db.String(50))  # NoError, ConnectorLockFailure, EVCommunicationError, etc.

    # Current charging session info
    current_transaction_id = db.Column(db.Integer)  # Active transaction ID
    current_power_kw = db.Column(db.Float)  # Current charging power in kW
    current_energy_kwh = db.Column(db.Float)  # Energy delivered in current session
    current_soc = db.Column(db.Integer)  # Vehicle SoC if reported

    # Meter values
    meter_value_kwh = db.Column(db.Float)  # Total energy meter reading

    # Configuration
    max_power_kw = db.Column(db.Float)  # Maximum power capability
    num_connectors = db.Column(db.Integer, default=1)  # Number of connectors

    # User settings
    display_name = db.Column(db.String(100))  # User-friendly name
    enable_automations = db.Column(db.Boolean, default=True)  # Enable in automations

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('ocpp_chargers', lazy='dynamic'))

    def __repr__(self):
        return f'<OCPPCharger {self.display_name or self.charger_id}>'

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'charger_id': self.charger_id,
            'display_name': self.display_name,
            'vendor': self.vendor,
            'model': self.model,
            'serial_number': self.serial_number,
            'firmware_version': self.firmware_version,
            'is_connected': self.is_connected,
            'status': self.status,
            'error_code': self.error_code,
            'current_transaction_id': self.current_transaction_id,
            'current_power_kw': self.current_power_kw,
            'current_energy_kwh': self.current_energy_kwh,
            'current_soc': self.current_soc,
            'meter_value_kwh': self.meter_value_kwh,
            'max_power_kw': self.max_power_kw,
            'num_connectors': self.num_connectors,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'last_boot': self.last_boot.isoformat() if self.last_boot else None,
            'enable_automations': self.enable_automations,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class OCPPTransaction(db.Model):
    """OCPP charging transaction/session."""
    id = db.Column(db.Integer, primary_key=True)
    charger_id = db.Column(db.Integer, db.ForeignKey('ocpp_charger.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Transaction identification
    transaction_id = db.Column(db.Integer, nullable=False)  # OCPP transaction ID
    id_tag = db.Column(db.String(50))  # RFID tag or identifier used to authorize

    # Connector (for multi-connector chargers)
    connector_id = db.Column(db.Integer, default=1)

    # Session timing
    start_time = db.Column(db.DateTime, nullable=False)
    stop_time = db.Column(db.DateTime)
    stop_reason = db.Column(db.String(50))  # Local, Remote, EVDisconnected, PowerLoss, etc.

    # Energy
    meter_start = db.Column(db.Float)  # Meter reading at start (Wh)
    meter_stop = db.Column(db.Float)  # Meter reading at stop (Wh)
    energy_kwh = db.Column(db.Float)  # Energy delivered (kWh)

    # Peak values during session
    max_power_kw = db.Column(db.Float)  # Maximum power during session

    # Cost (if calculated)
    cost = db.Column(db.Float)  # Calculated cost
    cost_currency = db.Column(db.String(3))  # Currency code (e.g., "AUD")

    # Relationships
    charger = db.relationship('OCPPCharger', backref=db.backref('transactions', lazy='dynamic'))
    user = db.relationship('User', backref=db.backref('ocpp_transactions', lazy='dynamic'))

    def __repr__(self):
        return f'<OCPPTransaction {self.transaction_id} on {self.charger_id}>'

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'transaction_id': self.transaction_id,
            'charger_id': self.charger_id,
            'connector_id': self.connector_id,
            'id_tag': self.id_tag,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'stop_time': self.stop_time.isoformat() if self.stop_time else None,
            'stop_reason': self.stop_reason,
            'meter_start': self.meter_start,
            'meter_stop': self.meter_stop,
            'energy_kwh': self.energy_kwh,
            'max_power_kw': self.max_power_kw,
            'cost': self.cost,
            'cost_currency': self.cost_currency,
        }


class SolcastForecast(db.Model):
    """Cached Solcast solar production forecasts.

    Stores forecast data from Solcast API to minimize API calls.
    Forecasts are updated periodically (hobbyist tier: 10 calls/day).
    """
    __tablename__ = 'solcast_forecast'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    # Forecast period end time (UTC)
    period_end = db.Column(db.DateTime, nullable=False, index=True)

    # PV power estimates in kW
    pv_estimate = db.Column(db.Float)  # 50th percentile (most likely)
    pv_estimate10 = db.Column(db.Float)  # 10th percentile (conservative/cloudy)
    pv_estimate90 = db.Column(db.Float)  # 90th percentile (optimistic/clear)

    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('solcast_forecasts', lazy='dynamic'))

    def __repr__(self):
        return f'<SolcastForecast {self.period_end} - {self.pv_estimate}kW>'

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'pv_estimate': self.pv_estimate,
            'pv_estimate10': self.pv_estimate10,
            'pv_estimate90': self.pv_estimate90,
        }
