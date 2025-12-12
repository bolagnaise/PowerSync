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

    # Fleet API Credentials (for direct Tesla Fleet API)
    fleet_api_client_id_encrypted = db.Column(db.LargeBinary)
    fleet_api_client_secret_encrypted = db.Column(db.LargeBinary)
    fleet_api_redirect_uri = db.Column(db.String(255))  # OAuth redirect URI (not encrypted, just a URL)
    fleet_api_access_token_encrypted = db.Column(db.LargeBinary)
    fleet_api_refresh_token_encrypted = db.Column(db.LargeBinary)
    fleet_api_token_expires_at = db.Column(db.DateTime)

    # Two-Factor Authentication
    totp_secret = db.Column(db.String(32))  # Base32 encoded secret for TOTP
    two_factor_enabled = db.Column(db.Boolean, default=False)

    # Status Tracking
    last_update_status = db.Column(db.String(255))
    last_update_time = db.Column(db.DateTime)

    # User Preferences
    timezone = db.Column(db.String(50), default='Australia/Brisbane')  # IANA timezone string
    sync_enabled = db.Column(db.Boolean, default=True)  # Enable/disable automatic Tesla syncing

    # Amber Electric Preferences
    amber_forecast_type = db.Column(db.String(20), default='predicted')  # 'low', 'predicted', 'high'
    solar_curtailment_enabled = db.Column(db.Boolean, default=False)  # Enable solar curtailment when export price <= 0
    current_export_rule = db.Column(db.String(20))  # Cached export rule: 'never', 'pv_only', 'battery_ok'
    current_export_rule_updated = db.Column(db.DateTime)  # When the export rule was last updated
    last_tariff_hash = db.Column(db.String(32))  # MD5 hash of last synced tariff for deduplication

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

    # Relationships
    price_records = db.relationship('PriceRecord', backref='user', lazy='dynamic')
    energy_records = db.relationship('EnergyRecord', backref='user', lazy='dynamic')
    saved_tou_profiles = db.relationship('SavedTOUProfile', backref='user', lazy='dynamic', cascade='all, delete-orphan', foreign_keys='SavedTOUProfile.user_id')
    aemo_saved_tariff = db.relationship('SavedTOUProfile', foreign_keys=[aemo_saved_tariff_id], post_update=True)

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
            issuer_name="Tesla Amber Sync"
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
