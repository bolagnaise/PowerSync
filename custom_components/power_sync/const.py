"""Constants for the PowerSync integration."""
from datetime import timedelta
import json
from pathlib import Path

# Integration domain
DOMAIN = "power_sync"

# Version from manifest.json (single source of truth)
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
try:
    with open(_MANIFEST_PATH) as f:
        _manifest = json.load(f)
    POWER_SYNC_VERSION = _manifest.get("version", "0.0.0")
except (FileNotFoundError, json.JSONDecodeError):
    POWER_SYNC_VERSION = "0.0.0"

# User-Agent for API identification
POWER_SYNC_USER_AGENT = f"PowerSync/{POWER_SYNC_VERSION} HomeAssistant"

# Configuration keys
CONF_AMBER_API_TOKEN = "amber_api_token"
CONF_AMBER_SITE_ID = "amber_site_id"
CONF_TESLEMETRY_API_TOKEN = "teslemetry_api_token"
CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
CONF_AUTO_SYNC_ENABLED = "auto_sync_enabled"
CONF_TIMEZONE = "timezone"
CONF_AMBER_FORECAST_TYPE = "amber_forecast_type"
CONF_BATTERY_CURTAILMENT_ENABLED = "battery_curtailment_enabled"

# Automations - OpenWeatherMap API for weather triggers
CONF_OPENWEATHERMAP_API_KEY = "openweathermap_api_key"
CONF_WEATHER_LOCATION = "weather_location"

# EV Charging configuration
CONF_EV_CHARGING_ENABLED = "ev_charging_enabled"

# EV Control Provider selection
CONF_EV_PROVIDER = "ev_provider"
EV_PROVIDER_FLEET_API = "fleet_api"  # Tesla Fleet API / Teslemetry
EV_PROVIDER_TESLA_BLE = "tesla_ble"  # ESPHome Tesla BLE
EV_PROVIDER_BOTH = "both"  # Use both providers

EV_PROVIDERS = {
    EV_PROVIDER_FLEET_API: "Tesla Fleet API / Teslemetry",
    EV_PROVIDER_TESLA_BLE: "Tesla BLE (ESPHome)",
    EV_PROVIDER_BOTH: "Both (Fleet API + BLE)",
}

# Tesla BLE configuration (ESPHome Tesla BLE integration)
CONF_TESLA_BLE_ENABLED = "tesla_ble_enabled"
CONF_TESLA_BLE_ENTITY_PREFIX = "tesla_ble_entity_prefix"
DEFAULT_TESLA_BLE_ENTITY_PREFIX = "tesla_ble"

# Tesla BLE entity patterns (based on esphome-tesla-ble)
# Sensors
TESLA_BLE_SENSOR_CHARGE_LEVEL = "sensor.{prefix}_charge_level"
TESLA_BLE_SENSOR_CHARGING_STATE = "sensor.{prefix}_charging_state"
TESLA_BLE_SENSOR_CHARGE_LIMIT = "sensor.{prefix}_charge_limit"
TESLA_BLE_SENSOR_CHARGE_CURRENT = "sensor.{prefix}_charge_current"
TESLA_BLE_SENSOR_CHARGE_POWER = "sensor.{prefix}_charge_power"
TESLA_BLE_SENSOR_RANGE = "sensor.{prefix}_range"
# Binary sensors
TESLA_BLE_BINARY_ASLEEP = "binary_sensor.{prefix}_asleep"
TESLA_BLE_BINARY_STATUS = "binary_sensor.{prefix}_status"
TESLA_BLE_BINARY_CHARGE_FLAP = "binary_sensor.{prefix}_charge_flap"
# Controls
TESLA_BLE_SWITCH_CHARGER = "switch.{prefix}_charger"
TESLA_BLE_NUMBER_CHARGING_AMPS = "number.{prefix}_charging_amps"
TESLA_BLE_NUMBER_CHARGING_LIMIT = "number.{prefix}_charging_limit"
TESLA_BLE_BUTTON_WAKE_UP = "button.{prefix}_wake_up"
TESLA_BLE_BUTTON_UNLOCK_CHARGE_PORT = "button.{prefix}_unlock_charge_port"

# OCPP Central System configuration
CONF_OCPP_ENABLED = "ocpp_enabled"
CONF_OCPP_PORT = "ocpp_port"
DEFAULT_OCPP_PORT = 9000

# Battery System Selection
CONF_BATTERY_SYSTEM = "battery_system"
BATTERY_SYSTEM_TESLA = "tesla"
BATTERY_SYSTEM_SIGENERGY = "sigenergy"
BATTERY_SYSTEM_SUNGROW = "sungrow"

BATTERY_SYSTEMS = {
    BATTERY_SYSTEM_TESLA: "Tesla Powerwall",
    BATTERY_SYSTEM_SIGENERGY: "Sigenergy",
    BATTERY_SYSTEM_SUNGROW: "Sungrow SH-series",
}

# Sungrow SH-series Battery System Configuration (Modbus TCP)
# Hybrid inverters with integrated battery control
CONF_SUNGROW_HOST = "sungrow_host"
CONF_SUNGROW_PORT = "sungrow_port"
CONF_SUNGROW_SLAVE_ID = "sungrow_slave_id"
DEFAULT_SUNGROW_PORT = 502
DEFAULT_SUNGROW_SLAVE_ID = 1

# Sungrow Modbus Register Addresses (Battery Control)
# Reference: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
# Read Registers (Input/Holding)
SUNGROW_REG_BATTERY_VOLTAGE = 13018      # 0.1V scale
SUNGROW_REG_BATTERY_CURRENT = 13019      # 0.1A scale (signed)
SUNGROW_REG_BATTERY_POWER = 13020        # 1W (signed)
SUNGROW_REG_BATTERY_SOC = 13021          # 0.1% scale
SUNGROW_REG_BATTERY_SOH = 13022          # 0.1% scale
SUNGROW_REG_BATTERY_TEMP = 13023         # 0.1°C scale
SUNGROW_REG_LOAD_POWER = 13006           # 1W (32-bit signed)
SUNGROW_REG_EXPORT_POWER = 13008         # 1W (32-bit signed)
SUNGROW_REG_TOTAL_ACTIVE_POWER = 13032   # 1W

# Control Registers (Holding - write)
SUNGROW_REG_EMS_MODE = 13050             # 0=Self-consumption, 2=Forced, 3=External EMS
SUNGROW_REG_CHARGE_CMD = 13051           # 0xAA=Charge, 0xBB=Discharge, 0xCC=Stop
SUNGROW_REG_MAX_SOC = 13058              # 0.1% scale
SUNGROW_REG_MIN_SOC = 13059              # 0.1% scale (backup reserve)
SUNGROW_REG_MAX_DISCHARGE_CURRENT = 13066  # 0.001A scale (milliamps)
SUNGROW_REG_MAX_CHARGE_CURRENT = 13067   # 0.001A scale (milliamps)
SUNGROW_REG_EXPORT_LIMIT = 13074         # 1W
SUNGROW_REG_EXPORT_LIMIT_ENABLED = 13087 # 0=Disabled, 1=Enabled
SUNGROW_REG_BACKUP_RESERVE = 13100       # 0.1% scale

# Sungrow Command Values
SUNGROW_CMD_CHARGE = 0xAA
SUNGROW_CMD_DISCHARGE = 0xBB
SUNGROW_CMD_STOP = 0xCC
SUNGROW_EMS_SELF_CONSUMPTION = 0
SUNGROW_EMS_FORCED = 2
SUNGROW_EMS_EXTERNAL = 3

# Sungrow Battery Voltage (for current/power calculations)
SUNGROW_BATTERY_VOLTAGE_DEFAULT = 48  # Typical LFP battery pack voltage

# Tesla API Provider selection
CONF_TESLA_API_PROVIDER = "tesla_api_provider"
TESLA_PROVIDER_TESLEMETRY = "teslemetry"
TESLA_PROVIDER_FLEET_API = "fleet_api"

# All supported Tesla/EV integrations (for device/entity discovery)
# These are the HA integration domain names used in device identifiers
TESLA_INTEGRATIONS = [
    "tesla_fleet",    # Official Tesla Fleet API integration
    "teslemetry",     # Teslemetry integration
    "tessie",         # Tessie integration
    "tesla_custom",   # Tesla Custom Integration
    "tesla",          # Older Tesla integration
]

# Fleet API configuration (direct Tesla API)
CONF_FLEET_API_ACCESS_TOKEN = "fleet_api_access_token"
CONF_FLEET_API_REFRESH_TOKEN = "fleet_api_refresh_token"
CONF_FLEET_API_TOKEN_EXPIRES_AT = "fleet_api_token_expires_at"
CONF_FLEET_API_CLIENT_ID = "fleet_api_client_id"
CONF_FLEET_API_CLIENT_SECRET = "fleet_api_client_secret"

# Sigenergy Cloud API configuration
CONF_SIGENERGY_USERNAME = "sigenergy_username"
CONF_SIGENERGY_PASSWORD = "sigenergy_password"  # Plain password (will be encoded)
CONF_SIGENERGY_PASS_ENC = "sigenergy_pass_enc"  # Encoded password (backwards compat)
CONF_SIGENERGY_DEVICE_ID = "sigenergy_device_id"
CONF_SIGENERGY_STATION_ID = "sigenergy_station_id"
CONF_SIGENERGY_ACCESS_TOKEN = "sigenergy_access_token"
CONF_SIGENERGY_REFRESH_TOKEN = "sigenergy_refresh_token"
CONF_SIGENERGY_TOKEN_EXPIRES_AT = "sigenergy_token_expires_at"

# Sigenergy API
SIGENERGY_API_BASE_URL = "https://api-aus.sigencloud.com"
SIGENERGY_AUTH_ENDPOINT = "/auth/oauth/token"
SIGENERGY_SAVE_PRICE_ENDPOINT = "/device/stationelecsetprice/save"
SIGENERGY_STATIONS_ENDPOINT = "/device/station/list"
SIGENERGY_BASIC_AUTH = "Basic c2lnZW46c2lnZW4="  # base64 of "sigen:sigen"

# Sigenergy DC Curtailment via Modbus TCP
# Controls the DC solar input to Sigenergy battery system
# Reference: https://github.com/TypQxQ/Sigenergy-Local-Modbus
CONF_SIGENERGY_DC_CURTAILMENT_ENABLED = "sigenergy_dc_curtailment_enabled"
CONF_SIGENERGY_MODBUS_HOST = "sigenergy_modbus_host"
CONF_SIGENERGY_MODBUS_PORT = "sigenergy_modbus_port"
CONF_SIGENERGY_MODBUS_SLAVE_ID = "sigenergy_modbus_slave_id"
DEFAULT_SIGENERGY_MODBUS_PORT = 502
DEFAULT_SIGENERGY_MODBUS_SLAVE_ID = 247  # Sigenergy uses unit ID 247 (or 0)

# Demand charge configuration
CONF_DEMAND_CHARGE_ENABLED = "demand_charge_enabled"
CONF_DEMAND_CHARGE_RATE = "demand_charge_rate"
CONF_DEMAND_CHARGE_START_TIME = "demand_charge_start_time"
CONF_DEMAND_CHARGE_END_TIME = "demand_charge_end_time"
CONF_DEMAND_CHARGE_DAYS = "demand_charge_days"
CONF_DEMAND_CHARGE_BILLING_DAY = "demand_charge_billing_day"
CONF_DEMAND_CHARGE_APPLY_TO = "demand_charge_apply_to"
CONF_DEMAND_ARTIFICIAL_PRICE = "demand_artificial_price_enabled"

# Daily supply charge configuration
CONF_DAILY_SUPPLY_CHARGE = "daily_supply_charge"
CONF_MONTHLY_SUPPLY_CHARGE = "monthly_supply_charge"

# AEMO Spike Detection configuration (Tesla)
CONF_AEMO_SPIKE_ENABLED = "aemo_spike_enabled"
CONF_AEMO_REGION = "aemo_region"
CONF_AEMO_SPIKE_THRESHOLD = "aemo_spike_threshold"

# Sungrow AEMO Spike Detection configuration
# Hard-coded threshold for Globird VPP events ($3000/MWh = $3/kWh)
CONF_SUNGROW_AEMO_SPIKE_ENABLED = "sungrow_aemo_spike_enabled"
SUNGROW_AEMO_SPIKE_THRESHOLD = 3000.0  # $3000/MWh - Globird's VPP trigger price

# AEMO region options (NEM regions)
AEMO_REGIONS = {
    "NSW1": "NSW - New South Wales",
    "QLD1": "QLD - Queensland",
    "VIC1": "VIC - Victoria",
    "SA1": "SA - South Australia",
    "TAS1": "TAS - Tasmania",
}

# Flow Power Electricity Provider configuration
CONF_ELECTRICITY_PROVIDER = "electricity_provider"
CONF_FLOW_POWER_STATE = "flow_power_state"
CONF_FLOW_POWER_PRICE_SOURCE = "flow_power_price_source"
CONF_AEMO_SENSOR_ENTITY = "aemo_sensor_entity"  # Legacy - kept for backwards compatibility

# AEMO NEM Data sensor configuration (auto-generated based on state selection)
CONF_AEMO_SENSOR_5MIN = "aemo_sensor_5min"
CONF_AEMO_SENSOR_30MIN = "aemo_sensor_30min"

# AEMO NEM Data sensor naming patterns
# These match the sensor entity_ids created by the HA_AemoNemData integration
AEMO_SENSOR_5MIN_PATTERN = "sensor.aemo_nem_{region}_current_5min_period_price"
AEMO_SENSOR_30MIN_PATTERN = "sensor.aemo_nem_{region}_current_30min_forecast"

# Electricity provider options
ELECTRICITY_PROVIDERS = {
    "amber": "Amber Electric",
    "flow_power": "Flow Power",
    "globird": "Globird",
    "aemo_vpp": "AEMO VPP (AGL, Engie, etc.)",
    "octopus": "Octopus Energy (UK)",
}

# Octopus Energy UK configuration
CONF_OCTOPUS_PRODUCT = "octopus_product"
CONF_OCTOPUS_REGION = "octopus_region"
CONF_OCTOPUS_PRODUCT_CODE = "octopus_product_code"
CONF_OCTOPUS_TARIFF_CODE = "octopus_tariff_code"
CONF_OCTOPUS_EXPORT_PRODUCT_CODE = "octopus_export_product_code"
CONF_OCTOPUS_EXPORT_TARIFF_CODE = "octopus_export_tariff_code"

# Octopus API base URL
OCTOPUS_API_BASE_URL = "https://api.octopus.energy/v1"

# Octopus products
OCTOPUS_PRODUCTS = {
    "agile": "Agile Octopus (dynamic half-hourly)",
    "go": "Octopus Go (EV tariff)",
    "flux": "Octopus Flux (solar/battery)",
    "tracker": "Octopus Tracker (daily price)",
}

# Octopus product codes (latest versions)
OCTOPUS_PRODUCT_CODES = {
    "agile": "AGILE-24-10-01",
    "go": "GO-VAR-22-10-14",
    "flux": "FLUX-IMPORT-23-02-14",
    "tracker": "SILVER-FLEX-BB-23-02-08",
}

# Octopus export product codes
OCTOPUS_EXPORT_PRODUCT_CODES = {
    "agile": "AGILE-OUTGOING-19-05-13",  # Agile Outgoing for dynamic export
    "flux": "FLUX-EXPORT-23-02-14",  # Flux export tariff
}

# UK Grid Supply Points (GSP) - Octopus regional pricing
# Each region has different wholesale prices due to transmission constraints
OCTOPUS_GSP_REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and North Wales",
    "E": "Midlands",
    "F": "North Eastern",
    "G": "North Western",
    "H": "Southern",
    "J": "South Eastern",
    "K": "South Wales",
    "L": "South Western",
    "M": "Yorkshire",
    "N": "South Scotland",
    "P": "North Scotland",
}

# Flow Power state options with export rates
FLOW_POWER_STATES = {
    "NSW1": "New South Wales (45c export)",
    "VIC1": "Victoria (35c export)",
    "QLD1": "Queensland (45c export)",
    "SA1": "South Australia (45c export)",
}

# Flow Power price source options
FLOW_POWER_PRICE_SOURCES = {
    "amber": "Amber API",
    "aemo": "AEMO Direct (NEMWeb)",
    "aemo_sensor": "AEMO NEM Data Sensor (Legacy)",  # Kept for backwards compatibility
}

# Network Tariff configuration (for Flow Power + AEMO)
# AEMO wholesale prices don't include DNSP network fees
# Primary: Use aemo_to_tariff library with distributor + tariff code
# Fallback: Manual rate entry when use_manual_rates is True
CONF_NETWORK_DISTRIBUTOR = "network_distributor"
CONF_NETWORK_TARIFF_CODE = "network_tariff_code"
CONF_NETWORK_USE_MANUAL_RATES = "network_use_manual_rates"

# Manual rate entry configuration
CONF_NETWORK_TARIFF_TYPE = "network_tariff_type"
CONF_NETWORK_FLAT_RATE = "network_flat_rate"
CONF_NETWORK_PEAK_RATE = "network_peak_rate"
CONF_NETWORK_SHOULDER_RATE = "network_shoulder_rate"
CONF_NETWORK_OFFPEAK_RATE = "network_offpeak_rate"
CONF_NETWORK_PEAK_START = "network_peak_start"
CONF_NETWORK_PEAK_END = "network_peak_end"
CONF_NETWORK_OFFPEAK_START = "network_offpeak_start"
CONF_NETWORK_OFFPEAK_END = "network_offpeak_end"
CONF_NETWORK_OTHER_FEES = "network_other_fees"
CONF_NETWORK_INCLUDE_GST = "network_include_gst"

# Network tariff type options
NETWORK_TARIFF_TYPES = {
    "flat": "Flat Rate (single rate all day)",
    "tou": "Time of Use (peak/shoulder/off-peak)",
}

# Network distributor (DNSP) options
# These match the module names in the aemo_to_tariff library
# CitiPower and United use generic Victoria tariffs
NETWORK_DISTRIBUTORS = {
    "energex": "Energex (QLD SE)",
    "ergon": "Ergon Energy (QLD Regional)",
    "ausgrid": "Ausgrid (NSW)",
    "endeavour": "Endeavour Energy (NSW)",
    "essential": "Essential Energy (NSW Regional)",
    "sapower": "SA Power Networks (SA)",
    "powercor": "Powercor (VIC West)",
    "citipower": "CitiPower (VIC Melbourne)",
    "ausnet": "AusNet Services (VIC East)",
    "jemena": "Jemena (VIC North)",
    "united": "United Energy (VIC South)",
    "tasnetworks": "TasNetworks (TAS)",
    "evoenergy": "Evoenergy (ACT)",
}

# Network tariffs per distributor (from aemo_to_tariff library)
# Format: {distributor: {code: name, ...}}
NETWORK_TARIFFS = {
    "energex": {
        "6900": "Residential Time of Use",
        "8400": "Residential Flat",
        "3700": "Residential Demand",
        "3900": "Residential Transitional Demand",
        "6800": "Small Business ToU",
        "8500": "Small Business Flat",
        "3600": "Small Business Demand",
        "3800": "Small Business Transitional Demand",
        "6000": "Small Business Wide IFT",
        "8800": "Small 8800 TOU",
        "8900": "Small 8900 TOU",
        "6600": "Large Residential Energy",
        "6700": "Large Business Energy",
        "7200": "LV Demand Time-of-Use",
        "8100": "Demand Large",
        "8300": "SAC Demand Small",
        "94300": "Large TOU Energy",
    },
    "ergon": {
        "6900": "Residential Time of Use",
        "ERTOUET1": "Residential Battery ToU",
        "WRTOUET1": "Residential Wide ToU",
        "MRTOUET4": "Residential Multi ToU",
    },
    "ausgrid": {
        "EA025": "Residential ToU",
        "EA010": "Residential Flat",
        "EA111": "Residential Demand (Intro)",
        "EA116": "Residential Demand",
        "EA225": "Small Business ToU",
        "EA305": "Small Business LV",
    },
    "endeavour": {
        "N71": "Residential Seasonal TOU",
        "N70": "Residential Flat",
        "N90": "General Supply Block",
        "N91": "GS Seasonal TOU",
        "N19": "LV Seasonal STOU Demand",
        "N95": "Storage",
    },
    "essential": {
        "BLNT3AU": "Residential TOU (Basic)",
        "BLNT3AL": "Residential TOU (Interval)",
        "BLNN2AU": "Residential Anytime",
        "BLNRSS2": "Residential Sun Soaker",
        "BLND1AR": "Residential Demand",
        "BLNT2AU": "Small Business TOU (Basic)",
        "BLNT2AL": "Small Business TOU (Interval)",
        "BLNN1AU": "Small Business Anytime",
        "BLNBSS1": "Small Business Sun Soaker",
        "BLND1AB": "Small Business Demand",
        "BLNC1AU": "Controlled Load 1",
        "BLNC2AU": "Controlled Load 2",
        "BLNT1AO": "Small Business TOU (100-160 MWh)",
    },
    "sapower": {
        "RTOU": "Residential Time of Use",
        "RSR": "Residential Single Rate",
        "RTOUNE": "Residential TOU (New)",
        "RPRO": "Residential Prosumer",
        "RELE": "Residential Electrify",
        "RESELE": "Residential Electrify (Alt)",
        "RELE2W": "Residential Electrify 2W",
        "SBTOU": "Small Business Time of Use",
        "SBTOUNE": "Small Business TOU (New)",
        "SBELE": "Small Business Electrify",
        "B2R": "Business Two Rate",
    },
    "powercor": {
        "PRTOU": "Residential TOU",
        "D1": "Residential Single Rate",
        "NDMO21": "NDMO21 TOU",
        "NDTOU": "NDTOU TOU",
        "PRDS": "Residential Daytime Saver",
    },
    "citipower": {
        "VICR_TOU": "Residential Time of Use",
        "VICR_SINGLE": "Residential Single Rate",
        "VICR_DEMAND": "Residential Demand",
        "VICS_TOU": "Small Business Time of Use",
        "VICS_SINGLE": "Small Business Single Rate",
        "VICS_DEMAND": "Small Business Demand",
    },
    "ausnet": {
        "NAST11S": "Small Business Time of Use",
    },
    "jemena": {
        "PRTOU": "Residential TOU",
        "D1": "Residential Single Rate",
    },
    "united": {
        "VICR_TOU": "Residential Time of Use",
        "VICR_SINGLE": "Residential Single Rate",
        "VICR_DEMAND": "Residential Demand",
        "VICS_TOU": "Small Business Time of Use",
        "VICS_SINGLE": "Small Business Single Rate",
        "VICS_DEMAND": "Small Business Demand",
    },
    "tasnetworks": {
        "TAS93": "Residential TOU Consumption",
        "TAS87": "Residential TOU Demand",
        "TAS97": "Residential TOU CER",
        "TAS94": "Small Business TOU Consumption",
        "TAS88": "Small Business TOU Demand",
    },
    "evoenergy": {
        "017": "Residential TOU Network",
        "018": "Residential TOU Network XMC",
        "015": "Residential TOU (Closed)",
        "016": "Residential TOU XMC (Closed)",
        "026": "Residential Demand",
        "090": "Component Charge",
    },
}


def get_tariff_options(distributor: str) -> dict[str, str]:
    """Get tariff options for a specific distributor."""
    tariffs = NETWORK_TARIFFS.get(distributor, {})
    return {code: f"{code} - {name}" for code, name in tariffs.items()}


def get_all_tariff_options() -> dict[str, str]:
    """Get all tariff options as distributor:code -> description."""
    options = {}
    for distributor, tariffs in NETWORK_TARIFFS.items():
        dist_name = NETWORK_DISTRIBUTORS.get(distributor, distributor)
        # Extract short name (before the parenthesis)
        short_name = dist_name.split(" (")[0] if " (" in dist_name else dist_name
        for code, name in tariffs.items():
            key = f"{distributor}:{code}"
            options[key] = f"{short_name} - {code} ({name})"
    return options


# Pre-built flat list of all tariffs for dropdown
# Format: "distributor:code" -> "Distributor - Code (Name)"
ALL_NETWORK_TARIFFS = get_all_tariff_options()

# Flow Power Happy Hour export rates ($/kWh)
FLOW_POWER_EXPORT_RATES = {
    "NSW1": 0.45,   # 45c/kWh
    "QLD1": 0.45,   # 45c/kWh
    "SA1": 0.45,    # 45c/kWh
    "VIC1": 0.35,   # 35c/kWh
}

# Flow Power Happy Hour periods (5:30pm to 7:30pm)
FLOW_POWER_HAPPY_HOUR_PERIODS = [
    "PERIOD_17_30",  # 5:30pm - 6:00pm
    "PERIOD_18_00",  # 6:00pm - 6:30pm
    "PERIOD_18_30",  # 6:30pm - 7:00pm
    "PERIOD_19_00",  # 7:00pm - 7:30pm
]

# Flow Power PEA (Price Efficiency Adjustment) configuration
# PEA adjusts pricing based on wholesale market efficiency
# Formula: PEA = wholesale - market_avg - benchmark = wholesale - 9.7c
CONF_PEA_ENABLED = "pea_enabled"
CONF_FLOW_POWER_BASE_RATE = "flow_power_base_rate"
CONF_PEA_CUSTOM_VALUE = "pea_custom_value"

# PEA Constants
FLOW_POWER_MARKET_AVG = 8.0       # Market TWAP average (c/kWh)
FLOW_POWER_BENCHMARK = 1.7       # BPEA - benchmark customer performance (c/kWh)
FLOW_POWER_PEA_OFFSET = 9.7      # Combined: MARKET_AVG + BENCHMARK (c/kWh)
FLOW_POWER_DEFAULT_BASE_RATE = 34.0  # Default Flow Power base rate (c/kWh)

# Data coordinator update intervals
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)  # Amber updates every 5 minutes
UPDATE_INTERVAL_ENERGY = timedelta(minutes=1)  # Tesla energy data every minute

# Amber API
AMBER_API_BASE_URL = "https://api.amber.com.au/v1"

# AEMO API
AEMO_API_BASE_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"

# Teslemetry API
TESLEMETRY_API_BASE_URL = "https://api.teslemetry.com"

# Tesla Fleet API (direct)
FLEET_API_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
FLEET_API_AUTH_URL = "https://auth.tesla.com/oauth2/v3"
FLEET_API_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

# Services
SERVICE_SYNC_TOU = "sync_tou_schedule"
SERVICE_SYNC_NOW = "sync_now"

# Sensor types
SENSOR_TYPE_CURRENT_PRICE = "current_price"  # Legacy - kept for compatibility
SENSOR_TYPE_CURRENT_IMPORT_PRICE = "current_import_price"
SENSOR_TYPE_CURRENT_EXPORT_PRICE = "current_export_price"
SENSOR_TYPE_FORECAST_PRICE = "forecast_price"
SENSOR_TYPE_SOLAR_POWER = "solar_power"
SENSOR_TYPE_GRID_POWER = "grid_power"
SENSOR_TYPE_BATTERY_POWER = "battery_power"
SENSOR_TYPE_HOME_LOAD = "home_load"
SENSOR_TYPE_BATTERY_LEVEL = "battery_level"
SENSOR_TYPE_DAILY_SOLAR_ENERGY = "daily_solar_energy"
SENSOR_TYPE_DAILY_GRID_IMPORT = "daily_grid_import"
SENSOR_TYPE_DAILY_GRID_EXPORT = "daily_grid_export"
SENSOR_TYPE_DAILY_BATTERY_CHARGE = "daily_battery_charge"
SENSOR_TYPE_DAILY_BATTERY_DISCHARGE = "daily_battery_discharge"

# Demand charge sensors
SENSOR_TYPE_GRID_IMPORT_POWER = "grid_import_power"
SENSOR_TYPE_IN_DEMAND_CHARGE_PERIOD = "in_demand_charge_period"
SENSOR_TYPE_PEAK_DEMAND_THIS_CYCLE = "peak_demand_this_cycle"
SENSOR_TYPE_DEMAND_CHARGE_COST = "demand_charge_cost"
SENSOR_TYPE_DAYS_UNTIL_DEMAND_RESET = "days_until_demand_reset"

# Supply charge sensors
SENSOR_TYPE_DAILY_SUPPLY_CHARGE_COST = "daily_supply_charge_cost"
SENSOR_TYPE_MONTHLY_SUPPLY_CHARGE = "monthly_supply_charge"
SENSOR_TYPE_TOTAL_MONTHLY_COST = "total_monthly_cost"

# Switch types
SWITCH_TYPE_AUTO_SYNC = "auto_sync"
SWITCH_TYPE_FORCE_DISCHARGE = "force_discharge"
SWITCH_TYPE_FORCE_CHARGE = "force_charge"

# Battery mode sensor (for automation triggers)
SENSOR_TYPE_BATTERY_MODE = "battery_mode"

# Battery mode states
BATTERY_MODE_STATE_NORMAL = "normal"
BATTERY_MODE_STATE_FORCE_CHARGE = "force_charge"
BATTERY_MODE_STATE_FORCE_DISCHARGE = "force_discharge"

# Services for manual battery control
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_RESTORE_NORMAL = "restore_normal"
SERVICE_GET_CALENDAR_HISTORY = "get_calendar_history"
SERVICE_SYNC_BATTERY_HEALTH = "sync_battery_health"
SERVICE_SET_BACKUP_RESERVE = "set_backup_reserve"
SERVICE_SET_OPERATION_MODE = "set_operation_mode"
SERVICE_SET_GRID_EXPORT = "set_grid_export"
SERVICE_SET_GRID_CHARGING = "set_grid_charging"
SERVICE_CURTAIL_INVERTER = "curtail_inverter"
SERVICE_RESTORE_INVERTER = "restore_inverter"

# Manual discharge/charge duration options (minutes)
DISCHARGE_DURATIONS = [15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240]
DEFAULT_DISCHARGE_DURATION = 30

# Duration dropdown entity option keys (stored in ConfigEntry.options)
CONF_FORCE_CHARGE_DURATION = "force_charge_duration"
CONF_FORCE_DISCHARGE_DURATION = "force_discharge_duration"

# AEMO Spike sensors
SENSOR_TYPE_AEMO_PRICE = "aemo_price"
SENSOR_TYPE_AEMO_SPIKE_STATUS = "aemo_spike_status"

# Solcast Solar Forecast sensors
SENSOR_TYPE_SOLCAST_TODAY = "solcast_today_forecast"
SENSOR_TYPE_SOLCAST_TOMORROW = "solcast_tomorrow_forecast"
SENSOR_TYPE_SOLCAST_CURRENT = "solcast_current_estimate"

# Solcast Configuration
CONF_SOLCAST_API_KEY = "solcast_api_key"
CONF_SOLCAST_RESOURCE_ID = "solcast_resource_id"
CONF_SOLCAST_ENABLED = "solcast_enabled"

# Tariff schedule sensor
SENSOR_TYPE_TARIFF_SCHEDULE = "tariff_schedule"

# Solar curtailment sensor
SENSOR_TYPE_SOLAR_CURTAILMENT = "solar_curtailment"

# Flow Power price sensors
SENSOR_TYPE_FLOW_POWER_PRICE = "flow_power_price"
SENSOR_TYPE_FLOW_POWER_EXPORT_PRICE = "flow_power_export_price"

# Battery health sensor (from mobile app TEDAPI scans)
SENSOR_TYPE_BATTERY_HEALTH = "battery_health"

# Amber Export Price Boost configuration
# Artificially increase export prices to trigger Powerwall exports
CONF_EXPORT_PRICE_OFFSET = "export_price_offset"
CONF_EXPORT_MIN_PRICE = "export_min_price"
CONF_EXPORT_BOOST_ENABLED = "export_boost_enabled"
CONF_EXPORT_BOOST_START = "export_boost_start"
CONF_EXPORT_BOOST_END = "export_boost_end"
CONF_EXPORT_BOOST_THRESHOLD = "export_boost_threshold"  # Min price to activate boost

# Default values for export boost
DEFAULT_EXPORT_PRICE_OFFSET = 0.0  # c/kWh
DEFAULT_EXPORT_MIN_PRICE = 0.0     # c/kWh
DEFAULT_EXPORT_BOOST_START = "17:00"
DEFAULT_EXPORT_BOOST_END = "21:00"
DEFAULT_EXPORT_BOOST_THRESHOLD = 0.0  # c/kWh (0 = always apply boost)

# Chip Mode configuration
# Inverse of Export Boost - prevents exports unless price exceeds threshold
# Useful for overnight stability while still capturing price spikes
CONF_CHIP_MODE_ENABLED = "chip_mode_enabled"
CONF_CHIP_MODE_START = "chip_mode_start"
CONF_CHIP_MODE_END = "chip_mode_end"
CONF_CHIP_MODE_THRESHOLD = "chip_mode_threshold"

# Default values for Chip Mode
DEFAULT_CHIP_MODE_START = "22:00"
DEFAULT_CHIP_MODE_END = "06:00"
DEFAULT_CHIP_MODE_THRESHOLD = 30.0  # c/kWh (allow export only above this)

# Amber Spike Protection configuration
# Prevents Powerwall from charging from grid during price spikes
# When Amber reports spikeStatus='potential' or 'spike', override buy prices
# to max(sell_prices) + $1.00 to eliminate arbitrage opportunities
CONF_SPIKE_PROTECTION_ENABLED = "spike_protection_enabled"

# Settled Prices Only mode
# Skips the initial forecast sync at :00 and only syncs when actual/settled prices
# arrive via the Amber API at :35/:60 seconds into each 5-minute period
CONF_SETTLED_PRICES_ONLY = "settled_prices_only"

# Forecast Discrepancy Alert configuration
# Compares predicted forecast against conservative/low forecast and alerts if
# they differ significantly (indicates forecast model may be unreliable)
CONF_FORECAST_DISCREPANCY_ALERT = "forecast_discrepancy_alert"
CONF_FORECAST_DISCREPANCY_THRESHOLD = "forecast_discrepancy_threshold"
DEFAULT_FORECAST_DISCREPANCY_THRESHOLD = 10.0  # c/kWh - alert if avg difference > 10c

# Price Spike Alert configuration
# Alerts when any forecast interval exceeds a price threshold (catches extreme prices)
# This is separate from discrepancy - it catches unrealistic predicted prices
# Supports separate thresholds for import (buy) and export (sell) prices
CONF_PRICE_SPIKE_ALERT = "price_spike_alert"
CONF_PRICE_SPIKE_IMPORT_THRESHOLD = "price_spike_import_threshold"
CONF_PRICE_SPIKE_EXPORT_THRESHOLD = "price_spike_export_threshold"
DEFAULT_PRICE_SPIKE_IMPORT_THRESHOLD = 100.0  # c/kWh - alert if import > $1/kWh
DEFAULT_PRICE_SPIKE_EXPORT_THRESHOLD = 50.0  # c/kWh - alert if export > $0.50/kWh (negative = you get paid)

# Alpha: Force tariff mode toggle
# After uploading a tariff, briefly switch to self_consumption then back to autonomous
# to force Powerwall to immediately recalculate behavior based on new prices
CONF_FORCE_TARIFF_MODE_TOGGLE = "force_tariff_mode_toggle"

# Attributes
ATTR_LAST_SYNC = "last_sync"
ATTR_SYNC_STATUS = "sync_status"
ATTR_PRICE_SPIKE = "price_spike"
ATTR_WHOLESALE_PRICE = "wholesale_price"
ATTR_NETWORK_PRICE = "network_price"
ATTR_AEMO_REGION = "aemo_region"
ATTR_AEMO_THRESHOLD = "aemo_threshold"
ATTR_SPIKE_START_TIME = "spike_start_time"

# AC-Coupled Inverter Curtailment configuration
# Direct control of solar inverters for AC-coupled systems where Tesla
# curtailment alone cannot prevent grid export (solar bypasses Powerwall)
CONF_AC_INVERTER_CURTAILMENT_ENABLED = "ac_inverter_curtailment_enabled"
CONF_INVERTER_BRAND = "inverter_brand"
CONF_INVERTER_MODEL = "inverter_model"
CONF_INVERTER_HOST = "inverter_host"
CONF_INVERTER_PORT = "inverter_port"
CONF_INVERTER_SLAVE_ID = "inverter_slave_id"
CONF_INVERTER_TOKEN = "inverter_token"  # JWT token for Enphase IQ Gateway (firmware 7.x+)
CONF_ENPHASE_USERNAME = "enphase_username"  # Enlighten username/email for auto token refresh
CONF_ENPHASE_PASSWORD = "enphase_password"  # Enlighten password for auto token refresh
CONF_ENPHASE_SERIAL = "enphase_serial"  # Envoy serial number (optional, auto-detected)
CONF_ENPHASE_NORMAL_PROFILE = "enphase_normal_profile"  # Grid profile name for normal operation (fallback)
CONF_ENPHASE_ZERO_EXPORT_PROFILE = "enphase_zero_export_profile"  # Grid profile for zero export (fallback)
CONF_ENPHASE_IS_INSTALLER = "enphase_is_installer"  # Whether to request installer-level token for grid profile access
CONF_INVERTER_RESTORE_SOC = "inverter_restore_soc"  # Battery SOC % below which to restore inverter
DEFAULT_INVERTER_RESTORE_SOC = 98  # Restore inverter when battery drops below 98%
# Fronius-specific: load following mode for users without 0W export profile
CONF_FRONIUS_LOAD_FOLLOWING = "fronius_load_following"

# Supported AC-coupled inverter brands (for systems with separate solar inverter)
# Note: Sigenergy is NOT here - it's a DC-coupled battery system, not an AC inverter
INVERTER_BRANDS = {
    "sungrow": "Sungrow",
    "fronius": "Fronius",
    "goodwe": "GoodWe",
    "huawei": "Huawei",
    "enphase": "Enphase",
    "zeversolar": "Zeversolar",
}

# Fronius models (SunSpec Modbus)
FRONIUS_MODELS = {
    "primo": "Primo (Single Phase)",
    "symo": "Symo (Three Phase)",
    "gen24": "Gen24 / Tauro",
    "eco": "Eco",
}

# GoodWe models (ET/EH/BT/BH series support export limiting)
# Note: DT/D-NS series do NOT support export limiting via Modbus
GOODWE_MODELS = {
    "et": "ET Series (Hybrid)",
    "eh": "EH Series (Hybrid)",
    "bt": "BT Series (Hybrid)",
    "bh": "BH Series (Hybrid)",
    "es": "ES Series (Hybrid)",
    "em": "EM Series (Hybrid)",
}

# Huawei SUN2000 series (via Smart Dongle Modbus TCP)
# Reference: https://github.com/wlcrs/huawei-solar-lib
# L1 Series (Single Phase Hybrid)
HUAWEI_L1_MODELS = {
    "sun2000-2ktl-l1": "SUN2000-2KTL-L1",
    "sun2000-3ktl-l1": "SUN2000-3KTL-L1",
    "sun2000-3.68ktl-l1": "SUN2000-3.68KTL-L1",
    "sun2000-4ktl-l1": "SUN2000-4KTL-L1",
    "sun2000-4.6ktl-l1": "SUN2000-4.6KTL-L1",
    "sun2000-5ktl-l1": "SUN2000-5KTL-L1",
    "sun2000-6ktl-l1": "SUN2000-6KTL-L1",
}

# M0/M1 Series (Three Phase)
HUAWEI_M1_MODELS = {
    "sun2000-3ktl-m0": "SUN2000-3KTL-M0",
    "sun2000-4ktl-m0": "SUN2000-4KTL-M0",
    "sun2000-5ktl-m0": "SUN2000-5KTL-M0",
    "sun2000-6ktl-m0": "SUN2000-6KTL-M0",
    "sun2000-8ktl-m0": "SUN2000-8KTL-M0",
    "sun2000-10ktl-m0": "SUN2000-10KTL-M0",
    "sun2000-3ktl-m1": "SUN2000-3KTL-M1",
    "sun2000-4ktl-m1": "SUN2000-4KTL-M1",
    "sun2000-5ktl-m1": "SUN2000-5KTL-M1",
    "sun2000-6ktl-m1": "SUN2000-6KTL-M1",
    "sun2000-8ktl-m1": "SUN2000-8KTL-M1",
    "sun2000-10ktl-m1": "SUN2000-10KTL-M1",
}

# M2 Series (Three Phase, Higher Power)
HUAWEI_M2_MODELS = {
    "sun2000-8ktl-m2": "SUN2000-8KTL-M2",
    "sun2000-10ktl-m2": "SUN2000-10KTL-M2",
    "sun2000-12ktl-m2": "SUN2000-12KTL-M2",
    "sun2000-15ktl-m2": "SUN2000-15KTL-M2",
    "sun2000-17ktl-m2": "SUN2000-17KTL-M2",
    "sun2000-20ktl-m2": "SUN2000-20KTL-M2",
}

# Combined Huawei models
HUAWEI_MODELS = {
    **HUAWEI_L1_MODELS,
    **HUAWEI_M1_MODELS,
    **HUAWEI_M2_MODELS,
}

# Enphase microinverter systems (via IQ Gateway/Envoy REST API)
# Reference: https://github.com/pyenphase/pyenphase
# Note: Requires JWT token for firmware 7.x+, DPEL requires installer access
ENPHASE_GATEWAY_MODELS = {
    "envoy": "Envoy (Legacy)",
    "envoy-s": "Envoy-S",
    "envoy-s-metered": "Envoy-S Metered",
    "iq-gateway": "IQ Gateway",
    "iq-gateway-metered": "IQ Gateway Metered",
}

ENPHASE_MICROINVERTER_MODELS = {
    "iq7": "IQ7 Series",
    "iq7+": "IQ7+ Series",
    "iq7a": "IQ7A Series",
    "iq7x": "IQ7X Series",
    "iq8": "IQ8 Series",
    "iq8+": "IQ8+ Series",
    "iq8a": "IQ8A Series",
    "iq8m": "IQ8M Series",
    "iq8h": "IQ8H Series",
}

# Combined Enphase models (show gateway models in dropdown)
ENPHASE_MODELS = {
    **ENPHASE_GATEWAY_MODELS,
}

# Zeversolar models (via HTTP API to built-in web interface)
# Uses POST to /pwrlim.cgi for power limiting
ZEVERSOLAR_MODELS = {
    "tlc5000": "TLC5000",
    "tlc6000": "TLC6000",
    "tlc8000": "TLC8000",
    "tlc10000": "TLC10000",
    "zeversolair-mini-3000": "Zeversolair Mini 3000",
    "zeversolair-tl3000": "Zeversolair TL3000",
}

# Sungrow SG series (string inverters) - single phase residential
SUNGROW_SG_MODELS = {
    "sg2.5rs": "SG2.5RS",
    "sg3.0rs": "SG3.0RS",
    "sg3.6rs": "SG3.6RS",
    "sg4.0rs": "SG4.0RS",
    "sg5.0rs": "SG5.0RS",
    "sg6.0rs": "SG6.0RS",
    "sg7.0rs": "SG7.0RS",
    "sg8.0rs": "SG8.0RS",
    "sg10rs": "SG10RS",
    "sg12rs": "SG12RS",
    "sg15rs": "SG15RS",
    "sg17rs": "SG17RS",
    "sg20rs": "SG20RS",
}

# Sungrow SH series (hybrid inverters with battery)
# Reference: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
# Single phase RS series
SUNGROW_SH_RS_MODELS = {
    "sh3.0rs": "SH3.0RS",
    "sh3.6rs": "SH3.6RS",
    "sh4.0rs": "SH4.0RS",
    "sh4.6rs": "SH4.6RS",
    "sh5.0rs": "SH5.0RS",
    "sh6.0rs": "SH6.0RS",
}

# Three phase RT series (residential)
SUNGROW_SH_RT_MODELS = {
    "sh5.0rt": "SH5.0RT",
    "sh6.0rt": "SH6.0RT",
    "sh8.0rt": "SH8.0RT",
    "sh10rt": "SH10RT",
    "sh5.0rt-20": "SH5.0RT-20",
    "sh6.0rt-20": "SH6.0RT-20",
    "sh8.0rt-20": "SH8.0RT-20",
    "sh10rt-20": "SH10RT-20",
    "sh8.0rt-v112": "SH8.0RT-V112",
    "sh10rt-v112": "SH10RT-V112",
}

# Three phase T series (commercial/C&I)
SUNGROW_SH_T_MODELS = {
    "sh15t": "SH15T",
    "sh20t": "SH20T",
    "sh25t": "SH25T",
}

# Legacy SH models
SUNGROW_SH_LEGACY_MODELS = {
    "sh3k6": "SH3K6",
    "sh4k6": "SH4K6",
    "sh5k-20": "SH5K-20",
    "sh5k-30": "SH5K-30",
    "sh5k-v13": "SH5K-V13",
}

# Combined SH models
SUNGROW_SH_MODELS = {
    **SUNGROW_SH_RS_MODELS,
    **SUNGROW_SH_RT_MODELS,
    **SUNGROW_SH_T_MODELS,
    **SUNGROW_SH_LEGACY_MODELS,
}

# Combined model list for UI dropdowns
SUNGROW_MODELS = {
    **SUNGROW_SG_MODELS,
    **SUNGROW_SH_MODELS,
}

# Default inverter configuration
DEFAULT_INVERTER_PORT = 502
DEFAULT_INVERTER_SLAVE_ID = 1

# Inverter status sensor
SENSOR_TYPE_INVERTER_STATUS = "inverter_status"


def get_models_for_brand(brand: str, battery_system: str = None) -> dict[str, str]:
    """Get model options for a specific AC-coupled inverter brand.

    Args:
        brand: Inverter brand name
        battery_system: Current battery system type (to filter out conflicts)

    Returns:
        Dictionary of model_id: model_name pairs
    """
    brand_models = {
        "sungrow": SUNGROW_MODELS,
        "fronius": FRONIUS_MODELS,
        "goodwe": GOODWE_MODELS,
        "huawei": HUAWEI_MODELS,
        "enphase": ENPHASE_MODELS,
        "zeversolar": ZEVERSOLAR_MODELS,
    }

    models = brand_models.get(brand.lower(), SUNGROW_MODELS)

    # If battery system is Sungrow and AC inverter is also Sungrow,
    # only show SG-series (string inverters), not SH-series (hybrid with battery)
    if brand.lower() == "sungrow" and battery_system == BATTERY_SYSTEM_SUNGROW:
        return SUNGROW_SG_MODELS

    return models


def get_brand_defaults(brand: str) -> dict[str, int]:
    """Get default port and slave ID for an AC-coupled inverter brand."""
    defaults = {
        "sungrow": {"port": 502, "slave_id": 1},
        "fronius": {"port": 502, "slave_id": 1},
        "goodwe": {"port": 502, "slave_id": 247},
        "huawei": {"port": 502, "slave_id": 1},
        "enphase": {"port": 443, "slave_id": 1},
        "zeversolar": {"port": 80, "slave_id": 1},
    }
    return defaults.get(brand.lower(), {"port": 502, "slave_id": 1})


# ============================================================
# Smart Optimization Configuration
# External optimizer-based battery scheduling using Linear Programming
# ============================================================

# Battery management mode selection
CONF_BATTERY_MANAGEMENT_MODE = "battery_management_mode"

# Management modes
BATTERY_MODE_MANUAL = "manual"        # Use automations for control
BATTERY_MODE_TOU_SYNC = "tou_sync"    # Sync prices to battery, let battery decide
BATTERY_MODE_SMART_OPT = "smart_optimization"  # Optimizer-based scheduling

BATTERY_MANAGEMENT_MODES = {
    BATTERY_MODE_MANUAL: "Manual (use automations)",
    BATTERY_MODE_TOU_SYNC: "TOU Sync (sync prices to battery)",
    BATTERY_MODE_SMART_OPT: "Smart Optimization (LP-based scheduling)",
}

# Optimization provider selection
CONF_OPTIMIZATION_PROVIDER = "optimization_provider"
OPT_PROVIDER_NATIVE = "native"           # Use battery's built-in optimization
OPT_PROVIDER_POWERSYNC = "powersync_ml"  # Use PowerSync optimization

# External Optimizer Integration (deprecated — built-in LP optimizer now used)
# Kept for backward compatibility during migration
OPTIMIZER_DOMAIN = "haeo"  # deprecated

# HAFO (Home Assistant Forecaster) Integration for ML-based load prediction
# HAFO creates forecast sensors from historical entity data
# Reference: https://hafo.haeo.io/
HAFO_DOMAIN = "hafo"
HAFO_INSTALL_URL = "https://hafo.haeo.io/"
HAFO_LOAD_SENSOR_PREFIX = "sensor.hafo_"

# Map battery system to native optimization name
OPTIMIZATION_PROVIDER_NATIVE_NAMES = {
    BATTERY_SYSTEM_TESLA: "Tesla Powerwall",
    BATTERY_SYSTEM_SIGENERGY: "Sigenergy",
    BATTERY_SYSTEM_SUNGROW: "Sungrow",
}

OPTIMIZATION_PROVIDERS = {
    OPT_PROVIDER_NATIVE: "Use battery's built-in optimization",
    OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
}

# Optimization configuration keys
CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
CONF_OPTIMIZATION_COST_FUNCTION = "optimization_cost_function"
CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
CONF_OPTIMIZATION_INTERVAL = "optimization_interval"
CONF_OPTIMIZATION_HORIZON = "optimization_horizon"
CONF_OPTIMIZATION_EV_INTEGRATION = "optimization_ev_integration"
CONF_OPTIMIZATION_VPP_ENABLED = "optimization_vpp_enabled"
CONF_OPTIMIZATION_MULTI_BATTERY = "optimization_multi_battery"
CONF_OPTIMIZATION_ML_FORECASTING = "optimization_ml_forecasting"
CONF_OPTIMIZATION_WEATHER_INTEGRATION = "optimization_weather_integration"

# Optimization cost functions
COST_FUNCTION_COST = "cost"            # Minimize total electricity cost
COST_FUNCTION_PROFIT = "profit"        # Maximize profit from grid trading
COST_FUNCTION_SELF_USE = "self_consumption"  # Maximize solar self-consumption

OPTIMIZATION_COST_FUNCTIONS = {
    COST_FUNCTION_COST: "Cost Minimization - lowest electricity bill",
    COST_FUNCTION_PROFIT: "Profit Maximization - best for Amber/VPP",
    COST_FUNCTION_SELF_USE: "Self-Consumption - maximize solar usage",
}

# Default optimization settings
DEFAULT_OPTIMIZATION_INTERVAL = 30     # Re-optimize every 30 minutes
DEFAULT_OPTIMIZATION_HORIZON = 48      # 48-hour forecast horizon
DEFAULT_OPTIMIZATION_BACKUP_RESERVE = 0.20  # 20% minimum SOC

# Battery capacity defaults by system (Wh)
BATTERY_CAPACITY_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 13500,     # Powerwall 2: 13.5 kWh
    BATTERY_SYSTEM_SIGENERGY: 10000,  # Varies, default 10 kWh
    BATTERY_SYSTEM_SUNGROW: 10000,    # Varies, default 10 kWh
}

# Max charge/discharge power defaults by system (W)
BATTERY_POWER_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 5000,       # Powerwall 2: 5 kW continuous
    BATTERY_SYSTEM_SIGENERGY: 5000,   # Varies
    BATTERY_SYSTEM_SUNGROW: 5000,     # Varies
}

# Optimization service
SERVICE_OPTIMIZATION_REFRESH = "optimization_refresh"

# Optimization sensor types
SENSOR_TYPE_OPTIMIZATION_STATUS = "optimization_status"
SENSOR_TYPE_OPTIMIZATION_SAVINGS = "optimization_savings"
SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION = "optimization_next_action"

# ============================================================
# EV Smart Charging Configuration
# Coordinates EV charging alongside battery optimization
# ============================================================

# EV smart charging mode configuration
CONF_EV_SMART_CHARGING_ENABLED = "ev_smart_charging_enabled"
CONF_EV_CHARGER_ENTITY = "ev_charger_entity"
CONF_EV_CHARGING_MODE = "ev_charging_mode"
CONF_EV_TARGET_SOC = "ev_target_soc"
CONF_EV_DEPARTURE_TIME = "ev_departure_time"
CONF_EV_PRICE_THRESHOLD = "ev_price_threshold"

# EV charging modes
EV_MODE_OFF = "off"
EV_MODE_SMART = "smart"          # Charge during cheap periods
EV_MODE_SOLAR_ONLY = "solar_only"  # Only charge from excess solar
EV_MODE_IMMEDIATE = "immediate"   # Charge immediately
EV_MODE_SCHEDULED = "scheduled"   # User-defined schedule

EV_CHARGING_MODES = {
    EV_MODE_OFF: "Off - Manual control only",
    EV_MODE_SMART: "Smart - Charge during cheap electricity",
    EV_MODE_SOLAR_ONLY: "Solar Only - Charge from excess solar",
    EV_MODE_IMMEDIATE: "Immediate - Charge whenever plugged in",
    EV_MODE_SCHEDULED: "Scheduled - Charge at specific times",
}

# Default EV charging settings
DEFAULT_EV_TARGET_SOC = 0.8          # 80%
DEFAULT_EV_DEPARTURE_TIME = "07:00"
DEFAULT_EV_PRICE_THRESHOLD = 0.15    # $0.15/kWh

# EV sensor types
SENSOR_TYPE_EV_CHARGING_STATUS = "ev_charging_status"
SENSOR_TYPE_EV_NEXT_CHARGE_WINDOW = "ev_next_charge_window"
