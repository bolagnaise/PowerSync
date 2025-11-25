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

# Daily supply charge configuration
CONF_DAILY_SUPPLY_CHARGE = "daily_supply_charge"
CONF_MONTHLY_SUPPLY_CHARGE = "monthly_supply_charge"

# Data coordinator update intervals
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)  # Amber updates every 5 minutes
UPDATE_INTERVAL_ENERGY = timedelta(minutes=1)  # Tesla energy data every minute

# Amber API
AMBER_API_BASE_URL = "https://api.amber.com.au/v1"

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

# Attributes
ATTR_LAST_SYNC = "last_sync"
ATTR_SYNC_STATUS = "sync_status"
ATTR_PRICE_SPIKE = "price_spike"
ATTR_WHOLESALE_PRICE = "wholesale_price"
ATTR_NETWORK_PRICE = "network_price"
