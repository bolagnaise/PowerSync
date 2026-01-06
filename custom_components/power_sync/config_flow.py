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
    CONF_SOLAR_CURTAILMENT_ENABLED,
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
    BATTERY_SYSTEMS,
    # Sigenergy configuration
    CONF_SIGENERGY_USERNAME,
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
    # Alpha: Force tariff mode toggle
    CONF_FORCE_TARIFF_MODE_TOGGLE,
    # Inverter curtailment configuration
    CONF_INVERTER_CURTAILMENT_ENABLED,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_MODEL,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SLAVE_ID,
    CONF_INVERTER_TOKEN,
    CONF_INVERTER_RESTORE_SOC,
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


class TeslaAmberSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PowerSync."""

    VERSION = 2

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
        self._aemo_only_mode: bool = False  # True if using AEMO spike only (no Amber)
        self._aemo_data: dict[str, Any] = {}
        self._flow_power_data: dict[str, Any] = {}
        self._selected_electricity_provider: str = "amber"

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
            elif provider == "globird":
                # Globird: AEMO spike only mode
                self._aemo_only_mode = True
                self._amber_data = {}
                return await self.async_step_aemo_config()
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
                "globird_desc": "AEMO spike detection for VPP exports",
            },
        )

    async def async_step_flow_power_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Flow Power specific setup - region, price source, network tariff, PEA."""
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

            # Store Flow Power configuration
            self._flow_power_data = user_input

            # Check if using AEMO as price source (no Amber needed)
            price_source = user_input.get(CONF_FLOW_POWER_PRICE_SOURCE, "amber")

            if price_source == "aemo":
                # AEMO wholesale - no Amber API needed
                self._amber_data = {}
                self._aemo_only_mode = False  # Not spike-only, just using AEMO for pricing
                # Route based on battery system selection
                if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                    return await self.async_step_sigenergy_credentials()
                else:
                    return await self.async_step_tesla_provider()
            else:
                # Using Amber API for pricing - need Amber token
                return await self.async_step_amber()

        return self.async_show_form(
            step_id="flow_power_setup",
            data_schema=vol.Schema({
                vol.Required(CONF_FLOW_POWER_STATE, default="QLD1"): vol.In(FLOW_POWER_STATES),
                vol.Required(CONF_FLOW_POWER_PRICE_SOURCE, default="aemo"): vol.In(FLOW_POWER_PRICE_SOURCES),
                # Network Tariff - Combined dropdown with all distributors and tariffs
                vol.Required(CONF_NETWORK_TARIFF_COMBINED, default="energex:6900"): vol.In(ALL_NETWORK_TARIFFS),
                # Manual override - enable to enter rates manually instead of using library
                vol.Optional(CONF_NETWORK_USE_MANUAL_RATES, default=False): bool,
                # Manual rate entry (used when use_manual_rates=True)
                vol.Optional(CONF_NETWORK_TARIFF_TYPE, default="flat"): vol.In(NETWORK_TARIFF_TYPES),
                vol.Optional(CONF_NETWORK_FLAT_RATE, default=8.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=50.0)
                ),
                vol.Optional(CONF_NETWORK_PEAK_RATE, default=15.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=50.0)
                ),
                vol.Optional(CONF_NETWORK_SHOULDER_RATE, default=5.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=50.0)
                ),
                vol.Optional(CONF_NETWORK_OFFPEAK_RATE, default=2.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=50.0)
                ),
                vol.Optional(CONF_NETWORK_PEAK_START, default="16:00"): vol.In(
                    {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}
                ),
                vol.Optional(CONF_NETWORK_PEAK_END, default="21:00"): vol.In(
                    {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}
                ),
                vol.Optional(CONF_NETWORK_OFFPEAK_START, default="10:00"): vol.In(
                    {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}
                ),
                vol.Optional(CONF_NETWORK_OFFPEAK_END, default="15:00"): vol.In(
                    {f"{h:02d}:00": f"{h:02d}:00" for h in range(24)}
                ),
                vol.Optional(CONF_NETWORK_OTHER_FEES, default=1.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=20.0)
                ),
                vol.Optional(CONF_NETWORK_INCLUDE_GST, default=True): bool,
                # Flow Power PEA (Price Efficiency Adjustment)
                vol.Optional(CONF_PEA_ENABLED, default=True): bool,
                vol.Optional(CONF_FLOW_POWER_BASE_RATE, default=FLOW_POWER_DEFAULT_BASE_RATE): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=100.0)
                ),
                vol.Optional(CONF_PEA_CUSTOM_VALUE, default=None): vol.Any(
                    None, vol.All(vol.Coerce(float), vol.Range(min=-50.0, max=50.0))
                ),
                # Sync and other settings
                vol.Optional(CONF_AUTO_SYNC_ENABLED, default=True): bool,
                vol.Optional(CONF_SOLAR_CURTAILMENT_ENABLED, default=False): bool,
            }),
            errors=errors,
            description_placeholders={
                "rate_hint": "Select your network tariff from the dropdown",
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
        is_tesla = self._selected_battery_system != BATTERY_SYSTEM_SIGENERGY

        if user_input is not None:
            # Store Amber settings in _amber_data
            self._amber_data[CONF_SPIKE_PROTECTION_ENABLED] = user_input.get(CONF_SPIKE_PROTECTION_ENABLED, False)
            self._amber_data[CONF_SETTLED_PRICES_ONLY] = user_input.get(CONF_SETTLED_PRICES_ONLY, False)
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
                return await self.async_step_sigenergy_credentials()
            else:
                return await self.async_step_tesla_provider()

        # Build schema - force mode toggle only shown for Tesla
        schema_dict = {
            # Spike and price protection settings
            vol.Optional(CONF_SPIKE_PROTECTION_ENABLED, default=False): bool,
            vol.Optional(CONF_SETTLED_PRICES_ONLY, default=False): bool,
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

            # Both battery systems need electricity provider setup next
            # Provider selection handles Amber/Flow Power/Globird config
            return await self.async_step_provider_selection()

        return self.async_show_form(
            step_id="battery_system",
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_SYSTEM, default=BATTERY_SYSTEM_TESLA): vol.In(BATTERY_SYSTEMS),
            }),
            description_placeholders={
                "tesla_desc": "Tesla Powerwall with Fleet API or Teslemetry",
                "sigenergy_desc": "Sigenergy via Cloud API + optional Modbus curtailment",
            },
        )

    async def async_step_sigenergy_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Sigenergy credential entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_SIGENERGY_USERNAME, "").strip()
            pass_enc = user_input.get(CONF_SIGENERGY_PASS_ENC, "").strip()
            device_id = user_input.get(CONF_SIGENERGY_DEVICE_ID, "").strip()

            if not username or not pass_enc or not device_id:
                errors["base"] = "missing_credentials"
            elif len(device_id) != 13 or not device_id.isdigit():
                errors["base"] = "invalid_device_id"
            else:
                # Validate credentials
                validation_result = await validate_sigenergy_credentials(
                    self.hass, username, pass_enc, device_id
                )

                if validation_result["success"]:
                    self._sigenergy_data = {
                        CONF_SIGENERGY_USERNAME: username,
                        CONF_SIGENERGY_PASS_ENC: pass_enc,
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
                vol.Required(CONF_SIGENERGY_PASS_ENC): str,
                vol.Required(CONF_SIGENERGY_DEVICE_ID): str,
            }),
            errors=errors,
            description_placeholders={
                "credentials_help": "Capture credentials from browser dev tools when logging into Sigenergy web portal",
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
            return await self.async_step_finish_sigenergy()

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
            **self._flow_power_data,
            **self._sigenergy_data,
        }

        # Add electricity provider if set
        if self._selected_electricity_provider:
            final_data[CONF_ELECTRICITY_PROVIDER] = self._selected_electricity_provider

        # Generate title based on station ID
        station_id = self._sigenergy_data.get(CONF_SIGENERGY_STATION_ID, "Unknown")
        title = f"PowerSync - Sigenergy ({station_id})"

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

                    # Route based on battery system selection
                    if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                        return await self.async_step_sigenergy_credentials()
                    else:
                        return await self.async_step_tesla_provider()
            else:
                # AEMO disabled
                self._aemo_data = {CONF_AEMO_SPIKE_ENABLED: False}

                if self._aemo_only_mode:
                    # Can't be in AEMO-only mode without AEMO enabled
                    errors["base"] = "aemo_required_in_aemo_mode"
                else:
                    # Amber mode without AEMO: route based on battery system
                    if self._selected_battery_system == BATTERY_SYSTEM_SIGENERGY:
                        return await self.async_step_sigenergy_credentials()
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
                vol.Optional(CONF_AEMO_SPIKE_THRESHOLD, default=300.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=20000.0)
                ),
            }
        )

        return self.async_show_form(
            step_id="aemo_config",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "threshold_hint": "300 = $300/MWh (typical spike level)",
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
                self._site_data[CONF_SOLAR_CURTAILMENT_ENABLED] = user_input.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
            elif self._aemo_only_mode:
                # AEMO-only mode doesn't use Amber sync
                self._site_data[CONF_AUTO_SYNC_ENABLED] = False
            # For Flow Power, these settings are already in _flow_power_data

            # Go to optional demand charge configuration
            return await self.async_step_demand_charges()

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
            data_schema_dict[vol.Optional(CONF_SOLAR_CURTAILMENT_ENABLED, default=False)] = bool
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

    async def async_step_demand_charges(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle optional demand charge configuration (minimal implementation)."""
        if user_input is not None:
            # Combine all data
            data = {
                **self._amber_data,
                **self._teslemetry_data,
                **self._site_data,
                **self._aemo_data,  # Include AEMO configuration
                **self._flow_power_data,  # Include Flow Power configuration
                CONF_ELECTRICITY_PROVIDER: self._selected_electricity_provider,
            }

            # Add demand charge configuration if enabled
            if user_input.get(CONF_DEMAND_CHARGE_ENABLED, False):
                data.update({
                    CONF_DEMAND_CHARGE_ENABLED: True,
                    CONF_DEMAND_CHARGE_RATE: user_input[CONF_DEMAND_CHARGE_RATE],
                    CONF_DEMAND_CHARGE_START_TIME: user_input[CONF_DEMAND_CHARGE_START_TIME],
                    CONF_DEMAND_CHARGE_END_TIME: user_input[CONF_DEMAND_CHARGE_END_TIME],
                    CONF_DEMAND_CHARGE_DAYS: user_input[CONF_DEMAND_CHARGE_DAYS],
                    CONF_DEMAND_CHARGE_BILLING_DAY: user_input[CONF_DEMAND_CHARGE_BILLING_DAY],
                    CONF_DEMAND_CHARGE_APPLY_TO: user_input[CONF_DEMAND_CHARGE_APPLY_TO],
                    CONF_DEMAND_ARTIFICIAL_PRICE: user_input.get(CONF_DEMAND_ARTIFICIAL_PRICE, False),
                })
            else:
                data[CONF_DEMAND_CHARGE_ENABLED] = False

            # Add supply charges (always include, even if 0)
            data[CONF_DAILY_SUPPLY_CHARGE] = user_input.get(CONF_DAILY_SUPPLY_CHARGE, 0.0)
            data[CONF_MONTHLY_SUPPLY_CHARGE] = user_input.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0)

            # Set appropriate title based on provider
            if self._aemo_only_mode:
                title = "PowerSync AEMO Spike"
            elif self._selected_electricity_provider == "flow_power":
                title = "PowerSync (Flow Power)"
            else:
                title = "PowerSync"
            return self.async_create_entry(title=title, data=data)

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
                    vol.Coerce(int), vol.Range(min=1, max=28)
                ),
                vol.Optional(CONF_DEMAND_CHARGE_APPLY_TO, default="Buy Only"): vol.In(
                    ["Buy Only", "Sell Only", "Both"]
                ),
                vol.Optional(CONF_DEMAND_ARTIFICIAL_PRICE, default=False): bool,
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
        is_sigenergy = battery_system == BATTERY_SYSTEM_SIGENERGY

        if is_sigenergy:
            return await self.async_step_init_sigenergy(user_input)
        else:
            return await self.async_step_init_tesla(user_input)

    async def async_step_init_tesla(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Tesla users: Select electricity provider and Tesla API provider."""
        if user_input is not None:
            # Store provider selections
            self._provider = user_input.get(CONF_ELECTRICITY_PROVIDER, "amber")
            self._tesla_provider = user_input.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)

            # Check if switching to Teslemetry and need token
            current_tesla_provider = self.config_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)
            current_teslemetry_token = self.config_entry.data.get(CONF_TESLEMETRY_API_TOKEN)

            if self._tesla_provider == TESLA_PROVIDER_TESLEMETRY and (
                current_tesla_provider != TESLA_PROVIDER_TESLEMETRY or not current_teslemetry_token
            ):
                # Need to get Teslemetry token
                return await self.async_step_teslemetry_token()

            # Update config entry data with new Tesla provider
            if self._tesla_provider != current_tesla_provider:
                new_data = dict(self.config_entry.data)
                new_data[CONF_TESLA_API_PROVIDER] = self._tesla_provider
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

            # Route to provider-specific step
            if self._provider == "amber":
                return await self.async_step_amber_options()
            elif self._provider == "flow_power":
                return await self.async_step_flow_power_options()
            elif self._provider == "globird":
                return await self.async_step_globird_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_tesla_provider = self.config_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_TESLEMETRY)

        # Build Tesla provider choices
        tesla_providers = {
            TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - requires Tesla Fleet integration)",
            TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$4/month - easier setup)",
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
                }
            ),
        )

    async def async_step_init_sigenergy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Sigenergy users: Configure Modbus connection and DC curtailment."""
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
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to provider-specific step
                if self._provider == "amber":
                    return await self.async_step_amber_options()
                elif self._provider == "flow_power":
                    return await self.async_step_flow_power_options()
                elif self._provider == "globird":
                    return await self.async_step_globird_options()

        current_provider = self._get_option(CONF_ELECTRICITY_PROVIDER, "amber")
        current_modbus_host = self._get_option(CONF_SIGENERGY_MODBUS_HOST, "")
        current_modbus_port = self._get_option(CONF_SIGENERGY_MODBUS_PORT, DEFAULT_SIGENERGY_MODBUS_PORT)
        current_modbus_slave_id = self._get_option(CONF_SIGENERGY_MODBUS_SLAVE_ID, DEFAULT_SIGENERGY_MODBUS_SLAVE_ID)
        current_dc_curtailment = self._get_option(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False)

        return self.async_show_form(
            step_id="init_sigenergy",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ELECTRICITY_PROVIDER,
                        default=current_provider,
                    ): vol.In(ELECTRICITY_PROVIDERS),
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
                            elif self._provider == "globird":
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
        is_tesla = battery_system != BATTERY_SYSTEM_SIGENERGY

        if user_input is not None:
            # Check if solar curtailment is being disabled
            was_curtailment_enabled = self._get_option(CONF_SOLAR_CURTAILMENT_ENABLED, False)
            new_curtailment_enabled = user_input.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)

            if was_curtailment_enabled and not new_curtailment_enabled:
                await self._restore_export_rule()

            # Store amber options temporarily
            self._amber_options = user_input
            self._amber_options[CONF_ELECTRICITY_PROVIDER] = "amber"

            # Force tariff mode toggle only applies to Tesla - set to False for Sigenergy
            if not is_tesla:
                self._amber_options[CONF_FORCE_TARIFF_MODE_TOGGLE] = False

            # Check if AC-coupled inverter curtailment needs configuration
            # Route to inverter setup if:
            # 1. User is enabling inverter curtailment now, OR
            # 2. Inverter curtailment is already enabled (for editing)
            if user_input.get(CONF_INVERTER_CURTAILMENT_ENABLED, False):
                return await self.async_step_inverter_brand()

            # No inverter curtailment - save and exit
            return self.async_create_entry(title="", data=self._amber_options)

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
                CONF_SOLAR_CURTAILMENT_ENABLED,
                default=self._get_option(CONF_SOLAR_CURTAILMENT_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_SPIKE_PROTECTION_ENABLED,
                default=self._get_option(CONF_SPIKE_PROTECTION_ENABLED, False),
            ): bool,
            vol.Optional(
                CONF_SETTLED_PRICES_ONLY,
                default=self._get_option(CONF_SETTLED_PRICES_ONLY, False),
            ): bool,
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
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=28)),
            vol.Optional(
                CONF_DEMAND_CHARGE_APPLY_TO,
                default=self._get_option(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only"),
            ): vol.In(["Buy Only", "Sell Only", "Both"]),
            vol.Optional(
                CONF_DEMAND_ARTIFICIAL_PRICE,
                default=self._get_option(CONF_DEMAND_ARTIFICIAL_PRICE, False),
            ): bool,
            vol.Optional(
                CONF_DAILY_SUPPLY_CHARGE,
                default=self._get_option(CONF_DAILY_SUPPLY_CHARGE, 0.0),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_MONTHLY_SUPPLY_CHARGE,
                default=self._get_option(CONF_MONTHLY_SUPPLY_CHARGE, 0.0),
            ): vol.Coerce(float),
            # AC-Coupled Inverter Curtailment toggle (configuration in separate steps)
            vol.Optional(
                CONF_INVERTER_CURTAILMENT_ENABLED,
                default=self._get_option(CONF_INVERTER_CURTAILMENT_ENABLED, False),
            ): bool,
        })

        return self.async_show_form(
            step_id="amber_options",
            data_schema=vol.Schema(schema_dict),
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
        if user_input is not None:
            # Combine amber options with inverter config
            final_data = {**self._amber_options}
            final_data[CONF_INVERTER_BRAND] = self._inverter_brand
            final_data[CONF_INVERTER_MODEL] = user_input.get(CONF_INVERTER_MODEL)
            final_data[CONF_INVERTER_HOST] = user_input.get(CONF_INVERTER_HOST, "")
            final_data[CONF_INVERTER_PORT] = user_input.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT)

            # Only include slave ID for Modbus brands (not Enphase/Zeversolar which use HTTP)
            if self._inverter_brand not in ("enphase", "zeversolar"):
                final_data[CONF_INVERTER_SLAVE_ID] = user_input.get(
                    CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID
                )
            else:
                final_data[CONF_INVERTER_SLAVE_ID] = 1  # Default for HTTP-based inverters

            # Include JWT token for Enphase (required for firmware 7.x+)
            if self._inverter_brand == "enphase":
                final_data[CONF_INVERTER_TOKEN] = user_input.get(CONF_INVERTER_TOKEN, "")

            # Restore SOC threshold for AC inverter curtailment
            final_data[CONF_INVERTER_RESTORE_SOC] = user_input.get(
                CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC
            )

            return self.async_create_entry(title="", data=final_data)

        # Get brand-specific models and defaults
        brand = self._inverter_brand
        models = get_models_for_brand(brand)
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

        # Show JWT token field for Enphase (required for firmware 7.x+)
        if brand == "enphase":
            current_token = self._get_option(CONF_INVERTER_TOKEN, "")
            schema_dict[vol.Optional(
                CONF_INVERTER_TOKEN,
                default=current_token,
            )] = str

        # Restore SOC threshold - restore inverter when battery drops below this %
        current_restore_soc = self._get_option(CONF_INVERTER_RESTORE_SOC, DEFAULT_INVERTER_RESTORE_SOC)
        schema_dict[vol.Optional(
            CONF_INVERTER_RESTORE_SOC,
            default=current_restore_soc,
        )] = vol.All(vol.Coerce(int), vol.Range(min=50, max=100))

        return self.async_show_form(
            step_id="inverter_config",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "brand": INVERTER_BRANDS.get(brand, brand),
            },
        )

    async def async_step_flow_power_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b: Flow Power specific options."""
        if user_input is not None:
            # Check if solar curtailment is being disabled
            was_curtailment_enabled = self._get_option(CONF_SOLAR_CURTAILMENT_ENABLED, False)
            new_curtailment_enabled = user_input.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)

            if was_curtailment_enabled and not new_curtailment_enabled:
                await self._restore_export_rule()

            # Parse combined tariff selection (format: "distributor:code")
            combined = user_input.get(CONF_NETWORK_TARIFF_COMBINED)
            if combined and ":" in combined:
                distributor, tariff_code = combined.split(":", 1)
                user_input[CONF_NETWORK_DISTRIBUTOR] = distributor
                user_input[CONF_NETWORK_TARIFF_CODE] = tariff_code
            # Remove combined key before storing
            user_input.pop(CONF_NETWORK_TARIFF_COMBINED, None)

            # Auto-generate AEMO sensor entity names if using AEMO sensor source
            if user_input.get(CONF_FLOW_POWER_PRICE_SOURCE) == "aemo_sensor":
                region = user_input.get(CONF_FLOW_POWER_STATE, "NSW1").lower()
                user_input[CONF_AEMO_SENSOR_5MIN] = AEMO_SENSOR_5MIN_PATTERN.format(region=region)
                user_input[CONF_AEMO_SENSOR_30MIN] = AEMO_SENSOR_30MIN_PATTERN.format(region=region)
                _LOGGER.info(
                    "Auto-generated AEMO sensor entities for %s: 5min=%s, 30min=%s",
                    region.upper(),
                    user_input[CONF_AEMO_SENSOR_5MIN],
                    user_input[CONF_AEMO_SENSOR_30MIN]
                )

            # Add provider to the data
            user_input[CONF_ELECTRICITY_PROVIDER] = "flow_power"
            return self.async_create_entry(title="", data=user_input)

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
                        default=self._get_option(CONF_FLOW_POWER_PRICE_SOURCE, "amber"),
                    ): vol.In(FLOW_POWER_PRICE_SOURCES),
                    # Network Tariff - Combined dropdown with all distributors and tariffs
                    vol.Optional(
                        CONF_NETWORK_TARIFF_COMBINED,
                        default=current_combined,
                    ): vol.In(ALL_NETWORK_TARIFFS),
                    vol.Optional(
                        CONF_NETWORK_USE_MANUAL_RATES,
                        default=self._get_option(CONF_NETWORK_USE_MANUAL_RATES, False),
                    ): bool,
                    # Network Tariff - Fallback: Manual rate entry (used when use_manual_rates=True)
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
                    # End Network Tariff
                    # Flow Power PEA (Price Efficiency Adjustment)
                    # When enabled, uses Flow Power's actual billing model: Base Rate + PEA
                    vol.Optional(
                        CONF_PEA_ENABLED,
                        default=self._get_option(CONF_PEA_ENABLED, True),
                    ): bool,
                    vol.Optional(
                        CONF_FLOW_POWER_BASE_RATE,
                        default=self._get_option(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
                    vol.Optional(
                        CONF_PEA_CUSTOM_VALUE,
                        default=self._get_option(CONF_PEA_CUSTOM_VALUE, None),
                    ): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=-50.0, max=50.0))),
                    # End PEA Configuration
                    vol.Optional(
                        CONF_AUTO_SYNC_ENABLED,
                        default=self._get_option(CONF_AUTO_SYNC_ENABLED, True),
                    ): bool,
                    vol.Optional(
                        CONF_SOLAR_CURTAILMENT_ENABLED,
                        default=self._get_option(CONF_SOLAR_CURTAILMENT_ENABLED, False),
                    ): bool,
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
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=28)),
                    vol.Optional(
                        CONF_DEMAND_CHARGE_APPLY_TO,
                        default=self._get_option(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only"),
                    ): vol.In(["Buy Only", "Sell Only", "Both"]),
                    vol.Optional(
                        CONF_DAILY_SUPPLY_CHARGE,
                        default=self._get_option(CONF_DAILY_SUPPLY_CHARGE, 0.0),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_MONTHLY_SUPPLY_CHARGE,
                        default=self._get_option(CONF_MONTHLY_SUPPLY_CHARGE, 0.0),
                    ): vol.Coerce(float),
                }
            ),
        )

    async def async_step_globird_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2c: Globird (AEMO Spike Detection) specific options."""
        if user_input is not None:
            # Add provider to the data
            user_input[CONF_ELECTRICITY_PROVIDER] = "globird"
            # Enable AEMO spike detection for Globird
            user_input[CONF_AEMO_SPIKE_ENABLED] = True
            return self.async_create_entry(title="", data=user_input)

        # Build region choices for AEMO
        region_choices = {"": "Select Region..."}
        region_choices.update(AEMO_REGIONS)

        return self.async_show_form(
            step_id="globird_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AEMO_REGION,
                        default=self._get_option(CONF_AEMO_REGION, ""),
                    ): vol.In(region_choices),
                    vol.Optional(
                        CONF_AEMO_SPIKE_THRESHOLD,
                        default=self._get_option(CONF_AEMO_SPIKE_THRESHOLD, 300.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=20000.0)),
                }
            ),
        )
