"""Config flow for PowerSync integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_AMBER_API_TOKEN,
    CONF_AMBER_SITE_ID,
    CONF_AMBER_FORECAST_TYPE,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    CONF_TESLEMETRY_API_TOKEN,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_AUTO_SYNC_ENABLED,
    CONF_DEMAND_CHARGE_ENABLED,
    CONF_DEMAND_CHARGE_RATE,
    CONF_DEMAND_CHARGE_START_TIME,
    CONF_DEMAND_CHARGE_END_TIME,
    CONF_DEMAND_CHARGE_DAYS,
    CONF_DEMAND_CHARGE_BILLING_DAY,
    CONF_DEMAND_CHARGE_APPLY_TO,
    CONF_DEMAND_ARTIFICIAL_PRICE,
    CONF_DEMAND_ALLOW_GRID_CHARGING,
    CONF_DAILY_SUPPLY_CHARGE,
    CONF_MONTHLY_SUPPLY_CHARGE,
    CONF_TESLA_API_PROVIDER,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    AMBER_API_BASE_URL,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    # Battery system selection
    CONF_BATTERY_SYSTEM,
    BATTERY_SYSTEM_TESLA,
    BATTERY_SYSTEM_SIGENERGY,
    BATTERY_SYSTEM_SUNGROW,
    BATTERY_SYSTEM_FOXESS,
    BATTERY_SYSTEMS,
    # Sungrow battery system configuration
    CONF_SUNGROW_HOST,
    CONF_SUNGROW_PORT,
    CONF_SUNGROW_SLAVE_ID,
    DEFAULT_SUNGROW_PORT,
    DEFAULT_SUNGROW_SLAVE_ID,
    # Sigenergy configuration
    CONF_SIGENERGY_USERNAME,
    CONF_SIGENERGY_PASSWORD,
    CONF_SIGENERGY_PASS_ENC,
    CONF_SIGENERGY_DEVICE_ID,
    CONF_SIGENERGY_STATION_ID,
    CONF_SIGENERGY_ACCESS_TOKEN,
    CONF_SIGENERGY_REFRESH_TOKEN,
    CONF_SIGENERGY_TOKEN_EXPIRES_AT,
    # Sigenergy DC Curtailment via Modbus
    CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
    CONF_SIGENERGY_MODBUS_HOST,
    CONF_SIGENERGY_MODBUS_PORT,
    CONF_SIGENERGY_MODBUS_SLAVE_ID,
    DEFAULT_SIGENERGY_MODBUS_PORT,
    DEFAULT_SIGENERGY_MODBUS_SLAVE_ID,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_AEMO_REGION,
    CONF_AEMO_SPIKE_THRESHOLD,
    AEMO_REGIONS,
    # Flow Power configuration
    CONF_ELECTRICITY_PROVIDER,
    CONF_FLOW_POWER_STATE,
    CONF_FLOW_POWER_PRICE_SOURCE,
    CONF_AEMO_SENSOR_ENTITY,
    CONF_AEMO_SENSOR_5MIN,
    CONF_AEMO_SENSOR_30MIN,
    AEMO_SENSOR_5MIN_PATTERN,
    AEMO_SENSOR_30MIN_PATTERN,
    ELECTRICITY_PROVIDERS,
    FLOW_POWER_STATES,
    FLOW_POWER_PRICE_SOURCES,
    # Flow Power PEA configuration
    CONF_PEA_ENABLED,
    CONF_FLOW_POWER_BASE_RATE,
    CONF_PEA_CUSTOM_VALUE,
    FLOW_POWER_DEFAULT_BASE_RATE,
    # Export price boost configuration
    CONF_EXPORT_BOOST_ENABLED,
    CONF_EXPORT_PRICE_OFFSET,
    CONF_EXPORT_MIN_PRICE,
    CONF_EXPORT_BOOST_START,
    CONF_EXPORT_BOOST_END,
    CONF_EXPORT_BOOST_THRESHOLD,
    DEFAULT_EXPORT_BOOST_START,
    DEFAULT_EXPORT_BOOST_END,
    DEFAULT_EXPORT_BOOST_THRESHOLD,
    # Chip Mode configuration (inverse of export boost)
    CONF_CHIP_MODE_ENABLED,
    CONF_CHIP_MODE_START,
    CONF_CHIP_MODE_END,
    CONF_CHIP_MODE_THRESHOLD,
    DEFAULT_CHIP_MODE_START,
    DEFAULT_CHIP_MODE_END,
    DEFAULT_CHIP_MODE_THRESHOLD,
    # Spike protection configuration
    CONF_SPIKE_PROTECTION_ENABLED,
    # Settled prices only mode
    CONF_SETTLED_PRICES_ONLY,
    # Forecast discrepancy alert
    CONF_FORECAST_DISCREPANCY_ALERT,
    CONF_FORECAST_DISCREPANCY_THRESHOLD,
    DEFAULT_FORECAST_DISCREPANCY_THRESHOLD,
    # Alpha: Force tariff mode toggle
    CONF_FORCE_TARIFF_MODE_TOGGLE,
    # Inverter curtailment configuration
    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_MODEL,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SLAVE_ID,
    CONF_INVERTER_TOKEN,
    CONF_ENPHASE_USERNAME,
    CONF_ENPHASE_PASSWORD,
    CONF_ENPHASE_SERIAL,
    CONF_ENPHASE_NORMAL_PROFILE,
    CONF_ENPHASE_ZERO_EXPORT_PROFILE,
    CONF_ENPHASE_IS_INSTALLER,
    CONF_INVERTER_RESTORE_SOC,
    CONF_FRONIUS_LOAD_FOLLOWING,
    INVERTER_BRANDS,
    DEFAULT_INVERTER_PORT,
    DEFAULT_INVERTER_SLAVE_ID,
    DEFAULT_INVERTER_RESTORE_SOC,
    get_models_for_brand,
    get_brand_defaults,
    # Network Tariff configuration
    CONF_NETWORK_DISTRIBUTOR,
    CONF_NETWORK_TARIFF_CODE,
    CONF_NETWORK_USE_MANUAL_RATES,
    CONF_NETWORK_TARIFF_TYPE,
    CONF_NETWORK_FLAT_RATE,
    CONF_NETWORK_PEAK_RATE,
    CONF_NETWORK_SHOULDER_RATE,
    CONF_NETWORK_OFFPEAK_RATE,
    CONF_NETWORK_PEAK_START,
    CONF_NETWORK_PEAK_END,
    CONF_NETWORK_OFFPEAK_START,
    CONF_NETWORK_OFFPEAK_END,
    CONF_NETWORK_OTHER_FEES,
    CONF_NETWORK_INCLUDE_GST,
    NETWORK_TARIFF_TYPES,
    NETWORK_DISTRIBUTORS,
    ALL_NETWORK_TARIFFS,
    # Automations - OpenWeatherMap API for weather triggers
    CONF_OPENWEATHERMAP_API_KEY,
    CONF_WEATHER_LOCATION,
    # EV Charging and OCPP configuration
    CONF_EV_CHARGING_ENABLED,
    CONF_EV_PROVIDER,
    EV_PROVIDER_FLEET_API,
    EV_PROVIDER_TESLA_BLE,
    EV_PROVIDER_TESLEMETRY_BT,
    EV_PROVIDER_BOTH,
    EV_PROVIDERS,
    CONF_TESLA_BLE_ENTITY_PREFIX,
    DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    CONF_OCPP_ENABLED,
    CONF_OCPP_PORT,
    DEFAULT_OCPP_PORT,
    # Zaptec EV charger configuration
    CONF_ZAPTEC_CHARGER_ENTITY,
    CONF_ZAPTEC_INSTALLATION_ID,
    CONF_ZAPTEC_STANDALONE_ENABLED,
    CONF_ZAPTEC_USERNAME,
    CONF_ZAPTEC_PASSWORD,
    CONF_ZAPTEC_CHARGER_ID,
    CONF_ZAPTEC_INSTALLATION_ID_CLOUD,
    # Solcast Solar Forecast configuration
    CONF_SOLCAST_ENABLED,
    CONF_SOLCAST_API_KEY,
    CONF_SOLCAST_RESOURCE_ID,
    # Octopus Energy UK configuration
    CONF_OCTOPUS_PRODUCT,
    CONF_OCTOPUS_REGION,
    CONF_OCTOPUS_PRODUCT_CODE,
    CONF_OCTOPUS_TARIFF_CODE,
    CONF_OCTOPUS_EXPORT_PRODUCT_CODE,
    CONF_OCTOPUS_EXPORT_TARIFF_CODE,
    OCTOPUS_PRODUCTS,
    OCTOPUS_PRODUCT_CODES,
    OCTOPUS_EXPORT_PRODUCT_CODES,
    OCTOPUS_GSP_REGIONS,
    # Smart Optimization configuration
    CONF_BATTERY_MANAGEMENT_MODE,
    BATTERY_MODE_MANUAL,
    BATTERY_MODE_TOU_SYNC,
    BATTERY_MODE_SMART_OPT,
    BATTERY_MANAGEMENT_MODES,
    CONF_OPTIMIZATION_ENABLED,
    CONF_OPTIMIZATION_COST_FUNCTION,
    CONF_OPTIMIZATION_BACKUP_RESERVE,
    COST_FUNCTION_COST,
    DEFAULT_OPTIMIZATION_BACKUP_RESERVE,
    # Optimization provider selection
    CONF_OPTIMIZATION_PROVIDER,
    OPT_PROVIDER_NATIVE,
    OPT_PROVIDER_POWERSYNC,
    OPTIMIZATION_PROVIDERS,
    OPTIMIZATION_PROVIDER_NATIVE_NAMES,
    # FoxESS battery system configuration
    CONF_FOXESS_HOST,
    CONF_FOXESS_PORT,
    CONF_FOXESS_SLAVE_ID,
    CONF_FOXESS_CONNECTION_TYPE,
    CONF_FOXESS_SERIAL_PORT,
    CONF_FOXESS_SERIAL_BAUDRATE,
    CONF_FOXESS_MODEL_FAMILY,
    CONF_FOXESS_DETECTED_MODEL,
    CONF_FOXESS_CLOUD_USERNAME,
    CONF_FOXESS_CLOUD_PASSWORD,
    CONF_FOXESS_CLOUD_DEVICE_SN,
    CONF_FOXESS_CLOUD_API_KEY,
    DEFAULT_FOXESS_PORT,
    DEFAULT_FOXESS_SLAVE_ID,
    DEFAULT_FOXESS_SERIAL_BAUDRATE,
    FOXESS_CONNECTION_TCP,
    FOXESS_CONNECTION_SERIAL,
    FOXESS_MODEL_H3_PRO,
    FOXESS_MODEL_H3_SMART,
    # GoodWe battery system configuration
    CONF_GOODWE_HOST,
    CONF_GOODWE_PORT,
    CONF_GOODWE_PROTOCOL,
    DEFAULT_GOODWE_PORT_UDP,
    DEFAULT_GOODWE_PORT_TCP,
    BATTERY_SYSTEM_GOODWE,
)

# Combined network tariff key for config flow
CONF_NETWORK_TARIFF_COMBINED = "network_tariff_combined"

_LOGGER = logging.getLogger(__name__)


async def validate_amber_token(hass: HomeAssistant, api_token: str) -> dict[str, Any]:
    """Validate the Amber API token."""
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {api_token}"}

    try:
        async with session.get(
            f"{AMBER_API_BASE_URL}/sites",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 200:
                sites = await response.json()
                if sites and len(sites) > 0:
                    return {
                        "success": True,
                        "sites": sites,
                    }
                else:
                    return {"success": False, "error": "no_sites"}
            elif response.status == 401:
                return {"success": False, "error": "invalid_auth"}
            else:
                return {"success": False, "error": "cannot_connect"}
    except aiohttp.ClientError:
        return {"success": False, "error": "cannot_connect"}
    except Exception as err:
        _LOGGER.exception("Unexpected error validating Amber token: %s", err)
        return {"success": False, "error": "unknown"}


async def validate_teslemetry_token(
    hass: HomeAssistant, api_token: str
) -> dict[str, Any]:
    """Validate the Teslemetry API token and get sites."""
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        async with session.get(
            f"{TESLEMETRY_API_BASE_URL}/api/1/products",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                products = data.get("response", [])

                # Filter for energy sites
                energy_sites = [
                    p for p in products
                    if "energy_site_id" in p
                ]

                if energy_sites:
                    return {
                        "success": True,
                        "sites": energy_sites,
                    }
                else:
                    return {"success": False, "error": "no_energy_sites"}
            elif response.status == 401:
                return {"success": False, "error": "invalid_auth"}
            else:
                error_text = await response.text()
                _LOGGER.error("Teslemetry API error %s: %s", response.status, error_text)
                return {"success": False, "error": "cannot_connect"}
    except aiohttp.ClientError as err:
        _LOGGER.exception("Error connecting to Teslemetry API: %s", err)
        return {"success": False, "error": "cannot_connect"}
    except Exception as err:
        _LOGGER.exception("Unexpected error validating Teslemetry token: %s", err)
        return {"success": False, "error": "unknown"}


async def validate_fleet_api_token(
    hass: HomeAssistant, api_token: str
) -> dict[str, Any]:
    """Validate the Fleet API token and get sites."""
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        async with session.get(
            f"{FLEET_API_BASE_URL}/api/1/products",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                products = data.get("response", [])

                # Filter for energy sites
                energy_sites = [
                    p for p in products
                    if "energy_site_id" in p
                ]

                if energy_sites:
                    return {
                        "success": True,
                        "sites": energy_sites,
                    }
                else:
                    return {"success": False, "error": "no_energy_sites"}
            elif response.status == 401:
                return {"success": False, "error": "invalid_auth"}
            else:
                error_text = await response.text()
                _LOGGER.error("Fleet API error %s: %s", response.status, error_text)
                return {"success": False, "error": "cannot_connect"}
    except aiohttp.ClientError as err:
        _LOGGER.exception("Error connecting to Fleet API: %s", err)
        return {"success": False, "error": "cannot_connect"}
    except Exception as err:
        _LOGGER.exception("Unexpected error validating Fleet API token: %s", err)
        return {"success": False, "error": "unknown"}


async def validate_sigenergy_credentials(
    hass: HomeAssistant,
    username: str,
    pass_enc: str,
    device_id: str,
) -> dict[str, Any]:
    """Validate Sigenergy credentials and get stations list."""
    from .sigenergy_api import SigenergyAPIClient

    try:
        session = async_get_clientsession(hass)
        client = SigenergyAPIClient(
            username=username,
            pass_enc=pass_enc,
            device_id=device_id,
            session=session,
        )

        # Authenticate
        auth_result = await client.authenticate()
        if "error" in auth_result:
            _LOGGER.error(f"Sigenergy auth failed: {auth_result['error']}")
            return {"success": False, "error": "invalid_auth"}

        # Authentication succeeded - save tokens
        result = {
            "success": True,
            "auth_success": True,
            "access_token": auth_result.get("access_token"),
            "refresh_token": auth_result.get("refresh_token"),
            "expires_at": auth_result.get("expires_at"),
        }

        # Try to get stations (may fail with 404 on some accounts)
        stations_result = await client.get_stations()
        if "error" in stations_result:
            _LOGGER.warning(f"Sigenergy get stations failed: {stations_result['error']} - manual station ID required")
            result["stations"] = []
            result["stations_error"] = stations_result["error"]
        else:
            stations = stations_result.get("stations", [])
            result["stations"] = stations

        return result

    except Exception as err:
        _LOGGER.exception("Unexpected error validating Sigenergy credentials: %s", err)
        return {"success": False, "error": "unknown"}


async def test_sungrow_connection(
    hass: HomeAssistant,
    host: str,
    port: int = 502,
    slave_id: int = 1,
) -> dict[str, Any]:
    """Test Sungrow Modbus connection by reading battery SOC."""
    from .inverters.sungrow_sh import SungrowSHController

    try:
        controller = SungrowSHController(host=host, port=port, slave_id=slave_id)
        async with controller:
            # Try to read battery SOC as a connection test
            data = await controller.get_battery_data()
            if data and "battery_soc" in data:
                return {
                    "success": True,
                    "battery_soc": data.get("battery_soc"),
                    "battery_soh": data.get("battery_soh"),
                }
            else:
                return {"success": False, "error": "cannot_connect"}
    except Exception as err:
        _LOGGER.error("Sungrow connection test failed: %s", err)
        return {"success": False, "error": "cannot_connect"}


async def test_foxess_connection(
    hass: HomeAssistant,
    host: str,
    port: int = 502,
    slave_id: int = 247,
    connection_type: str = "tcp",
    serial_port: str | None = None,
    baudrate: int = 9600,
) -> dict[str, Any]:
    """Test FoxESS Modbus connection by detecting model and reading battery SOC."""
    from .inverters.foxess import FoxESSController

    try:
        controller = FoxESSController(
            host=host,
            port=port,
            slave_id=slave_id,
            connection_type=connection_type,
            serial_port=serial_port,
            baudrate=baudrate,
        )
        async with controller:
            # Auto-detect model family
            model_family = await controller.detect_model()

            # Try to read battery SOC
            data = await controller.get_battery_data()
            if data and "battery_soc" in data:
                return {
                    "success": True,
                    "battery_soc": data.get("battery_soc"),
                    "model_family": model_family.value,
                }
            else:
                return {"success": False, "error": "cannot_connect"}
    except Exception as err:
        _LOGGER.error("FoxESS connection test failed: %s", err)
        return {"success": False, "error": "cannot_connect"}


async def test_goodwe_connection(
    hass: HomeAssistant,
    host: str,
    port: int = 8899,
) -> dict[str, Any]:
    """Test GoodWe connection and return inverter info."""
    import goodwe

    try:
        inverter = await goodwe.connect(host=host, port=port, timeout=5, retries=2)
        await inverter.read_device_info()
        # Check if battery-capable (ET/ES family has set_ongrid_battery_dod)
        has_battery = hasattr(inverter, "set_ongrid_battery_dod")
        return {
            "success": True,
            "model_name": inverter.model_name,
            "serial_number": inverter.serial_number,
            "rated_power": inverter.rated_power,
            "has_battery": has_battery,
        }
    except Exception as err:
        _LOGGER.error("GoodWe connection test failed: %s", err)
        return {"success": False, "error": str(err)}


class TeslaAmberSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PowerSync."""

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._amber_data: dict[str, Any] = {}
        self._amber_sites: list[dict[str, Any]] = []
        self._teslemetry_data: dict[str, Any] = {}
        self._tesla_sites: list[dict[str, Any]] = []
        self._site_data: dict[str, Any] = {}
        self._tesla_fleet_available: bool = False
        self._tesla_fleet_token: str | None = None
        self._selected_provider: str | None = None
        # Battery system selection
        self._selected_battery_system: str = BATTERY_SYSTEM_TESLA
        self._sigenergy_data: dict[str, Any] = {}
        self._sigenergy_stations: list[dict[str, Any]] = []
        self._sungrow_data: dict[str, Any] = {}  # Sungrow Modbus configuration
        self._foxess_data: dict[str, Any] = {}  # FoxESS Modbus configuration
        self._goodwe_data: dict[str, Any] = {}  # GoodWe configuration
        self._aemo_only_mode: bool = False  # True if using AEMO spike only (no Amber)
        self._aemo_data: dict[str, Any] = {}
        self._flow_power_data: dict[str, Any] = {}
        self._octopus_data: dict[str, Any] = {}  # Octopus Energy UK configuration
        self._selected_electricity_provider: str = "amber"
        self._custom_tariff_data: dict[str, Any] = {}  # Custom tariff for non-Amber users
        # Optimization provider selection (for Tesla/Sigenergy)
        self._optimization_provider: str = OPT_PROVIDER_NATIVE
        self._ml_options: dict[str, Any] = {}  # Smart Optimization options

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - choose battery system first."""
        # Check if already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Battery system selection is the first step
        return await self.async_step_battery_system()

    async def async_step_provider_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle provider selection - first step in setup."""
        if user_input is not None:
            provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")
            self._selected_electricity_provider = provider

            if provider == "amber":
                # Amber: Need Amber API token
                self._aemo_only_mode = False
                return await self.async_step_amber()
            elif provider == "flow_power":
                # Flow Power: Configure region and price source first
                self._aemo_only_mode = False
                return await self.async_step_flow_power_setup()
            elif provider in ("globird", "aemo_vpp"):
                # Globird/AEMO VPP: AEMO spike only mode (static tariff)
                self._aemo_only_mode = True
                self._amber_data = {}
                return await self.async_step_aemo_config()
            elif provider == "octopus":
                # Octopus Energy UK: Dynamic pricing
                self._aemo_only_mode = False
                self._amber_data = {}  # No Amber API needed
                return await self.async_step_octopus()
            else:
                # Default to Amber
                self._aemo_only_mode = False
                return await self.async_step_amber()

        return self.async_show_form(
            step_id="provider_selection",
            data_schema=vol.Schema({
                vol.Required(CONF_ELECTRICITY_PROVIDER, default="amber"): vol.In(ELECTRICITY_PROVIDERS),
            }),
            description_placeholders={
                "amber_desc": "Full price sync with Amber Electric API",
                "flow_power_desc": "Flow Power with AEMO wholesale or Amber pricing",
                "globird_desc": "AEMO spike detection for static tariff users",
                "aemo_vpp_desc": "AEMO spike detection for VPP exports (AGL, Engie, etc.)",
                "octopus_desc": "Octopus Energy UK with dynamic Agile pricing",
            },
        )

    async def async_step_flow_power_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Flow Power setup - essentials only (region, tariff, base rate)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Parse combined tariff selection (format: "distributor:code")
            combined = user_input.get(CONF_NETWORK_TARIFF_COMBINED, "energex:6900")
            if ":" in combined:
                distributor, tariff_code = combined.split(":", 1)
                user_input[CONF_NETWORK_DISTRIBUTOR] = distributor
                user_input[CONF_NETWORK_TARIFF_CODE] = tariff_code
            # Remove combined key before storing
            user_input.pop(CONF_NETWORK_TARIFF_COMBINED, None)

            # Apply sensible defaults for fields not shown during initial setup
            user_input[CONF_FLOW_POWER_PRICE_SOURCE] = "aemo"
            user_input[CONF_PEA_ENABLED] = True
            user_input[CONF_PEA_CUSTOM_VALUE] = None
            user_input[CONF_NETWORK_USE_MANUAL_RATES] = False
            user_input[CONF_AUTO_SYNC_ENABLED] = True
            user_input[CONF_BATTERY_CURTAILMENT_ENABLED] = False

            # Store Flow Power configuration
            self._flow_power_data = user_input

            # AEMO Direct is the default - no Amber API needed
            self._amber_data = {}
            self._aemo_only_mode = False

            # Route based on battery system selection
            if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                return await self.async_step_sigenergy_credentials()
            elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                return await self.async_step_sungrow()
            elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                return await self.async_step_foxess_connection()
            elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                return await self.async_step_goodwe_connection()
            else:
                return await self.async_step_tesla_provider()

        return self.async_show_form(
            step_id="flow_power_setup",
            data_schema=vol.Schema({
                vol.Required(CONF_FLOW_POWER_STATE, default="QLD1"): vol.In(FLOW_POWER_STATES),
                vol.Required(CONF_NETWORK_TARIFF_COMBINED, default="energex:6900"): vol.In(ALL_NETWORK_TARIFFS),
                vol.Required(CONF_FLOW_POWER_BASE_RATE, default=FLOW_POWER_DEFAULT_BASE_RATE): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=100.0)
                ),
            }),
            errors=errors,
        )

    async def async_step_octopus(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Octopus Energy UK configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Build tariff code from product + region
            product_key = user_input.get(CONF_OCTOPUS_PRODUCT, "agile")
            region = user_input.get(CONF_OCTOPUS_REGION, "C")

            # Map product selection to actual product codes
            product_code = OCTOPUS_PRODUCT_CODES.get(product_key, OCTOPUS_PRODUCT_CODES["agile"])

            # Validate by fetching current prices
            try:
                from .octopus_api import OctopusAPIClient

                client = OctopusAPIClient(async_get_clientsession(self.hass))

                # Dynamically discover current Tracker product code
                if product_key == "tracker":
                    try:
                        discovered = await client.discover_tracker_product()
                        if discovered:
                            product_code = discovered
                    except Exception:
                        pass  # Fall back to hardcoded

                tariff_code = f"E-1R-{product_code}-{region}"

                # Get export product/tariff codes if available
                export_product_code = OCTOPUS_EXPORT_PRODUCT_CODES.get(product_key)
                export_tariff_code = f"E-1R-{export_product_code}-{region}" if export_product_code else None

                rates = await client.get_current_rates(product_code, tariff_code, page_size=5)

                if not rates:
                    errors["base"] = "no_prices"
                    _LOGGER.error(
                        "No Octopus prices found for tariff %s in region %s",
                        tariff_code, region
                    )
            except Exception as err:
                errors["base"] = "cannot_connect"
                _LOGGER.exception("Error validating Octopus tariff: %s", err)

            if not errors:
                # Store Octopus data
                self._octopus_data = {
                    CONF_OCTOPUS_PRODUCT: product_key,
                    CONF_OCTOPUS_REGION: region,
                    CONF_OCTOPUS_PRODUCT_CODE: product_code,
                    CONF_OCTOPUS_TARIFF_CODE: tariff_code,
                    CONF_OCTOPUS_EXPORT_PRODUCT_CODE: export_product_code,
                    CONF_OCTOPUS_EXPORT_TARIFF_CODE: export_tariff_code,
                }

                _LOGGER.info(
                    "Octopus tariff validated: product=%s, tariff=%s, region=%s",
                    product_code, tariff_code, region
                )

                # Route based on battery system selection
                if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                    return await self.async_step_sigenergy_credentials()
                elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                    return await self.async_step_sungrow()
                elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                    return await self.async_step_foxess_connection()
                elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                    return await self.async_step_goodwe_connection()
                else:
                    return await self.async_step_tesla_provider()

        # Build form schema
        data_schema = vol.Schema({
            vol.Required(CONF_OCTOPUS_PRODUCT, default="agile"): vol.In(OCTOPUS_PRODUCTS),
            vol.Required(CONF_OCTOPUS_REGION, default="C"): vol.In(OCTOPUS_GSP_REGIONS),
        })

        return self.async_show_form(
            step_id="octopus",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "octopus_url": "https://octopus.energy/smart/agile/",
            },
        )

    async def async_step_amber(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Amber API token entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate Amber API token
            validation_result = await validate_amber_token(
                self.hass, user_input[CONF_AMBER_API_TOKEN]
            )

            if validation_result["success"]:
                self._amber_data = user_input
                self._amber_sites = validation_result.get("sites", [])
                # Go to Amber settings (export boost, etc.) before Tesla provider
                return await self.async_step_amber_settings()
            else:
                errors["base"] = validation_result.get("error", "unknown")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_AMBER_API_TOKEN): str,
            }
        )

        return self.async_show_form(
            step_id="amber",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "amber_url": "https://app.amber.com.au/developers",
            },
        )

    async def async_step_amber_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Amber-specific settings (export boost, spike protection, etc.) during initial setup."""
        # Check if Tesla is selected (force mode toggle only applies to Tesla)
        is_tesla = self._selected_battery_system == BATTERY_SYSTEM_TESLA

        if user_input is not None:
            # Store Amber settings in _amber_data
            self._amber_data[CONF_SPIKE_PROTECTION_ENABLED] = user_input.get(CONF_SPIKE_PROTECTION_ENABLED, False)
            self._amber_data[CONF_SETTLED_PRICES_ONLY] = user_input.get(CONF_SETTLED_PRICES_ONLY, False)
            self._amber_data[CONF_FORECAST_DISCREPANCY_ALERT] = user_input.get(CONF_FORECAST_DISCREPANCY_ALERT, False)
            self._amber_data[CONF_FORECAST_DISCREPANCY_THRESHOLD] = user_input.get(CONF_FORECAST_DISCREPANCY_THRESHOLD, DEFAULT_FORECAST_DISCREPANCY_THRESHOLD)
            # Force tariff mode toggle only applies to Tesla
            if is_tesla:
                self._amber_data[CONF_FORCE_TARIFF_MODE_TOGGLE] = user_input.get(CONF_FORCE_TARIFF_MODE_TOGGLE, False)
            else:
                self._amber_data[CONF_FORCE_TARIFF_MODE_TOGGLE] = False
            self._amber_data[CONF_EXPORT_BOOST_ENABLED] = user_input.get(CONF_EXPORT_BOOST_ENABLED, False)
            self._amber_data[CONF_EXPORT_PRICE_OFFSET] = user_input.get(CONF_EXPORT_PRICE_OFFSET, 0.0)
            self._amber_data[CONF_EXPORT_MIN_PRICE] = user_input.get(CONF_EXPORT_MIN_PRICE, 0.0)
            self._amber_data[CONF_EXPORT_BOOST_START] = user_input.get(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START)
            self._amber_data[CONF_EXPORT_BOOST_END] = user_input.get(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END)
            self._amber_data[CONF_EXPORT_BOOST_THRESHOLD] = user_input.get(CONF_EXPORT_BOOST_THRESHOLD, DEFAULT_EXPORT_BOOST_THRESHOLD)
            # Chip Mode settings (inverse of export boost)
            self._amber_data[CONF_CHIP_MODE_ENABLED] = user_input.get(CONF_CHIP_MODE_ENABLED, False)
            self._amber_data[CONF_CHIP_MODE_START] = user_input.get(CONF_CHIP_MODE_START, DEFAULT_CHIP_MODE_START)
            self._amber_data[CONF_CHIP_MODE_END] = user_input.get(CONF_CHIP_MODE_END, DEFAULT_CHIP_MODE_END)
            self._amber_data[CONF_CHIP_MODE_THRESHOLD] = user_input.get(CONF_CHIP_MODE_THRESHOLD, DEFAULT_CHIP_MODE_THRESHOLD)

            # Route based on battery system selection
            if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                # Auto-select Amber site for Sigenergy (they don't go through Tesla site selection)
                if self._amber_sites:
                    # Prefer active site, fall back to first site
                    active_sites = [s for s in self._amber_sites if s.get("status") == "active"]
                    if active_sites:
                        amber_site_id = active_sites[0]["id"]
                    else:
                        amber_site_id = self._amber_sites[0]["id"]
                    # Store in _site_data for consistency with Tesla flow
                    self._site_data = {
                        CONF_AMBER_SITE_ID: amber_site_id,
                        CONF_AUTO_SYNC_ENABLED: True,
                        CONF_AMBER_FORECAST_TYPE: "predicted",
                    }
                    _LOGGER.info(f"Auto-selected Amber site for Sigenergy: {amber_site_id}")
                return await self.async_step_sigenergy_credentials()
            elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                # Auto-select Amber site for Sungrow (they don't go through Tesla site selection)
                if self._amber_sites:
                    # Prefer active site, fall back to first site
                    active_sites = [s for s in self._amber_sites if s.get("status") == "active"]
                    if active_sites:
                        amber_site_id = active_sites[0]["id"]
                    else:
                        amber_site_id = self._amber_sites[0]["id"]
                    # Store in _site_data for consistency with Tesla flow
                    self._site_data = {
                        CONF_AMBER_SITE_ID: amber_site_id,
                        CONF_AUTO_SYNC_ENABLED: True,
                        CONF_AMBER_FORECAST_TYPE: "predicted",
                    }
                    _LOGGER.info(f"Auto-selected Amber site for Sungrow: {amber_site_id}")
                return await self.async_step_sungrow()
            elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                # Auto-select Amber site for FoxESS
                if self._amber_sites:
                    active_sites = [s for s in self._amber_sites if s.get("status") == "active"]
                    if active_sites:
                        amber_site_id = active_sites[0]["id"]
                    else:
                        amber_site_id = self._amber_sites[0]["id"]
                    self._site_data = {
                        CONF_AMBER_SITE_ID: amber_site_id,
                        CONF_AUTO_SYNC_ENABLED: True,
                        CONF_AMBER_FORECAST_TYPE: "predicted",
                    }
                    _LOGGER.info(f"Auto-selected Amber site for FoxESS: {amber_site_id}")
                return await self.async_step_foxess_connection()
            elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                # Auto-select Amber site for GoodWe
                if self._amber_sites:
                    active_sites = [s for s in self._amber_sites if s.get("status") == "active"]
                    if active_sites:
                        amber_site_id = active_sites[0]["id"]
                    else:
                        amber_site_id = self._amber_sites[0]["id"]
                    self._site_data = {
                        CONF_AMBER_SITE_ID: amber_site_id,
                        CONF_AUTO_SYNC_ENABLED: True,
                        CONF_AMBER_FORECAST_TYPE: "predicted",
                    }
                    _LOGGER.info(f"Auto-selected Amber site for GoodWe: {amber_site_id}")
                return await self.async_step_goodwe_connection()
            else:
                return await self.async_step_tesla_provider()

        # Build schema - force mode toggle only shown for Tesla
        schema_dict = {
            # Spike and price protection settings
            vol.Optional(CONF_SPIKE_PROTECTION_ENABLED, default=False): bool,
            vol.Optional(CONF_SETTLED_PRICES_ONLY, default=False): bool,
            # Forecast discrepancy alert (notifies when predicted differs from conservative)
            vol.Optional(CONF_FORECAST_DISCREPANCY_ALERT, default=False): bool,
            vol.Optional(CONF_FORECAST_DISCREPANCY_THRESHOLD, default=DEFAULT_FORECAST_DISCREPANCY_THRESHOLD): vol.Coerce(float),
        }

        # Only show force mode toggle for Tesla (it's a Tesla-specific feature)
        if is_tesla:
            schema_dict[vol.Optional(CONF_FORCE_TARIFF_MODE_TOGGLE, default=False)] = bool

        # Export boost settings
        schema_dict.update({
            vol.Optional(CONF_EXPORT_BOOST_ENABLED, default=False): bool,
            vol.Optional(CONF_EXPORT_PRICE_OFFSET, default=0.0): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=50.0)
            ),
            vol.Optional(CONF_EXPORT_MIN_PRICE, default=0.0): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=100.0)
            ),
            vol.Optional(CONF_EXPORT_BOOST_START, default=DEFAULT_EXPORT_BOOST_START): str,
            vol.Optional(CONF_EXPORT_BOOST_END, default=DEFAULT_EXPORT_BOOST_END): str,
            vol.Optional(CONF_EXPORT_BOOST_THRESHOLD, default=DEFAULT_EXPORT_BOOST_THRESHOLD): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=50.0)
            ),
            # Chip Mode settings (inverse of export boost - suppress exports unless above threshold)
            vol.Optional(CONF_CHIP_MODE_ENABLED, default=False): bool,
            vol.Optional(CONF_CHIP_MODE_START, default=DEFAULT_CHIP_MODE_START): str,
            vol.Optional(CONF_CHIP_MODE_END, default=DEFAULT_CHIP_MODE_END): str,
            vol.Optional(CONF_CHIP_MODE_THRESHOLD, default=DEFAULT_CHIP_MODE_THRESHOLD): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=200.0)
            ),
        })

        data_schema = vol.Schema(schema_dict)

        return self.async_show_form(
            step_id="amber_settings",
            data_schema=data_schema,
        )

    async def async_step_battery_system(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user choose battery system - Tesla or Sigenergy (first step)."""
        if user_input is not None:
            self._selected_battery_system = user_input.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)

            # All battery systems can choose between native optimization and Smart Optimization
            return await self.async_step_optimization_provider()

        return self.async_show_form(
            step_id="battery_system",
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_SYSTEM, default=BATTERY_SYSTEM_TESLA): vol.In(BATTERY_SYSTEMS),
            }),
            description_placeholders={
                "tesla_desc": "Tesla Powerwall with Fleet API or Teslemetry",
                "sigenergy_desc": "Sigenergy via Cloud API + optional Modbus curtailment",
                "sungrow_desc": "Sungrow SH-series hybrid inverters via Modbus TCP",
                "foxess_desc": "FoxESS H1/H3/H3-Pro/KH inverters via Modbus TCP or RS485",
            },
        )

    async def async_step_optimization_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user choose optimization provider - native battery or Smart Optimization."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
            self._optimization_provider = provider

            if provider == OPT_PROVIDER_POWERSYNC:
                return await self.async_step_ml_options()
            else:
                # User wants native battery optimization - proceed to electricity provider
                return await self.async_step_provider_selection()

        # Get the native optimization name based on battery system
        native_name = OPTIMIZATION_PROVIDER_NATIVE_NAMES.get(
            self._selected_battery_system, "Battery"
        )

        # Build dynamic provider options with battery-specific naming
        providers = {
            OPT_PROVIDER_NATIVE: f"{native_name} built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="optimization_provider",
            data_schema=vol.Schema({
                vol.Required(CONF_OPTIMIZATION_PROVIDER, default=OPT_PROVIDER_POWERSYNC): vol.In(providers),
            }),
            errors=errors,
            description_placeholders={
                "battery_name": native_name,
            },
        )

    async def async_step_ml_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Smart Optimization options - backup reserve."""
        if user_input is not None:
            self._ml_options = {
                CONF_OPTIMIZATION_COST_FUNCTION: COST_FUNCTION_COST,
                CONF_OPTIMIZATION_BACKUP_RESERVE: user_input.get(
                    CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE
                ),
            }
            # Proceed to electricity provider selection
            return await self.async_step_provider_selection()

        return self.async_show_form(
            step_id="ml_options",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_OPTIMIZATION_BACKUP_RESERVE,
                    default=int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            }),
            description_placeholders={},
        )

    async def async_step_sigenergy_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Sigenergy credential entry.

        Supports both plain password (recommended) and pre-encoded pass_enc (advanced).
        If plain password is provided, it's encoded automatically.
        """
        from .sigenergy_api import encode_sigenergy_password

        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_SIGENERGY_USERNAME, "").strip()
            plain_password = user_input.get(CONF_SIGENERGY_PASSWORD, "").strip()
            pass_enc = user_input.get(CONF_SIGENERGY_PASS_ENC, "").strip()
            device_id = user_input.get(CONF_SIGENERGY_DEVICE_ID, "").strip()

            # Determine which password to use
            # Priority: pass_enc (explicit override) > password (encode it)
            if pass_enc:
                # Advanced user provided pre-encoded password
                final_pass_enc = pass_enc
            elif plain_password:
                # Normal user provided plain password - encode it
                final_pass_enc = encode_sigenergy_password(plain_password)
            else:
                final_pass_enc = ""

            if not username or not final_pass_enc:
                errors["base"] = "missing_credentials"
            elif device_id and (len(device_id) != 13 or not device_id.isdigit()):
                errors["base"] = "invalid_device_id"
            else:
                # Validate credentials
                validation_result = await validate_sigenergy_credentials(
                    self.hass, username, final_pass_enc, device_id
                )

                if validation_result["success"]:
                    self._sigenergy_data = {
                        CONF_SIGENERGY_USERNAME: username,
                        CONF_SIGENERGY_PASS_ENC: final_pass_enc,  # Always store encoded
                        CONF_SIGENERGY_DEVICE_ID: device_id,
                        CONF_SIGENERGY_ACCESS_TOKEN: validation_result.get("access_token"),
                        CONF_SIGENERGY_REFRESH_TOKEN: validation_result.get("refresh_token"),
                        CONF_SIGENERGY_TOKEN_EXPIRES_AT: validation_result.get("expires_at"),
                    }
                    self._sigenergy_stations = validation_result.get("stations", [])
                    return await self.async_step_sigenergy_station()
                else:
                    errors["base"] = validation_result.get("error", "unknown")

        return self.async_show_form(
            step_id="sigenergy_credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_SIGENERGY_USERNAME): str,
                vol.Required(CONF_SIGENERGY_PASSWORD): str,
                vol.Optional(CONF_SIGENERGY_DEVICE_ID, default=""): str,
                vol.Optional(CONF_SIGENERGY_PASS_ENC): str,  # Advanced: pre-encoded
            }),
            errors=errors,
            description_placeholders={
                "credentials_help": "Enter your Sigenergy account password. Device ID is from browser dev tools.",
            },
        )

    async def async_step_sigenergy_station(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Sigenergy station selection or manual entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            station_id = user_input.get(CONF_SIGENERGY_STATION_ID)
            if station_id:
                # Strip any whitespace
                station_id = str(station_id).strip()
                self._sigenergy_data[CONF_SIGENERGY_STATION_ID] = station_id
                # Go to Modbus connection configuration (required for energy data)
                return await self.async_step_sigenergy_modbus()
            else:
                errors["base"] = "no_station_selected"

        # Build station options from validated stations
        station_options = {}
        for station in self._sigenergy_stations:
            station_id = str(station.get("id") or station.get("stationId"))
            station_name = station.get("stationName") or station.get("name") or f"Station {station_id}"
            station_options[station_id] = station_name

        # If no stations found via API, show manual entry form
        if not station_options:
            return self.async_show_form(
                step_id="sigenergy_station",
                data_schema=vol.Schema({
                    vol.Required(CONF_SIGENERGY_STATION_ID): str,
                }),
                errors=errors,
                description_placeholders={
                    "station_help": "Station list unavailable. Enter your Station ID manually. "
                    "To find it, ask SigenAI 'Tell me my StationID' in the Sigenergy app.",
                },
            )

        return self.async_show_form(
            step_id="sigenergy_station",
            data_schema=vol.Schema({
                vol.Required(CONF_SIGENERGY_STATION_ID): vol.In(station_options),
            }),
            errors=errors,
        )

    async def async_step_sigenergy_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Sigenergy Modbus connection (required for energy data)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            modbus_host = user_input.get(CONF_SIGENERGY_MODBUS_HOST, "").strip()
            if not modbus_host:
                errors["base"] = "modbus_host_required"
            else:
                self._sigenergy_data[CONF_SIGENERGY_MODBUS_HOST] = modbus_host
                self._sigenergy_data[CONF_SIGENERGY_MODBUS_PORT] = user_input.get(
                    CONF_SIGENERGY_MODBUS_PORT, DEFAULT_SIGENERGY_MODBUS_PORT
                )
                self._sigenergy_data[CONF_SIGENERGY_MODBUS_SLAVE_ID] = user_input.get(
                    CONF_SIGENERGY_MODBUS_SLAVE_ID, DEFAULT_SIGENERGY_MODBUS_SLAVE_ID
                )
                # Go to optional DC curtailment configuration
                return await self.async_step_sigenergy_dc_curtailment()

        return self.async_show_form(
            step_id="sigenergy_modbus",
            data_schema=vol.Schema({
                vol.Required(CONF_SIGENERGY_MODBUS_HOST): str,
                vol.Optional(
                    CONF_SIGENERGY_MODBUS_PORT,
                    default=DEFAULT_SIGENERGY_MODBUS_PORT,
                ): int,
                vol.Optional(
                    CONF_SIGENERGY_MODBUS_SLAVE_ID,
                    default=DEFAULT_SIGENERGY_MODBUS_SLAVE_ID,
                ): int,
            }),
            errors=errors,
        )

    async def async_step_sigenergy_dc_curtailment(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Sigenergy DC solar curtailment (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            dc_enabled = user_input.get(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False)
            self._sigenergy_data[CONF_SIGENERGY_DC_CURTAILMENT_ENABLED] = dc_enabled
            return await self.async_step_demand_charges()

        return self.async_show_form(
            step_id="sigenergy_dc_curtailment",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
                    default=False,
                ): bool,
            }),
            errors=errors,
        )

    async def async_step_finish_sigenergy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish Sigenergy setup and create config entry."""
        # Build final data for Sigenergy
        final_data = {
            CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_SIGENERGY,
            CONF_AUTO_SYNC_ENABLED: True,
            **self._amber_data,
            **self._site_data,  # Include Amber site ID for NEM region auto-detection
            **self._flow_power_data,
            **self._octopus_data,  # Include Octopus Energy UK configuration
            **self._sigenergy_data,
            **self._aemo_data,  # Include AEMO configuration
            **getattr(self, '_demand_data', {}),
        }

        # Add electricity provider if set
        if self._selected_electricity_provider:
            final_data[CONF_ELECTRICITY_PROVIDER] = self._selected_electricity_provider

        # Include custom tariff data if configured (will be moved to automation_store on setup)
        if self._custom_tariff_data:
            final_data["initial_custom_tariff"] = self._custom_tariff_data

        # Include optimization provider selection
        final_data[CONF_OPTIMIZATION_PROVIDER] = self._optimization_provider
        if self._optimization_provider == OPT_PROVIDER_POWERSYNC and self._ml_options:
            final_data.update(self._ml_options)

        # Generate title based on station ID
        station_id = self._sigenergy_data.get(CONF_SIGENERGY_STATION_ID, "Unknown")
        title = f"PowerSync - Sigenergy ({station_id})"

        return self.async_create_entry(
            title=title,
            data=final_data,
        )

    async def async_step_sungrow(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Sungrow Modbus TCP connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input.get(CONF_SUNGROW_HOST, "").strip()
            port = user_input.get(CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT)
            slave_id = user_input.get(CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID)

            if not host:
                errors["base"] = "sungrow_host_required"
            else:
                # Test Modbus connection
                test_result = await test_sungrow_connection(self.hass, host, port, slave_id)

                if test_result["success"]:
                    # Store Sungrow configuration
                    self._sungrow_data = {
                        CONF_SUNGROW_HOST: host,
                        CONF_SUNGROW_PORT: port,
                        CONF_SUNGROW_SLAVE_ID: slave_id,
                    }
                    _LOGGER.info(
                        "Sungrow Modbus connection successful: host=%s, SOC=%.1f%%, SOH=%.1f%%",
                        host,
                        test_result.get("battery_soc", 0),
                        test_result.get("battery_soh", 0),
                    )
                    return await self.async_step_demand_charges()
                else:
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="sungrow",
            data_schema=vol.Schema({
                vol.Required(CONF_SUNGROW_HOST): str,
                vol.Optional(CONF_SUNGROW_PORT, default=DEFAULT_SUNGROW_PORT): int,
                vol.Optional(CONF_SUNGROW_SLAVE_ID, default=DEFAULT_SUNGROW_SLAVE_ID): int,
            }),
            errors=errors,
        )

    async def async_step_finish_sungrow(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish Sungrow setup and create config entry."""
        # Build final data for Sungrow
        final_data = {
            CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_SUNGROW,
            CONF_AUTO_SYNC_ENABLED: True,
            **self._amber_data,
            **self._site_data,  # Include Amber site ID for NEM region auto-detection
            **self._flow_power_data,
            **self._octopus_data,  # Include Octopus Energy UK configuration
            **self._sungrow_data,
            **self._aemo_data,  # Include AEMO configuration
            **getattr(self, '_demand_data', {}),
        }

        # Add electricity provider if set
        if self._selected_electricity_provider:
            final_data[CONF_ELECTRICITY_PROVIDER] = self._selected_electricity_provider

        # Include custom tariff data if configured (will be moved to automation_store on setup)
        if self._custom_tariff_data:
            final_data["initial_custom_tariff"] = self._custom_tariff_data

        # Include optimization provider selection
        final_data[CONF_OPTIMIZATION_PROVIDER] = self._optimization_provider
        if self._optimization_provider == OPT_PROVIDER_POWERSYNC and self._ml_options:
            final_data.update(self._ml_options)

        # Generate title based on host
        host = self._sungrow_data.get(CONF_SUNGROW_HOST, "Unknown")
        title = f"PowerSync - Sungrow ({host})"

        return self.async_create_entry(
            title=title,
            data=final_data,
        )

    # ---- FoxESS Config Flow Steps ----

    async def async_step_foxess_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose FoxESS connection type: TCP or Serial."""
        if user_input is not None:
            conn_type = user_input.get(CONF_FOXESS_CONNECTION_TYPE, FOXESS_CONNECTION_TCP)
            if conn_type == FOXESS_CONNECTION_SERIAL:
                return await self.async_step_foxess_serial()
            else:
                return await self.async_step_foxess_tcp()

        return self.async_show_form(
            step_id="foxess_connection",
            data_schema=vol.Schema({
                vol.Required(CONF_FOXESS_CONNECTION_TYPE, default=FOXESS_CONNECTION_TCP): vol.In({
                    FOXESS_CONNECTION_TCP: "Modbus TCP (LAN/Wi-Fi)",
                    FOXESS_CONNECTION_SERIAL: "RS485 Serial",
                }),
            }),
        )

    async def async_step_foxess_tcp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure FoxESS Modbus TCP connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input.get(CONF_FOXESS_HOST, "").strip()
            port = user_input.get(CONF_FOXESS_PORT, DEFAULT_FOXESS_PORT)
            slave_id = user_input.get(CONF_FOXESS_SLAVE_ID, DEFAULT_FOXESS_SLAVE_ID)

            if not host:
                errors["base"] = "foxess_host_required"
            else:
                # Test Modbus connection and auto-detect model
                test_result = await test_foxess_connection(
                    self.hass, host, port, slave_id,
                    connection_type="tcp",
                )

                if test_result["success"]:
                    detected_model = test_result.get("model_family", "unknown")
                    self._foxess_data = {
                        CONF_FOXESS_HOST: host,
                        CONF_FOXESS_PORT: port,
                        CONF_FOXESS_SLAVE_ID: slave_id,
                        CONF_FOXESS_CONNECTION_TYPE: FOXESS_CONNECTION_TCP,
                        CONF_FOXESS_MODEL_FAMILY: detected_model,
                    }
                    _LOGGER.info(
                        "FoxESS Modbus TCP connection successful: host=%s, model=%s, SOC=%.1f%%",
                        host,
                        detected_model,
                        test_result.get("battery_soc", 0),
                    )
                    # Let user confirm/override model if in H3-Pro register family
                    if detected_model in ("H3-Pro", "H3-Smart"):
                        return await self.async_step_foxess_model()
                    return await self.async_step_foxess_cloud()
                else:
                    errors["base"] = "foxess_tcp_failed"

        return self.async_show_form(
            step_id="foxess_tcp",
            data_schema=vol.Schema({
                vol.Required(CONF_FOXESS_HOST): str,
                vol.Optional(CONF_FOXESS_PORT, default=DEFAULT_FOXESS_PORT): int,
                vol.Optional(CONF_FOXESS_SLAVE_ID, default=DEFAULT_FOXESS_SLAVE_ID): int,
            }),
            errors=errors,
        )

    async def async_step_foxess_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure FoxESS RS485 serial connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            serial_port = user_input.get(CONF_FOXESS_SERIAL_PORT, "").strip()
            baudrate = user_input.get(CONF_FOXESS_SERIAL_BAUDRATE, DEFAULT_FOXESS_SERIAL_BAUDRATE)
            slave_id = user_input.get(CONF_FOXESS_SLAVE_ID, DEFAULT_FOXESS_SLAVE_ID)

            if not serial_port:
                errors["base"] = "foxess_serial_required"
            else:
                # Test serial connection
                test_result = await test_foxess_connection(
                    self.hass, "", 0, slave_id,
                    connection_type="serial",
                    serial_port=serial_port,
                    baudrate=baudrate,
                )

                if test_result["success"]:
                    detected_model = test_result.get("model_family", "unknown")
                    self._foxess_data = {
                        CONF_FOXESS_SERIAL_PORT: serial_port,
                        CONF_FOXESS_SERIAL_BAUDRATE: baudrate,
                        CONF_FOXESS_SLAVE_ID: slave_id,
                        CONF_FOXESS_CONNECTION_TYPE: FOXESS_CONNECTION_SERIAL,
                        CONF_FOXESS_MODEL_FAMILY: detected_model,
                    }
                    _LOGGER.info(
                        "FoxESS RS485 connection successful: port=%s, model=%s, SOC=%.1f%%",
                        serial_port,
                        detected_model,
                        test_result.get("battery_soc", 0),
                    )
                    # Let user confirm/override model if in H3-Pro register family
                    if detected_model in ("H3-Pro", "H3-Smart"):
                        return await self.async_step_foxess_model()
                    return await self.async_step_foxess_cloud()
                else:
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="foxess_serial",
            data_schema=vol.Schema({
                vol.Required(CONF_FOXESS_SERIAL_PORT, default="/dev/ttyUSB0"): str,
                vol.Optional(CONF_FOXESS_SERIAL_BAUDRATE, default=DEFAULT_FOXESS_SERIAL_BAUDRATE): int,
                vol.Optional(CONF_FOXESS_SLAVE_ID, default=DEFAULT_FOXESS_SLAVE_ID): int,
            }),
            errors=errors,
        )

    async def async_step_foxess_model(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm or override detected FoxESS model family.

        Shown when auto-detection finds H3-Pro-class registers, since H3-Pro
        and H3 Smart share the same register address space.
        """
        if user_input is not None:
            selected = user_input.get(CONF_FOXESS_MODEL_FAMILY, FOXESS_MODEL_H3_PRO)
            self._foxess_data[CONF_FOXESS_MODEL_FAMILY] = selected
            _LOGGER.info("FoxESS model confirmed by user: %s", selected)
            return await self.async_step_foxess_cloud()

        detected = self._foxess_data.get(CONF_FOXESS_MODEL_FAMILY, FOXESS_MODEL_H3_PRO)

        return self.async_show_form(
            step_id="foxess_model",
            data_schema=vol.Schema({
                vol.Required(CONF_FOXESS_MODEL_FAMILY, default=detected): vol.In({
                    FOXESS_MODEL_H3_SMART: "H3 Smart (Native WiFi Modbus)",
                    FOXESS_MODEL_H3_PRO: "H3-Pro",
                }),
            }),
        )

    async def async_step_foxess_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Optional FoxESS Cloud API key (for tariff schedule sync)."""
        errors = {}

        if user_input is not None:
            # Cloud is optional — store if provided, skip if empty
            api_key = user_input.get(CONF_FOXESS_CLOUD_API_KEY, "").strip()
            if api_key:
                device_sn = user_input.get(CONF_FOXESS_CLOUD_DEVICE_SN, "").strip()
                # Validate connection
                try:
                    from .foxess_api import FoxESSCloudClient
                    client = FoxESSCloudClient(api_key=api_key, device_sn=device_sn)
                    try:
                        success, message = await client.test_connection()
                    finally:
                        await client.close()

                    if success:
                        self._foxess_data[CONF_FOXESS_CLOUD_API_KEY] = api_key
                        self._foxess_data[CONF_FOXESS_CLOUD_DEVICE_SN] = device_sn
                        return await self.async_step_demand_charges()
                    else:
                        _LOGGER.warning("FoxESS Cloud connection test failed: %s", message)
                        errors["base"] = "foxess_cloud_auth_failed"
                except Exception as e:
                    _LOGGER.error("FoxESS Cloud connection error: %s", e)
                    errors["base"] = "foxess_cloud_connection_error"
            else:
                # Blank API key — skip cloud setup
                return await self.async_step_demand_charges()

        return self.async_show_form(
            step_id="foxess_cloud",
            data_schema=vol.Schema({
                vol.Optional(CONF_FOXESS_CLOUD_API_KEY, default=""): str,
                vol.Optional(CONF_FOXESS_CLOUD_DEVICE_SN, default=""): str,
            }),
            errors=errors,
        )

    async def async_step_finish_foxess(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish FoxESS setup and create config entry."""
        final_data = {
            CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_FOXESS,
            CONF_AUTO_SYNC_ENABLED: True,
            **self._amber_data,
            **self._site_data,
            **self._flow_power_data,
            **self._octopus_data,
            **self._foxess_data,
            **self._aemo_data,
            **getattr(self, '_demand_data', {}),
        }

        if self._selected_electricity_provider:
            final_data[CONF_ELECTRICITY_PROVIDER] = self._selected_electricity_provider

        if self._custom_tariff_data:
            final_data["initial_custom_tariff"] = self._custom_tariff_data

        final_data[CONF_OPTIMIZATION_PROVIDER] = self._optimization_provider
        if self._optimization_provider == OPT_PROVIDER_POWERSYNC and self._ml_options:
            final_data.update(self._ml_options)

        model_family = self._foxess_data.get(CONF_FOXESS_MODEL_FAMILY, "unknown")
        title = f"PowerSync - FoxESS ({model_family})"

        return self.async_create_entry(
            title=title,
            data=final_data,
        )

    async def async_step_goodwe_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure GoodWe inverter connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input.get(CONF_GOODWE_HOST, "").strip()
            protocol = user_input.get(CONF_GOODWE_PROTOCOL, "udp")
            port = user_input.get(
                CONF_GOODWE_PORT,
                DEFAULT_GOODWE_PORT_UDP if protocol == "udp" else DEFAULT_GOODWE_PORT_TCP,
            )

            if not host:
                errors["base"] = "goodwe_connect_failed"
            else:
                # Test connection
                result = await test_goodwe_connection(self.hass, host, port)

                if result.get("success"):
                    if not result.get("has_battery"):
                        errors["base"] = "goodwe_no_battery"
                    else:
                        self._goodwe_data = {
                            CONF_GOODWE_HOST: host,
                            CONF_GOODWE_PORT: port,
                            CONF_GOODWE_PROTOCOL: protocol,
                        }
                        _LOGGER.info(
                            "GoodWe connection successful: %s (SN: %s, %sW)",
                            result.get("model_name"),
                            result.get("serial_number"),
                            result.get("rated_power"),
                        )
                        return await self.async_step_demand_charges()
                else:
                    errors["base"] = "goodwe_connect_failed"

        return self.async_show_form(
            step_id="goodwe_connection",
            data_schema=vol.Schema({
                vol.Required(CONF_GOODWE_HOST): str,
                vol.Required(CONF_GOODWE_PROTOCOL, default="udp"): vol.In({
                    "udp": "UDP (WiFi dongle, port 8899)",
                    "tcp": "TCP (LAN dongle, port 502)",
                }),
                vol.Required(CONF_GOODWE_PORT, default=DEFAULT_GOODWE_PORT_UDP): int,
            }),
            errors=errors,
        )

    async def async_step_finish_goodwe(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish GoodWe setup and create config entry."""
        final_data = {
            CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_GOODWE,
            CONF_AUTO_SYNC_ENABLED: True,
            **self._amber_data,
            **self._site_data,
            **self._flow_power_data,
            **self._octopus_data,
            **self._goodwe_data,
            **self._aemo_data,
            **getattr(self, '_demand_data', {}),
        }

        if self._selected_electricity_provider:
            final_data[CONF_ELECTRICITY_PROVIDER] = self._selected_electricity_provider

        if self._custom_tariff_data:
            final_data["initial_custom_tariff"] = self._custom_tariff_data

        final_data[CONF_OPTIMIZATION_PROVIDER] = self._optimization_provider
        if self._optimization_provider == OPT_PROVIDER_POWERSYNC and self._ml_options:
            final_data.update(self._ml_options)

        title = "PowerSync - GoodWe"

        return self.async_create_entry(
            title=title,
            data=final_data,
        )

    async def async_step_tesla_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user choose between Tesla Fleet and Teslemetry."""
        # Check if Tesla Fleet integration is configured and loaded
        self._tesla_fleet_available = False
        self._tesla_fleet_token = None

        tesla_fleet_entries = self.hass.config_entries.async_entries("tesla_fleet")
        if tesla_fleet_entries:
            for tesla_entry in tesla_fleet_entries:
                if tesla_entry.state == ConfigEntryState.LOADED:
                    try:
                        if CONF_TOKEN in tesla_entry.data:
                            token_data = tesla_entry.data[CONF_TOKEN]
                            if CONF_ACCESS_TOKEN in token_data:
                                self._tesla_fleet_token = token_data[CONF_ACCESS_TOKEN]
                                self._tesla_fleet_available = True
                                _LOGGER.info("Tesla Fleet integration detected and available")
                                break
                    except Exception as e:
                        _LOGGER.warning("Failed to extract tokens from Tesla Fleet integration: %s", e)

        # If Tesla Fleet is not available, skip provider selection and go to Teslemetry (required)
        if not self._tesla_fleet_available:
            _LOGGER.info("Tesla Fleet not available - Teslemetry required")
            return await self.async_step_teslemetry()

        # Tesla Fleet is available - let user choose
        if user_input is not None:
            self._selected_provider = user_input[CONF_TESLA_API_PROVIDER]

            if self._selected_provider == TESLA_PROVIDER_FLEET_API:
                # User chose Fleet API - validate and get sites
                _LOGGER.info("User selected Tesla Fleet API")
                validation_result = await validate_fleet_api_token(
                    self.hass, self._tesla_fleet_token
                )

                if validation_result["success"]:
                    # Store empty Teslemetry token (we'll use Fleet API in __init__.py)
                    self._teslemetry_data = {CONF_TESLEMETRY_API_TOKEN: ""}
                    self._tesla_sites = validation_result.get("sites", [])
                    return await self.async_step_site_selection()
                else:
                    # Fleet API validation failed - show error
                    errors = {"base": validation_result.get("error", "unknown")}
                    return self.async_show_form(
                        step_id="tesla_provider",
                        data_schema=vol.Schema({
                            vol.Required(CONF_TESLA_API_PROVIDER, default=TESLA_PROVIDER_TESLEMETRY): vol.In({
                                TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - uses existing Tesla Fleet integration)",
                                TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$4/month - proxy service)",
                            }),
                        }),
                        errors=errors,
                    )
            else:
                # User chose Teslemetry
                _LOGGER.info("User selected Teslemetry")
                return await self.async_step_teslemetry()

        # Show provider selection form
        return self.async_show_form(
            step_id="tesla_provider",
            data_schema=vol.Schema({
                vol.Required(CONF_TESLA_API_PROVIDER, default=TESLA_PROVIDER_FLEET_API): vol.In({
                    TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - uses existing Tesla Fleet integration)",
                    TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$4/month - proxy service)",
                }),
            }),
            description_placeholders={
                "fleet_detected": "✓ Tesla Fleet integration detected!",
            },
        )

    async def async_step_teslemetry(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Teslemetry API token entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            teslemetry_token = user_input.get(CONF_TESLEMETRY_API_TOKEN, "").strip()

            if teslemetry_token:
                validation_result = await validate_teslemetry_token(
                    self.hass, teslemetry_token
                )

                if validation_result["success"]:
                    self._teslemetry_data = user_input
                    self._tesla_sites = validation_result.get("sites", [])
                    return await self.async_step_site_selection()
                else:
                    errors["base"] = validation_result.get("error", "unknown")
            else:
                errors["base"] = "no_token_provided"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_TESLEMETRY_API_TOKEN): str,
            }
        )

        return self.async_show_form(
            step_id="teslemetry",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "teslemetry_url": "https://teslemetry.com",
            },
        )

    async def async_step_aemo_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle AEMO spike detection configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate AEMO region is selected if enabled
            aemo_enabled = user_input.get(CONF_AEMO_SPIKE_ENABLED, False)

            if aemo_enabled:
                region = user_input.get(CONF_AEMO_REGION)
                if not region:
                    errors["base"] = "aemo_region_required"
                else:
                    # Store AEMO config
                    self._aemo_data = {
                        CONF_AEMO_SPIKE_ENABLED: True,
                        CONF_AEMO_REGION: region,
                        CONF_AEMO_SPIKE_THRESHOLD: user_input.get(
                            CONF_AEMO_SPIKE_THRESHOLD, 300.0
                        ),
                    }

                    # For Globird/AEMO VPP users with non-Tesla batteries, offer custom tariff
                    # Tesla users don't need custom tariff - rates come from the Tesla tariff API
                    if (self._selected_electricity_provider in ("globird", "aemo_vpp", "other")
                            and self._selected_battery_system != BATTERY_SYSTEM_TESLA):
                        return await self.async_step_custom_tariff()

                    # Route based on battery system selection
                    if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                        return await self.async_step_sigenergy_credentials()
                    elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                        return await self.async_step_sungrow()
                    elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                        return await self.async_step_foxess_connection()
                    elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                        return await self.async_step_goodwe_connection()
                    else:
                        return await self.async_step_tesla_provider()
            else:
                # AEMO disabled
                self._aemo_data = {CONF_AEMO_SPIKE_ENABLED: False}

                # For Globird/AEMO VPP with non-Tesla batteries, offer custom tariff
                # Tesla users don't need it - rates come from the Tesla tariff API
                if (self._selected_electricity_provider in ("globird", "aemo_vpp", "other")
                        and self._selected_battery_system != BATTERY_SYSTEM_TESLA):
                    return await self.async_step_custom_tariff()

                if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                    return await self.async_step_sigenergy_credentials()
                elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                    return await self.async_step_sungrow()
                elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                    return await self.async_step_foxess_connection()
                elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                    return await self.async_step_goodwe_connection()
                else:
                    return await self.async_step_tesla_provider()

        # Build region choices
        region_choices = {"": "Select Region..."}
        region_choices.update(AEMO_REGIONS)

        # Default to enabled if in AEMO-only mode
        default_enabled = self._aemo_only_mode

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_AEMO_SPIKE_ENABLED, default=default_enabled): bool,
                vol.Optional(CONF_AEMO_REGION, default=""): vol.In(region_choices),
                vol.Optional(CONF_AEMO_SPIKE_THRESHOLD, default=3000.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=20000.0)
                ),
            }
        )

        return self.async_show_form(
            step_id="aemo_config",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "threshold_hint": "Default: $3,000/MWh. GloBird spike exports use $3,000/MWh. Adjust only if your plan specifies a different threshold.",
            },
        )

    async def async_step_custom_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure custom tariff for non-Amber users (Globird/AEMO VPP/Other).

        This step allows users to define their TOU tariff structure which is then
        used for EV charging price decisions and Sigenergy Cloud tariff sync.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # User can skip tariff configuration
            skip_tariff = user_input.get("skip_tariff", False)

            if skip_tariff:
                self._custom_tariff_data = {}  # No custom tariff
            else:
                # Parse and validate tariff data
                tariff_type = user_input.get("tariff_type", "tou")

                # Get rates (in cents, will be converted to $/kWh)
                peak_rate = user_input.get("peak_rate", 45) / 100
                shoulder_rate = user_input.get("shoulder_rate", 28) / 100
                offpeak_rate = user_input.get("offpeak_rate", 15) / 100
                super_offpeak_rate = user_input.get("super_offpeak_rate")
                if super_offpeak_rate is not None:
                    super_offpeak_rate = super_offpeak_rate / 100
                fit_rate = user_input.get("fit_rate", 5) / 100

                # Parse time strings
                peak_start = user_input.get("peak_start", "15:00")
                peak_end = user_input.get("peak_end", "21:00")
                super_offpeak_start = user_input.get("super_offpeak_start")
                super_offpeak_end = user_input.get("super_offpeak_end")

                # Build TOU periods based on tariff type
                tou_periods = {}

                if tariff_type == "flat":
                    # Flat rate - single ALL period
                    tou_periods["ALL"] = [
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}
                    ]
                    energy_charges = {"ALL": peak_rate}  # Use peak_rate as the flat rate
                else:
                    # TOU - build periods from time inputs
                    try:
                        peak_start_hour = int(peak_start.split(":")[0])
                        peak_end_hour = int(peak_end.split(":")[0])
                    except (ValueError, IndexError):
                        peak_start_hour = 15
                        peak_end_hour = 21

                    # Peak: weekdays only
                    tou_periods["PEAK"] = [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": peak_start_hour, "toHour": peak_end_hour}
                    ]

                    # Super off-peak if configured (e.g., solar soak period)
                    if super_offpeak_start and super_offpeak_end:
                        try:
                            sop_start = int(super_offpeak_start.split(":")[0])
                            sop_end = int(super_offpeak_end.split(":")[0])
                            tou_periods["SUPER_OFF_PEAK"] = [
                                {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": sop_start, "toHour": sop_end}
                            ]
                        except (ValueError, IndexError):
                            pass

                    # Shoulder: weekday morning (7am to peak start)
                    if peak_start_hour > 7:
                        tou_periods["SHOULDER"] = [
                            {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 7, "toHour": peak_start_hour}
                        ]

                    # Off-peak: overnight and weekends
                    tou_periods["OFF_PEAK"] = [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": peak_end_hour, "toHour": 24},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 7},
                        {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 0, "toHour": 24},  # Sunday
                        {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24},  # Saturday
                    ]

                    # Build energy charges
                    energy_charges = {
                        "PEAK": peak_rate,
                        "OFF_PEAK": offpeak_rate,
                    }
                    if "SHOULDER" in tou_periods:
                        energy_charges["SHOULDER"] = shoulder_rate
                    if "SUPER_OFF_PEAK" in tou_periods and super_offpeak_rate is not None:
                        energy_charges["SUPER_OFF_PEAK"] = super_offpeak_rate

                # Build custom tariff in Tesla format
                provider_name = {
                    "globird": "Globird Energy",
                    "aemo_vpp": "VPP Provider",
                    "other": "Custom Provider",
                }.get(self._selected_electricity_provider, "Custom")

                self._custom_tariff_data = {
                    "name": user_input.get("plan_name", f"{provider_name} TOU"),
                    "utility": provider_name,
                    "seasons": {
                        "All Year": {
                            "fromMonth": 1,
                            "toMonth": 12,
                            "tou_periods": tou_periods,
                        }
                    },
                    "energy_charges": {
                        "All Year": energy_charges,
                    },
                    "sell_tariff": {
                        "energy_charges": {
                            "All Year": {
                                "ALL": fit_rate,
                            }
                        }
                    },
                }

            # Route to battery-specific setup
            if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                return await self.async_step_sigenergy_credentials()
            elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                return await self.async_step_sungrow()
            elif self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                return await self.async_step_foxess_connection()
            elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                return await self.async_step_goodwe_connection()
            else:
                return await self.async_step_tesla_provider()

        # Build hour options for time selection
        hour_options = {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}

        # Schema for custom tariff configuration
        data_schema = vol.Schema(
            {
                vol.Optional("skip_tariff", default=False): bool,
                vol.Optional("plan_name", default=""): str,
                vol.Required("tariff_type", default="tou"): vol.In({
                    "flat": "Flat Rate (single rate all day)",
                    "tou": "Time of Use (peak/shoulder/off-peak)",
                }),
                vol.Required("peak_rate", default=45): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)
                ),
                vol.Optional("shoulder_rate", default=28): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)
                ),
                vol.Required("offpeak_rate", default=15): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)
                ),
                vol.Optional("super_offpeak_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)
                ),
                vol.Required("fit_rate", default=5): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=50)
                ),
                vol.Optional("peak_start", default="15:00"): vol.In(hour_options),
                vol.Optional("peak_end", default="21:00"): vol.In(hour_options),
                vol.Optional("super_offpeak_start"): vol.In(hour_options),
                vol.Optional("super_offpeak_end"): vol.In(hour_options),
            }
        )

        return self.async_show_form(
            step_id="custom_tariff",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": "Configure your electricity tariff rates. All rates are in cents/kWh.",
                "skip_hint": "Check 'Skip tariff configuration' to use default estimation instead.",
            },
        )

    async def async_step_site_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle site selection for both Amber and Tesla."""
        errors: dict[str, str] = {}

        # Determine if we should show Amber-specific options
        # Show only if: not AEMO-only mode AND we have Amber sites AND not Flow Power (which handles settings separately)
        has_amber_sites = bool(self._amber_sites)
        is_flow_power = self._selected_electricity_provider == "flow_power"
        show_amber_options = not self._aemo_only_mode and has_amber_sites and not is_flow_power

        if user_input is not None:
            # Handle Amber site selection (only if we have Amber sites)
            amber_site_id = None
            if has_amber_sites:
                amber_site_id = user_input.get(CONF_AMBER_SITE_ID)
                if not amber_site_id:
                    # Auto-select: prefer active site, or fall back to first site
                    active_sites = [s for s in self._amber_sites if s.get("status") == "active"]
                    if len(active_sites) == 1:
                        amber_site_id = active_sites[0]["id"]
                        _LOGGER.info(f"Auto-selected single active Amber site: {amber_site_id}")
                    elif len(self._amber_sites) == 1:
                        amber_site_id = self._amber_sites[0]["id"]
                        _LOGGER.info(f"Auto-selected single Amber site: {amber_site_id}")

            # Store site selection data
            self._site_data = {
                CONF_TESLA_ENERGY_SITE_ID: user_input[CONF_TESLA_ENERGY_SITE_ID],
            }

            # Add Amber site if we have one
            if amber_site_id:
                self._site_data[CONF_AMBER_SITE_ID] = amber_site_id

            # For Amber provider (not Flow Power), get settings from this form
            if show_amber_options:
                self._site_data[CONF_AUTO_SYNC_ENABLED] = user_input.get(CONF_AUTO_SYNC_ENABLED, True)
                self._site_data[CONF_AMBER_FORECAST_TYPE] = user_input.get(CONF_AMBER_FORECAST_TYPE, "predicted")
                self._site_data[CONF_BATTERY_CURTAILMENT_ENABLED] = user_input.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
            elif self._aemo_only_mode:
                # AEMO-only mode doesn't use Amber sync
                self._site_data[CONF_AUTO_SYNC_ENABLED] = False
            # For Flow Power, these settings are already in _flow_power_data

            # Go to curtailment setup (for AC inverter configuration)
            return await self.async_step_curtailment_setup()

        data_schema_dict: dict[vol.Marker, Any] = {}

        if self._tesla_sites:
            # Build Tesla site options from Teslemetry API response
            tesla_site_options = {}
            for site in self._tesla_sites:
                site_id = str(site.get("energy_site_id"))
                site_name = site.get("site_name", f"Tesla Energy Site {site_id}")
                tesla_site_options[site_id] = f"{site_name} ({site_id})"

            data_schema_dict[vol.Required(CONF_TESLA_ENERGY_SITE_ID)] = vol.In(tesla_site_options)
        else:
            # No sites found - should not happen if validation worked
            _LOGGER.error("No Tesla energy sites found in Teslemetry account")
            return self.async_abort(reason="no_energy_sites")

        # Only add Amber-specific options for Amber provider with Amber sites
        if show_amber_options:
            # Build Amber site options with status indicator
            amber_site_options = {}
            default_amber_site = None
            for site in self._amber_sites:
                site_id = site["id"]
                site_nmi = site.get("nmi", site_id)
                site_status = site.get("status", "unknown")

                # Add status indicator to help users identify active vs closed sites
                if site_status == "active":
                    label = f"{site_nmi} (Active)"
                    # Default to active site
                    if default_amber_site is None:
                        default_amber_site = site_id
                elif site_status == "closed":
                    label = f"{site_nmi} (Closed)"
                else:
                    label = f"{site_nmi} ({site_status})"

                amber_site_options[site_id] = label

            # Always show Amber site selection dropdown (so user can see status)
            if amber_site_options:
                data_schema_dict[vol.Required(CONF_AMBER_SITE_ID, default=default_amber_site)] = vol.In(
                    amber_site_options
                )

            data_schema_dict[vol.Optional(CONF_AUTO_SYNC_ENABLED, default=True)] = bool
            data_schema_dict[vol.Optional(CONF_AMBER_FORECAST_TYPE, default="predicted")] = vol.In({
                "predicted": "Predicted (Default)",
                "low": "Low (Aggressive)",
                "high": "High (Conservative)"
            })
            data_schema_dict[vol.Optional(CONF_BATTERY_CURTAILMENT_ENABLED, default=False)] = bool
        elif has_amber_sites and is_flow_power:
            # Flow Power with Amber pricing - show Amber site selection only
            amber_site_options = {}
            default_amber_site = None
            for site in self._amber_sites:
                site_id = site["id"]
                site_nmi = site.get("nmi", site_id)
                site_status = site.get("status", "unknown")
                if site_status == "active":
                    label = f"{site_nmi} (Active)"
                    if default_amber_site is None:
                        default_amber_site = site_id
                elif site_status == "closed":
                    label = f"{site_nmi} (Closed)"
                else:
                    label = f"{site_nmi} ({site_status})"
                amber_site_options[site_id] = label

            if amber_site_options:
                data_schema_dict[vol.Required(CONF_AMBER_SITE_ID, default=default_amber_site)] = vol.In(
                    amber_site_options
                )

        data_schema = vol.Schema(data_schema_dict)

        return self.async_show_form(
            step_id="site_selection",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_curtailment_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle curtailment configuration during initial setup."""
        # Only show for Tesla battery system (Sigenergy/Sungrow have their own setup)
        if self._selected_battery_system in (BATTERY_SYSTEM_SIGENERGY, BATTERY_SYSTEM_SUNGROW):
            return await self.async_step_weather_setup()

        if user_input is not None:
            # Store curtailment settings
            self._curtailment_data = {
                CONF_BATTERY_CURTAILMENT_ENABLED: user_input.get(CONF_BATTERY_CURTAILMENT_ENABLED, False),
                CONF_AC_INVERTER_CURTAILMENT_ENABLED: user_input.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False),
            }

            # If AC inverter curtailment enabled, go to inverter brand selection
            if user_input.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False):
                return await self.async_step_inverter_brand_setup()

            # Otherwise, go to weather setup
            return await self.async_step_weather_setup()

        # Get default from site_data if already set
        default_curtailment = self._site_data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)

        return self.async_show_form(
            step_id="curtailment_setup",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    default=default_curtailment,
                ): bool,
                vol.Optional(
                    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
                    default=False,
                ): bool,
            }),
        )

    async def async_step_weather_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle weather and solar forecast configuration during initial setup."""
        if user_input is not None:
            # Store weather and Solcast settings in curtailment_data
            if not hasattr(self, '_curtailment_data'):
                self._curtailment_data = {}
            self._curtailment_data[CONF_WEATHER_LOCATION] = user_input.get(CONF_WEATHER_LOCATION, "")
            self._curtailment_data[CONF_OPENWEATHERMAP_API_KEY] = user_input.get(CONF_OPENWEATHERMAP_API_KEY, "")
            self._curtailment_data[CONF_SOLCAST_ENABLED] = user_input.get(CONF_SOLCAST_ENABLED, False)
            self._curtailment_data[CONF_SOLCAST_API_KEY] = user_input.get(CONF_SOLCAST_API_KEY, "")
            self._curtailment_data[CONF_SOLCAST_RESOURCE_ID] = user_input.get(CONF_SOLCAST_RESOURCE_ID, "")

            # Go to demand charges
            return await self.async_step_demand_charges()

        return self.async_show_form(
            step_id="weather_setup",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_WEATHER_LOCATION,
                    default="",
                ): str,
                vol.Optional(
                    CONF_OPENWEATHERMAP_API_KEY,
                    default="",
                ): str,
                vol.Optional(
                    CONF_SOLCAST_ENABLED,
                    default=False,
                ): bool,
                vol.Optional(
                    CONF_SOLCAST_API_KEY,
                    default="",
                ): str,
                vol.Optional(
                    CONF_SOLCAST_RESOURCE_ID,
                    default="",
                ): str,
            }),
        )

    async def async_step_inverter_brand_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle inverter brand selection during initial setup."""
        if user_input is not None:
            self._inverter_brand = user_input.get(CONF_INVERTER_BRAND, "sungrow")
            return await self.async_step_inverter_config_setup()

        return self.async_show_form(
            step_id="inverter_brand_setup",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_INVERTER_BRAND,
                    default="sungrow",
                ): vol.In(INVERTER_BRANDS),
            }),
        )

    async def async_step_inverter_config_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle inverter configuration during initial setup."""
        errors = {}

        if user_input is not None:
            # Store inverter configuration
            inverter_brand = getattr(self, '_inverter_brand', "sungrow")
            inverter_host = user_input.get(CONF_INVERTER_HOST, "")
            inverter_port = user_input.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
            inverter_slave_id = user_input.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)

            # Validate: if battery is Sungrow and AC inverter is also Sungrow,
            # check for IP/port/slave_id conflicts
            if (
                self._selected_battery_system == BATTERY_SYSTEM_SUNGROW
                and inverter_brand == "sungrow"
                and self._sungrow_data
            ):
                sungrow_host = self._sungrow_data.get(CONF_SUNGROW_HOST, "")
                sungrow_port = self._sungrow_data.get(CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT)
                sungrow_slave_id = self._sungrow_data.get(CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID)

                # Same host, port, AND slave ID = conflict
                if (
                    inverter_host == sungrow_host
                    and inverter_port == sungrow_port
                    and inverter_slave_id == sungrow_slave_id
                ):
                    errors["base"] = "sungrow_modbus_conflict"

            if not errors:
                self._inverter_data = {
                    CONF_INVERTER_BRAND: inverter_brand,
                    CONF_INVERTER_MODEL: user_input.get(CONF_INVERTER_MODEL),
                    CONF_INVERTER_HOST: inverter_host,
                    CONF_INVERTER_PORT: inverter_port,
                }

                # Only include slave ID for Modbus brands (not Enphase/Zeversolar which use HTTP)
                if inverter_brand not in ("enphase", "zeversolar"):
                    self._inverter_data[CONF_INVERTER_SLAVE_ID] = inverter_slave_id
                else:
                    self._inverter_data[CONF_INVERTER_SLAVE_ID] = 1

                # Include JWT token, Enlighten credentials, and grid profiles for Enphase
                if inverter_brand == "enphase":
                    self._inverter_data[CONF_INVERTER_TOKEN] = user_input.get(CONF_INVERTER_TOKEN, "")
                    self._inverter_data[CONF_ENPHASE_USERNAME] = user_input.get(CONF_ENPHASE_USERNAME, "")
                    self._inverter_data[CONF_ENPHASE_PASSWORD] = user_input.get(CONF_ENPHASE_PASSWORD, "")
                    self._inverter_data[CONF_ENPHASE_SERIAL] = user_input.get(CONF_ENPHASE_SERIAL, "")
                    # Grid profile names for profile switching fallback
                    self._inverter_data[CONF_ENPHASE_NORMAL_PROFILE] = user_input.get(CONF_ENPHASE_NORMAL_PROFILE, "")
                    self._inverter_data[CONF_ENPHASE_ZERO_EXPORT_PROFILE] = user_input.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE, "")
                    # Installer mode for grid profile access
                    self._inverter_data[CONF_ENPHASE_IS_INSTALLER] = user_input.get(CONF_ENPHASE_IS_INSTALLER, False)

                # Fronius-specific: load following mode
                if inverter_brand == "fronius":
                    self._inverter_data[CONF_FRONIUS_LOAD_FOLLOWING] = user_input.get(
                        CONF_FRONIUS_LOAD_FOLLOWING, False
                    )

                # Restore SOC threshold
                self._inverter_data[CONF_INVERTER_RESTORE_SOC] = user_input.get(
                    CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC
                )

                return await self.async_step_demand_charges()

        # Get brand-specific models and defaults
        # Pass battery system to filter out conflicting models (e.g., SH-series when battery is Sungrow)
        brand = getattr(self, '_inverter_brand', "sungrow")
        models = get_models_for_brand(brand, self._selected_battery_system)
        defaults = get_brand_defaults(brand)

        # Build brand-specific schema
        schema_dict: dict[vol.Marker, Any] = {
            vol.Required(
                CONF_INVERTER_MODEL,
                default=next(iter(models.keys())) if models else "",
            ): vol.In(models),
            vol.Required(
                CONF_INVERTER_HOST,
                default="",
            ): str,
            vol.Required(
                CONF_INVERTER_PORT,
                default=defaults["port"],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }

        # Only show Slave ID for Modbus brands
        if brand not in ("enphase", "zeversolar"):
            schema_dict[vol.Required(
                CONF_INVERTER_SLAVE_ID,
                default=defaults["slave_id"],
            )] = vol.All(vol.Coerce(int), vol.Range(min=1, max=247))

        # Show JWT token, Enlighten credentials, and grid profiles for Enphase
        if brand == "enphase":
            schema_dict[vol.Optional(CONF_INVERTER_TOKEN, default="")] = str
            schema_dict[vol.Optional(CONF_ENPHASE_USERNAME, default="")] = str
            schema_dict[vol.Optional(CONF_ENPHASE_PASSWORD, default="")] = str
            schema_dict[vol.Optional(CONF_ENPHASE_SERIAL, default="")] = str
            # Grid profile names for profile switching fallback (when DPEL/DER unavailable)
            schema_dict[vol.Optional(CONF_ENPHASE_NORMAL_PROFILE, default="")] = str
            schema_dict[vol.Optional(CONF_ENPHASE_ZERO_EXPORT_PROFILE, default="")] = str
            # Installer mode for accessing grid profile switching
            schema_dict[vol.Optional(CONF_ENPHASE_IS_INSTALLER, default=False)] = bool

        # Fronius load following mode
        if brand == "fronius":
            schema_dict[vol.Optional(CONF_FRONIUS_LOAD_FOLLOWING, default=False)] = bool

        # Restore SOC threshold for all brands
        schema_dict[vol.Optional(
            CONF_INVERTER_RESTORE_SOC,
            default=DEFAULT_INVERTER_RESTORE_SOC,
        )] = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))

        return self.async_show_form(
            step_id="inverter_config_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "brand": brand.title(),
            },
        )

    async def async_step_demand_charges(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle optional demand charge configuration (minimal implementation)."""
        if user_input is not None:
            # Store demand charge configuration
            self._demand_data = {}

            # Add demand charge configuration if enabled
            if user_input.get(CONF_DEMAND_CHARGE_ENABLED, False):
                self._demand_data.update({
                    CONF_DEMAND_CHARGE_ENABLED: True,
                    CONF_DEMAND_CHARGE_RATE: user_input[CONF_DEMAND_CHARGE_RATE],
                    CONF_DEMAND_CHARGE_START_TIME: user_input[CONF_DEMAND_CHARGE_START_TIME],
                    CONF_DEMAND_CHARGE_END_TIME: user_input[CONF_DEMAND_CHARGE_END_TIME],
                    CONF_DEMAND_CHARGE_DAYS: user_input[CONF_DEMAND_CHARGE_DAYS],
                    CONF_DEMAND_CHARGE_BILLING_DAY: user_input[CONF_DEMAND_CHARGE_BILLING_DAY],
                    CONF_DEMAND_CHARGE_APPLY_TO: user_input[CONF_DEMAND_CHARGE_APPLY_TO],
                    CONF_DEMAND_ARTIFICIAL_PRICE: user_input.get(CONF_DEMAND_ARTIFICIAL_PRICE, False),
                    CONF_DEMAND_ALLOW_GRID_CHARGING: user_input.get(CONF_DEMAND_ALLOW_GRID_CHARGING, False),
                })
            else:
                self._demand_data[CONF_DEMAND_CHARGE_ENABLED] = False

            # Add supply charges (always include, even if 0)
            self._demand_data[CONF_DAILY_SUPPLY_CHARGE] = user_input.get(CONF_DAILY_SUPPLY_CHARGE, 0.0)
            self._demand_data[CONF_MONTHLY_SUPPLY_CHARGE] = user_input.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0)

            # Route based on battery system
            if self._selected_battery_system == BATTERY_SYSTEM_FOXESS:
                return await self.async_step_finish_foxess()
            elif self._selected_battery_system == BATTERY_SYSTEM_GOODWE:
                return await self.async_step_finish_goodwe()
            elif self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                return await self.async_step_finish_sigenergy()
            elif self._selected_battery_system == BATTERY_SYSTEM_SUNGROW:
                return await self.async_step_finish_sungrow()
            return await self.async_step_ev_charging_setup()

        # Build the form schema
        data_schema = vol.Schema(
            {
                vol.Optional(CONF_DEMAND_CHARGE_ENABLED, default=False): bool,
                vol.Optional(CONF_DEMAND_CHARGE_RATE, default=10.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=100.0)
                ),
                vol.Optional(CONF_DEMAND_CHARGE_START_TIME, default="14:00"): str,
                vol.Optional(CONF_DEMAND_CHARGE_END_TIME, default="20:00"): str,
                vol.Optional(CONF_DEMAND_CHARGE_DAYS, default="All Days"): vol.In(
                    ["All Days", "Weekdays Only", "Weekends Only"]
                ),
                vol.Optional(CONF_DEMAND_CHARGE_BILLING_DAY, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=31)
                ),
                vol.Optional(CONF_DEMAND_CHARGE_APPLY_TO, default="Buy Only"): vol.In(
                    ["Buy Only", "Sell Only", "Both"]
                ),
                vol.Optional(CONF_DEMAND_ARTIFICIAL_PRICE, default=False): bool,
                vol.Optional(CONF_DEMAND_ALLOW_GRID_CHARGING, default=False): bool,
                vol.Optional(CONF_DAILY_SUPPLY_CHARGE, default=0.0): vol.Coerce(float),
                vol.Optional(CONF_MONTHLY_SUPPLY_CHARGE, default=0.0): vol.Coerce(float),
            }
        )

        return self.async_show_form(
            step_id="demand_charges",
            data_schema=data_schema,
            description_placeholders={
                "example_rate": "10.0",
                "example_time": "14:00",
            },
        )

    async def async_step_ev_charging_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle EV charging configuration during initial setup."""
        if user_input is not None:
            # Store EV data for potential zaptec_cloud step
            self._ev_data = dict(user_input)

            # If Zaptec standalone is enabled, go to credentials step
            if user_input.get(CONF_ZAPTEC_STANDALONE_ENABLED, False):
                return await self.async_step_zaptec_cloud()

            return self._create_final_entry_from_ev(user_input)

        return self.async_show_form(
            step_id="ev_charging_setup",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_EV_CHARGING_ENABLED,
                    default=False,
                ): bool,
                vol.Optional(
                    CONF_EV_PROVIDER,
                    default=EV_PROVIDER_FLEET_API,
                ): vol.In(EV_PROVIDERS),
                vol.Optional(
                    CONF_TESLA_BLE_ENTITY_PREFIX,
                    default=DEFAULT_TESLA_BLE_ENTITY_PREFIX,
                ): str,
                vol.Optional(
                    CONF_OCPP_ENABLED,
                    default=False,
                ): bool,
                vol.Optional(
                    CONF_OCPP_PORT,
                    default=DEFAULT_OCPP_PORT,
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(
                    CONF_ZAPTEC_CHARGER_ENTITY,
                    default="",
                ): str,
                vol.Optional(
                    CONF_ZAPTEC_INSTALLATION_ID,
                    default="",
                ): str,
                vol.Optional(
                    CONF_ZAPTEC_STANDALONE_ENABLED,
                    default=False,
                ): bool,
            }),
        )

    def _create_final_entry_from_ev(
        self, ev_input: dict[str, Any]
    ) -> FlowResult:
        """Create final config entry from EV charging step data."""
        # Combine all data
        data = {
            **self._amber_data,
            **self._teslemetry_data,
            **self._site_data,
            **self._aemo_data,
            **self._flow_power_data,
            **self._octopus_data,
            **getattr(self, '_curtailment_data', {}),
            **getattr(self, '_inverter_data', {}),
            **getattr(self, '_demand_data', {}),
            CONF_ELECTRICITY_PROVIDER: self._selected_electricity_provider,
        }

        # Include custom tariff data if configured
        if self._custom_tariff_data:
            data["initial_custom_tariff"] = self._custom_tariff_data

        # Include optimization provider selection (for Tesla)
        data[CONF_OPTIMIZATION_PROVIDER] = self._optimization_provider
        if self._optimization_provider == OPT_PROVIDER_POWERSYNC and self._ml_options:
            data.update(self._ml_options)

        # Add EV settings
        data[CONF_EV_CHARGING_ENABLED] = ev_input.get(CONF_EV_CHARGING_ENABLED, False)
        data[CONF_EV_PROVIDER] = ev_input.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)
        data[CONF_TESLA_BLE_ENTITY_PREFIX] = ev_input.get(
            CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX
        )

        # Add OCPP settings
        data[CONF_OCPP_ENABLED] = ev_input.get(CONF_OCPP_ENABLED, False)
        data[CONF_OCPP_PORT] = ev_input.get(CONF_OCPP_PORT, DEFAULT_OCPP_PORT)

        # Add Zaptec settings
        zaptec_entity = ev_input.get(CONF_ZAPTEC_CHARGER_ENTITY, "")
        if zaptec_entity:
            data[CONF_ZAPTEC_CHARGER_ENTITY] = zaptec_entity
        data[CONF_ZAPTEC_INSTALLATION_ID] = ev_input.get(
            CONF_ZAPTEC_INSTALLATION_ID, ""
        )

        # Add Zaptec standalone settings (from _ev_data or zaptec_cloud step)
        data[CONF_ZAPTEC_STANDALONE_ENABLED] = ev_input.get(
            CONF_ZAPTEC_STANDALONE_ENABLED, False
        )
        for key in (CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_PASSWORD,
                     CONF_ZAPTEC_CHARGER_ID, CONF_ZAPTEC_INSTALLATION_ID_CLOUD):
            if key in ev_input:
                data[key] = ev_input[key]

        # Set appropriate title based on provider
        if self._aemo_only_mode:
            title = "PowerSync Globird"
        elif self._selected_electricity_provider == "flow_power":
            title = "PowerSync Flow Power"
        elif self._selected_electricity_provider == "octopus":
            title = "PowerSync Octopus"
        else:
            title = "PowerSync Amber"
        return self.async_create_entry(title=title, data=data)

    async def async_step_zaptec_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Zaptec Cloud API credentials during initial setup."""
        errors = {}

        if user_input is not None:
            username = user_input.get(CONF_ZAPTEC_USERNAME, "")
            password = user_input.get(CONF_ZAPTEC_PASSWORD, "")

            if username and password:
                # Validate credentials
                from .zaptec_api import ZaptecCloudClient
                client = ZaptecCloudClient(username, password)
                try:
                    success, message = await client.test_connection()
                    if success:
                        # Auto-discover chargers and installations
                        chargers = await client.get_chargers()
                        installations = await client.get_installations()

                        ev_data = getattr(self, '_ev_data', {})
                        ev_data[CONF_ZAPTEC_USERNAME] = username
                        ev_data[CONF_ZAPTEC_PASSWORD] = password

                        # Store first charger/installation IDs
                        if chargers:
                            ev_data[CONF_ZAPTEC_CHARGER_ID] = chargers[0].get("Id", "")
                        if installations:
                            ev_data[CONF_ZAPTEC_INSTALLATION_ID_CLOUD] = installations[0].get("Id", "")

                        return self._create_final_entry_from_ev(ev_data)
                    else:
                        errors["base"] = "zaptec_auth_failed"
                        _LOGGER.error("Zaptec Cloud auth failed: %s", message)
                except Exception as e:
                    errors["base"] = "zaptec_auth_failed"
                    _LOGGER.error("Zaptec Cloud connection error: %s", e)
                finally:
                    await client.close()
            else:
                errors["base"] = "zaptec_missing_credentials"

        return self.async_show_form(
            step_id="zaptec_cloud",
            data_schema=vol.Schema({
                vol.Required(CONF_ZAPTEC_USERNAME): str,
                vol.Required(CONF_ZAPTEC_PASSWORD): str,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TeslaAmberSyncOptionsFlow:
        """Get the options flow for this handler."""
        return TeslaAmberSyncOptionsFlow()


class TeslaAmberSyncOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for PowerSync."""

    async def _restore_export_rule(self) -> None:
        """Restore Tesla export rule to battery_ok when curtailment is disabled."""
        site_id = self.config_entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
        if not site_id:
            _LOGGER.warning("Cannot restore export rule - no Tesla site ID configured")
            return

        # Determine API provider and get token
        api_provider = self.config_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)

        if api_provider == TESLA_PROVIDER_FLEET_API:
            # Try to get Fleet API token from Tesla Fleet integration
            tesla_fleet_entries = self.hass.config_entries.async_entries("tesla_fleet")
            api_token = None
            for tesla_entry in tesla_fleet_entries:
                if tesla_entry.state == ConfigEntryState.LOADED:
                    try:
                        if CONF_TOKEN in tesla_entry.data:
                            token_data = tesla_entry.data[CONF_TOKEN]
                            if CONF_ACCESS_TOKEN in token_data:
                                api_token = token_data[CONF_ACCESS_TOKEN]
                                break
                    except Exception:
                        pass
            if not api_token:
                _LOGGER.error("Cannot restore export rule - Fleet API token not available")
                return
            base_url = FLEET_API_BASE_URL
        else:
            # Teslemetry
            api_token = self.config_entry.data.get(CONF_TESLEMETRY_API_TOKEN)
            if not api_token:
                _LOGGER.error("Cannot restore export rule - Teslemetry API token not configured")
                return
            base_url = TESLEMETRY_API_BASE_URL

        try:
            session = async_get_clientsession(self.hass)
            headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
            url = f"{base_url}/api/1/energy_sites/{site_id}/grid_import_export"

            async with session.post(
                url,
                headers=headers,
                json={"customer_preferred_export_rule": "battery_ok"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    _LOGGER.info("✅ Solar curtailment disabled - restored export rule to 'battery_ok'")
                else:
                    error_text = await response.text()
                    _LOGGER.error(f"Failed to restore export rule: {response.status} - {error_text}")
        except Exception as e:
            _LOGGER.error(f"Error restoring export rule: {e}")

    def _get_option(self, key: str, default: Any = None) -> Any:
        """Get option value with fallback to data for backwards compatibility."""
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default)
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Select electricity provider and battery-specific settings."""
        # Detect battery system type
        battery_system = self.config_entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)

        if battery_system == BATTERY_SYSTEM_SIGENERGY:
            return await self.async_step_init_sigenergy(user_input)
        elif battery_system == BATTERY_SYSTEM_SUNGROW:
            return await self.async_step_init_sungrow(user_input)
        elif battery_system == BATTERY_SYSTEM_FOXESS:
            return await self.async_step_init_foxess(user_input)
        elif battery_system == BATTERY_SYSTEM_GOODWE:
            return await self.async_step_init_goodwe(user_input)
        else:
            return await self.async_step_init_tesla(user_input)

    async def async_step_init_tesla(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Tesla users: Select electricity provider, Tesla API provider, and optimization provider."""
        if user_input is not None:
            # Store provider selections
            self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")
            self._tesla_provider = user_input.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            optimization_provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)

            # Check if switching to Teslemetry and need token
            current_tesla_provider = self.config_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            current_teslemetry_token = self.config_entry.data.get(CONF_TESLEMETRY_API_TOKEN)

            if self._tesla_provider == TESLA_PROVIDER_TESLEMETRY and (
                current_tesla_provider != TESLA_PROVIDER_TESLEMETRY or not current_teslemetry_token
            ):
                # Need to get Teslemetry token
                return await self.async_step_teslemetry_token()

            # Update config entry data with new Tesla provider and optimization provider
            new_data = dict(self.config_entry.data)
            if self._tesla_provider != current_tesla_provider:
                new_data[CONF_TESLA_API_PROVIDER] = self._tesla_provider
            new_data[CONF_OPTIMIZATION_PROVIDER] = optimization_provider
            # If Smart Optimization, store ML options
            if optimization_provider == OPT_PROVIDER_POWERSYNC:
                new_data[CONF_OPTIMIZATION_COST_FUNCTION] = COST_FUNCTION_COST
                new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = user_input.get(
                    CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                ) / 100.0  # Convert from % to decimal
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

            # Route to provider-specific step
            if self._provider == "amber":
                return await self.async_step_amber_options()
            elif self._provider == "flow_power":
                return await self.async_step_flow_power_options()
            elif self._provider in ("globird", "aemo_vpp"):
                return await self.async_step_globird_options()
            elif self._provider == "octopus":
                return await self.async_step_octopus_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_tesla_provider = self.config_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
        current_opt_provider = self.config_entry.data.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
        current_backup_reserve = self.config_entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE)

        # Build Tesla provider choices
        tesla_providers = {
            TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - requires Tesla Fleet integration)",
            TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$4/month - easier setup)",
        }

        # Build optimization provider choices
        opt_providers = {
            OPT_PROVIDER_NATIVE: "Tesla Powerwall built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
                    vol.Required(
                        CONF_TESLA_API_PROVIDER,
                        default=current_tesla_provider,
                    ): vol.In(tesla_providers),
                    vol.Required(
                        CONF_OPTIMIZATION_PROVIDER,
                        default=current_opt_provider,
                    ): vol.In(opt_providers),
                    vol.Optional(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        default=int(current_backup_reserve * 100) if current_backup_reserve < 1 else int(current_backup_reserve),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                }
            ),
        )

    async def async_step_init_sigenergy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Sigenergy users: Configure Modbus connection and DC curtailment.

        Supports both plain password (recommended) and pre-encoded pass_enc (advanced).
        """
        from .sigenergy_api import encode_sigenergy_password

        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate Modbus host
            modbus_host = user_input.get(CONF_SIGENERGY_MODBUS_HOST, "").strip()
            if not modbus_host:
                errors["base"] = "modbus_host_required"
            else:
                # Store provider and Sigenergy settings
                self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")

                # Update config entry data with Sigenergy Modbus settings
                new_data = dict(self.config_entry.data)
                new_data[CONF_SIGENERGY_MODBUS_HOST] = modbus_host
                new_data[CONF_SIGENERGY_MODBUS_PORT] = user_input.get(
                    CONF_SIGENERGY_MODBUS_PORT, DEFAULT_SIGENERGY_MODBUS_PORT
                )
                new_data[CONF_SIGENERGY_MODBUS_SLAVE_ID] = user_input.get(
                    CONF_SIGENERGY_MODBUS_SLAVE_ID, DEFAULT_SIGENERGY_MODBUS_SLAVE_ID
                )
                new_data[CONF_SIGENERGY_DC_CURTAILMENT_ENABLED] = user_input.get(
                    CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False
                )

                # Update Sigenergy Cloud API credentials if provided
                sigen_username = user_input.get(CONF_SIGENERGY_USERNAME, "").strip()
                sigen_password = user_input.get(CONF_SIGENERGY_PASSWORD, "").strip()
                sigen_pass_enc = user_input.get(CONF_SIGENERGY_PASS_ENC, "").strip()
                sigen_device_id = user_input.get(CONF_SIGENERGY_DEVICE_ID, "").strip()
                sigen_station_id = user_input.get(CONF_SIGENERGY_STATION_ID, "").strip()

                # Determine final pass_enc: explicit pass_enc > encoded password
                if sigen_pass_enc:
                    final_pass_enc = sigen_pass_enc
                elif sigen_password:
                    final_pass_enc = encode_sigenergy_password(sigen_password)
                else:
                    final_pass_enc = ""

                if sigen_username:
                    new_data[CONF_SIGENERGY_USERNAME] = sigen_username
                if final_pass_enc:
                    new_data[CONF_SIGENERGY_PASS_ENC] = final_pass_enc
                if sigen_device_id:
                    new_data[CONF_SIGENERGY_DEVICE_ID] = sigen_device_id
                if sigen_station_id:
                    new_data[CONF_SIGENERGY_STATION_ID] = sigen_station_id

                # Optimization provider settings
                optimization_provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
                new_data[CONF_OPTIMIZATION_PROVIDER] = optimization_provider
                if optimization_provider == OPT_PROVIDER_POWERSYNC:
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = COST_FUNCTION_COST
                    new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = user_input.get(
                        CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                    ) / 100.0

                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to provider-specific step
                if self._provider == "amber":
                    return await self.async_step_amber_options()
                elif self._provider == "flow_power":
                    return await self.async_step_flow_power_options()
                elif self._provider in ("globird", "aemo_vpp"):
                    return await self.async_step_globird_options()
                elif self._provider == "octopus":
                    return await self.async_step_octopus_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_modbus_host = self._get_option(CONF_SIGENERGY_MODBUS_HOST, "")
        current_modbus_port = self._get_option(CONF_SIGENERGY_MODBUS_PORT, DEFAULT_SIGENERGY_MODBUS_PORT)
        current_modbus_slave_id = self._get_option(CONF_SIGENERGY_MODBUS_SLAVE_ID, DEFAULT_SIGENERGY_MODBUS_SLAVE_ID)
        current_dc_curtailment = self._get_option(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False)
        current_opt_provider = self.config_entry.data.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
        current_backup_reserve = self.config_entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE)

        # Get current Sigenergy Cloud credentials (for display, show empty if not set)
        current_sigen_username = self.config_entry.data.get(CONF_SIGENERGY_USERNAME, "")
        current_sigen_device_id = self.config_entry.data.get(CONF_SIGENERGY_DEVICE_ID, "")
        current_sigen_station_id = self.config_entry.data.get(CONF_SIGENERGY_STATION_ID, "")
        # Don't show current password for security - user must re-enter if changing

        # Build optimization provider choices
        opt_providers = {
            OPT_PROVIDER_NATIVE: "Sigenergy built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="init_sigenergy",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
                    vol.Required(
                        CONF_OPTIMIZATION_PROVIDER,
                        default=current_opt_provider,
                    ): vol.In(opt_providers),
                    vol.Optional(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        default=int(current_backup_reserve * 100) if current_backup_reserve < 1 else int(current_backup_reserve),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_SIGENERGY_MODBUS_HOST,
                        default=current_modbus_host,
                    ): str,
                    vol.Optional(
                        CONF_SIGENERGY_MODBUS_PORT,
                        default=current_modbus_port,
                    ): int,
                    vol.Optional(
                        CONF_SIGENERGY_MODBUS_SLAVE_ID,
                        default=current_modbus_slave_id,
                    ): int,
                    vol.Optional(
                        CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
                        default=current_dc_curtailment,
                    ): bool,
                    # Sigenergy Cloud API credentials for tariff sync
                    vol.Optional(
                        CONF_SIGENERGY_USERNAME,
                        default=current_sigen_username,
                        description={"suggested_value": current_sigen_username},
                    ): str,
                    vol.Optional(
                        CONF_SIGENERGY_PASSWORD,  # Plain password (recommended)
                        description={"suggested_value": ""},
                    ): str,
                    vol.Optional(
                        CONF_SIGENERGY_PASS_ENC,  # Advanced: pre-encoded
                        description={"suggested_value": ""},
                    ): str,
                    vol.Optional(
                        CONF_SIGENERGY_DEVICE_ID,
                        default=current_sigen_device_id,
                        description={"suggested_value": current_sigen_device_id},
                    ): str,
                    vol.Optional(
                        CONF_SIGENERGY_STATION_ID,
                        default=current_sigen_station_id,
                        description={"suggested_value": current_sigen_station_id},
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_init_sungrow(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Sungrow users: Configure Modbus connection settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate Modbus host
            modbus_host = user_input.get(CONF_SUNGROW_HOST, "").strip()
            if not modbus_host:
                errors["base"] = "sungrow_host_required"
            else:
                # Store provider and Sungrow settings
                self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")

                # Update config entry data with Sungrow Modbus settings
                new_data = dict(self.config_entry.data)
                new_data[CONF_SUNGROW_HOST] = modbus_host
                new_data[CONF_SUNGROW_PORT] = user_input.get(
                    CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT
                )
                new_data[CONF_SUNGROW_SLAVE_ID] = user_input.get(
                    CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID
                )

                # Optimization provider settings
                optimization_provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
                new_data[CONF_OPTIMIZATION_PROVIDER] = optimization_provider
                if optimization_provider == OPT_PROVIDER_POWERSYNC:
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = COST_FUNCTION_COST
                    new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = user_input.get(
                        CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                    ) / 100.0

                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to provider-specific step
                if self._provider == "amber":
                    return await self.async_step_amber_options()
                elif self._provider == "flow_power":
                    return await self.async_step_flow_power_options()
                elif self._provider in ("globird", "aemo_vpp"):
                    return await self.async_step_globird_options()
                elif self._provider == "octopus":
                    return await self.async_step_octopus_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_host = self._get_option(CONF_SUNGROW_HOST, "")
        current_port = self._get_option(CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT)
        current_slave_id = self._get_option(CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID)
        current_opt_provider = self.config_entry.data.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
        current_backup_reserve = self.config_entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE)

        # Build optimization provider choices
        opt_providers = {
            OPT_PROVIDER_NATIVE: "Sungrow built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="init_sungrow",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
                    vol.Required(
                        CONF_OPTIMIZATION_PROVIDER,
                        default=current_opt_provider,
                    ): vol.In(opt_providers),
                    vol.Optional(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        default=int(current_backup_reserve * 100) if current_backup_reserve < 1 else int(current_backup_reserve),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_SUNGROW_HOST,
                        default=current_host,
                    ): str,
                    vol.Optional(
                        CONF_SUNGROW_PORT,
                        default=current_port,
                    ): int,
                    vol.Optional(
                        CONF_SUNGROW_SLAVE_ID,
                        default=current_slave_id,
                    ): int,
                }
            ),
            errors=errors,
        )

    async def async_step_init_foxess(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for FoxESS users: Configure Modbus connection and optimization settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate connection
            conn_type = user_input.get(CONF_FOXESS_CONNECTION_TYPE, FOXESS_CONNECTION_TCP)
            modbus_host = user_input.get(CONF_FOXESS_HOST, "").strip()
            serial_port = user_input.get(CONF_FOXESS_SERIAL_PORT, "").strip()

            if conn_type == FOXESS_CONNECTION_TCP and not modbus_host:
                errors["base"] = "foxess_host_required"
            elif conn_type == FOXESS_CONNECTION_SERIAL and not serial_port:
                errors["base"] = "foxess_serial_required"
            else:
                self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")

                # Update config entry data with FoxESS settings
                new_data = dict(self.config_entry.data)
                new_data[CONF_FOXESS_CONNECTION_TYPE] = conn_type
                if conn_type == FOXESS_CONNECTION_TCP:
                    new_data[CONF_FOXESS_HOST] = modbus_host
                    new_data[CONF_FOXESS_PORT] = user_input.get(
                        CONF_FOXESS_PORT, DEFAULT_FOXESS_PORT
                    )
                else:
                    new_data[CONF_FOXESS_SERIAL_PORT] = serial_port
                    new_data[CONF_FOXESS_SERIAL_BAUDRATE] = user_input.get(
                        CONF_FOXESS_SERIAL_BAUDRATE, DEFAULT_FOXESS_SERIAL_BAUDRATE
                    )
                new_data[CONF_FOXESS_SLAVE_ID] = user_input.get(
                    CONF_FOXESS_SLAVE_ID, DEFAULT_FOXESS_SLAVE_ID
                )

                # Optimization provider settings
                optimization_provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
                new_data[CONF_OPTIMIZATION_PROVIDER] = optimization_provider
                if optimization_provider == OPT_PROVIDER_POWERSYNC:
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = COST_FUNCTION_COST
                    new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = user_input.get(
                        CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                    ) / 100.0

                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to provider-specific step
                if self._provider == "amber":
                    return await self.async_step_amber_options()
                elif self._provider == "flow_power":
                    return await self.async_step_flow_power_options()
                elif self._provider in ("globird", "aemo_vpp"):
                    return await self.async_step_globird_options()
                elif self._provider == "octopus":
                    return await self.async_step_octopus_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_conn_type = self._get_option(CONF_FOXESS_CONNECTION_TYPE, FOXESS_CONNECTION_TCP)
        current_host = self._get_option(CONF_FOXESS_HOST, "")
        current_port = self._get_option(CONF_FOXESS_PORT, DEFAULT_FOXESS_PORT)
        current_slave_id = self._get_option(CONF_FOXESS_SLAVE_ID, DEFAULT_FOXESS_SLAVE_ID)
        current_serial_port = self._get_option(CONF_FOXESS_SERIAL_PORT, "")
        current_baudrate = self._get_option(CONF_FOXESS_SERIAL_BAUDRATE, DEFAULT_FOXESS_SERIAL_BAUDRATE)
        current_opt_provider = self.config_entry.data.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
        current_backup_reserve = self.config_entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE)

        opt_providers = {
            OPT_PROVIDER_NATIVE: "FoxESS built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="init_foxess",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
                    vol.Required(
                        CONF_OPTIMIZATION_PROVIDER,
                        default=current_opt_provider,
                    ): vol.In(opt_providers),
                    vol.Optional(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        default=int(current_backup_reserve * 100) if current_backup_reserve < 1 else int(current_backup_reserve),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_FOXESS_CONNECTION_TYPE,
                        default=current_conn_type,
                    ): vol.In({
                        FOXESS_CONNECTION_TCP: "Modbus TCP",
                        FOXESS_CONNECTION_SERIAL: "RS485 Serial",
                    }),
                    vol.Optional(
                        CONF_FOXESS_HOST,
                        default=current_host,
                    ): str,
                    vol.Optional(
                        CONF_FOXESS_PORT,
                        default=current_port,
                    ): int,
                    vol.Optional(
                        CONF_FOXESS_SLAVE_ID,
                        default=current_slave_id,
                    ): int,
                    vol.Optional(
                        CONF_FOXESS_SERIAL_PORT,
                        default=current_serial_port,
                    ): str,
                    vol.Optional(
                        CONF_FOXESS_SERIAL_BAUDRATE,
                        default=current_baudrate,
                    ): int,
                }
            ),
            errors=errors,
        )

    async def async_step_init_goodwe(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for GoodWe users: Configure connection and optimization settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            goodwe_host = user_input.get(CONF_GOODWE_HOST, "").strip()

            if not goodwe_host:
                errors["base"] = "goodwe_connect_failed"
            else:
                self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")

                # Update config entry data with GoodWe settings
                new_data = dict(self.config_entry.data)
                new_data[CONF_GOODWE_HOST] = goodwe_host
                new_data[CONF_GOODWE_PORT] = user_input.get(
                    CONF_GOODWE_PORT, DEFAULT_GOODWE_PORT_UDP
                )
                new_data[CONF_GOODWE_PROTOCOL] = user_input.get(
                    CONF_GOODWE_PROTOCOL, "udp"
                )

                # Optimization provider settings
                optimization_provider = user_input.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
                new_data[CONF_OPTIMIZATION_PROVIDER] = optimization_provider
                if optimization_provider == OPT_PROVIDER_POWERSYNC:
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = COST_FUNCTION_COST
                    new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = user_input.get(
                        CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
                    ) / 100.0

                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to provider-specific step
                if self._provider == "amber":
                    return await self.async_step_amber_options()
                elif self._provider == "flow_power":
                    return await self.async_step_flow_power_options()
                elif self._provider in ("globird", "aemo_vpp"):
                    return await self.async_step_globird_options()
                elif self._provider == "octopus":
                    return await self.async_step_octopus_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_host = self._get_option(CONF_GOODWE_HOST, "")
        current_port = self._get_option(CONF_GOODWE_PORT, DEFAULT_GOODWE_PORT_UDP)
        current_protocol = self._get_option(CONF_GOODWE_PROTOCOL, "udp")
        current_opt_provider = self.config_entry.data.get(CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE)
        current_backup_reserve = self.config_entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, DEFAULT_OPTIMIZATION_BACKUP_RESERVE)

        opt_providers = {
            OPT_PROVIDER_NATIVE: "GoodWe built-in optimization",
            OPT_PROVIDER_POWERSYNC: "Smart Optimization (Built-in LP)",
        }

        return self.async_show_form(
            step_id="init_goodwe",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
                    vol.Required(
                        CONF_OPTIMIZATION_PROVIDER,
                        default=current_opt_provider,
                    ): vol.In(opt_providers),
                    vol.Optional(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        default=int(current_backup_reserve * 100) if current_backup_reserve < 1 else int(current_backup_reserve),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_GOODWE_HOST,
                        default=current_host,
                    ): str,
                    vol.Required(
                        CONF_GOODWE_PROTOCOL,
                        default=current_protocol,
                    ): vol.In({
                        "udp": "UDP (WiFi dongle, port 8899)",
                        "tcp": "TCP (LAN dongle, port 502)",
                    }),
                    vol.Optional(
                        CONF_GOODWE_PORT,
                        default=current_port,
                    ): int,
                }
            ),
            errors=errors,
        )

    async def async_step_teslemetry_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step to enter Teslemetry API token."""
        errors = {}

        if user_input is not None:
            token = user_input.get(CONF_TESLEMETRY_API_TOKEN, "").strip()

            if not token:
                errors["base"] = "no_token_provided"
            else:
                # Validate token by testing API
                session = async_get_clientsession(self.hass)
                headers = {"Authorization": f"Bearer {token}"}

                try:
                    async with session.get(
                        f"{TESLEMETRY_API_BASE_URL}/api/1/products",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as response:
                        if response.status == 200:
                            # Token is valid, update config entry data
                            new_data = dict(self.config_entry.data)
                            new_data[CONF_TESLA_API_PROVIDER] = TESLA_PROVIDER_TESLEMETRY
                            new_data[CONF_TESLEMETRY_API_TOKEN] = token
                            self.hass.config_entries.async_update_entry(
                                self.config_entry, data=new_data
                            )

                            # Route to provider-specific step
                            if self._provider == "amber":
                                return await self.async_step_amber_options()
                            elif self._provider == "flow_power":
                                return await self.async_step_flow_power_options()
                            elif self._provider in ("globird", "aemo_vpp"):
                                return await self.async_step_globird_options()
                        else:
                            errors["base"] = "invalid_auth"
                except Exception:
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="teslemetry_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TESLEMETRY_API_TOKEN): str,
                }
            ),
            errors=errors,
        )

    async def async_step_amber_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2a: Amber Electric specific options."""
        # Check if Tesla is selected (force mode toggle only applies to Tesla)
        battery_system = self.config_entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)
        is_tesla = battery_system == BATTERY_SYSTEM_TESLA

        if user_input is not None:
            # Store amber options temporarily
            self._amber_options = user_input
            self._amber_options[CONF_ELECTRICITY_PROVIDER] = "amber"

            # Force tariff mode toggle only applies to Tesla - set to False for Sigenergy
            if not is_tesla:
                self._amber_options[CONF_FORCE_TARIFF_MODE_TOGGLE] = False

            # Route to demand charge options page
            return await self.async_step_demand_charge_options()

        # Build schema dict - conditionally include force mode toggle for Tesla only
        schema_dict = {
            vol.Optional(
                CONF_AUTO_SYNC_ENABLED,
                default=self._get_option(CONF_AUTO_SYNC_ENABLED, True),
            ): bool,
            vol.Optional(
                CONF_AMBER_FORECAST_TYPE,
                default=self._get_option(CONF_AMBER_FORECAST_TYPE, "predicted"),
            ): vol.In({
                "predicted": "Predicted (Default)",
                "low": "Low (Aggressive)",
                "high": "High (Conservative)"
            }),
            vol.Optional(
                CONF_SPIKE_PROTECTION_ENABLED,
                default=self._get_option(CONF_SPIKE_PROTECTION_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_SETTLED_PRICES_ONLY,
                default=self._get_option(CONF_SETTLED_PRICES_ONLY, False),
            ): bool,
            vol.Optional(
                CONF_FORECAST_DISCREPANCY_ALERT,
                default=self._get_option(CONF_FORECAST_DISCREPANCY_ALERT, False),
            ): bool,
            vol.Optional(
                CONF_FORECAST_DISCREPANCY_THRESHOLD,
                default=self._get_option(CONF_FORECAST_DISCREPANCY_THRESHOLD, DEFAULT_FORECAST_DISCREPANCY_THRESHOLD),
            ): vol.Coerce(float),
        }

        # Only show force mode toggle for Tesla (it's a Tesla-specific feature)
        if is_tesla:
            schema_dict[vol.Optional(
                CONF_FORCE_TARIFF_MODE_TOGGLE,
                default=self._get_option(CONF_FORCE_TARIFF_MODE_TOGGLE, False),
            )] = bool

        schema_dict.update({
            vol.Optional(
                CONF_EXPORT_BOOST_ENABLED,
                default=self._get_option(CONF_EXPORT_BOOST_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_EXPORT_PRICE_OFFSET,
                default=self._get_option(CONF_EXPORT_PRICE_OFFSET, 0.0),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional(
                CONF_EXPORT_MIN_PRICE,
                default=self._get_option(CONF_EXPORT_MIN_PRICE, 0.0),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Optional(
                CONF_EXPORT_BOOST_START,
                default=self._get_option(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START),
            ): str,
            vol.Optional(
                CONF_EXPORT_BOOST_END,
                default=self._get_option(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END),
            ): str,
            vol.Optional(
                CONF_EXPORT_BOOST_THRESHOLD,
                default=self._get_option(CONF_EXPORT_BOOST_THRESHOLD, DEFAULT_EXPORT_BOOST_THRESHOLD),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            # Chip Mode (inverse of export boost)
            vol.Optional(
                CONF_CHIP_MODE_ENABLED,
                default=self._get_option(CONF_CHIP_MODE_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_CHIP_MODE_START,
                default=self._get_option(CONF_CHIP_MODE_START, DEFAULT_CHIP_MODE_START),
            ): str,
            vol.Optional(
                CONF_CHIP_MODE_END,
                default=self._get_option(CONF_CHIP_MODE_END, DEFAULT_CHIP_MODE_END),
            ): str,
            vol.Optional(
                CONF_CHIP_MODE_THRESHOLD,
                default=self._get_option(CONF_CHIP_MODE_THRESHOLD, DEFAULT_CHIP_MODE_THRESHOLD),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=200.0)),
        })

        return self.async_show_form(
            step_id="amber_options",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_demand_charge_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dedicated step for Network Demand Charge configuration."""
        if user_input is not None:
            # Store demand charge options
            self._demand_options = user_input
            # Route to curtailment options
            return await self.async_step_curtailment_options()

        return self.async_show_form(
            step_id="demand_charge_options",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_DEMAND_CHARGE_ENABLED,
                    default=self._get_option(CONF_DEMAND_CHARGE_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_DEMAND_CHARGE_RATE,
                    default=self._get_option(CONF_DEMAND_CHARGE_RATE, 10.0),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
                vol.Optional(
                    CONF_DEMAND_CHARGE_START_TIME,
                    default=self._get_option(CONF_DEMAND_CHARGE_START_TIME, "14:00"),
                ): str,
                vol.Optional(
                    CONF_DEMAND_CHARGE_END_TIME,
                    default=self._get_option(CONF_DEMAND_CHARGE_END_TIME, "20:00"),
                ): str,
                vol.Optional(
                    CONF_DEMAND_CHARGE_DAYS,
                    default=self._get_option(CONF_DEMAND_CHARGE_DAYS, "All Days"),
                ): vol.In(["All Days", "Weekdays Only", "Weekends Only"]),
                vol.Optional(
                    CONF_DEMAND_CHARGE_BILLING_DAY,
                    default=self._get_option(CONF_DEMAND_CHARGE_BILLING_DAY, 1),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
                vol.Optional(
                    CONF_DEMAND_CHARGE_APPLY_TO,
                    default=self._get_option(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only"),
                ): vol.In(["Buy Only", "Sell Only", "Both"]),
                vol.Optional(
                    CONF_DEMAND_ARTIFICIAL_PRICE,
                    default=self._get_option(CONF_DEMAND_ARTIFICIAL_PRICE, False),
                ): bool,
                vol.Optional(
                    CONF_DEMAND_ALLOW_GRID_CHARGING,
                    default=self._get_option(CONF_DEMAND_ALLOW_GRID_CHARGING, False),
                ): bool,
                vol.Optional(
                    CONF_DAILY_SUPPLY_CHARGE,
                    default=self._get_option(CONF_DAILY_SUPPLY_CHARGE, 0.0),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_MONTHLY_SUPPLY_CHARGE,
                    default=self._get_option(CONF_MONTHLY_SUPPLY_CHARGE, 0.0),
                ): vol.Coerce(float),
            }),
        )

    async def async_step_curtailment_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dedicated step for Solar Curtailment configuration."""
        battery_system = self.config_entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)
        is_sigenergy = battery_system == BATTERY_SYSTEM_SIGENERGY
        is_sungrow = battery_system == BATTERY_SYSTEM_SUNGROW

        if user_input is not None:
            # Check if solar curtailment is being disabled
            was_curtailment_enabled = self._get_option(CONF_BATTERY_CURTAILMENT_ENABLED, False)
            new_curtailment_enabled = user_input.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)

            if was_curtailment_enabled and not new_curtailment_enabled:
                await self._restore_export_rule()

            # Store curtailment settings (no weather options here)
            self._curtailment_options = {
                CONF_BATTERY_CURTAILMENT_ENABLED: new_curtailment_enabled,
            }

            if is_sigenergy:
                # Sigenergy DC curtailment - save DC settings to config entry data
                dc_enabled = user_input.get(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False)
                new_data = dict(self.config_entry.data)
                new_data[CONF_SIGENERGY_DC_CURTAILMENT_ENABLED] = dc_enabled
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                # Route to weather options
                return await self.async_step_weather_options()
            elif is_sungrow:
                # Sungrow doesn't have separate curtailment config - go straight to weather
                return await self.async_step_weather_options()
            else:
                # Tesla - check if AC inverter curtailment needs configuration
                ac_enabled = user_input.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
                self._curtailment_options[CONF_AC_INVERTER_CURTAILMENT_ENABLED] = ac_enabled

                if ac_enabled:
                    # Route to AC inverter brand selection
                    return await self.async_step_inverter_brand()

                # No AC inverter - route to weather options
                return await self.async_step_weather_options()

        # Build schema based on battery system
        schema_dict: dict[vol.Marker, Any] = {
            vol.Optional(
                CONF_BATTERY_CURTAILMENT_ENABLED,
                default=self._get_option(CONF_BATTERY_CURTAILMENT_ENABLED, False),
            ): bool,
        }

        if is_sigenergy:
            # Sigenergy DC curtailment option
            schema_dict[vol.Optional(
                CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
                default=self.config_entry.data.get(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False),
            )] = bool
        elif is_sungrow:
            # Sungrow doesn't need additional curtailment options - battery controls built-in
            pass
        else:
            # Tesla AC inverter curtailment option
            schema_dict[vol.Optional(
                CONF_AC_INVERTER_CURTAILMENT_ENABLED,
                default=self._get_option(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False),
            )] = bool

        return self.async_show_form(
            step_id="curtailment_options",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_weather_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Weather and solar forecast configuration in options flow."""
        if user_input is not None:
            # Store weather and Solcast settings
            weather_options = {
                CONF_WEATHER_LOCATION: user_input.get(CONF_WEATHER_LOCATION, ""),
                CONF_OPENWEATHERMAP_API_KEY: user_input.get(CONF_OPENWEATHERMAP_API_KEY, ""),
                CONF_SOLCAST_ENABLED: user_input.get(CONF_SOLCAST_ENABLED, False),
                CONF_SOLCAST_API_KEY: user_input.get(CONF_SOLCAST_API_KEY, ""),
                CONF_SOLCAST_RESOURCE_ID: user_input.get(CONF_SOLCAST_RESOURCE_ID, ""),
            }

            # Combine with previous options - check if came from inverter_config or curtailment_options
            if hasattr(self, '_inverter_options') and self._inverter_options:
                # Came from inverter_config - _inverter_options already has everything except weather
                final_data = {**self._inverter_options, **getattr(self, '_demand_options', {}), **weather_options}
            else:
                # Came directly from curtailment_options
                final_data = {**getattr(self, '_amber_options', {}), **getattr(self, '_demand_options', {}), **getattr(self, '_curtailment_options', {}), **weather_options}

            self._final_options = final_data
            return await self.async_step_ev_charging()

        return self.async_show_form(
            step_id="weather_options",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_WEATHER_LOCATION,
                    default=self._get_option(CONF_WEATHER_LOCATION, ""),
                ): str,
                vol.Optional(
                    CONF_OPENWEATHERMAP_API_KEY,
                    default=self._get_option(CONF_OPENWEATHERMAP_API_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_SOLCAST_ENABLED,
                    default=self._get_option(CONF_SOLCAST_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_SOLCAST_API_KEY,
                    default=self._get_option(CONF_SOLCAST_API_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_SOLCAST_RESOURCE_ID,
                    default=self._get_option(CONF_SOLCAST_RESOURCE_ID, ""),
                ): str,
            }),
        )

    async def async_step_inverter_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step for selecting inverter brand for AC-coupled curtailment."""
        if user_input is not None:
            # Store selected brand and proceed to brand-specific config
            self._inverter_brand = user_input.get(CONF_INVERTER_BRAND, "sungrow")
            return await self.async_step_inverter_config()

        # Get current brand from existing config
        current_brand = self._get_option(CONF_INVERTER_BRAND, "sungrow")

        return self.async_show_form(
            step_id="inverter_brand",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INVERTER_BRAND,
                        default=current_brand,
                    ): vol.In(INVERTER_BRANDS),
                }
            ),
        )

    async def async_step_inverter_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step for configuring inverter-specific settings."""
        errors = {}

        if user_input is not None:
            # Get brand and configuration values
            inverter_brand = getattr(self, '_inverter_brand', None) or self._get_option(CONF_INVERTER_BRAND, "sungrow")
            inverter_host = user_input.get(CONF_INVERTER_HOST, "")
            inverter_port = user_input.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)
            inverter_slave_id = user_input.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID)

            # Validate: if battery is Sungrow and AC inverter is also Sungrow,
            # check for IP/port/slave_id conflicts
            battery_system = self.config_entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)
            if battery_system == BATTERY_SYSTEM_SUNGROW and inverter_brand == "sungrow":
                sungrow_host = self.config_entry.data.get(CONF_SUNGROW_HOST, "")
                sungrow_port = self.config_entry.data.get(CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT)
                sungrow_slave_id = self.config_entry.data.get(CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID)

                # Same host, port, AND slave ID = conflict
                if (
                    inverter_host == sungrow_host
                    and inverter_port == sungrow_port
                    and inverter_slave_id == sungrow_slave_id
                ):
                    errors["base"] = "sungrow_modbus_conflict"

            if not errors:
                # Combine amber options, curtailment options, and inverter config
                final_data = {**getattr(self, '_amber_options', {})}
                final_data.update(getattr(self, '_curtailment_options', {}))
                final_data[CONF_INVERTER_BRAND] = inverter_brand
                final_data[CONF_INVERTER_MODEL] = user_input.get(CONF_INVERTER_MODEL)
                final_data[CONF_INVERTER_HOST] = inverter_host
                final_data[CONF_INVERTER_PORT] = inverter_port

                # Only include slave ID for Modbus brands (not Enphase/Zeversolar which use HTTP)
                if inverter_brand not in ("enphase", "zeversolar"):
                    final_data[CONF_INVERTER_SLAVE_ID] = inverter_slave_id
                else:
                    final_data[CONF_INVERTER_SLAVE_ID] = 1  # Default for HTTP-based inverters

                # Include JWT token, Enlighten credentials, and grid profiles for Enphase (firmware 7.x+)
                if inverter_brand == "enphase":
                    final_data[CONF_INVERTER_TOKEN] = user_input.get(CONF_INVERTER_TOKEN, "")
                    final_data[CONF_ENPHASE_USERNAME] = user_input.get(CONF_ENPHASE_USERNAME, "")
                    final_data[CONF_ENPHASE_PASSWORD] = user_input.get(CONF_ENPHASE_PASSWORD, "")
                    final_data[CONF_ENPHASE_SERIAL] = user_input.get(CONF_ENPHASE_SERIAL, "")
                    # Grid profile names for profile switching fallback
                    final_data[CONF_ENPHASE_NORMAL_PROFILE] = user_input.get(CONF_ENPHASE_NORMAL_PROFILE, "")
                    final_data[CONF_ENPHASE_ZERO_EXPORT_PROFILE] = user_input.get(CONF_ENPHASE_ZERO_EXPORT_PROFILE, "")
                    # Installer mode for grid profile access
                    final_data[CONF_ENPHASE_IS_INSTALLER] = user_input.get(CONF_ENPHASE_IS_INSTALLER, False)

                # Fronius-specific: load following mode (for users without 0W export profile)
                if inverter_brand == "fronius":
                    final_data[CONF_FRONIUS_LOAD_FOLLOWING] = user_input.get(
                        CONF_FRONIUS_LOAD_FOLLOWING, False
                    )

                # Restore SOC threshold for AC inverter curtailment
                final_data[CONF_INVERTER_RESTORE_SOC] = user_input.get(
                    CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC
                )

                # Store inverter config and route to weather options
                self._inverter_options = final_data
                return await self.async_step_weather_options()

        # Get brand-specific models and defaults
        # Fall back to existing config if _inverter_brand not set (options flow)
        brand = getattr(self, '_inverter_brand', None) or self._get_option(CONF_INVERTER_BRAND, "sungrow")
        # Pass battery system to filter out conflicting models (e.g., SH-series when battery is Sungrow)
        battery_system = self.config_entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)
        models = get_models_for_brand(brand, battery_system)
        defaults = get_brand_defaults(brand)

        # Get current values from existing config (for editing)
        current_model = self._get_option(CONF_INVERTER_MODEL)
        # If current model doesn't belong to selected brand, use first model from brand
        if current_model not in models:
            current_model = next(iter(models.keys())) if models else ""

        current_host = self._get_option(CONF_INVERTER_HOST, "")
        current_port = self._get_option(CONF_INVERTER_PORT, defaults["port"])
        current_slave_id = self._get_option(CONF_INVERTER_SLAVE_ID, defaults["slave_id"])

        # Build brand-specific schema
        schema_dict: dict[vol.Marker, Any] = {
            vol.Required(
                CONF_INVERTER_MODEL,
                default=current_model,
            ): vol.In(models),
            vol.Required(
                CONF_INVERTER_HOST,
                default=current_host,
            ): str,
            vol.Required(
                CONF_INVERTER_PORT,
                default=current_port,
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }

        # Only show Slave ID for Modbus brands (not Enphase/Zeversolar which use HTTP)
        if brand not in ("enphase", "zeversolar"):
            schema_dict[vol.Required(
                CONF_INVERTER_SLAVE_ID,
                default=current_slave_id,
            )] = vol.All(vol.Coerce(int), vol.Range(min=1, max=247))

        # Show JWT token and Enlighten credentials for Enphase (firmware 7.x+)
        if brand == "enphase":
            current_token = self._get_option(CONF_INVERTER_TOKEN, "")
            schema_dict[vol.Optional(
                CONF_INVERTER_TOKEN,
                default=current_token,
            )] = str

            # Enlighten credentials for automatic JWT token refresh (recommended)
            current_enphase_username = self._get_option(CONF_ENPHASE_USERNAME, "")
            schema_dict[vol.Optional(
                CONF_ENPHASE_USERNAME,
                default=current_enphase_username,
            )] = str

            current_enphase_password = self._get_option(CONF_ENPHASE_PASSWORD, "")
            schema_dict[vol.Optional(
                CONF_ENPHASE_PASSWORD,
                default=current_enphase_password,
            )] = str

            current_enphase_serial = self._get_option(CONF_ENPHASE_SERIAL, "")
            schema_dict[vol.Optional(
                CONF_ENPHASE_SERIAL,
                default=current_enphase_serial,
            )] = str

            # Grid profile names for profile switching fallback (when DPEL/DER unavailable)
            current_normal_profile = self._get_option(CONF_ENPHASE_NORMAL_PROFILE, "")
            schema_dict[vol.Optional(
                CONF_ENPHASE_NORMAL_PROFILE,
                default=current_normal_profile,
            )] = str

            current_zero_export_profile = self._get_option(CONF_ENPHASE_ZERO_EXPORT_PROFILE, "")
            schema_dict[vol.Optional(
                CONF_ENPHASE_ZERO_EXPORT_PROFILE,
                default=current_zero_export_profile,
            )] = str

            # Installer mode for grid profile access
            current_is_installer = self._get_option(CONF_ENPHASE_IS_INSTALLER, False)
            schema_dict[vol.Optional(
                CONF_ENPHASE_IS_INSTALLER,
                default=current_is_installer,
            )] = bool

        # Fronius-specific: load following mode (for users without 0W export profile)
        if brand == "fronius":
            current_load_following = self._get_option(CONF_FRONIUS_LOAD_FOLLOWING, False)
            schema_dict[vol.Optional(
                CONF_FRONIUS_LOAD_FOLLOWING,
                default=current_load_following,
                description={"suggested_value": current_load_following},
            )] = bool

        # Restore SOC threshold - restore inverter when battery drops below this %
        current_restore_soc = self._get_option(CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC)
        schema_dict[vol.Optional(
            CONF_INVERTER_RESTORE_SOC,
            default=current_restore_soc,
        )] = vol.All(vol.Coerce(int), vol.Range(min=50, max=100))

        return self.async_show_form(
            step_id="inverter_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "brand": INVERTER_BRANDS.get(brand, brand),
            },
        )

    async def async_step_ev_charging(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step for EV Charging and OCPP configuration."""
        if user_input is not None:
            # Store EV data for potential zaptec_cloud step
            self._ev_options_data = dict(user_input)

            # If Zaptec standalone is being newly enabled, go to credentials step
            was_standalone = self._get_option(CONF_ZAPTEC_STANDALONE_ENABLED, False)
            now_standalone = user_input.get(CONF_ZAPTEC_STANDALONE_ENABLED, False)
            has_credentials = bool(self._get_option(CONF_ZAPTEC_USERNAME, ""))

            if now_standalone and (not was_standalone or not has_credentials):
                return await self.async_step_zaptec_cloud_options()

            return self._save_ev_options(user_input)

        # Build schema for EV and OCPP options
        current_ev_enabled = self._get_option(CONF_EV_CHARGING_ENABLED, False)
        current_ev_provider = self._get_option(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API)

        schema_dict: dict[vol.Marker, Any] = {
            # EV Charging settings
            vol.Optional(
                CONF_EV_CHARGING_ENABLED,
                default=current_ev_enabled,
            ): bool,
            vol.Optional(
                CONF_EV_PROVIDER,
                default=current_ev_provider,
            ): vol.In(EV_PROVIDERS),
            vol.Optional(
                CONF_TESLA_BLE_ENTITY_PREFIX,
                default=self._get_option(
                    CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX
                ),
            ): str,
            # OCPP settings
            vol.Optional(
                CONF_OCPP_ENABLED,
                default=self._get_option(CONF_OCPP_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_OCPP_PORT,
                default=self._get_option(CONF_OCPP_PORT, DEFAULT_OCPP_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            # Zaptec settings
            vol.Optional(
                CONF_ZAPTEC_CHARGER_ENTITY,
                default=self._get_option(CONF_ZAPTEC_CHARGER_ENTITY, ""),
            ): str,
            vol.Optional(
                CONF_ZAPTEC_INSTALLATION_ID,
                default=self._get_option(CONF_ZAPTEC_INSTALLATION_ID, ""),
            ): str,
            # Zaptec standalone (direct API)
            vol.Optional(
                CONF_ZAPTEC_STANDALONE_ENABLED,
                default=self._get_option(CONF_ZAPTEC_STANDALONE_ENABLED, False),
            ): bool,
        }

        return self.async_show_form(
            step_id="ev_charging",
            data_schema=vol.Schema(schema_dict),
        )

    def _save_ev_options(self, ev_input: dict[str, Any]) -> FlowResult:
        """Save EV charging options and create entry."""
        # Start with existing options to preserve any settings not in this flow
        final_data = dict(self.config_entry.options)

        # Update with options collected from earlier steps in this flow
        flow_options = getattr(self, '_final_options', {})
        final_data.update(flow_options)

        # Add EV settings
        final_data[CONF_EV_CHARGING_ENABLED] = ev_input.get(
            CONF_EV_CHARGING_ENABLED, False
        )
        final_data[CONF_EV_PROVIDER] = ev_input.get(
            CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API
        )
        final_data[CONF_TESLA_BLE_ENTITY_PREFIX] = ev_input.get(
            CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX
        )

        # Add OCPP settings
        final_data[CONF_OCPP_ENABLED] = ev_input.get(CONF_OCPP_ENABLED, False)
        final_data[CONF_OCPP_PORT] = ev_input.get(
            CONF_OCPP_PORT, DEFAULT_OCPP_PORT
        )

        # Add Zaptec settings
        zaptec_entity = ev_input.get(CONF_ZAPTEC_CHARGER_ENTITY, "")
        if zaptec_entity:
            final_data[CONF_ZAPTEC_CHARGER_ENTITY] = zaptec_entity
        final_data[CONF_ZAPTEC_INSTALLATION_ID] = ev_input.get(
            CONF_ZAPTEC_INSTALLATION_ID, ""
        )

        # Add Zaptec standalone settings
        final_data[CONF_ZAPTEC_STANDALONE_ENABLED] = ev_input.get(
            CONF_ZAPTEC_STANDALONE_ENABLED, False
        )
        for key in (CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_PASSWORD,
                     CONF_ZAPTEC_CHARGER_ID, CONF_ZAPTEC_INSTALLATION_ID_CLOUD):
            if key in ev_input:
                final_data[key] = ev_input[key]

        return self.async_create_entry(title="", data=final_data)

    async def async_step_zaptec_cloud_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Zaptec Cloud API credentials in options flow."""
        errors = {}

        if user_input is not None:
            username = user_input.get(CONF_ZAPTEC_USERNAME, "")
            password = user_input.get(CONF_ZAPTEC_PASSWORD, "")

            if username and password:
                from .zaptec_api import ZaptecCloudClient
                client = ZaptecCloudClient(username, password)
                try:
                    success, message = await client.test_connection()
                    if success:
                        chargers = await client.get_chargers()
                        installations = await client.get_installations()

                        ev_data = getattr(self, '_ev_options_data', {})
                        ev_data[CONF_ZAPTEC_USERNAME] = username
                        ev_data[CONF_ZAPTEC_PASSWORD] = password

                        if chargers:
                            ev_data[CONF_ZAPTEC_CHARGER_ID] = chargers[0].get("Id", "")
                        if installations:
                            ev_data[CONF_ZAPTEC_INSTALLATION_ID_CLOUD] = installations[0].get("Id", "")

                        return self._save_ev_options(ev_data)
                    else:
                        errors["base"] = "zaptec_auth_failed"
                        _LOGGER.error("Zaptec Cloud auth failed: %s", message)
                except Exception as e:
                    errors["base"] = "zaptec_auth_failed"
                    _LOGGER.error("Zaptec Cloud connection error: %s", e)
                finally:
                    await client.close()
            else:
                errors["base"] = "zaptec_missing_credentials"

        return self.async_show_form(
            step_id="zaptec_cloud_options",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ZAPTEC_USERNAME,
                    default=self._get_option(CONF_ZAPTEC_USERNAME, ""),
                ): str,
                vol.Required(CONF_ZAPTEC_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_flow_power_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b: Flow Power main settings (region, tariff, base rate, PEA, sync)."""
        if user_input is not None:
            # Parse combined tariff selection (format: "distributor:code")
            combined = user_input.get(CONF_NETWORK_TARIFF_COMBINED)
            if combined and ":" in combined:
                distributor, tariff_code = combined.split(":", 1)
                user_input[CONF_NETWORK_DISTRIBUTOR] = distributor
                user_input[CONF_NETWORK_TARIFF_CODE] = tariff_code
            # Remove combined key before storing
            user_input.pop(CONF_NETWORK_TARIFF_COMBINED, None)

            # Store main options temporarily
            self._flow_power_main_options = user_input

            # If switching to Amber and no token exists, collect it first
            price_source = user_input.get(CONF_FLOW_POWER_PRICE_SOURCE, "aemo")
            if price_source == "amber" and not self.config_entry.data.get(CONF_AMBER_API_TOKEN):
                return await self.async_step_flow_power_amber_token()

            return await self.async_step_flow_power_network_options()

        # Build current combined tariff value from stored options
        current_distributor = self._get_option(CONF_NETWORK_DISTRIBUTOR, "energex")
        current_tariff_code = self._get_option(CONF_NETWORK_TARIFF_CODE, "6900")
        current_combined = f"{current_distributor}:{current_tariff_code}"
        # Validate it exists in options, otherwise use default
        if current_combined not in ALL_NETWORK_TARIFFS:
            current_combined = "energex:6900"

        return self.async_show_form(
            step_id="flow_power_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FLOW_POWER_STATE,
                        default=self._get_option(CONF_FLOW_POWER_STATE, "NSW1"),
                    ): vol.In(FLOW_POWER_STATES),
                    vol.Required(
                        CONF_FLOW_POWER_PRICE_SOURCE,
                        default=self._get_option(CONF_FLOW_POWER_PRICE_SOURCE, "aemo"),
                    ): vol.In(FLOW_POWER_PRICE_SOURCES),
                    vol.Required(
                        CONF_NETWORK_TARIFF_COMBINED,
                        default=current_combined,
                    ): vol.In(ALL_NETWORK_TARIFFS),
                    vol.Required(
                        CONF_FLOW_POWER_BASE_RATE,
                        default=self._get_option(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
                    vol.Optional(
                        CONF_PEA_ENABLED,
                        default=self._get_option(CONF_PEA_ENABLED, True),
                    ): bool,
                    vol.Optional(
                        CONF_AUTO_SYNC_ENABLED,
                        default=self._get_option(CONF_AUTO_SYNC_ENABLED, True),
                    ): bool,
                }
            ),
        )

    async def async_step_flow_power_amber_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect Amber API token when Flow Power user switches to Amber pricing."""
        errors: dict[str, str] = {}

        if user_input is not None:
            validation_result = await validate_amber_token(
                self.hass, user_input[CONF_AMBER_API_TOKEN]
            )

            if validation_result["success"]:
                # Store token in config entry data
                new_data = {**self.config_entry.data}
                new_data[CONF_AMBER_API_TOKEN] = user_input[CONF_AMBER_API_TOKEN]
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return await self.async_step_flow_power_network_options()
            else:
                errors["base"] = validation_result.get("error", "unknown")

        return self.async_show_form(
            step_id="flow_power_amber_token",
            data_schema=vol.Schema({
                vol.Required(CONF_AMBER_API_TOKEN): str,
            }),
            errors=errors,
            description_placeholders={
                "amber_url": "https://app.amber.com.au/developers",
            },
        )

    async def async_step_flow_power_network_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b-2: Flow Power network & advanced settings."""
        if user_input is not None:
            # Merge main options from previous step with network/advanced options
            merged = {**self._flow_power_main_options, **user_input}
            self._flow_power_main_options = {}

            # Store flow power options and route to demand charge page
            merged[CONF_ELECTRICITY_PROVIDER] = "flow_power"
            self._amber_options = merged
            return await self.async_step_demand_charge_options()

        return self.async_show_form(
            step_id="flow_power_network_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PEA_CUSTOM_VALUE,
                        default=self._get_option(CONF_PEA_CUSTOM_VALUE, None),
                    ): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=-50.0, max=50.0))),
                    vol.Optional(
                        CONF_NETWORK_USE_MANUAL_RATES,
                        default=self._get_option(CONF_NETWORK_USE_MANUAL_RATES, False),
                    ): bool,
                    vol.Optional(
                        CONF_NETWORK_TARIFF_TYPE,
                        default=self._get_option(CONF_NETWORK_TARIFF_TYPE, "flat"),
                    ): vol.In(NETWORK_TARIFF_TYPES),
                    vol.Optional(
                        CONF_NETWORK_FLAT_RATE,
                        default=self._get_option(CONF_NETWORK_FLAT_RATE, 8.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
                    vol.Optional(
                        CONF_NETWORK_PEAK_RATE,
                        default=self._get_option(CONF_NETWORK_PEAK_RATE, 15.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
                    vol.Optional(
                        CONF_NETWORK_SHOULDER_RATE,
                        default=self._get_option(CONF_NETWORK_SHOULDER_RATE, 5.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
                    vol.Optional(
                        CONF_NETWORK_OFFPEAK_RATE,
                        default=self._get_option(CONF_NETWORK_OFFPEAK_RATE, 2.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
                    vol.Optional(
                        CONF_NETWORK_PEAK_START,
                        default=self._get_option(CONF_NETWORK_PEAK_START, "16:00"),
                    ): vol.In({f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}),
                    vol.Optional(
                        CONF_NETWORK_PEAK_END,
                        default=self._get_option(CONF_NETWORK_PEAK_END, "21:00"),
                    ): vol.In({f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}),
                    vol.Optional(
                        CONF_NETWORK_OFFPEAK_START,
                        default=self._get_option(CONF_NETWORK_OFFPEAK_START, "10:00"),
                    ): vol.In({f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}),
                    vol.Optional(
                        CONF_NETWORK_OFFPEAK_END,
                        default=self._get_option(CONF_NETWORK_OFFPEAK_END, "15:00"),
                    ): vol.In({f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}),
                    vol.Optional(
                        CONF_NETWORK_OTHER_FEES,
                        default=self._get_option(CONF_NETWORK_OTHER_FEES, 1.5),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=20.0)),
                    vol.Optional(
                        CONF_NETWORK_INCLUDE_GST,
                        default=self._get_option(CONF_NETWORK_INCLUDE_GST, True),
                    ): bool,
                }
            ),
        )

    async def async_step_globird_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2c: Globird specific options."""
        if user_input is not None:
            # Add provider to the data
            user_input[CONF_ELECTRICITY_PROVIDER] = "globird"

            # If spike not enabled, ensure region/threshold don't cause issues
            if not user_input.get(CONF_AEMO_SPIKE_ENABLED, False):
                user_input.pop(CONF_AEMO_REGION, None)
                user_input.pop(CONF_AEMO_SPIKE_THRESHOLD, None)

            # Check if user wants to configure custom tariff
            configure_tariff = user_input.pop("configure_custom_tariff", False)

            # Store options and route accordingly
            self._amber_options = user_input

            if configure_tariff:
                return await self.async_step_custom_tariff_options()

            return await self.async_step_demand_charge_options()

        # Build region choices for AEMO
        region_choices = {"": "Select Region..."}
        region_choices.update(AEMO_REGIONS)

        return self.async_show_form(
            step_id="globird_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_AEMO_SPIKE_ENABLED,
                        default=self._get_option(CONF_AEMO_SPIKE_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        CONF_AEMO_REGION,
                        default=self._get_option(CONF_AEMO_REGION, ""),
                    ): vol.In(region_choices),
                    vol.Optional(
                        CONF_AEMO_SPIKE_THRESHOLD,
                        default=self._get_option(CONF_AEMO_SPIKE_THRESHOLD, 3000.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=20000.0)),
                    vol.Optional(
                        "configure_custom_tariff",
                        default=False,
                    ): bool,
                }
            ),
            description_placeholders={
                "tariff_hint": "Enable 'Configure Custom Tariff' to set your TOU rates for EV charging",
            },
        )

    async def async_step_octopus_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2d: Octopus Energy UK specific options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Build tariff code from product + region
            product_key = user_input.get(CONF_OCTOPUS_PRODUCT, "agile")
            region = user_input.get(CONF_OCTOPUS_REGION, "C")

            # Map product selection to actual product codes
            product_code = OCTOPUS_PRODUCT_CODES.get(product_key, OCTOPUS_PRODUCT_CODES["agile"])

            # Validate by fetching current prices
            try:
                from .octopus_api import OctopusAPIClient

                client = OctopusAPIClient(async_get_clientsession(self.hass))

                # Dynamically discover current Tracker product code
                if product_key == "tracker":
                    try:
                        discovered = await client.discover_tracker_product()
                        if discovered:
                            product_code = discovered
                    except Exception:
                        pass  # Fall back to hardcoded

                tariff_code = f"E-1R-{product_code}-{region}"

                # Get export product/tariff codes if available
                export_product_code = OCTOPUS_EXPORT_PRODUCT_CODES.get(product_key)
                export_tariff_code = f"E-1R-{export_product_code}-{region}" if export_product_code else None

                rates = await client.get_current_rates(product_code, tariff_code, page_size=5)

                if not rates:
                    errors["base"] = "no_prices"
                    _LOGGER.error(
                        "No Octopus prices found for tariff %s in region %s",
                        tariff_code, region
                    )
            except Exception as err:
                errors["base"] = "cannot_connect"
                _LOGGER.exception("Error validating Octopus tariff: %s", err)

            if not errors:
                # Store Octopus options
                self._amber_options = {
                    CONF_ELECTRICITY_PROVIDER: "octopus",
                    CONF_OCTOPUS_PRODUCT: product_key,
                    CONF_OCTOPUS_REGION: region,
                    CONF_OCTOPUS_PRODUCT_CODE: product_code,
                    CONF_OCTOPUS_TARIFF_CODE: tariff_code,
                    CONF_OCTOPUS_EXPORT_PRODUCT_CODE: export_product_code,
                    CONF_OCTOPUS_EXPORT_TARIFF_CODE: export_tariff_code,
                    CONF_AUTO_SYNC_ENABLED: user_input.get(CONF_AUTO_SYNC_ENABLED, True),
                    CONF_BATTERY_CURTAILMENT_ENABLED: user_input.get(CONF_BATTERY_CURTAILMENT_ENABLED, False),
                }

                _LOGGER.info(
                    "Octopus options validated: product=%s, tariff=%s, region=%s",
                    product_code, tariff_code, region
                )

                # Continue to demand charge options
                return await self.async_step_demand_charge_options()

        # Get current values
        current_product = self._get_option(CONF_OCTOPUS_PRODUCT, "agile")
        current_region = self._get_option(CONF_OCTOPUS_REGION, "C")

        return self.async_show_form(
            step_id="octopus_options",
            data_schema=vol.Schema({
                vol.Required(CONF_OCTOPUS_PRODUCT, default=current_product): vol.In(OCTOPUS_PRODUCTS),
                vol.Required(CONF_OCTOPUS_REGION, default=current_region): vol.In(OCTOPUS_GSP_REGIONS),
                vol.Optional(
                    CONF_AUTO_SYNC_ENABLED,
                    default=self._get_option(CONF_AUTO_SYNC_ENABLED, True),
                ): bool,
                vol.Optional(
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    default=self._get_option(CONF_BATTERY_CURTAILMENT_ENABLED, False),
                ): bool,
            }),
            errors=errors,
            description_placeholders={
                "octopus_url": "https://octopus.energy/smart/agile/",
            },
        )

    async def async_step_custom_tariff_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure custom tariff rates for Globird/AEMO VPP users.

        This allows users to define their TOU tariff structure which is used
        for EV charging price decisions and Sigenergy Cloud tariff sync.
        """
        from .const import DOMAIN

        errors: dict[str, str] = {}

        if user_input is not None:
            # Build custom tariff from user input
            tariff_type = user_input.get("tariff_type", "tou")

            # Get rates (in cents, will be converted to $/kWh)
            peak_rate = user_input.get("peak_rate", 45) / 100
            shoulder_rate = user_input.get("shoulder_rate", 28) / 100
            offpeak_rate = user_input.get("offpeak_rate", 15) / 100
            super_offpeak_rate = user_input.get("super_offpeak_rate")
            if super_offpeak_rate is not None:
                super_offpeak_rate = super_offpeak_rate / 100
            fit_rate = user_input.get("fit_rate", 5) / 100

            # Parse time strings
            peak_start = user_input.get("peak_start", "15:00")
            peak_end = user_input.get("peak_end", "21:00")
            super_offpeak_start = user_input.get("super_offpeak_start")
            super_offpeak_end = user_input.get("super_offpeak_end")

            # Build TOU periods
            tou_periods = {}

            if tariff_type == "flat":
                tou_periods["ALL"] = [
                    {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}
                ]
                energy_charges = {"ALL": peak_rate}
            else:
                try:
                    peak_start_hour = int(peak_start.split(":")[0])
                    peak_end_hour = int(peak_end.split(":")[0])
                except (ValueError, IndexError):
                    peak_start_hour = 15
                    peak_end_hour = 21

                tou_periods["PEAK"] = [
                    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": peak_start_hour, "toHour": peak_end_hour}
                ]

                if super_offpeak_start and super_offpeak_end:
                    try:
                        sop_start = int(super_offpeak_start.split(":")[0])
                        sop_end = int(super_offpeak_end.split(":")[0])
                        tou_periods["SUPER_OFF_PEAK"] = [
                            {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": sop_start, "toHour": sop_end}
                        ]
                    except (ValueError, IndexError):
                        pass

                if peak_start_hour > 7:
                    tou_periods["SHOULDER"] = [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 7, "toHour": peak_start_hour}
                    ]

                tou_periods["OFF_PEAK"] = [
                    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": peak_end_hour, "toHour": 24},
                    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 7},
                    {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 0, "toHour": 24},
                    {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24},
                ]

                energy_charges = {
                    "PEAK": peak_rate,
                    "OFF_PEAK": offpeak_rate,
                }
                if "SHOULDER" in tou_periods:
                    energy_charges["SHOULDER"] = shoulder_rate
                if "SUPER_OFF_PEAK" in tou_periods and super_offpeak_rate is not None:
                    energy_charges["SUPER_OFF_PEAK"] = super_offpeak_rate

            # Build custom tariff
            custom_tariff = {
                "name": user_input.get("plan_name", "Custom TOU"),
                "utility": "Globird Energy",
                "seasons": {
                    "All Year": {
                        "fromMonth": 1,
                        "toMonth": 12,
                        "tou_periods": tou_periods,
                    }
                },
                "energy_charges": {
                    "All Year": energy_charges,
                },
                "sell_tariff": {
                    "energy_charges": {
                        "All Year": {
                            "ALL": fit_rate,
                        }
                    }
                },
            }

            # Save custom tariff to automation_store
            if DOMAIN in self.hass.data:
                for entry_id, entry_data in self.hass.data.get(DOMAIN, {}).items():
                    if isinstance(entry_data, dict) and "automation_store" in entry_data:
                        store = entry_data["automation_store"]
                        store.set_custom_tariff(custom_tariff)
                        await store.async_save()

                        # Also update tariff_schedule for immediate use
                        from . import convert_custom_tariff_to_schedule
                        tariff_schedule = convert_custom_tariff_to_schedule(custom_tariff)
                        entry_data["tariff_schedule"] = tariff_schedule
                        _LOGGER.info("Custom tariff saved via options flow")
                        break

            # Continue to demand charge options
            return await self.async_step_demand_charge_options()

        # Build hour options
        hour_options = {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}

        # Get current custom tariff if exists
        current_tariff = None
        if DOMAIN in self.hass.data:
            for entry_id, entry_data in self.hass.data.get(DOMAIN, {}).items():
                if isinstance(entry_data, dict) and "automation_store" in entry_data:
                    store = entry_data["automation_store"]
                    current_tariff = store.get_custom_tariff()
                    break

        # Set defaults from current tariff or use standard defaults
        default_peak = 45
        default_shoulder = 28
        default_offpeak = 15
        default_fit = 5
        default_peak_start = "15:00"
        default_peak_end = "21:00"

        if current_tariff:
            charges = current_tariff.get("energy_charges", {}).get("All Year", {})
            default_peak = int(charges.get("PEAK", 0.45) * 100)
            default_shoulder = int(charges.get("SHOULDER", 0.28) * 100)
            default_offpeak = int(charges.get("OFF_PEAK", 0.15) * 100)

            sell_charges = current_tariff.get("sell_tariff", {}).get("energy_charges", {}).get("All Year", {})
            default_fit = int(sell_charges.get("ALL", 0.05) * 100)

        return self.async_show_form(
            step_id="custom_tariff_options",
            data_schema=vol.Schema(
                {
                    vol.Optional("plan_name", default=""): str,
                    vol.Required("tariff_type", default="tou"): vol.In({
                        "flat": "Flat Rate (single rate all day)",
                        "tou": "Time of Use (peak/shoulder/off-peak)",
                    }),
                    vol.Required("peak_rate", default=default_peak): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Optional("shoulder_rate", default=default_shoulder): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Required("offpeak_rate", default=default_offpeak): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Optional("super_offpeak_rate"): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Required("fit_rate", default=default_fit): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=50)
                    ),
                    vol.Optional("peak_start", default=default_peak_start): vol.In(hour_options),
                    vol.Optional("peak_end", default=default_peak_end): vol.In(hour_options),
                    vol.Optional("super_offpeak_start"): vol.In(hour_options),
                    vol.Optional("super_offpeak_end"): vol.In(hour_options),
                }
            ),
            errors=errors,
            description_placeholders={
                "info": "Configure your electricity tariff rates. All rates are in cents/kWh.",
            },
        )
