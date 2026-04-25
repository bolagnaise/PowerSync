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

# Dashboard JS version — bump this to cache-bust the strategy JS independently of the app version
DASHBOARD_JS_VERSION = "11"

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
CONF_WEATHER_ENTITY = "weather_entity"

# EV Charging configuration
CONF_EV_CHARGING_ENABLED = "ev_charging_enabled"

# EV Control Provider selection
CONF_EV_PROVIDER = "ev_provider"
EV_PROVIDER_FLEET_API = "fleet_api"  # Tesla Fleet API / Teslemetry
EV_PROVIDER_TESLA_BLE = "tesla_ble"  # ESPHome Tesla BLE
EV_PROVIDER_TESLEMETRY_BT = "teslemetry_bt"  # Teslemetry Bluetooth (native HA)
EV_PROVIDER_BOTH = "both"  # Use both providers (any local BLE/BT + Fleet API fallback)

EV_PROVIDERS = {
    EV_PROVIDER_FLEET_API: "Tesla Fleet API / Teslemetry",
    EV_PROVIDER_TESLA_BLE: "Tesla BLE (ESPHome)",
    EV_PROVIDER_TESLEMETRY_BT: "Teslemetry Bluetooth",
    EV_PROVIDER_BOTH: "Both (Fleet API + local BLE/BT)",
}

# Tesla EV API Provider selection (v2.10.1+).
# Selects which Tesla cloud API is used for vehicle commands when the energy
# site provider is PowerSync.cc (which has no vehicle endpoints). Independent
# from CONF_TESLA_API_PROVIDER, which controls energy site calls only.
CONF_TESLA_EV_API_PROVIDER = "tesla_ev_api_provider"
TESLA_EV_API_PROVIDER_NONE = "none"
TESLA_EV_API_PROVIDER_FLEET_API = "tesla_fleet"
TESLA_EV_API_PROVIDER_TESLEMETRY = "teslemetry"

TESLA_EV_API_PROVIDERS = {
    TESLA_EV_API_PROVIDER_NONE: "None (no Tesla cloud vehicle commands)",
    TESLA_EV_API_PROVIDER_FLEET_API: "Tesla Fleet API",
    TESLA_EV_API_PROVIDER_TESLEMETRY: "Teslemetry",
}

# Token slot for Teslemetry when used purely for vehicles (the energy-site
# Teslemetry token lives in CONF_TESLEMETRY_API_TOKEN — keeping these
# separate lets users mix energy = PowerSync + EV = Teslemetry).
CONF_TESLA_EV_TELEMETRY_TOKEN = "tesla_ev_teslemetry_token"

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

# Teslemetry Bluetooth entity patterns (prefix = VIN)
# Integration: https://github.com/Teslemetry/hass-tesla-bluetooth (domain: tesla_bluetooth)
TESLEMETRY_BT_SWITCH_CHARGE = "switch.{prefix}_charge"
TESLEMETRY_BT_NUMBER_CHARGE_AMPS = "number.{prefix}_charge_current_request"
TESLEMETRY_BT_SENSOR_CHARGER_POWER = "sensor.{prefix}_charger_power"
TESLEMETRY_BT_SENSOR_BATTERY_LEVEL = "sensor.{prefix}_battery_level"
TESLEMETRY_BT_SENSOR_CHARGING_STATE = "sensor.{prefix}_charging_state"
TESLEMETRY_BT_DEVICE_TRACKER_LOCATION = "device_tracker.{prefix}_location"

# OCPP Central System configuration
CONF_OCPP_ENABLED = "ocpp_enabled"
CONF_OCPP_PORT = "ocpp_port"
DEFAULT_OCPP_PORT = 9000

# Generic Charger configuration
CONF_GENERIC_CHARGER_ENABLED = "generic_charger_enabled"
CONF_GENERIC_CHARGER_SWITCH_ENTITY = "generic_charger_switch_entity"
CONF_GENERIC_CHARGER_AMPS_ENTITY = "generic_charger_amps_entity"
CONF_GENERIC_CHARGER_STATUS_ENTITY = "generic_charger_status_entity"
CONF_GENERIC_CHARGER_SOC_ENTITY = "generic_charger_soc_entity"

# Battery System Selection
CONF_BATTERY_SYSTEM = "battery_system"
BATTERY_SYSTEM_TESLA = "tesla"
BATTERY_SYSTEM_SIGENERGY = "sigenergy"
BATTERY_SYSTEM_SUNGROW = "sungrow"

BATTERY_SYSTEM_FOXESS = "foxess"
BATTERY_SYSTEM_GOODWE = "goodwe"
BATTERY_SYSTEM_ALPHAESS = "alphaess"
BATTERY_SYSTEM_ESY_SUNHOME = "esy_sunhome"
BATTERY_SYSTEM_SOLAX = "solax"
BATTERY_SYSTEM_SAJ_H2 = "saj_h2"

BATTERY_SYSTEMS = {
    BATTERY_SYSTEM_TESLA: "Tesla Powerwall — Fleet API or Teslemetry",
    BATTERY_SYSTEM_SIGENERGY: "Sigenergy — Cloud API + optional Modbus",
    BATTERY_SYSTEM_SUNGROW: "Sungrow SH-series — Modbus TCP",
    BATTERY_SYSTEM_FOXESS: "FoxESS — Modbus TCP or RS485 serial",
    BATTERY_SYSTEM_GOODWE: "GoodWe ET/EH/ES/EM — UDP or TCP",
    BATTERY_SYSTEM_ALPHAESS: "AlphaESS SMILE/Storion — Modbus TCP + optional Cloud API",
    BATTERY_SYSTEM_ESY_SUNHOME: "ESY Sunhome — via esy_sunhome companion integration",
    BATTERY_SYSTEM_SOLAX: "Solax Hybrid — via Solax Modbus integration",
    BATTERY_SYSTEM_SAJ_H2: "SAJ H2/HS2 — via SAJ H2 Modbus integration",
}

# Sungrow SH-series Battery System Configuration (Modbus TCP)
# Hybrid inverters with integrated battery control
CONF_SUNGROW_HOST = "sungrow_host"
CONF_SUNGROW_PORT = "sungrow_port"
CONF_SUNGROW_SLAVE_ID = "sungrow_slave_id"
DEFAULT_SUNGROW_PORT = 502
DEFAULT_SUNGROW_SLAVE_ID = 1

# Dual Sungrow (secondary inverter, optional)
CONF_SUNGROW_HOST_2 = "sungrow_host_2"
CONF_SUNGROW_PORT_2 = "sungrow_port_2"
CONF_SUNGROW_SLAVE_ID_2 = "sungrow_slave_id_2"

# Dual Sungrow grid-forming inverter SOC cap
CONF_SUNGROW_GRID_INVERTER_SOC_CAP = "sungrow_grid_inverter_soc_cap"
DEFAULT_SUNGROW_GRID_INVERTER_SOC_CAP = 100  # disabled by default

# Dual Sungrow battery capacity weights
CONF_SUNGROW_BATTERY_CAPACITY_1 = "sungrow_battery_capacity_1"
CONF_SUNGROW_BATTERY_CAPACITY_2 = "sungrow_battery_capacity_2"
DEFAULT_SUNGROW_BATTERY_CAPACITY = 25.6  # kWh — one SBR256 unit

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

# FoxESS Battery System Configuration (Modbus TCP / RS485 Serial)
# Hybrid inverters with integrated battery control
# Reference: https://github.com/nathanmarlor/foxess_modbus
CONF_FOXESS_HOST = "foxess_host"
CONF_FOXESS_PORT = "foxess_port"
CONF_FOXESS_SLAVE_ID = "foxess_slave_id"
CONF_FOXESS_CONNECTION_TYPE = "foxess_connection_type"
CONF_FOXESS_SERIAL_PORT = "foxess_serial_port"
CONF_FOXESS_SERIAL_BAUDRATE = "foxess_serial_baudrate"
CONF_FOXESS_MODEL_FAMILY = "foxess_model_family"
CONF_FOXESS_DETECTED_MODEL = "foxess_detected_model"
CONF_FOXESS_CLOUD_USERNAME = "foxess_cloud_username"
CONF_FOXESS_CLOUD_PASSWORD = "foxess_cloud_password"
CONF_FOXESS_CLOUD_DEVICE_SN = "foxess_cloud_device_sn"
CONF_FOXESS_CLOUD_API_KEY = "foxess_cloud_api_key"
FOXESS_CLOUD_BASE_URL = "https://www.foxesscloud.com"
FOXESS_MAX_SCHEDULE_PERIODS = 8

DEFAULT_FOXESS_PORT = 502
DEFAULT_FOXESS_SLAVE_ID = 247
DEFAULT_FOXESS_SERIAL_BAUDRATE = 9600

# FoxESS connection types
FOXESS_CONNECTION_TCP = "tcp"
FOXESS_CONNECTION_SERIAL = "serial"

# FoxESS model families
FOXESS_MODEL_H1 = "H1"
FOXESS_MODEL_H3 = "H3"
FOXESS_MODEL_H3_PRO = "H3-Pro"
FOXESS_MODEL_H3_SMART = "H3-Smart"
FOXESS_MODEL_KH = "KH"
FOXESS_MODEL_UNKNOWN = "unknown"

FOXESS_MODEL_FAMILIES = {
    FOXESS_MODEL_H1: "H1 Series (Single Phase)",
    FOXESS_MODEL_H3: "H3 Series (Three Phase)",
    FOXESS_MODEL_H3_PRO: "H3-Pro Series (Three Phase, Higher Power)",
    FOXESS_MODEL_H3_SMART: "H3 Smart Series (Three Phase, Native WiFi Modbus)",
    FOXESS_MODEL_KH: "KH Series (Single Phase Hybrid)",
}

# FoxESS Work Mode constants — DEPRECATED, use FoxESSRegisterMap fields instead.
# Kept for backward compat; these match register 41000 (H1/H3/KH) 0-based values.
FOXESS_WORK_MODE_SELF_USE = 0
FOXESS_WORK_MODE_FEED_IN = 1
FOXESS_WORK_MODE_BACKUP = 2

# FoxESS Work Mode names — DEPRECATED, use FoxESSRegisterMap.work_mode_names() instead.
FOXESS_WORK_MODES = {
    0: "Self Use",
    1: "Feed-in First",
    2: "Backup",
}

# GoodWe Battery System Configuration (goodwe PyPI library)
# Hybrid inverters: ET/EH/BT/BH (3-phase), ES/EM/BP (single-phase)
# Reference: https://github.com/marcelblijleven/goodwe
CONF_GOODWE_HOST = "goodwe_host"
CONF_GOODWE_PORT = "goodwe_port"
CONF_GOODWE_PROTOCOL = "goodwe_protocol"  # "udp" or "tcp"
CONF_GOODWE_EMS_ENTITY_PREFIX = "goodwe_ems_entity_prefix"  # e.g. "goodwe" → uses select.goodwe_ems_mode etc.
DEFAULT_GOODWE_PORT_UDP = 8899
DEFAULT_GOODWE_PORT_TCP = 502

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
TESLA_PROVIDER_POWERSYNC = "powersync"  # PowerSync.cc cloud OAuth proxy (free, recommended)

# All supported Tesla/EV integrations (for device/entity discovery)
# These are the HA integration domain names used in device identifiers
TESLA_INTEGRATIONS = [
    "tesla_fleet",    # Official Tesla Fleet API integration
    "teslemetry",     # Teslemetry integration
    "tessie",         # Tessie integration
    "tesla_custom",   # Tesla Custom Integration
    "tesla",          # Older Tesla integration
]

# BYD vehicle integration (hass-byd-vehicle)
BYD_INTEGRATION = "byd_vehicle"

# Fleet API configuration (direct Tesla API)
CONF_FLEET_API_ACCESS_TOKEN = "fleet_api_access_token"
CONF_FLEET_API_REFRESH_TOKEN = "fleet_api_refresh_token"
CONF_FLEET_API_TOKEN_EXPIRES_AT = "fleet_api_token_expires_at"
CONF_FLEET_API_BASE_URL = "fleet_api_base_url"
CONF_FLEET_API_CLIENT_ID = "fleet_api_client_id"
CONF_FLEET_API_CLIENT_SECRET = "fleet_api_client_secret"

# Powerwall local control (LAN / TEDAPI v1r)
# Set only after the pairing flow completes. Stored in entry.data so HA
# encrypts the private key at rest. The IP and customer password are
# mirrored from the mobile app so local monitoring works device-independently.
CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
CONF_POWERWALL_LOCAL_PRIVATE_KEY = "powerwall_local_private_key_pem"
CONF_POWERWALL_LOCAL_PUBLIC_KEY = "powerwall_local_public_key_der"
CONF_POWERWALL_LOCAL_DIN = "powerwall_local_din"
CONF_POWERWALL_LOCAL_IP = "powerwall_local_ip"
CONF_POWERWALL_LOCAL_VERSION = "powerwall_local_version"  # "pw2" | "pw3"
CONF_POWERWALL_LOCAL_CUSTOMER_PASSWORD = "powerwall_local_customer_password"
CONF_POWERWALL_LOCAL_WIFI_SSID = "powerwall_local_wifi_ssid"
CONF_POWERWALL_LOCAL_WIFI_PASSWORD = "powerwall_local_wifi_password"
CONF_POWERWALL_LOCAL_ENERGY_SITE_ID = "powerwall_local_energy_site_id"
CONF_POWERWALL_LOCAL_PAIRED_AT = "powerwall_local_paired_at"
# Minimum battery SOC (%) below which off-grid commands are refused.
CONF_POWERWALL_OFF_GRID_MIN_SOC = "powerwall_off_grid_min_soc"
DEFAULT_POWERWALL_OFF_GRID_MIN_SOC = 20
# Local poll interval for meters/SOC/grid_status when paired.
POWERWALL_LOCAL_POLL_INTERVAL = 10  # seconds
# Pairing window the user has to toggle the Powerwall switch.
POWERWALL_PAIRING_WINDOW_SECONDS = 120

# Powerwall off-grid as a curtailment fallback — opt-in feature for users
# with inverters that can't curtail (Enphase AGF profile, no inverter
# configured, etc). When the normal curtailment path is unavailable AND
# excess solar would be exported at negative prices, the integration can
# instead physically open the Powerwall grid contactor so the house runs
# islanded until the trigger condition clears.
CONF_POWERWALL_OFFGRID_AS_CURTAILMENT = "powerwall_offgrid_as_curtailment"
DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT = False
# Higher SOC floor than manual off-grid — the house will be running off
# battery for potentially hours, so we need more headroom.
CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC = "powerwall_offgrid_curtailment_min_soc"
DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC = 40
# Cumulative daily cap on off-grid-as-curtailment duration (seconds).
# Prevents a runaway loop when the price trigger is sticky or when the
# battery is being drained faster than solar can refill it.
CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS = "powerwall_offgrid_curtailment_max_seconds"
DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS = 6 * 60 * 60  # 6h

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
CONF_SIGENERGY_EXPORT_LIMIT_KW = "sigenergy_export_limit_kw"
DEFAULT_SIGENERGY_MODBUS_PORT = 502
DEFAULT_SIGENERGY_MODBUS_SLAVE_ID = 247  # Sigenergy uses unit ID 247 (or 0)

# AlphaESS Modbus TCP (SMILE / Storion hybrid inverter-battery)
# Reference: official AlphaESS-HouseholdModbusRegisterParameterList.pdf
CONF_ALPHAESS_MODBUS_HOST = "alphaess_modbus_host"
CONF_ALPHAESS_MODBUS_PORT = "alphaess_modbus_port"
CONF_ALPHAESS_MODBUS_SLAVE_ID = "alphaess_modbus_slave_id"
CONF_ALPHAESS_EXPORT_LIMIT_KW = "alphaess_export_limit_kw"
CONF_ALPHAESS_DC_CURTAILMENT_ENABLED = "alphaess_dc_curtailment_enabled"
CONF_ALPHAESS_MODEL = "alphaess_model"
DEFAULT_ALPHAESS_MODBUS_PORT = 502
DEFAULT_ALPHAESS_MODBUS_SLAVE_ID = 85  # 0x55 — AlphaESS factory default (register 080FH)

# AlphaESS Cloud API (openapi.alphaess.com)
# App ID / App Secret issued from https://open.alphaess.com
# Signature = SHA-512(AppID + AppSecret + Timestamp)
CONF_ALPHAESS_CLOUD_ENABLED = "alphaess_cloud_enabled"
CONF_ALPHAESS_CLOUD_APP_ID = "alphaess_cloud_app_id"
CONF_ALPHAESS_CLOUD_APP_SECRET = "alphaess_cloud_app_secret"
CONF_ALPHAESS_CLOUD_SERIAL = "alphaess_cloud_serial"
ALPHAESS_CLOUD_BASE_URL = "https://openapi.alphaess.com/api"

# ESY Sunhome battery system — bridges via upstream esy_sunhome companion integration
# Install the esy_sunhome integration from HACS first; PowerSync reads its entities.
CONF_ESY_CONFIG_ENTRY_ID = "esy_config_entry_id"  # UUID of the upstream esy_sunhome config entry

# Solax Hybrid battery system — bridges via wills106/homeassistant-solax-modbus integration
# Install solax_modbus from HACS first; PowerSync reads/writes its entities.
# Supports Gen4/Gen5/Gen6 Hybrid and AC Retro-Fit (X1/X3 families).
CONF_SOLAX_CONFIG_ENTRY_ID = "solax_config_entry_id"
CONF_SOLAX_ENTITY_PREFIX = "solax_entity_prefix"          # e.g. "solax" → sensor.solax_battery_capacity
CONF_SOLAX_BATTERY_CAPACITY_KWH = "solax_battery_capacity_kwh"   # kWh, for LP optimizer
CONF_SOLAX_BATTERY_NOMINAL_V = "solax_battery_nominal_v"         # V, for current→power conversion
CONF_SOLAX_MAX_CHARGE_CURRENT_A = "solax_max_charge_current_a"   # A, hardware limit
CONF_SOLAX_MAX_DISCHARGE_CURRENT_A = "solax_max_discharge_current_a"  # A, hardware limit
DEFAULT_SOLAX_ENTITY_PREFIX = "solax"
DEFAULT_SOLAX_BATTERY_CAPACITY_KWH = 11.6   # T-BAT-SYS-HV 11.6 kWh
DEFAULT_SOLAX_BATTERY_NOMINAL_V = 51.2      # LFP T-BAT; override to 102.4 for HV packs
DEFAULT_SOLAX_MAX_CHARGE_CURRENT_A = 25
DEFAULT_SOLAX_MAX_DISCHARGE_CURRENT_A = 25

# SAJ H2 / HS2 battery system — bridges via stanus74/home-assistant-saj-h2-modbus
# Install saj_h2_modbus from HACS first; PowerSync reads/writes its entities.
CONF_SAJ_CONFIG_ENTRY_ID = "saj_config_entry_id"
CONF_SAJ_BATTERY_CAPACITY_KWH = "saj_battery_capacity_kwh"
DEFAULT_SAJ_BATTERY_CAPACITY_KWH = 10.0

# Demand charge configuration
CONF_DEMAND_CHARGE_ENABLED = "demand_charge_enabled"
CONF_DEMAND_CHARGE_RATE = "demand_charge_rate"
CONF_DEMAND_CHARGE_START_TIME = "demand_charge_start_time"
CONF_DEMAND_CHARGE_END_TIME = "demand_charge_end_time"
CONF_DEMAND_CHARGE_DAYS = "demand_charge_days"
CONF_DEMAND_CHARGE_BILLING_DAY = "demand_charge_billing_day"
CONF_DEMAND_CHARGE_APPLY_TO = "demand_charge_apply_to"
CONF_DEMAND_ARTIFICIAL_PRICE = "demand_artificial_price_enabled"
CONF_DEMAND_ALLOW_GRID_CHARGING = "demand_allow_grid_charging"

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
    "amber": "Amber Electric — real-time wholesale pricing (AU)",
    "localvolts": "Localvolts — 5-minute NEM wholesale pricing (AU)",
    "flow_power": "Flow Power — wholesale with Happy Hour exports (AU)",
    "globird": "Globird — static tariff with AEMO spike export (AU)",
    "aemo_vpp": "AEMO VPP — spike detection for VPP plans (AGL, Engie, etc.)",
    "octopus": "Octopus Energy — dynamic Agile/Go/Flux pricing (UK)",
    "epex": "EPEX Day-Ahead — European day-ahead market pricing (EU)",
    "nz": "New Zealand TOU — Octopus NZ, Electric Kiwi, Contact, etc.",
    "other": "Other / Custom TOU — enter your own rates manually",
}

# Localvolts configuration
CONF_LOCALVOLTS_API_KEY = "localvolts_api_key"
CONF_LOCALVOLTS_PARTNER_ID = "localvolts_partner_id"
CONF_LOCALVOLTS_NMI = "localvolts_nmi"
LOCALVOLTS_API_BASE_URL = "https://api.localvolts.com/v1"

# EPEX Day-Ahead configuration (EU markets via epexpredictor.batzill.com)
CONF_EPEX_REGION = "epex_region"
CONF_EPEX_SURCHARGE = "epex_surcharge"  # Fixed surcharge in ct/kWh (network fees, levies)
CONF_EPEX_TAX_PERCENT = "epex_tax_percent"  # Tax percentage (e.g. 21% VAT in Belgium)
CONF_EPEX_EXPORT_RATE = "epex_export_rate"  # Fixed feed-in rate in ct/kWh (0 = wholesale)
EPEX_API_BASE_URL = "https://epexpredictor.batzill.com"
EPEX_REGIONS = {
    "DE": "Germany",
    "AT": "Austria",
    "BE": "Belgium",
    "NL": "Netherlands",
    "SE1": "Sweden (Zone 1)",
    "SE2": "Sweden (Zone 2)",
    "SE3": "Sweden (Zone 3)",
    "SE4": "Sweden (Zone 4)",
    "DK1": "Denmark (Zone 1)",
    "DK2": "Denmark (Zone 2)",
}

# NZ Electricity provider configuration
CONF_NZ_RETAILER = "nz_retailer"
CONF_NZ_DISTRIBUTION_ZONE = "nz_distribution_zone"
CONF_NZ_PEAK_RATE = "nz_peak_rate"
CONF_NZ_SHOULDER_RATE = "nz_shoulder_rate"
CONF_NZ_OFFPEAK_RATE = "nz_offpeak_rate"
CONF_NZ_PEAK_EXPORT = "nz_peak_export"
CONF_NZ_OFFPEAK_EXPORT = "nz_offpeak_export"
CONF_NZ_DAILY_SUPPLY = "nz_daily_supply"

NZ_RETAILERS = {
    "octopus_nz": "Octopus Energy NZ",
    "electric_kiwi": "Electric Kiwi",
    "contact_good_weekends": "Contact Energy - Good Weekends",
    "contact_good_nights": "Contact Energy - Good Nights",
    "contact_good_charge": "Contact Energy - Good Charge",
    "nz_custom": "Custom NZ TOU",
}

NZ_DISTRIBUTION_ZONES = {
    "vector": "Vector (Auckland)",
    "wellington": "Wellington Electricity",
    "orion": "Orion (Canterbury)",
    "powerco": "Powerco",
    "unison": "Unison",
    "aurora": "Aurora (Otago/Southland)",
    "other": "Other / Generic",
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
    "intelligent_go": "Intelligent Octopus Go (smart EV/battery)",
    "flux": "Octopus Flux (solar/battery)",
    "intelligent_flux": "Intelligent Octopus Flux (smart battery)",
    "tracker": "Octopus Tracker (daily price)",
}

# Octopus product codes (latest versions)
OCTOPUS_PRODUCT_CODES = {
    "agile": "AGILE-24-10-01",
    "go": "GO-VAR-22-10-14",
    "intelligent_go": "INTELLI-VAR-24-10-29",
    "flux": "FLUX-IMPORT-23-02-14",
    "intelligent_flux": "INTELLI-FLUX-IMPORT-23-07-14",
    "tracker": "SILVER-FLEX-BB-23-02-08",  # Fallback — dynamically discovered at setup
}

# Octopus export product codes
OCTOPUS_EXPORT_PRODUCT_CODES = {
    "agile": "AGILE-OUTGOING-19-05-13",  # Agile Outgoing for dynamic export
    "go": "OUTGOING-VAR-24-10-26",  # Outgoing Octopus (standard variable flat rate)
    "intelligent_go": "OUTGOING-VAR-24-10-26",  # Outgoing Octopus (standard variable flat rate)
    "flux": "FLUX-EXPORT-23-02-14",  # Flux export tariff
    "intelligent_flux": "INTELLI-FLUX-EXPORT-23-07-14",  # Intelligent Flux export
    "tracker": "OUTGOING-VAR-24-10-26",  # Outgoing Octopus (standard variable flat rate)
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

# Octopus Saving Sessions
CONF_OCTOPUS_SAVING_SESSIONS_ENABLED = "octopus_saving_sessions_enabled"
CONF_OCTOPUS_SAVING_SESSIONS_SOURCE = "octopus_saving_sessions_source"  # "direct" or "entity"
CONF_OCTOPUS_API_KEY = "octopus_api_key"  # GraphQL auth (different from public price API)
CONF_OCTOPUS_ACCOUNT_NUMBER = "octopus_account_number"  # e.g. "A-12345678"
CONF_OCTOPUS_SAVING_SESSIONS_ENTITY = "octopus_saving_sessions_entity"  # Bottlecap Dave entity
CONF_OCTOPUS_SAVING_SESSIONS_AUTO_JOIN = "octopus_saving_sessions_auto_join"
CONF_OCTOPUS_OCTOPOINTS_PER_PENNY = "octopus_octopoints_per_penny"  # Default 8
DEFAULT_OCTOPOINTS_PER_PENNY = 8

# Saving session sensor types
SENSOR_TYPE_SAVING_SESSION_ACTIVE = "saving_session_active"
SENSOR_TYPE_NEXT_SAVING_SESSION = "next_saving_session"
SENSOR_TYPE_SAVING_SESSION_RATE = "saving_session_rate"

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
    "TAS1": 0.00,   # No Happy Hour in Tasmania
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
# Legacy formula: PEA = wholesale - TWAP - BPEA
# V2 formula: PEA = GST*Spot + Tariff - GST*TWAP - AvgDailyTariff - BPEA
CONF_PEA_ENABLED = "pea_enabled"
CONF_FLOW_POWER_BASE_RATE = "flow_power_base_rate"
CONF_PEA_CUSTOM_VALUE = "pea_custom_value"

# Flow Power v2 tariff configuration (optional — enables corrected formula)
CONF_FP_NETWORK = "fp_network"                # DNSP display name (e.g. "SAPN")
CONF_FP_TARIFF_CODE = "fp_tariff_code"        # Tariff code (e.g. "RESELE")
CONF_FP_TWAP_OVERRIDE = "fp_twap_override"    # Manual TWAP override (c/kWh)
CONF_FP_AMBER_MARKUP = "fp_amber_markup"      # Amber comparison markup (c/kWh)

# PEA Constants
FLOW_POWER_GST = 1.1              # GST multiplier (10%)
FLOW_POWER_MARKET_AVG = 8.0       # Market TWAP average (c/kWh) — fallback only
FLOW_POWER_BENCHMARK = 1.7       # BPEA - benchmark customer performance (c/kWh)
FLOW_POWER_PEA_OFFSET = 9.7      # Combined: MARKET_AVG + BENCHMARK (c/kWh)
FLOW_POWER_DEFAULT_BASE_RATE = 34.0  # Default Flow Power base rate (c/kWh)

# Flow Power Portal configuration
CONF_FLOWPOWER_EMAIL = "flowpower_email"
CONF_FLOWPOWER_PASSWORD = "flowpower_password"
UPDATE_INTERVAL_FLOWPOWER = 1800  # 30 minutes

# Portal account sensors — (sensor_type, name, data_key, unit, icon, source_label)
FLOW_POWER_PORTAL_SENSORS = [
    ("fp_account_pea", "Flow Power PEA (Actual)", "pea_actual", "c/kWh", "mdi:account-cash", "portal"),
    ("fp_account_pea_30d", "Flow Power PEA 30-Day", "pea_30_days", "c/kWh", "mdi:calendar-month", "portal"),
    ("fp_account_bpea", "Flow Power BPEA (Benchmark)", "bpea", "c/kWh", "mdi:target", "portal"),
    ("fp_account_cpea", "Flow Power CPEA (Customer)", "cpea", "c/kWh", "mdi:account-arrow-right", "calculated"),
    ("fp_account_pea_import", "Flow Power PEA Import", "pea_actual_import", "c/kWh", "mdi:import", "portal"),
    ("fp_account_lwap", "Flow Power LWAP", "lwap", "c/kWh", "mdi:scale-balance", "portal"),
    ("fp_account_lwap_actual", "Flow Power LWAP (Actual)", "lwap_actual", "c/kWh", "mdi:scale-balance", "portal"),
    ("fp_account_twap", "Flow Power TWAP (Portal)", "twap", "c/kWh", "mdi:chart-timeline-variant", "portal"),
    ("fp_account_avg_rrp", "Flow Power Avg Spot Price", "avg_rrp", "c/kWh", "mdi:lightning-bolt", "portal"),
    ("fp_account_dlf", "Flow Power DLF (Site Losses)", "site_losses_dlf", None, "mdi:transmission-tower", "portal"),
    ("fp_account_avg_usage", "Flow Power Avg Demand", "avg_usage_kw", "kW", "mdi:flash-outline", "portal"),
    ("fp_account_max_usage", "Flow Power Max Demand", "max_usage_kw", "kW", "mdi:flash-alert", "portal"),
]

# Default Amber comparison markup by region (c/kWh)
# Approximate retailer margin + hedging costs
DEFAULT_FP_AMBER_MARKUP = {
    "NSW1": 4.2,
    "QLD1": 4.0,
    "SA1": 4.2,
    "VIC1": 4.0,
}

# Region → list of DNSP display names
REGION_NETWORKS = {
    "NSW1": ["Ausgrid", "Endeavour", "Essential"],
    "QLD1": ["Energex", "Ergon"],
    "SA1": ["SAPN"],
    "VIC1": ["Powercor", "CitiPower", "AusNet", "Jemena", "United"],
    "TAS1": ["TasNetworks"],
}

# Display name → aemo_to_tariff network parameter (for spot_to_tariff() calls)
NETWORK_API_NAME = {
    "Ausgrid": "ausgrid",
    "Endeavour": "endeavour",
    "Essential": "essential",
    "Energex": "energex",
    "Ergon": "ergon",
    "SAPN": "sapn",
    "Powercor": "powercor",
    "CitiPower": "victoria",
    "AusNet": "ausnet",
    "Jemena": "jemena",
    "United": "victoria",
    "TasNetworks": "tasnetworks",
    "Evoenergy": "evoenergy",
}

# Display name → aemo_to_tariff module name (for importlib imports)
NETWORK_MODULE_NAME = {
    "Ausgrid": "ausgrid",
    "Endeavour": "endeavour",
    "Essential": "essential",
    "Energex": "energex",
    "Ergon": "ergon",
    "SAPN": "sapower",
    "Powercor": "powercor",
    "CitiPower": "victoria",
    "AusNet": "ausnet",
    "Jemena": "jemena",
    "United": "victoria",
    "TasNetworks": "tasnetworks",
    "Evoenergy": "evoenergy",
}

# TWAP (Time Weighted Average Price) Settings
DEFAULT_TWAP_WINDOW_DAYS = 30     # Rolling window for TWAP calculation
MIN_TWAP_SAMPLES = 12            # Minimum samples (~1 hour) before using dynamic TWAP

# Data coordinator update intervals
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)  # Amber updates every 5 minutes
UPDATE_INTERVAL_ENERGY = timedelta(seconds=15)  # Tesla energy data every 15 seconds

# Amber API
AMBER_API_BASE_URL = "https://api.amber.com.au/v1"

# Teslemetry API
TESLEMETRY_API_BASE_URL = "https://api.teslemetry.com"

# Tesla Fleet API (direct)
FLEET_API_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
FLEET_API_AUTH_URL = "https://auth.tesla.com/oauth2/v3"
FLEET_API_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

# PowerSync.cc cloud proxy — free OAuth + Tesla Fleet API proxy
# Users authenticate via Sign in with Tesla on https://api.powersync.cc/auth/start
# and get a psync_xxx token. Coordinator hits the proxy at /api/proxy/api/1/...
POWERSYNC_API_BASE_URL = "https://api.powersync.cc/api/proxy"
POWERSYNC_AUTH_START_URL = "https://api.powersync.cc/auth/start"
POWERSYNC_AUTH_ME_URL = "https://api.powersync.cc/auth/me"


def get_tesla_api_base_url(
    provider: str | None, fleet_base_url: str | None = None
) -> str:
    """Return the Tesla API base URL for a given provider.

    Used by all Tesla service handlers to construct API request URLs.
    All three providers expose the same /api/1/... path structure, only
    the base differs.

    fleet_base_url overrides FLEET_API_BASE_URL for Fleet API provider — pass
    entry.data.get(CONF_FLEET_API_BASE_URL) to support EU/AP regional endpoints.
    """
    if provider == TESLA_PROVIDER_POWERSYNC:
        return POWERSYNC_API_BASE_URL
    if provider == TESLA_PROVIDER_FLEET_API:
        return fleet_base_url or FLEET_API_BASE_URL
    return TESLEMETRY_API_BASE_URL

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
SENSOR_TYPE_GRID_STATUS = "grid_status"
SENSOR_TYPE_BATTERY_POWER = "battery_power"
SENSOR_TYPE_HOME_LOAD = "home_load"
SENSOR_TYPE_BATTERY_LEVEL = "battery_level"
# Battery BMS-reported power limits (kW) — used by force-mode defaults and mobile sliders
SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"
SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"

# Dual Sungrow per-inverter sensor types
SENSOR_TYPE_BATTERY_LEVEL_1 = "battery_level_1"
SENSOR_TYPE_BATTERY_LEVEL_2 = "battery_level_2"

# FoxESS-specific sensor types
SENSOR_TYPE_PV1_POWER = "pv1_power"
SENSOR_TYPE_PV2_POWER = "pv2_power"
SENSOR_TYPE_CT2_POWER = "ct2_power"
SENSOR_TYPE_WORK_MODE = "work_mode"
SENSOR_TYPE_MIN_SOC = "min_soc"
SENSOR_TYPE_DAILY_BATTERY_CHARGE_FOXESS = "daily_battery_charge_foxess"
SENSOR_TYPE_DAILY_BATTERY_DISCHARGE_FOXESS = "daily_battery_discharge_foxess"

SENSOR_TYPE_DAILY_SOLAR_ENERGY = "daily_solar_energy"
SENSOR_TYPE_DAILY_GRID_IMPORT = "daily_grid_import"
SENSOR_TYPE_DAILY_GRID_EXPORT = "daily_grid_export"
SENSOR_TYPE_DAILY_BATTERY_CHARGE = "daily_battery_charge"
SENSOR_TYPE_DAILY_BATTERY_DISCHARGE = "daily_battery_discharge"
SENSOR_TYPE_DAILY_LOAD = "daily_load"
SENSOR_TYPE_DAILY_IMPORT_COST = "daily_import_cost"
SENSOR_TYPE_DAILY_EXPORT_EARNINGS = "daily_export_earnings"
SENSOR_TYPE_DAILY_AVG_COST_PER_KWH = "daily_avg_cost_per_kwh"
SENSOR_TYPE_MTD_AVG_COST_PER_KWH = "mtd_avg_cost_per_kwh"

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
SWITCH_TYPE_MONITORING_MODE = "monitoring_mode"
SWITCH_TYPE_AWAY_MODE = "away_mode"
SWITCH_TYPE_PROFIT_MAX_MODE = "profit_max_mode"

# Monitoring mode — blocks all battery/inverter control commands
CONF_MONITORING_MODE = "monitoring_mode"

# Battery mode sensor (for automation triggers)
SENSOR_TYPE_BATTERY_MODE = "battery_mode"

# Battery mode states
BATTERY_MODE_STATE_NORMAL = "normal"
BATTERY_MODE_STATE_FORCE_CHARGE = "force_charge"
BATTERY_MODE_STATE_FORCE_DISCHARGE = "force_discharge"
BATTERY_MODE_STATE_HOLD_SOC = "hold_soc"
BATTERY_MODE_STATE_SELF_CONSUMPTION = "self_consumption"

# Services for manual battery control
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_HOLD_BATTERY_SOC = "hold_battery_soc"
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
DISCHARGE_DURATIONS = [5, 10, 15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240]
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
SENSOR_TYPE_FLOW_POWER_TWAP = "flow_power_twap"
SENSOR_TYPE_NETWORK_TARIFF = "flow_power_network_tariff"
SENSOR_TYPE_AMBER_COMPARISON = "flow_power_amber_comparison"

# Battery health sensor (from mobile app TEDAPI scans)
SENSOR_TYPE_BATTERY_HEALTH = "battery_health"
SENSOR_TYPE_FIRMWARE = "firmware"

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
    "sigenergy": "Sigenergy",
    "solax": "Solax",
    "alphaess": "AlphaESS",
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
    BATTERY_SYSTEM_FOXESS: "FoxESS",
    BATTERY_SYSTEM_GOODWE: "GoodWe",
    BATTERY_SYSTEM_ALPHAESS: "AlphaESS",
    BATTERY_SYSTEM_ESY_SUNHOME: "ESY Sunhome",
    BATTERY_SYSTEM_SOLAX: "Solax",
}

OPTIMIZATION_PROVIDERS = {
    OPT_PROVIDER_NATIVE: "Use battery's built-in optimization",
    OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
}

# Optimization configuration keys
CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
CONF_OPTIMIZATION_COST_FUNCTION = "optimization_cost_function"
CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
CONF_HARDWARE_BACKUP_RESERVE = "hardware_backup_reserve"
CONF_OPTIMIZATION_INTERVAL = "optimization_interval"
CONF_OPTIMIZATION_HORIZON = "optimization_horizon"
CONF_OPTIMIZATION_EV_INTEGRATION = "optimization_ev_integration"
CONF_OPTIMIZATION_VPP_ENABLED = "optimization_vpp_enabled"
CONF_OPTIMIZATION_MULTI_BATTERY = "optimization_multi_battery"
CONF_OPTIMIZATION_ML_FORECASTING = "optimization_ml_forecasting"
CONF_OPTIMIZATION_BATTERY_CAPACITY_WH = "optimization_battery_capacity_wh"
CONF_OPTIMIZATION_MAX_CHARGE_W = "optimization_max_charge_w"
CONF_OPTIMIZATION_MAX_DISCHARGE_W = "optimization_max_discharge_w"
CONF_OPTIMIZATION_WEATHER_INTEGRATION = "optimization_weather_integration"
CONF_AWAY_ENABLED_AT = "away_enabled_at"    # ISO timestamp when away mode was turned on
CONF_AWAY_DISABLED_AT = "away_disabled_at"  # ISO timestamp when away mode was turned off
CONF_PROFIT_MAX_ENABLED = "profit_max_enabled"  # Whether profit maximisation mode is on

# Optimization cost function (only cost minimization — self-consumption is the battery's native mode)
COST_FUNCTION_COST = "cost"

# Default optimization settings
DEFAULT_OPTIMIZATION_INTERVAL = 30     # Re-optimize every 30 minutes
DEFAULT_OPTIMIZATION_HORIZON = 48      # 48-hour forecast horizon
DEFAULT_OPTIMIZATION_BACKUP_RESERVE = 0.20  # 20% minimum SOC

# Battery capacity defaults by system (Wh)
BATTERY_CAPACITY_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 13500,     # Powerwall 2: 13.5 kWh
    BATTERY_SYSTEM_SIGENERGY: 10000,  # Varies, default 10 kWh
    BATTERY_SYSTEM_SUNGROW: 10000,    # Varies, default 10 kWh
    BATTERY_SYSTEM_FOXESS: 10000,     # Varies, default 10 kWh
    BATTERY_SYSTEM_GOODWE: 10000,     # Varies, default 10 kWh
    BATTERY_SYSTEM_ALPHAESS: 10000,   # Varies (SMILE5 ~ 5.7 kWh, Storion ~ 30 kWh), default 10 kWh
    BATTERY_SYSTEM_ESY_SUNHOME: 10000,  # HM6 varies; default 10 kWh
    BATTERY_SYSTEM_SOLAX: 11600,      # T-BAT-SYS-HV 11.6 kWh typical
}

# Max charge/discharge power defaults by system (W)
BATTERY_POWER_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 5000,       # Powerwall 2: 5 kW continuous
    BATTERY_SYSTEM_SIGENERGY: 5000,   # Varies
    BATTERY_SYSTEM_SUNGROW: 5000,     # Varies
    BATTERY_SYSTEM_FOXESS: 5000,      # Varies by model
    BATTERY_SYSTEM_GOODWE: 5000,      # Varies by model
    BATTERY_SYSTEM_ALPHAESS: 5000,    # Varies by model (SMILE5 = 5 kW, Storion-T30 larger)
    BATTERY_SYSTEM_ESY_SUNHOME: 5000,  # HM6; rate is firmware-decided, using 5 kW default
    BATTERY_SYSTEM_SOLAX: 5000,        # Varies by model (X1-Hybrid G4: 3.7 kW, X3-Hybrid: 6 kW)
}

# Optimization service
SERVICE_OPTIMIZATION_REFRESH = "optimization_refresh"

# Optimization sensor types
SENSOR_TYPE_OPTIMIZATION_STATUS = "optimization_status"
SENSOR_TYPE_OPTIMIZATION_SAVINGS = "optimization_savings"
SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION = "optimization_next_action"
SENSOR_TYPE_OPTIMIZATION_FORCE_CHARGE_WINDOWS = "optimization_force_charge_windows"

# LP forecast sensors (populated from built-in optimizer data each cycle)
SENSOR_TYPE_LP_SOLAR_FORECAST = "lp_solar_forecast"
SENSOR_TYPE_LP_LOAD_FORECAST = "lp_load_forecast"
SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST = "lp_import_price_forecast"
SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST = "lp_export_price_forecast"
SENSOR_TYPE_LOAD_FORECAST_TODAY_REMAINING = "load_forecast_today_remaining"
SENSOR_TYPE_LOAD_FORECAST_TOMORROW = "load_forecast_tomorrow"

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

# Zaptec EV charger configuration
CONF_ZAPTEC_CHARGER_ENTITY = "zaptec_charger_entity"
CONF_ZAPTEC_INSTALLATION_ID = "zaptec_installation_id"

# Zaptec Cloud API standalone configuration
# Direct API access without requiring custom-components/zaptec HA integration
CONF_ZAPTEC_STANDALONE_ENABLED = "zaptec_standalone_enabled"
CONF_ZAPTEC_USERNAME = "zaptec_username"
CONF_ZAPTEC_PASSWORD = "zaptec_password"
CONF_ZAPTEC_CHARGER_ID = "zaptec_charger_id"  # API charger UUID
CONF_ZAPTEC_INSTALLATION_ID_CLOUD = "zaptec_installation_id_cloud"  # API installation UUID

# EV sensor types
SENSOR_TYPE_EV_CHARGING_STATUS = "ev_charging_status"
SENSOR_TYPE_EV_NEXT_CHARGE_WINDOW = "ev_next_charge_window"
SENSOR_TYPE_EV_POWER = "ev_power"
SENSOR_TYPE_EV_BATTERY_LEVEL = "ev_battery_level"

# Sigenergy-specific PV sensor types
SENSOR_TYPE_PV_DC_POWER = "pv_dc_power"
SENSOR_TYPE_PV_AC_POWER = "pv_ac_power"

# Amber Usage API sensors (actual metered cost data)
SENSOR_TYPE_AMBER_USAGE_YESTERDAY_COST = "amber_usage_yesterday_cost"
SENSOR_TYPE_AMBER_USAGE_YESTERDAY_SAVINGS = "amber_usage_yesterday_savings"
SENSOR_TYPE_AMBER_USAGE_MONTH_COST = "amber_usage_month_cost"
SENSOR_TYPE_AMBER_USAGE_MONTH_SAVINGS = "amber_usage_month_savings"

# ============================================================
# Device Family Grouping
# Each family maps to a HA sub-device linked via via_device to the parent
# entry device, so sensors appear in logical groups rather than one flat list.
# ============================================================
SENSOR_FAMILY_LP_OPTIMIZER = "lp_optimizer"
SENSOR_FAMILY_BATTERY = "battery"
SENSOR_FAMILY_SOLAR_INVERTER = "solar_inverter"
SENSOR_FAMILY_GRID_HOME = "grid_home"
SENSOR_FAMILY_PRICING = "pricing"
SENSOR_FAMILY_FLOW_POWER = "flow_power"
SENSOR_FAMILY_AEMO = "aemo"
SENSOR_FAMILY_EV_CHARGING = "ev_charging"
SENSOR_FAMILY_OCTOPUS = "octopus"
SENSOR_FAMILY_CONTROLS = "controls"

FAMILY_DISPLAY_NAMES: dict[str, str] = {
    SENSOR_FAMILY_LP_OPTIMIZER: "LP Optimizer",
    SENSOR_FAMILY_BATTERY: "Battery",
    SENSOR_FAMILY_SOLAR_INVERTER: "Solar & Inverter",
    SENSOR_FAMILY_GRID_HOME: "Grid & Home",
    SENSOR_FAMILY_PRICING: "Pricing & Cost",
    SENSOR_FAMILY_FLOW_POWER: "Flow Power",
    SENSOR_FAMILY_AEMO: "AEMO",
    SENSOR_FAMILY_EV_CHARGING: "EV Charging",
    SENSOR_FAMILY_OCTOPUS: "Octopus",
    SENSOR_FAMILY_CONTROLS: "Controls",
}

SENSOR_KEY_TO_FAMILY: dict[str, str] = {
    # LP Optimizer
    "optimization_status": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_next_action": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_force_charge_windows": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_savings": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_solar_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_load_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_import_price_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_export_price_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "load_forecast_today_remaining": SENSOR_FAMILY_LP_OPTIMIZER,
    "load_forecast_tomorrow": SENSOR_FAMILY_LP_OPTIMIZER,
    "tariff_schedule": SENSOR_FAMILY_LP_OPTIMIZER,
    # Battery
    "battery_power": SENSOR_FAMILY_BATTERY,
    "battery_level": SENSOR_FAMILY_BATTERY,
    "battery_level_1": SENSOR_FAMILY_BATTERY,
    "battery_level_2": SENSOR_FAMILY_BATTERY,
    "battery_max_charge_power": SENSOR_FAMILY_BATTERY,
    "battery_max_discharge_power": SENSOR_FAMILY_BATTERY,
    "battery_health": SENSOR_FAMILY_BATTERY,
    "battery_mode": SENSOR_FAMILY_BATTERY,
    "min_soc": SENSOR_FAMILY_BATTERY,
    "daily_battery_charge": SENSOR_FAMILY_BATTERY,
    "daily_battery_discharge": SENSOR_FAMILY_BATTERY,
    "daily_battery_charge_foxess": SENSOR_FAMILY_BATTERY,
    "daily_battery_discharge_foxess": SENSOR_FAMILY_BATTERY,
    # Solar & Inverter
    "solar_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "daily_solar_energy": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv1_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv2_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "ct2_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv_dc_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv_ac_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "work_mode": SENSOR_FAMILY_SOLAR_INVERTER,
    "firmware": SENSOR_FAMILY_SOLAR_INVERTER,
    "solar_curtailment": SENSOR_FAMILY_SOLAR_INVERTER,
    "inverter_status": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_today_forecast": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_tomorrow_forecast": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_current_estimate": SENSOR_FAMILY_SOLAR_INVERTER,
    # Grid & Home
    "grid_power": SENSOR_FAMILY_GRID_HOME,
    "grid_status": SENSOR_FAMILY_GRID_HOME,
    "home_load": SENSOR_FAMILY_GRID_HOME,
    "daily_grid_import": SENSOR_FAMILY_GRID_HOME,
    "daily_grid_export": SENSOR_FAMILY_GRID_HOME,
    "daily_load": SENSOR_FAMILY_GRID_HOME,
    "grid_import_power": SENSOR_FAMILY_GRID_HOME,
    # Pricing & Cost
    "current_price": SENSOR_FAMILY_PRICING,
    "current_import_price": SENSOR_FAMILY_PRICING,
    "current_export_price": SENSOR_FAMILY_PRICING,
    "forecast_price": SENSOR_FAMILY_PRICING,
    "daily_import_cost": SENSOR_FAMILY_PRICING,
    "daily_export_earnings": SENSOR_FAMILY_PRICING,
    "daily_avg_cost_per_kwh": SENSOR_FAMILY_PRICING,
    "mtd_avg_cost_per_kwh": SENSOR_FAMILY_PRICING,
    "in_demand_charge_period": SENSOR_FAMILY_PRICING,
    "peak_demand_this_cycle": SENSOR_FAMILY_PRICING,
    "demand_charge_cost": SENSOR_FAMILY_PRICING,
    "days_until_demand_reset": SENSOR_FAMILY_PRICING,
    "daily_supply_charge_cost": SENSOR_FAMILY_PRICING,
    "monthly_supply_charge": SENSOR_FAMILY_PRICING,
    "total_monthly_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_yesterday_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_yesterday_savings": SENSOR_FAMILY_PRICING,
    "amber_usage_month_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_month_savings": SENSOR_FAMILY_PRICING,
    # Flow Power
    "flow_power_price": SENSOR_FAMILY_FLOW_POWER,
    "flow_power_export_price": SENSOR_FAMILY_FLOW_POWER,
    "flow_power_twap": SENSOR_FAMILY_FLOW_POWER,
    "flow_power_network_tariff": SENSOR_FAMILY_FLOW_POWER,
    "flow_power_amber_comparison": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_pea": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_pea_30d": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_bpea": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_cpea": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_pea_import": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_lwap": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_lwap_actual": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_twap": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_avg_rrp": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_dlf": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_avg_usage": SENSOR_FAMILY_FLOW_POWER,
    "fp_account_max_usage": SENSOR_FAMILY_FLOW_POWER,
    # AEMO
    "aemo_price": SENSOR_FAMILY_AEMO,
    "aemo_spike_status": SENSOR_FAMILY_AEMO,
    # EV Charging
    "ev_power": SENSOR_FAMILY_EV_CHARGING,
    "ev_battery_level": SENSOR_FAMILY_EV_CHARGING,
    "ev_charging_status": SENSOR_FAMILY_EV_CHARGING,
    "ev_next_charge_window": SENSOR_FAMILY_EV_CHARGING,
    # Octopus
    "saving_session_active": SENSOR_FAMILY_OCTOPUS,
    "next_saving_session": SENSOR_FAMILY_OCTOPUS,
    "saving_session_rate": SENSOR_FAMILY_OCTOPUS,
}


def family_device_info(entry_id: str, family: str) -> dict:
    """Return device_info dict pointing all entities at the single parent device."""
    return {
        "identifiers": {(DOMAIN, entry_id)},
    }
