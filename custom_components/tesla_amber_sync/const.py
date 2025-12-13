"""Constants for the Tesla Sync integration."""
from datetime import timedelta

# Integration domain
DOMAIN = "tesla_amber_sync"

# Configuration keys
CONF_AMBER_API_TOKEN = "amber_api_token"
CONF_AMBER_SITE_ID = "amber_site_id"
CONF_TESLEMETRY_API_TOKEN = "teslemetry_api_token"
CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
CONF_AUTO_SYNC_ENABLED = "auto_sync_enabled"
CONF_TIMEZONE = "timezone"
CONF_AMBER_FORECAST_TYPE = "amber_forecast_type"
CONF_SOLAR_CURTAILMENT_ENABLED = "solar_curtailment_enabled"

# Tesla API Provider selection
CONF_TESLA_API_PROVIDER = "tesla_api_provider"
TESLA_PROVIDER_TESLEMETRY = "teslemetry"
TESLA_PROVIDER_FLEET_API = "fleet_api"

# Fleet API configuration (direct Tesla API)
CONF_FLEET_API_ACCESS_TOKEN = "fleet_api_access_token"
CONF_FLEET_API_REFRESH_TOKEN = "fleet_api_refresh_token"
CONF_FLEET_API_TOKEN_EXPIRES_AT = "fleet_api_token_expires_at"
CONF_FLEET_API_CLIENT_ID = "fleet_api_client_id"
CONF_FLEET_API_CLIENT_SECRET = "fleet_api_client_secret"

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

# AEMO Spike Detection configuration
CONF_AEMO_SPIKE_ENABLED = "aemo_spike_enabled"
CONF_AEMO_REGION = "aemo_region"
CONF_AEMO_SPIKE_THRESHOLD = "aemo_spike_threshold"

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
SENSOR_TYPE_CURRENT_PRICE = "current_price"
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

# AEMO Spike sensors
SENSOR_TYPE_AEMO_PRICE = "aemo_price"
SENSOR_TYPE_AEMO_SPIKE_STATUS = "aemo_spike_status"

# Tariff schedule sensor
SENSOR_TYPE_TARIFF_SCHEDULE = "tariff_schedule"

# Solar curtailment sensor
SENSOR_TYPE_SOLAR_CURTAILMENT = "solar_curtailment"

# Attributes
ATTR_LAST_SYNC = "last_sync"
ATTR_SYNC_STATUS = "sync_status"
ATTR_PRICE_SPIKE = "price_spike"
ATTR_WHOLESALE_PRICE = "wholesale_price"
ATTR_NETWORK_PRICE = "network_price"
ATTR_AEMO_REGION = "aemo_region"
ATTR_AEMO_THRESHOLD = "aemo_threshold"
ATTR_SPIKE_START_TIME = "spike_start_time"
