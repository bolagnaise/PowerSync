"""Config flow for Tesla Sync integration."""
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
    CONF_DAILY_SUPPLY_CHARGE,
    CONF_MONTHLY_SUPPLY_CHARGE,
    CONF_TESLA_API_PROVIDER,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    AMBER_API_BASE_URL,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
)

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


class TeslaAmberSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tesla Sync."""

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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        # Check if already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return await self.async_step_amber()

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
                return await self.async_step_tesla_provider()
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
                                TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$3/month - proxy service)",
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
                    TESLA_PROVIDER_TESLEMETRY: "Teslemetry (~$3/month - proxy service)",
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

    async def async_step_site_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle site selection for both Amber and Tesla."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # If only one Amber site, use it automatically
            amber_site_id = user_input.get(CONF_AMBER_SITE_ID)
            if not amber_site_id and len(self._amber_sites) == 1:
                amber_site_id = self._amber_sites[0]["id"]
                _LOGGER.info(f"Auto-selected single Amber site: {amber_site_id}")

            # Store site selection data
            self._site_data = {
                CONF_AMBER_SITE_ID: amber_site_id,
                CONF_TESLA_ENERGY_SITE_ID: user_input[CONF_TESLA_ENERGY_SITE_ID],
                CONF_AUTO_SYNC_ENABLED: user_input.get(CONF_AUTO_SYNC_ENABLED, True),
                CONF_AMBER_FORECAST_TYPE: user_input.get(CONF_AMBER_FORECAST_TYPE, "predicted"),
                CONF_SOLAR_CURTAILMENT_ENABLED: user_input.get(CONF_SOLAR_CURTAILMENT_ENABLED, False),
            }

            # Go to optional demand charge configuration
            return await self.async_step_demand_charges()

        # Build selection options
        amber_site_options = {
            site["id"]: site.get("nmi", site["id"])
            for site in self._amber_sites
        }

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

        # Only add Amber site selection if multiple sites
        if len(self._amber_sites) > 1:
            data_schema_dict[vol.Required(CONF_AMBER_SITE_ID)] = vol.In(
                amber_site_options
            )

        data_schema_dict[vol.Optional(CONF_AUTO_SYNC_ENABLED, default=True)] = bool
        data_schema_dict[vol.Optional(CONF_AMBER_FORECAST_TYPE, default="predicted")] = vol.In({
            "predicted": "Predicted (Default)",
            "low": "Low (Conservative)",
            "high": "High (Optimistic)"
        })
        data_schema_dict[vol.Optional(CONF_SOLAR_CURTAILMENT_ENABLED, default=False)] = bool

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
                })
            else:
                data[CONF_DEMAND_CHARGE_ENABLED] = False

            # Add supply charges (always include, even if 0)
            data[CONF_DAILY_SUPPLY_CHARGE] = user_input.get(CONF_DAILY_SUPPLY_CHARGE, 0.0)
            data[CONF_MONTHLY_SUPPLY_CHARGE] = user_input.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0)

            return self.async_create_entry(title="Tesla Sync", data=data)

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
    """Handle options flow for Tesla Sync."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Get current values from options (fallback to data for backwards compatibility)
        current_auto_sync = self.config_entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            self.config_entry.data.get(CONF_AUTO_SYNC_ENABLED, True)
        )
        current_forecast_type = self.config_entry.options.get(
            CONF_AMBER_FORECAST_TYPE,
            self.config_entry.data.get(CONF_AMBER_FORECAST_TYPE, "predicted")
        )
        current_demand_enabled = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False)
        )
        current_demand_rate = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_RATE,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_RATE, 10.0)
        )
        current_start_time = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_START_TIME,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_START_TIME, "14:00")
        )
        current_end_time = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_END_TIME,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_END_TIME, "20:00")
        )
        current_days = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_DAYS,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_DAYS, "All Days")
        )
        current_billing_day = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_BILLING_DAY,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_BILLING_DAY, 1)
        )
        current_apply_to = self.config_entry.options.get(
            CONF_DEMAND_CHARGE_APPLY_TO,
            self.config_entry.data.get(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only")
        )
        current_solar_curtailment = self.config_entry.options.get(
            CONF_SOLAR_CURTAILMENT_ENABLED,
            self.config_entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
        )
        current_daily_supply_charge = self.config_entry.options.get(
            CONF_DAILY_SUPPLY_CHARGE,
            self.config_entry.data.get(CONF_DAILY_SUPPLY_CHARGE, 0.0)
        )
        current_monthly_supply_charge = self.config_entry.options.get(
            CONF_MONTHLY_SUPPLY_CHARGE,
            self.config_entry.data.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0)
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_AUTO_SYNC_ENABLED,
                        default=current_auto_sync,
                    ): bool,
                    vol.Optional(
                        CONF_AMBER_FORECAST_TYPE,
                        default=current_forecast_type,
                    ): vol.In({
                        "predicted": "Predicted (Default)",
                        "low": "Low (Conservative)",
                        "high": "High (Optimistic)"
                    }),
                    vol.Optional(
                        CONF_SOLAR_CURTAILMENT_ENABLED,
                        default=current_solar_curtailment,
                    ): bool,
                    vol.Optional(
                        CONF_DEMAND_CHARGE_ENABLED,
                        default=current_demand_enabled,
                    ): bool,
                    vol.Optional(
                        CONF_DEMAND_CHARGE_RATE,
                        default=current_demand_rate,
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
                    vol.Optional(
                        CONF_DEMAND_CHARGE_START_TIME,
                        default=current_start_time,
                    ): str,
                    vol.Optional(
                        CONF_DEMAND_CHARGE_END_TIME,
                        default=current_end_time,
                    ): str,
                    vol.Optional(
                        CONF_DEMAND_CHARGE_DAYS,
                        default=current_days,
                    ): vol.In(["All Days", "Weekdays Only", "Weekends Only"]),
                    vol.Optional(
                        CONF_DEMAND_CHARGE_BILLING_DAY,
                        default=current_billing_day,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=28)),
                    vol.Optional(
                        CONF_DEMAND_CHARGE_APPLY_TO,
                        default=current_apply_to,
                    ): vol.In(["Buy Only", "Sell Only", "Both"]),
                    vol.Optional(
                        CONF_DAILY_SUPPLY_CHARGE,
                        default=current_daily_supply_charge,
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_MONTHLY_SUPPLY_CHARGE,
                        default=current_monthly_supply_charge,
                    ): vol.Coerce(float),
                }
            ),
        )
