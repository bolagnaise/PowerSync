# app/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, DecimalField, IntegerField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Optional, NumberRange, Length
from app.models import User

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')

class RegistrationForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    password2 = PasswordField(
        'Repeat Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Register')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError('Please use a different email address.')

class SettingsForm(FlaskForm):
    amber_token = StringField('Amber Electric API Token')
    # Tesla Site ID is now auto-detected when connecting Tesla account

    # Tesla API Provider Selection
    tesla_api_provider = SelectField('Tesla API Provider', choices=[
        ('teslemetry', 'Teslemetry (Easier Setup, ~$4/month)'),
        ('fleet_api', 'Tesla Fleet API (Direct, Free)')
    ], default='teslemetry',
    description='Choose between Teslemetry proxy service (easier) or direct Tesla Fleet API (free but complex setup)')

    teslemetry_api_key = StringField('Teslemetry API Key (get from teslemetry.com)')

    # Fleet API OAuth Credentials
    fleet_api_client_id = StringField('Fleet API Client ID (from developer.tesla.com)')
    fleet_api_client_secret = StringField('Fleet API Client Secret (from developer.tesla.com)')
    fleet_api_redirect_uri = StringField('Fleet API Redirect URI',
        description='OAuth callback URL (e.g., http://localhost:5001/fleet-api/callback). Must match what you registered in Tesla Developer Portal.')

    # AEMO Spike Detection
    aemo_spike_detection_enabled = BooleanField('Enable AEMO Spike Detection')
    aemo_region = SelectField('AEMO Region', choices=[
        ('', 'Select Region...'),
        ('NSW1', 'NSW - New South Wales'),
        ('QLD1', 'QLD - Queensland'),
        ('VIC1', 'VIC - Victoria'),
        ('SA1', 'SA - South Australia'),
        ('TAS1', 'TAS - Tasmania')
    ], validators=[Optional()])
    aemo_spike_threshold = DecimalField(
        'Spike Threshold ($/MWh)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=300.0,
        description='Price threshold in $/MWh to trigger spike mode (e.g., 300 for $300/MWh)'
    )

    submit = SubmitField('Save Settings')


class DemandChargeForm(FlaskForm):
    """Form for configuring demand charge periods"""
    # Enable/disable demand charges
    enable_demand_charges = BooleanField('Enable Demand Charges')

    # Peak demand period
    peak_rate = DecimalField('Peak Rate ($/kW)', validators=[Optional(), NumberRange(min=0)], places=4, default=0)
    peak_start_hour = IntegerField('Peak Start Hour', validators=[Optional(), NumberRange(min=0, max=23)], default=14)
    peak_start_minute = IntegerField('Peak Start Minute', validators=[Optional(), NumberRange(min=0, max=59)], default=0)
    peak_end_hour = IntegerField('Peak End Hour', validators=[Optional(), NumberRange(min=0, max=23)], default=20)
    peak_end_minute = IntegerField('Peak End Minute', validators=[Optional(), NumberRange(min=0, max=59)], default=0)
    peak_days = SelectField('Peak Days', choices=[
        ('weekdays', 'Weekdays Only'),
        ('all', 'All Days'),
        ('weekends', 'Weekends Only')
    ], default='weekdays')
    demand_charge_apply_to = SelectField('Apply Demand Charges To', choices=[
        ('buy', 'Buy Only (Grid Import)'),
        ('sell', 'Sell Only (Solar Export)'),
        ('both', 'Both Buy and Sell')
    ], default='buy')

    # Off-peak demand period
    offpeak_rate = DecimalField('Off-Peak Rate ($/kW)', validators=[Optional(), NumberRange(min=0)], places=4, default=0)

    # Shoulder demand period (optional)
    shoulder_rate = DecimalField('Shoulder Rate ($/kW)', validators=[Optional(), NumberRange(min=0)], places=4, default=0)
    shoulder_start_hour = IntegerField('Shoulder Start Hour', validators=[Optional(), NumberRange(min=0, max=23)], default=7)
    shoulder_start_minute = IntegerField('Shoulder Start Minute', validators=[Optional(), NumberRange(min=0, max=59)], default=0)
    shoulder_end_hour = IntegerField('Shoulder End Hour', validators=[Optional(), NumberRange(min=0, max=23)], default=14)
    shoulder_end_minute = IntegerField('Shoulder End Minute', validators=[Optional(), NumberRange(min=0, max=59)], default=0)

    # Daily supply charges (optional - only for custom TOU schedules)
    daily_supply_charge = DecimalField(
        'Daily Supply Charge ($)',
        validators=[Optional(), NumberRange(min=0)],
        places=4,
        default=0,
        description='Fixed daily charge from your electricity bill (e.g., 1.1770 for $1.18/day). Amber users: This should include your daily network access and metering charges.'
    )
    monthly_supply_charge = DecimalField(
        'Monthly Supply Charge ($)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=0,
        description='Monthly account fee (e.g., $25 for Amber subscription, or other monthly charges from your provider)'
    )

    demand_artificial_price_enabled = BooleanField(
        'Artificial Price Increase (ALPHA)',
        default=False,
        description='When enabled, adds $2/kWh to import prices during your configured demand periods. This discourages the Powerwall from importing grid energy during peak times, helping reduce demand charges. This is an experimental feature.'
    )

    submit = SubmitField('Save Demand Charges')


class AmberSettingsForm(FlaskForm):
    """Form for configuring Amber Electric specific settings"""
    # Amber site selection (for accounts with multiple sites)
    amber_site_id = SelectField('Amber Site',
    choices=[],
    coerce=str,
    description='Select which Amber Electric site to use for pricing data. This will be auto-populated from your Amber account.')

    # Forecast type selection
    amber_forecast_type = SelectField('Forecast Pricing Type', choices=[
        ('predicted', 'Predicted (Default)'),
        ('low', 'Low (Conservative)'),
        ('high', 'High (Optimistic)')
    ], default='predicted', validators=[DataRequired()],
    description='Select which Amber forecast to use for TOU tariff: Low (conservative), Predicted (default), or High (optimistic)')

    # Solar curtailment toggle
    solar_curtailment_enabled = BooleanField('Enable DC Solar Curtailment',
    description='Prevent solar export when Amber feed-in price is 0c or negative. Automatically sets Powerwall export to "never" during negative pricing periods to avoid paying to export.')

    submit = SubmitField('Save Amber Settings')


class CustomTOUScheduleForm(FlaskForm):
    """Form for creating/editing custom TOU schedules"""
    # Tesla API: Utility Provider - shown in Tesla app as the electricity provider
    utility = StringField(
        'Utility Provider',
        validators=[DataRequired()],
        description='Your electricity company (e.g., "Origin Energy", "AGL", "Energy Australia")'
    )

    # Tesla API: Rate Plan Name - shown in Tesla app as the tariff name
    name = StringField(
        'Rate Plan Name',
        validators=[DataRequired()],
        description='Descriptive name for this rate plan (e.g., "Single Rate + TOU", "Residential Demand TOU")'
    )

    # Tesla API: Tariff Code - optional identifier for the specific tariff
    code = StringField(
        'Tariff Code (Optional)',
        validators=[Optional()],
        description='Official tariff code from your provider (e.g., "EA205", "DMO1", "TOU-GS")'
    )

    # Daily and monthly charges
    daily_charge = DecimalField(
        'Daily Supply Charge ($)',
        validators=[Optional(), NumberRange(min=0)],
        places=4,
        default=0,
        description='Fixed daily charge in AUD (e.g., 1.1770 for $1.18/day)'
    )
    monthly_charge = DecimalField(
        'Monthly Fixed Charge ($)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=0,
        description='Fixed monthly charge if applicable'
    )

    submit = SubmitField('Save Schedule')


class TOUSeasonForm(FlaskForm):
    """Form for adding/editing seasons in a TOU schedule"""
    name = StringField('Season Name', validators=[DataRequired()])
    from_month = IntegerField('From Month (1-12)', validators=[DataRequired(), NumberRange(min=1, max=12)])
    from_day = IntegerField('From Day (1-31)', validators=[DataRequired(), NumberRange(min=1, max=31)])
    to_month = IntegerField('To Month (1-12)', validators=[DataRequired(), NumberRange(min=1, max=12)])
    to_day = IntegerField('To Day (1-31)', validators=[DataRequired(), NumberRange(min=1, max=31)])
    submit = SubmitField('Save Season')


class TOUPeriodForm(FlaskForm):
    """Form for adding/editing time periods in a season"""
    name = StringField('Period Name', validators=[DataRequired()])
    from_hour = IntegerField('From Hour (0-23)', validators=[DataRequired(), NumberRange(min=0, max=23)])
    from_minute = SelectField('From Minute', choices=[('0', '00'), ('30', '30')], validators=[DataRequired()])
    to_hour = IntegerField('To Hour (0-23)', validators=[DataRequired(), NumberRange(min=0, max=23)])
    to_minute = SelectField('To Minute', choices=[('0', '00'), ('30', '30')], validators=[DataRequired()])
    from_day_of_week = SelectField('From Day', choices=[
        ('0', 'Monday'),
        ('1', 'Tuesday'),
        ('2', 'Wednesday'),
        ('3', 'Thursday'),
        ('4', 'Friday'),
        ('5', 'Saturday'),
        ('6', 'Sunday')
    ], default='0', validators=[DataRequired()])
    to_day_of_week = SelectField('To Day', choices=[
        ('0', 'Monday'),
        ('1', 'Tuesday'),
        ('2', 'Wednesday'),
        ('3', 'Thursday'),
        ('4', 'Friday'),
        ('5', 'Saturday'),
        ('6', 'Sunday')
    ], default='6', validators=[DataRequired()])
    energy_rate = DecimalField('Buy Rate ($/kWh)', validators=[DataRequired(), NumberRange(min=0)], places=4)
    sell_rate = DecimalField('Sell Rate ($/kWh)', validators=[DataRequired(), NumberRange(min=0)], places=4)
    demand_rate = DecimalField('Demand Rate ($/kW)', validators=[Optional(), NumberRange(min=0)], places=4, default=0)
    submit = SubmitField('Save Period')


class TwoFactorSetupForm(FlaskForm):
    """Form for verifying TOTP code during 2FA setup"""
    token = StringField('Verification Code', validators=[DataRequired(), Length(min=6, max=6, message='Enter the 6-digit code from your authenticator app')])
    submit = SubmitField('Enable 2FA')


class TwoFactorVerifyForm(FlaskForm):
    """Form for verifying TOTP code during login"""
    token = StringField('Verification Code', validators=[DataRequired(), Length(min=6, max=6, message='Enter the 6-digit code from your authenticator app')])
    submit = SubmitField('Verify')


class TwoFactorDisableForm(FlaskForm):
    """Form for disabling 2FA (requires current TOTP code)"""
    token = StringField('Verification Code', validators=[DataRequired(), Length(min=6, max=6, message='Enter the 6-digit code to confirm')])
    submit = SubmitField('Disable 2FA')


class ChangePasswordForm(FlaskForm):
    """Form for changing password (requires current password)"""
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=6, message='Password must be at least 6 characters')])
    confirm_password = PasswordField('Confirm New Password', validators=[DataRequired(), EqualTo('new_password', message='Passwords must match')])
    submit = SubmitField('Change Password')

