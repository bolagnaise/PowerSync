"""Sensor platform for PowerSync integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CURRENCY_DOLLAR,
    UnitOfEnergy,
    UnitOfPower,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from datetime import timedelta

from .const import (
    DOMAIN,
    SENSOR_TYPE_CURRENT_PRICE,
    SENSOR_TYPE_CURRENT_IMPORT_PRICE,
    SENSOR_TYPE_CURRENT_EXPORT_PRICE,
    SENSOR_TYPE_SOLAR_POWER,
    SENSOR_TYPE_GRID_POWER,
    SENSOR_TYPE_BATTERY_POWER,
    SENSOR_TYPE_HOME_LOAD,
    SENSOR_TYPE_BATTERY_LEVEL,
    SENSOR_TYPE_DAILY_SOLAR_ENERGY,
    SENSOR_TYPE_DAILY_GRID_IMPORT,
    SENSOR_TYPE_DAILY_GRID_EXPORT,
    SENSOR_TYPE_GRID_IMPORT_POWER,
    SENSOR_TYPE_IN_DEMAND_CHARGE_PERIOD,
    SENSOR_TYPE_PEAK_DEMAND_THIS_CYCLE,
    SENSOR_TYPE_DEMAND_CHARGE_COST,
    SENSOR_TYPE_DAYS_UNTIL_DEMAND_RESET,
    SENSOR_TYPE_DAILY_SUPPLY_CHARGE_COST,
    SENSOR_TYPE_MONTHLY_SUPPLY_CHARGE,
    SENSOR_TYPE_TOTAL_MONTHLY_COST,
    SENSOR_TYPE_AEMO_PRICE,
    SENSOR_TYPE_AEMO_SPIKE_STATUS,
    SENSOR_TYPE_SOLCAST_TODAY,
    SENSOR_TYPE_SOLCAST_TOMORROW,
    SENSOR_TYPE_SOLCAST_CURRENT,
    CONF_SOLCAST_ENABLED,
    CONF_ELECTRICITY_PROVIDER,
    SENSOR_TYPE_TARIFF_SCHEDULE,
    SENSOR_TYPE_SOLAR_CURTAILMENT,
    SENSOR_TYPE_FLOW_POWER_PRICE,
    SENSOR_TYPE_FLOW_POWER_EXPORT_PRICE,
    SENSOR_TYPE_FLOW_POWER_TWAP,
    SENSOR_TYPE_NETWORK_TARIFF,
    SENSOR_TYPE_AMBER_COMPARISON,
    CONF_FP_NETWORK,
    CONF_FP_TARIFF_CODE,
    CONF_FP_TWAP_OVERRIDE,
    CONF_FP_AMBER_MARKUP,
    FLOW_POWER_GST,
    NETWORK_API_NAME,
    DEFAULT_FP_AMBER_MARKUP,
    SENSOR_TYPE_BATTERY_HEALTH,
    SENSOR_TYPE_INVERTER_STATUS,
    SENSOR_TYPE_BATTERY_MODE,
    SENSOR_TYPE_PV1_POWER,
    SENSOR_TYPE_PV2_POWER,
    SENSOR_TYPE_CT2_POWER,
    SENSOR_TYPE_WORK_MODE,
    SENSOR_TYPE_MIN_SOC,
    SENSOR_TYPE_DAILY_BATTERY_CHARGE_FOXESS,
    SENSOR_TYPE_DAILY_BATTERY_DISCHARGE_FOXESS,
    SENSOR_TYPE_BATTERY_LEVEL_1,
    SENSOR_TYPE_BATTERY_LEVEL_2,
    SENSOR_TYPE_OPTIMIZATION_STATUS,
    SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION,
    SENSOR_TYPE_LP_SOLAR_FORECAST,
    SENSOR_TYPE_LP_LOAD_FORECAST,
    SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST,
    SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST,
    SENSOR_TYPE_AMBER_USAGE_YESTERDAY_COST,
    SENSOR_TYPE_AMBER_USAGE_YESTERDAY_SAVINGS,
    SENSOR_TYPE_AMBER_USAGE_MONTH_COST,
    SENSOR_TYPE_AMBER_USAGE_MONTH_SAVINGS,
    BATTERY_MODE_STATE_NORMAL,
    BATTERY_MODE_STATE_FORCE_CHARGE,
    BATTERY_MODE_STATE_FORCE_DISCHARGE,
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
    CONF_ENPHASE_IS_INSTALLER,
    CONF_ENPHASE_NORMAL_PROFILE,
    CONF_ENPHASE_ZERO_EXPORT_PROFILE,
    CONF_FRONIUS_LOAD_FOLLOWING,
    CONF_DEMAND_CHARGE_ENABLED,
    CONF_DEMAND_CHARGE_RATE,
    CONF_DEMAND_CHARGE_START_TIME,
    CONF_DEMAND_CHARGE_END_TIME,
    CONF_DEMAND_CHARGE_DAYS,
    CONF_DEMAND_CHARGE_BILLING_DAY,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    CONF_ELECTRICITY_PROVIDER,
    CONF_FLOW_POWER_STATE,
    CONF_PEA_ENABLED,
    CONF_FLOW_POWER_BASE_RATE,
    CONF_PEA_CUSTOM_VALUE,
    FLOW_POWER_MARKET_AVG,
    FLOW_POWER_BENCHMARK,
    FLOW_POWER_DEFAULT_BASE_RATE,
    FLOW_POWER_EXPORT_RATES,
    FLOW_POWER_HAPPY_HOUR_PERIODS,
    ATTR_PRICE_SPIKE,
    ATTR_WHOLESALE_PRICE,
    ATTR_NETWORK_PRICE,
    ATTR_AEMO_REGION,
    ATTR_AEMO_THRESHOLD,
    ATTR_SPIKE_START_TIME,
)
from .coordinator import AmberPriceCoordinator, TeslaEnergyCoordinator, DemandChargeCoordinator, SolcastForecastCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PowerSyncSensorEntityDescription(SensorEntityDescription):
    """Describes PowerSync sensor entity."""

    value_fn: Callable[[Any], Any] | None = None
    attr_fn: Callable[[Any], dict[str, Any]] | None = None


def _get_import_price(data):
    """Extract import (general) price from Amber data."""
    if not data:
        _LOGGER.debug("_get_import_price: No data available")
        return None
    if not data.get("current"):
        _LOGGER.debug("_get_import_price: No 'current' key in data. Keys: %s", list(data.keys()) if isinstance(data, dict) else "not a dict")
        return None
    current_prices = data.get("current", [])
    _LOGGER.debug("_get_import_price: Found %d current price entries", len(current_prices))
    for price in current_prices:
        if price.get("channelType") == "general":
            raw_price = price.get("perKwh", 0)
            converted_price = raw_price / 100
            _LOGGER.debug("_get_import_price: Found general price: %s c/kWh -> %s $/kWh", raw_price, converted_price)
            return converted_price
    _LOGGER.debug("_get_import_price: No 'general' channel found in current prices")
    return None


def _get_export_price(data):
    """Extract export earnings from Amber feedIn data.

    Amber convention: feedIn.perKwh is NEGATIVE when you earn money (good)
                      feedIn.perKwh is POSITIVE when you pay to export (bad)

    We negate to show user-friendly "export earnings":
        Positive = earning money per kWh exported
        Negative = paying money per kWh exported
    """
    if not data:
        _LOGGER.debug("_get_export_price: No data available")
        return None
    if not data.get("current"):
        _LOGGER.debug("_get_export_price: No 'current' key in data")
        return None
    current_prices = data.get("current", [])
    channel_types = [p.get("channelType") for p in current_prices]
    _LOGGER.debug("_get_export_price: Found %d entries with channels: %s", len(current_prices), channel_types)
    for price in current_prices:
        if price.get("channelType") == "feedIn":
            raw_price = price.get("perKwh", 0)
            # Negate to convert from Amber feedIn to export earnings
            # Amber feedIn +10 (paying) → sensor -0.10 (negative earnings)
            # Amber feedIn -10 (earning) → sensor +0.10 (positive earnings)
            converted_price = -raw_price / 100
            _LOGGER.debug("_get_export_price: Found feedIn price: %s c/kWh -> %s $/kWh", raw_price, converted_price)
            return converted_price
    _LOGGER.debug("_get_export_price: No 'feedIn' channel found in current prices")
    return None


PRICE_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_CURRENT_IMPORT_PRICE,
        name="Current Import Price",
        native_unit_of_measurement=f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=4,
        value_fn=_get_import_price,
        attr_fn=lambda data: {
            ATTR_PRICE_SPIKE: data.get("current", [{}])[0].get("spikeStatus")
            if data and data.get("current")
            else None,
            ATTR_WHOLESALE_PRICE: data.get("current", [{}])[0].get("wholesaleKWHPrice", 0) / 100
            if data and data.get("current")
            else 0,
            ATTR_NETWORK_PRICE: data.get("current", [{}])[0].get("networkKWHPrice", 0) / 100
            if data and data.get("current")
            else 0,
        },
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_CURRENT_EXPORT_PRICE,
        name="Current Export Price",
        native_unit_of_measurement=f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=4,
        icon="mdi:transmission-tower-export",
        value_fn=_get_export_price,
        attr_fn=lambda data: {
            "channel_type": "feedIn",
        },
    ),
)

ENERGY_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SOLAR_POWER,
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("solar_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_GRID_POWER,
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("grid_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_POWER,
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("battery_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_HOME_LOAD,
        name="Home Load",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("load_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_LEVEL,
        name="Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("battery_level") if data else None,
    ),
)

FOXESS_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_PV1_POWER,
        name="PV1 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: data.get("pv1_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_PV2_POWER,
        name="PV2 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: data.get("pv2_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_CT2_POWER,
        name="CT2 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:current-ac",
        value_fn=lambda data: data.get("ct2_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_WORK_MODE,
        name="Inverter Work Mode",
        icon="mdi:cog",
        value_fn=lambda data: data.get("work_mode_name") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_MIN_SOC,
        name="Minimum SOC",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-low",
        suggested_display_precision=0,
        value_fn=lambda data: data.get("min_soc") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_CHARGE_FOXESS,
        name="Daily Battery Charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-charging",
        value_fn=lambda data: data.get("energy_summary", {}).get("charge_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_DISCHARGE_FOXESS,
        name="Daily Battery Discharge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: data.get("energy_summary", {}).get("discharge_today_kwh") if data else None,
    ),
)

DUAL_SUNGROW_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_LEVEL_1,
        name="Battery Level (Inverter 1)",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("battery_level_1") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_LEVEL_2,
        name="Battery Level (Inverter 2)",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("battery_level_2") if data else None,
    ),
)

OPTIMIZER_ACTION_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_STATUS,
        name="Optimizer Current Action",
        icon="mdi:battery-sync",
        value_fn=lambda data: data.get("current_action") if data else None,
        attr_fn=lambda data: {
            "power_w": data.get("current_power_w"),
            "status": data.get("status"),
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION,
        name="Optimizer Next Action",
        icon="mdi:clock-fast",
        value_fn=lambda data: data.get("next_action") if data else None,
        attr_fn=lambda data: {
            "time": data.get("next_action_time"),
            "power_w": data.get("next_action_power_w"),
        } if data else {},
    ),
)

DEMAND_CHARGE_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_IN_DEMAND_CHARGE_PERIOD,
        name="In Demand Charge Period",
        value_fn=lambda data: data.get("in_peak_period", False) if data else False,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_PEAK_DEMAND_THIS_CYCLE,
        name="Peak Demand This Cycle",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("peak_demand_kw", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DEMAND_CHARGE_COST,
        name="Estimated Demand Charge Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("estimated_cost", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_SUPPLY_CHARGE_COST,
        name="Daily Supply Charge Cost This Month",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,  # MONETARY only supports 'total', not 'total_increasing'
        suggested_display_precision=2,
        value_fn=lambda data: data.get("daily_supply_charge_cost", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_MONTHLY_SUPPLY_CHARGE,
        name="Monthly Supply Charge",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("monthly_supply_charge", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_TOTAL_MONTHLY_COST,
        name="Total Estimated Monthly Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("total_monthly_cost", 0.0) if data else 0.0,
    ),
)


# AEMO Spike Detection Sensors
AEMO_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_AEMO_PRICE,
        name="AEMO Wholesale Price",
        native_unit_of_measurement="$/MWh",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("last_price") if data else None,
        attr_fn=lambda data: {
            ATTR_AEMO_REGION: data.get("region") if data else None,
            ATTR_AEMO_THRESHOLD: data.get("threshold") if data else None,
            "last_check": data.get("last_check") if data else None,
        },
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_AEMO_SPIKE_STATUS,
        name="AEMO Spike Status",
        icon="mdi:alert-decagram",
        value_fn=lambda data: "Spike Active" if data and data.get("in_spike_mode") else "Normal",
        attr_fn=lambda data: {
            ATTR_AEMO_REGION: data.get("region") if data else None,
            ATTR_AEMO_THRESHOLD: data.get("threshold") if data else None,
            ATTR_SPIKE_START_TIME: data.get("spike_start_time") if data else None,
            "last_price": data.get("last_price") if data else None,
        },
    ),
)


# Solcast Solar Forecast Sensors
SOLCAST_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SOLCAST_TODAY,
        name="Solar Forecast Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        icon="mdi:solar-power",
        suggested_display_precision=1,
        value_fn=lambda data: data.get("today_forecast_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("today_peak_kw") if data else None,
            "remaining_kwh": data.get("today_remaining_kwh") if data else None,
            "hourly_forecast": data.get("hourly_forecast") if data else None,  # For chart overlay
            "last_update": data.get("last_update").isoformat() if data and data.get("last_update") else None,
            "source": data.get("source", "api") if data else None,  # "solcast_integration" or "api"
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SOLCAST_TOMORROW,
        name="Solar Forecast Tomorrow",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        icon="mdi:solar-power-variant",
        suggested_display_precision=1,
        value_fn=lambda data: data.get("tomorrow_total_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("tomorrow_peak_kw") if data else None,
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SOLCAST_CURRENT,
        name="Solar Forecast Current",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:white-balance-sunny",
        suggested_display_precision=2,
        value_fn=lambda data: data.get("current_estimate_kw") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "forecast_periods": data.get("forecast_periods") if data else None,
        } if data else {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync sensor entities."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    amber_coordinator: AmberPriceCoordinator | None = domain_data.get("amber_coordinator")
    tesla_coordinator: TeslaEnergyCoordinator | None = domain_data.get("tesla_coordinator")
    sigenergy_coordinator = domain_data.get("sigenergy_coordinator")
    sungrow_coordinator = domain_data.get("sungrow_coordinator")
    foxess_coordinator = domain_data.get("foxess_coordinator")
    goodwe_coordinator = domain_data.get("goodwe_coordinator")
    demand_charge_coordinator: DemandChargeCoordinator | None = domain_data.get("demand_charge_coordinator")
    aemo_spike_manager = domain_data.get("aemo_spike_manager")
    is_sigenergy = domain_data.get("is_sigenergy", False)
    is_sungrow = domain_data.get("is_sungrow", False)
    is_foxess = domain_data.get("is_foxess", False)
    is_goodwe = domain_data.get("is_goodwe", False)

    entities: list[SensorEntity] = []

    # Add price sensors
    # For Amber users: use AmberPriceSensor with live API data
    # For non-Amber users (Globird, etc.): use TariffPriceSensor with TOU schedule
    if amber_coordinator:
        _LOGGER.info("Adding Amber price sensors (import and export)")
        for description in PRICE_SENSORS:
            entities.append(
                AmberPriceSensor(
                    coordinator=amber_coordinator,
                    description=description,
                    entry=entry,
                )
            )
    else:
        # For non-Amber providers (Globird, Flow Power, etc.), always create TariffPriceSensor.
        # The sensor handles missing tariff_schedule gracefully (returns None until
        # the tariff is fetched later during setup). This avoids a race condition
        # where sensors were skipped because tariff_schedule hadn't been fetched yet.
        electricity_provider = entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        tou_providers = ("globird", "aemo_vpp", "other", "tou_only", "octopus", "flow_power")
        tariff_schedule = domain_data.get("tariff_schedule")
        if tariff_schedule or electricity_provider in tou_providers:
            _LOGGER.info(
                "Adding tariff-based price sensors (import and export) for %s provider",
                electricity_provider or "non-Amber",
            )
            entities.append(
                TariffPriceSensor(
                    hass=hass,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_CURRENT_IMPORT_PRICE,
                    name="Current Import Price",
                )
            )
            entities.append(
                TariffPriceSensor(
                    hass=hass,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_CURRENT_EXPORT_PRICE,
                    name="Current Export Price",
                )
            )
        else:
            _LOGGER.debug("No price coordinator or known provider - skipping price sensors")

    # Add energy sensors - select the correct coordinator for battery system type
    # All coordinators return data with same field names (solar_power, grid_power, etc.)
    if is_foxess:
        energy_coordinator = foxess_coordinator
    elif is_goodwe:
        energy_coordinator = goodwe_coordinator
    elif is_sungrow:
        energy_coordinator = sungrow_coordinator
    elif is_sigenergy:
        energy_coordinator = sigenergy_coordinator
    else:
        energy_coordinator = tesla_coordinator
    if energy_coordinator:
        for description in ENERGY_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
    else:
        _LOGGER.warning("No energy coordinator available - energy sensors will not be created")

    # Add FoxESS-specific sensors (PV strings, CT2, work mode, etc.)
    if is_foxess and energy_coordinator:
        for description in FOXESS_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("FoxESS-specific sensors added (PV1, PV2, CT2, work mode, min SOC, daily energy)")

    # Add dual Sungrow per-inverter SOC sensors
    if is_sungrow and energy_coordinator and hasattr(energy_coordinator, '_coord2'):
        for description in DUAL_SUNGROW_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Dual Sungrow per-inverter SOC sensors added")

    # Add demand charge sensors if enabled and coordinator exists
    if demand_charge_coordinator and demand_charge_coordinator.enabled:
        _LOGGER.info("Demand charge tracking enabled - adding sensors")
        for description in DEMAND_CHARGE_SENSORS:
            entities.append(
                DemandChargeSensor(
                    coordinator=demand_charge_coordinator,
                    description=description,
                    entry=entry,
                )
            )

    # Add AEMO spike sensors if spike manager exists
    if aemo_spike_manager:
        _LOGGER.info("AEMO spike detection enabled - adding sensors")
        for description in AEMO_SENSORS:
            entities.append(
                AEMOSpikeSensor(
                    spike_manager=aemo_spike_manager,
                    description=description,
                    entry=entry,
                )
            )

    # Add Solcast solar forecast sensors if enabled and coordinator exists
    solcast_coordinator: SolcastForecastCoordinator | None = domain_data.get("solcast_coordinator")
    if solcast_coordinator:
        _LOGGER.info("Solcast solar forecasting enabled - adding sensors")
        for description in SOLCAST_SENSORS:
            entities.append(
                SolcastForecastSensor(
                    coordinator=solcast_coordinator,
                    description=description,
                    entry=entry,
                )
            )

    # Add LP forecast sensors if optimization coordinator exists.
    # The optimizer is initialized AFTER sensor platform setup, so the coordinator
    # usually won't exist yet. Store the callback so __init__.py can add these
    # sensors later when the optimizer is ready.
    optimization_coordinator = domain_data.get("optimization_coordinator")
    if optimization_coordinator:
        _LOGGER.info("LP optimizer active - adding forecast sensors")
        for description in LP_FORECAST_SENSORS:
            entities.append(
                LPForecastSensor(
                    coordinator=optimization_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        for description in OPTIMIZER_ACTION_SENSORS:
            entities.append(
                OptimizerActionSensor(
                    coordinator=optimization_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Optimizer action sensors added (current action, next action)")
    else:
        # Store callback for deferred LP forecast + optimizer action sensor creation
        domain_data["sensor_async_add_entities"] = async_add_entities

    # Add Amber usage sensors if usage coordinator exists
    amber_usage_coordinator = domain_data.get("amber_usage_coordinator")
    if amber_usage_coordinator:
        _LOGGER.info("Amber usage tracking active - adding metered cost sensors")
        sensor_names = {
            SENSOR_TYPE_AMBER_USAGE_YESTERDAY_COST: "Yesterday Billed Cost",
            SENSOR_TYPE_AMBER_USAGE_YESTERDAY_SAVINGS: "Yesterday Battery Savings",
            SENSOR_TYPE_AMBER_USAGE_MONTH_COST: "Month To Date Billed Cost",
            SENSOR_TYPE_AMBER_USAGE_MONTH_SAVINGS: "Month To Date Battery Savings",
        }
        for sensor_type, period, value_key in AMBER_USAGE_SENSORS:
            entities.append(
                AmberUsageSensor(
                    entry=entry,
                    sensor_type=sensor_type,
                    name=sensor_names[sensor_type],
                    period=period,
                    value_key=value_key,
                )
            )

    # Add tariff schedule sensor (always added for visualization)
    entities.append(
        TariffScheduleSensor(
            hass=hass,
            entry=entry,
        )
    )
    _LOGGER.info("Tariff schedule sensor added for TOU visualization")

    # Add solar curtailment sensor if curtailment is enabled
    curtailment_enabled = entry.options.get(
        CONF_BATTERY_CURTAILMENT_ENABLED,
        entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
    )
    if curtailment_enabled:
        entities.append(
            SolarCurtailmentSensor(
                hass=hass,
                entry=entry,
            )
        )
        _LOGGER.info("Solar curtailment sensor added")

    # Add inverter status sensor if inverter curtailment is enabled
    inverter_enabled = entry.options.get(
        CONF_AC_INVERTER_CURTAILMENT_ENABLED,
        entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
    )
    if inverter_enabled:
        entities.append(
            InverterStatusSensor(
                hass=hass,
                entry=entry,
            )
        )
        _LOGGER.info("Inverter status sensor added")

    # Add Flow Power price sensors if Flow Power provider is selected
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER)
    )
    if electricity_provider == "flow_power":
        # Get the price coordinator (Amber or AEMO)
        price_coordinator = amber_coordinator or domain_data.get("aemo_sensor_coordinator")
        if price_coordinator:
            # Add import price sensor
            entities.append(
                FlowPowerPriceSensor(
                    coordinator=price_coordinator,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_FLOW_POWER_PRICE,
                )
            )
            # Add export price sensor
            entities.append(
                FlowPowerPriceSensor(
                    coordinator=price_coordinator,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_FLOW_POWER_EXPORT_PRICE,
                )
            )
            # Add TWAP sensor
            entities.append(
                FlowPowerTWAPSensor(
                    hass=hass,
                    entry=entry,
                )
            )

            # Add tariff-dependent sensors if network tariff is configured
            fp_network = entry.options.get(
                CONF_FP_NETWORK, entry.data.get(CONF_FP_NETWORK)
            )
            fp_tariff_code = entry.options.get(
                CONF_FP_TARIFF_CODE, entry.data.get(CONF_FP_TARIFF_CODE)
            )
            if fp_network and fp_tariff_code:
                entities.append(
                    FlowPowerNetworkTariffSensor(
                        hass=hass,
                        entry=entry,
                    )
                )
                entities.append(
                    FlowPowerAmberComparisonSensor(
                        hass=hass,
                        entry=entry,
                        coordinator=price_coordinator,
                    )
                )
                _LOGGER.info("Flow Power tariff sensors added (network tariff + Amber comparison)")

            _LOGGER.info("Flow Power price sensors added (import, export, and TWAP)")

    # Always add battery health sensor (receives data from mobile app)
    entities.append(BatteryHealthSensor(entry=entry))
    _LOGGER.info("Battery health sensor added")

    # Always add battery mode sensor (for automation triggers)
    entities.append(BatteryModeSensor(hass=hass, entry=entry))
    _LOGGER.info("Battery mode sensor added")

    async_add_entities(entities)


class AmberPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Amber electricity prices."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: AmberPriceCoordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry
        _LOGGER.debug("AmberPriceSensor initialized: %s (unique_id=%s)", description.key, self._attr_unique_id)

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            value = self.entity_description.value_fn(self.coordinator.data)
            _LOGGER.debug("AmberPriceSensor %s native_value: %s", self.entity_description.key, value)
            return value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            return self.entity_description.attr_fn(self.coordinator.data)
        return {}


class TeslaEnergySensor(CoordinatorEntity, SensorEntity):
    """Sensor for Tesla energy data."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: TeslaEnergyCoordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self.coordinator.data)
        return None


class OptimizerActionSensor(CoordinatorEntity, SensorEntity):
    """Sensor for optimizer current/next action (reads from OptimizationCoordinator.data)."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{description.key}"

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self.coordinator.data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            return self.entity_description.attr_fn(self.coordinator.data)
        return {}


class DemandChargeSensor(CoordinatorEntity, SensorEntity):
    """Sensor for demand charge tracking (simplified - uses coordinator data)."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: DemandChargeCoordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor (uses coordinator data)."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self.coordinator.data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}

        attributes = {}
        coordinator_data = self.coordinator.data

        if self.entity_description.key == SENSOR_TYPE_PEAK_DEMAND_THIS_CYCLE:
            # Add peak demand value as attribute
            peak_kw = coordinator_data.get("peak_demand_kw", 0.0)
            attributes["peak_kw"] = peak_kw
            # Add timestamp if available
            if "last_update" in coordinator_data:
                attributes["last_update"] = coordinator_data["last_update"].isoformat()

        elif self.entity_description.key == SENSOR_TYPE_DEMAND_CHARGE_COST:
            # Get rate from config (check options first, then data)
            rate = self.coordinator.rate
            peak_kw = coordinator_data.get("peak_demand_kw", 0.0)
            attributes["peak_kw"] = peak_kw
            attributes["rate"] = rate

        return attributes


class AEMOSpikeSensor(SensorEntity):
    """Sensor for AEMO spike detection status."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        spike_manager,  # AEMOSpikeManager from __init__.py
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self._spike_manager = spike_manager
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self._spike_manager.get_status())
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            return self.entity_description.attr_fn(self._spike_manager.get_status())
        return {}


class SolcastForecastSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Solcast solar production forecasts."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: SolcastForecastCoordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self.coordinator.data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            return self.entity_description.attr_fn(self.coordinator.data)
        return {}


# ============================================================
# LP Forecast Sensors (built-in optimizer forecast data)
# ============================================================

LP_FORECAST_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_SOLAR_FORECAST,
        name="Solar Forecast",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: data.get("solar_forecast_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("solar_peak_kw"),
            "forecast_values_kw": data.get("solar_forecast"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_LOAD_FORECAST,
        name="Load Forecast",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: data.get("load_forecast_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("load_peak_kw"),
            "forecast_values_kw": data.get("load_forecast"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST,
        name="Import Price Forecast",
        native_unit_of_measurement=f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=4,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.get("import_price_avg") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "min_price": data.get("import_price_min"),
            "max_price": data.get("import_price_max"),
            "price_values": data.get("import_prices"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST,
        name="Export Price Forecast",
        native_unit_of_measurement=f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=4,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.get("export_price_avg") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "min_price": data.get("export_price_min"),
            "max_price": data.get("export_price_max"),
            "price_values": data.get("export_prices"),
        } if data and data.get("available") else {},
    ),
)


# Amber Usage sensors — actual metered cost data from NEM
AMBER_USAGE_SENSORS = (
    (SENSOR_TYPE_AMBER_USAGE_YESTERDAY_COST, "yesterday", "net_cost"),
    (SENSOR_TYPE_AMBER_USAGE_YESTERDAY_SAVINGS, "yesterday", "savings"),
    (SENSOR_TYPE_AMBER_USAGE_MONTH_COST, "month", "net_cost"),
    (SENSOR_TYPE_AMBER_USAGE_MONTH_SAVINGS, "month", "savings"),
)


class LPForecastSensor(CoordinatorEntity, SensorEntity):
    """Sensor for LP optimizer forecast data (solar, load, prices).

    Reads forecast data stored by the OptimizationCoordinator each
    optimization cycle via get_forecast_data().
    """

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator,
        description: PowerSyncSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def _forecast_data(self) -> dict[str, Any]:
        """Get forecast data from the optimization coordinator."""
        if hasattr(self.coordinator, "get_forecast_data"):
            return self.coordinator.get_forecast_data()
        return {}

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self._forecast_data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            return self.entity_description.attr_fn(self._forecast_data)
        return {}


SIGNAL_TARIFF_UPDATED = "power_sync_tariff_updated_{}"


class TariffScheduleSensor(SensorEntity):
    """Sensor for displaying the current tariff schedule sent to Tesla."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_TARIFF_SCHEDULE}"
        self._attr_has_entity_name = True
        self._attr_name = "TOU Schedule"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_TARIFF_SCHEDULE}"
        self._attr_icon = "mdi:calendar-clock"
        self._unsub_dispatcher = None
        self._unsub_time_interval = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        # Log entity_id to help users configure dashboards
        _LOGGER.info(
            "Tariff schedule sensor registered with entity_id: %s",
            self.entity_id
        )

        @callback
        def _handle_tariff_update():
            """Handle tariff update signal."""
            _LOGGER.debug("Tariff schedule sensor received update signal")
            self.async_write_ha_state()

        # Subscribe to tariff update signal
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id),
            _handle_tariff_update,
        )

        # Update every minute to reflect real-time price/period changes
        @callback
        def _periodic_update(_now=None):
            """Update sensor periodically to catch TOU period changes."""
            self.async_write_ha_state()

        self._unsub_time_interval = async_track_time_interval(
            self.hass,
            _periodic_update,
            timedelta(minutes=1),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
        if self._unsub_time_interval:
            self._unsub_time_interval()

    @property
    def native_value(self) -> Any:
        """Return the state - current tariff period and price (recalculated in real-time)."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if tariff_data:
            from . import get_current_price_from_tariff_schedule
            buy_price_cents, _, current_period = get_current_price_from_tariff_schedule(tariff_data)
            if current_period and current_period != "UNKNOWN":
                return f"{current_period} ({buy_price_cents:.1f}c/kWh)"
            # Fallback to last sync time
            return tariff_data.get("last_sync", "Unknown")
        return "Not synced"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the tariff schedule as attributes for visualization."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return {}

        # Support both Amber format (buy_prices with PERIOD_HH_MM keys)
        # and Tesla/Globird format (buy_rates with TOU period names)
        buy_prices = tariff_data.get("buy_prices", {})
        sell_prices = tariff_data.get("sell_prices", {})
        buy_rates = tariff_data.get("buy_rates", {})
        sell_rates = tariff_data.get("sell_rates", {})
        tou_periods = tariff_data.get("tou_periods", {})

        # Calculate real-time current price and period
        from . import get_current_price_from_tariff_schedule
        now = datetime.now()
        buy_price_cents, sell_price_cents, current_period = get_current_price_from_tariff_schedule(tariff_data)

        attributes = {
            "last_sync": tariff_data.get("last_sync"),
            "utility": tariff_data.get("utility"),
            "plan_name": tariff_data.get("plan_name"),
            "current_period": current_period,
            "current_season": tariff_data.get("current_season"),
            # Real-time prices (cents/kWh) - updated every minute
            "buy_price": round(buy_price_cents, 2),
            "sell_price": round(sell_price_cents, 2),
            # Current time marker for chart vertical line/tooltip
            "current_time": now.strftime("%H:%M"),
            "current_hour": now.hour,
            "current_minute": now.minute,
        }

        # Amber format: PERIOD_HH_MM keys with 30-min granularity
        if buy_prices:
            attributes["period_count"] = len(buy_prices)
            # Create a list format suitable for apexcharts-card visualization
            schedule_list = []
            for period_key in sorted(buy_prices.keys()):
                # Convert PERIOD_HH_MM to HH:MM
                parts = period_key.replace("PERIOD_", "").split("_")
                time_str = f"{parts[0]}:{parts[1]}"
                schedule_list.append({
                    "time": time_str,
                    "buy": buy_prices.get(period_key, 0),
                    "sell": sell_prices.get(period_key, 0),
                })
            attributes["schedule"] = schedule_list
            attributes["buy_prices"] = buy_prices
            attributes["sell_prices"] = sell_prices

        # Tesla/Globird format: TOU period names (ON_PEAK, OFF_PEAK, etc.)
        elif buy_rates:
            attributes["period_count"] = len(buy_rates)

            # Create TOU schedule list with period names and rates
            tou_schedule = []
            for period_name, rate in buy_rates.items():
                # Convert rate from $/kWh to c/kWh if needed
                buy_cents = rate * 100 if rate < 1 else rate
                sell_rate = sell_rates.get(period_name, 0)
                sell_cents = sell_rate * 100 if sell_rate < 1 else sell_rate

                # Get time windows for this period
                period_times = tou_periods.get(period_name, [])
                # Handle both list format and Tesla {"periods": [...]} format
                if isinstance(period_times, dict) and "periods" in period_times:
                    periods_list = period_times["periods"]
                elif isinstance(period_times, list):
                    periods_list = period_times
                else:
                    periods_list = []
                time_windows = []
                for window in periods_list:
                    from_hour = window.get("fromHour", 0)
                    to_hour = window.get("toHour", 24)
                    from_dow = window.get("fromDayOfWeek", 0)
                    to_dow = window.get("toDayOfWeek", 6)
                    time_windows.append({
                        "from_hour": from_hour,
                        "to_hour": to_hour,
                        "from_day": from_dow,
                        "to_day": to_dow,
                    })

                tou_schedule.append({
                    "period": period_name,
                    "buy": round(buy_cents, 2),
                    "sell": round(sell_cents, 2),
                    "windows": time_windows,
                })

            attributes["tou_schedule"] = tou_schedule
            attributes["buy_rates"] = {k: round(v * 100 if v < 1 else v, 2) for k, v in buy_rates.items()}
            attributes["sell_rates"] = {k: round(v * 100 if v < 1 else v, 2) for k, v in sell_rates.items()}

        return attributes


class TariffPriceSensor(SensorEntity):
    """Sensor for current price derived from TOU tariff schedule.

    This sensor provides current import/export prices for non-Amber users
    (e.g., Globird) by calculating prices from the stored tariff schedule.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sensor_type: str,
        name: str,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        self._attr_has_entity_name = True
        self._attr_name = name
        # Use same entity naming as AmberPriceSensor for mobile app compatibility
        # Creates: sensor.power_sync_current_import_price, sensor.power_sync_current_export_price
        self._attr_suggested_object_id = f"power_sync_{sensor_type}"
        self._attr_native_unit_of_measurement = f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:currency-usd" if "import" in sensor_type else "mdi:transmission-tower-export"
        self._unsub_dispatcher = None
        self._unsub_time_interval = None
        self._current_period = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        _LOGGER.info(
            "Tariff price sensor registered: %s (entity_id=%s)",
            self._sensor_type,
            self.entity_id
        )

        @callback
        def _handle_tariff_update():
            """Handle tariff update signal."""
            _LOGGER.debug("Tariff price sensor received update signal: %s", self._sensor_type)
            self.async_write_ha_state()

        # Subscribe to tariff update signal
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id),
            _handle_tariff_update,
        )

        # Also update every minute to catch TOU period changes
        @callback
        def _periodic_update(_now=None):
            """Update sensor periodically to catch TOU period changes."""
            self.async_write_ha_state()

        self._unsub_time_interval = async_track_time_interval(
            self.hass,
            _periodic_update,
            timedelta(minutes=1),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
        if self._unsub_time_interval:
            self._unsub_time_interval()

    @property
    def native_value(self) -> float | None:
        """Return the current price from tariff schedule."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return None

        # Import the function from __init__.py
        from . import get_current_price_from_tariff_schedule

        buy_price_cents, sell_price_cents, current_period = get_current_price_from_tariff_schedule(tariff_data)

        # Update current period for attributes
        self._current_period = current_period

        # Return appropriate price (in $/kWh)
        if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE:
            return round(buy_price_cents / 100, 4)  # Convert cents to dollars
        else:  # export price
            return round(sell_price_cents / 100, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return {}

        attributes = {
            "source": "tariff_schedule",
            "current_period": self._current_period,
            "utility": tariff_data.get("utility"),
            "plan_name": tariff_data.get("plan_name"),
        }

        if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE:
            attributes["price_spike"] = None  # No spike detection for tariff-based pricing
        else:
            attributes["channel_type"] = "feedIn"

        return attributes


SIGNAL_CURTAILMENT_UPDATED = "power_sync_curtailment_updated_{}"


class SolarCurtailmentSensor(SensorEntity):
    """Sensor for displaying solar curtailment status."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_SOLAR_CURTAILMENT}"
        self._attr_has_entity_name = True
        self._attr_name = "DC Solar Curtailment"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_SOLAR_CURTAILMENT}"
        self._attr_icon = "mdi:solar-power-variant"
        self._unsub_dispatcher = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        @callback
        def _handle_curtailment_update():
            """Handle curtailment update signal."""
            self.async_write_ha_state()

        # Subscribe to curtailment update signal
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            SIGNAL_CURTAILMENT_UPDATED.format(self._entry.entry_id),
            _handle_curtailment_update,
        )

        # Also subscribe to Amber coordinator updates so state updates when prices change
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        amber_coordinator = entry_data.get("amber_coordinator")
        if amber_coordinator:
            self._unsub_amber = amber_coordinator.async_add_listener(
                _handle_curtailment_update
            )
        else:
            self._unsub_amber = None

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
        if hasattr(self, '_unsub_amber') and self._unsub_amber:
            self._unsub_amber()

    def _get_feedin_price(self) -> float | None:
        """Get current feed-in price from Amber coordinator."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        amber_coordinator = entry_data.get("amber_coordinator")
        if not amber_coordinator or not amber_coordinator.data:
            return None

        # Look for feed-in price in current prices
        current_prices = amber_coordinator.data.get("current", [])
        for price in current_prices:
            if price.get("channelType") == "feedIn":
                return price.get("perKwh")
        return None

    def _is_curtailed(self) -> bool:
        """Determine if curtailment should be active based on current price."""
        feedin_price = self._get_feedin_price()

        if feedin_price is not None:
            # Export earnings = -feedin_price (Amber uses negative for feed-in costs)
            export_earnings = -feedin_price
            # Curtailment active when export earnings < 1c/kWh
            return export_earnings < 1.0

        # No price data, fall back to cached rule
        cached_rule = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("cached_export_rule")
        return cached_rule == "never"

    @property
    def native_value(self) -> str:
        """Return the state - whether curtailment is active."""
        if self._is_curtailed():
            return "Active"
        return "Normal"

    @property
    def icon(self) -> str:
        """Return the icon based on state."""
        if self._is_curtailed():
            return "mdi:solar-power-variant-outline"  # Different icon when curtailed
        return "mdi:solar-power-variant"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        cached_rule = entry_data.get("cached_export_rule")
        curtailment_enabled = self._entry.options.get(
            CONF_BATTERY_CURTAILMENT_ENABLED,
            self._entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
        )
        feedin_price = self._get_feedin_price()
        export_earnings = -feedin_price if feedin_price is not None else None

        return {
            "export_rule": cached_rule,
            "curtailment_enabled": curtailment_enabled,
            "feedin_price": feedin_price,
            "export_earnings": export_earnings,
            "description": "Export blocked due to negative feed-in price" if self._is_curtailed() else "Normal solar export allowed",
        }


class InverterStatusSensor(SensorEntity):
    """Sensor for displaying AC-coupled inverter status.

    Actively polls the inverter to get real-time status rather than
    relying only on cached state from curtailment operations.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_INVERTER_STATUS}"
        self._attr_has_entity_name = True
        self._attr_name = "Inverter Status"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_INVERTER_STATUS}"
        self._attr_icon = "mdi:solar-panel"
        self._unsub_dispatcher = None
        self._unsub_interval = None
        self._cached_state = None
        self._cached_attrs = {}
        self._controller = None  # Cached controller to preserve state (e.g., JWT token timestamp)

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        _LOGGER.info("InverterStatusSensor added to hass - setting up polling")

        @callback
        def _handle_curtailment_update():
            """Handle curtailment update signal (inverter state may change too)."""
            # Schedule a poll to get updated state
            _LOGGER.debug("Curtailment update signal received - scheduling inverter poll")
            self.hass.async_create_task(self._async_poll_inverter())

        # Subscribe to curtailment update signal
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            SIGNAL_CURTAILMENT_UPDATED.format(self._entry.entry_id),
            _handle_curtailment_update,
        )

        # Track consecutive offline/error states for backoff
        # Initialize BEFORE initial poll so exception handler can use it
        self._offline_count = 0
        self._max_offline_before_backoff = 3  # After 3 failed polls, reduce frequency

        # Do initial poll
        _LOGGER.info("Performing initial inverter poll")
        await self._async_poll_inverter()

        # Set up periodic polling (every 30 seconds for responsive load-following)
        async def _periodic_poll(_now=None):
            # If inverter has been offline for a while, reduce polling frequency
            if self._offline_count >= self._max_offline_before_backoff:
                # Only poll every 5 minutes when offline (every 10th call at 30s interval)
                if self._offline_count % 10 != 0:
                    self._offline_count += 1
                    _LOGGER.debug(f"Inverter offline - skipping poll (backoff, count={self._offline_count})")
                    return

            _LOGGER.debug("Periodic inverter poll triggered")
            await self._async_poll_inverter()

        self._unsub_interval = async_track_time_interval(
            self.hass,
            _periodic_poll,
            timedelta(seconds=30),
        )
        _LOGGER.info("Inverter polling scheduled every 30 seconds (with offline backoff)")

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
        if self._unsub_interval:
            self._unsub_interval()
        # Disconnect cached controller
        if self._controller:
            try:
                await self._controller.disconnect()
            except Exception:
                pass
            self._controller = None

    async def _async_poll_inverter(self) -> None:
        """Poll the inverter to get current status."""
        from .inverters import get_inverter_controller

        inverter_enabled = self._get_config_value(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        if not inverter_enabled:
            _LOGGER.debug("Inverter curtailment not enabled - skipping poll")
            self._cached_state = "disabled"
            self.async_write_ha_state()
            return

        inverter_brand = self._get_config_value(CONF_INVERTER_BRAND, "sungrow")
        inverter_host = self._get_config_value(CONF_INVERTER_HOST, "")
        inverter_port = self._get_config_value(CONF_INVERTER_PORT, 502)
        inverter_slave_id = self._get_config_value(CONF_INVERTER_SLAVE_ID, 1)
        inverter_model = self._get_config_value(CONF_INVERTER_MODEL)
        inverter_token = self._get_config_value(CONF_INVERTER_TOKEN)  # For Enphase JWT
        fronius_load_following = self._get_config_value(CONF_FRONIUS_LOAD_FOLLOWING, False)

        # Enphase Enlighten credentials for automatic JWT token refresh
        enphase_username = self._get_config_value(CONF_ENPHASE_USERNAME)
        enphase_password = self._get_config_value(CONF_ENPHASE_PASSWORD)
        enphase_serial = self._get_config_value(CONF_ENPHASE_SERIAL)
        enphase_is_installer = self._get_config_value(CONF_ENPHASE_IS_INSTALLER, False)
        enphase_normal_profile = self._get_config_value(CONF_ENPHASE_NORMAL_PROFILE)
        enphase_zero_export_profile = self._get_config_value(CONF_ENPHASE_ZERO_EXPORT_PROFILE)

        if not inverter_host:
            _LOGGER.debug("Inverter host not configured - skipping poll")
            self._cached_state = "not_configured"
            self.async_write_ha_state()
            return

        _LOGGER.debug(f"Polling inverter: {inverter_brand} at {inverter_host}:{inverter_port}")

        try:
            # Reuse cached controller if config matches (preserves JWT token state for Enphase)
            controller_key = f"{inverter_brand}:{inverter_host}:{inverter_port}"
            if self._controller and getattr(self._controller, '_cache_key', None) == controller_key:
                controller = self._controller
                _LOGGER.debug("Reusing cached inverter controller")
            else:
                # Config changed or no cached controller - create new one
                if self._controller:
                    try:
                        await self._controller.disconnect()
                    except Exception:
                        pass
                controller = get_inverter_controller(
                    brand=inverter_brand,
                    host=inverter_host,
                    port=inverter_port,
                    slave_id=inverter_slave_id,
                    model=inverter_model,
                    token=inverter_token,
                    load_following=fronius_load_following,
                    enphase_username=enphase_username,
                    enphase_password=enphase_password,
                    enphase_serial=enphase_serial,
                    enphase_normal_profile=enphase_normal_profile,
                    enphase_zero_export_profile=enphase_zero_export_profile,
                    enphase_is_installer=enphase_is_installer,
                )
                if controller:
                    controller._cache_key = controller_key
                    self._controller = controller
                    _LOGGER.debug("Created new inverter controller")

            if not controller:
                _LOGGER.warning(f"Failed to create controller for {inverter_brand}")
                self._cached_state = "error"
                self._cached_attrs = {"error": f"Unsupported brand: {inverter_brand}"}
                self.async_write_ha_state()
                return

            # Get status from inverter (don't disconnect - keep state for next poll)
            state = await controller.get_status()

            # Update cached state based on inverter response
            if state.status.value == "offline":
                self._cached_state = "offline"
                self._offline_count += 1
            elif state.status.value == "error":
                self._cached_state = "error"
                self._offline_count += 1
            elif state.is_curtailed:
                self._cached_state = "curtailed"
                self._offline_count = 0  # Reset backoff on successful poll
            else:
                # Check if curtailment logic has set state to "curtailed" (e.g., Fronius simple mode)
                # In simple mode, inverter uses soft export limit but doesn't report power_limit_enabled
                # So we trust the cached state from curtailment logic if it says "curtailed"
                entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
                cached_curtail_state = entry_data.get("inverter_last_state")
                if cached_curtail_state == "curtailed" and not fronius_load_following:
                    # Fronius simple mode: trust curtailment logic, not register values
                    _LOGGER.debug(
                        "Fronius simple mode: inverter not reporting curtailed but "
                        "curtailment logic says curtailed - keeping curtailed state"
                    )
                    self._cached_state = "curtailed"
                else:
                    self._cached_state = "running"
                self._offline_count = 0  # Reset backoff on successful poll

            # Store attributes from inverter
            self._cached_attrs = state.attributes or {}
            self._cached_attrs["power_limit_percent"] = state.power_limit_percent
            self._cached_attrs["power_output_w"] = state.power_output_w
            self._cached_attrs["brand"] = inverter_brand
            self._cached_attrs["last_poll"] = dt_util.now().isoformat()

            # Also update hass.data for consistency with curtailment logic
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            if entry_data:
                entry_data["inverter_last_state"] = self._cached_state
                entry_data["inverter_attributes"] = self._cached_attrs

            _LOGGER.info(f"Inverter poll: state={self._cached_state}, power={state.power_limit_percent}%")

        except Exception as e:
            _LOGGER.warning(f"Error polling inverter {inverter_host}: {e}")
            self._cached_state = "error"
            self._cached_attrs = {"error": str(e), "brand": inverter_brand}
            self._offline_count += 1  # Increment backoff counter on error

        self.async_write_ha_state()

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    @property
    def native_value(self) -> str:
        """Return the inverter status."""
        if self._cached_state == "curtailed":
            return "Curtailed"
        elif self._cached_state == "running":
            return "Normal"
        elif self._cached_state == "offline":
            return "Offline"
        elif self._cached_state == "disabled":
            return "Disabled"
        elif self._cached_state == "not_configured":
            return "Not Configured"
        elif self._cached_state == "error":
            return "Error"
        else:
            return "Unknown"

    @property
    def icon(self) -> str:
        """Return the icon based on state."""
        if self._cached_state == "curtailed":
            return "mdi:solar-panel-large"  # Darker icon when curtailed
        elif self._cached_state == "offline":
            return "mdi:solar-panel-variant-outline"
        elif self._cached_state in ("error", "not_configured", "disabled"):
            return "mdi:solar-panel-variant"
        return "mdi:solar-panel"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes including register data."""
        inverter_enabled = self._get_config_value(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
        inverter_brand = self._get_config_value(CONF_INVERTER_BRAND, "sungrow")
        inverter_host = self._get_config_value(CONF_INVERTER_HOST, "")
        inverter_model = self._get_config_value(CONF_INVERTER_MODEL, "")

        # Base attributes
        attrs = {
            "enabled": inverter_enabled,
            "brand": inverter_brand,
            "host": inverter_host,
            "model": inverter_model,
            "state": self._cached_state,
        }

        # Add description based on state
        if self._cached_state == "curtailed":
            attrs["description"] = "Inverter power limited to prevent negative export"
        elif self._cached_state == "running":
            attrs["description"] = "Inverter operating normally"
        elif self._cached_state == "offline":
            # Check if inverter is sleeping (stopped) vs actually unreachable
            running_state = self._cached_attrs.get("running_state", "")
            if running_state == "stopped":
                attrs["description"] = "Inverter sleeping (nighttime)"
            else:
                attrs["description"] = "Cannot reach inverter"
        elif self._cached_state == "error":
            attrs["description"] = "Inverter reported fault condition"
        elif self._cached_state == "disabled":
            attrs["description"] = "Inverter curtailment not enabled"
        elif self._cached_state == "not_configured":
            attrs["description"] = "Inverter host not configured"
        else:
            attrs["description"] = "Status unknown"

        # Add cached attributes from inverter polling
        attrs.update(self._cached_attrs)

        return attrs


class FlowPowerPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Flow Power electricity prices with PEA adjustment.

    Shows real-time import price calculated as:
    Final Rate = Base Rate + PEA
               = Base Rate + (wholesale - 9.7c)

    Updates every 5 minutes from the underlying price coordinator.
    Compatible with Home Assistant Energy Dashboard.
    """

    def __init__(
        self,
        coordinator,  # AmberPriceCoordinator or AEMOPriceCoordinator
        entry: ConfigEntry,
        sensor_type: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{sensor_type}"

        # Configure based on sensor type
        if sensor_type == SENSOR_TYPE_FLOW_POWER_PRICE:
            self._attr_name = "Flow Power Import Price"
            self._attr_icon = "mdi:lightning-bolt"
        else:
            self._attr_name = "Flow Power Export Price"
            self._attr_icon = "mdi:solar-power"

        self._attr_native_unit_of_measurement = f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_suggested_display_precision = 4

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _get_wholesale_price_cents(self) -> float | None:
        """Extract current wholesale price in cents from coordinator data."""
        if not self.coordinator.data:
            return None

        current_prices = self.coordinator.data.get("current", [])
        for price in current_prices:
            if price.get("channelType") == "general":
                # Amber data has wholesaleKWHPrice (c/kWh)
                wholesale = price.get("wholesaleKWHPrice")
                if wholesale is not None:
                    return wholesale
                # AEMO data uses perKwh directly (already in c/kWh)
                return price.get("perKwh", 0)
        return None

    def _is_happy_hour(self) -> bool:
        """Check if current time is within Flow Power Happy Hour (5:30pm-7:30pm)."""
        now = dt_util.now()
        hour = now.hour
        minute = now.minute

        # Happy Hour: 17:30 to 19:30
        current_period = f"PERIOD_{hour:02d}_{(minute // 30) * 30:02d}"
        return current_period in FLOW_POWER_HAPPY_HOUR_PERIODS

    def _get_twap_tracker(self):
        """Get the FlowPowerTWAPTracker from hass.data if available."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return domain_data.get("flow_power_twap_tracker")

    def _get_market_avg(self) -> float:
        """Get the market average (dynamic TWAP or fallback)."""
        tracker = self._get_twap_tracker()
        if tracker and tracker.twap is not None:
            return tracker.twap
        return FLOW_POWER_MARKET_AVG

    def _get_tariff_data(self) -> tuple[float | None, float | None]:
        """Get tariff_rate and avg_daily_tariff from hass.data if available."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tariff_rate = domain_data.get("fp_tariff_rate")
        avg_daily_tariff = domain_data.get("fp_avg_daily_tariff")
        return tariff_rate, avg_daily_tariff

    def _get_effective_twap(self) -> float:
        """Get effective TWAP: override if set, else dynamic, else fallback."""
        override = self._get_config_value(CONF_FP_TWAP_OVERRIDE)
        if override is not None and override != "":
            try:
                return float(override)
            except (ValueError, TypeError):
                pass
        return self._get_market_avg()

    def _calculate_import_price(self) -> float | None:
        """Calculate Flow Power import price with PEA in $/kWh.

        V2 formula (when tariff configured):
            PEA = GST*Spot + Tariff - GST*TWAP - AvgDailyTariff - BPEA
            Final = Base + PEA

        Legacy formula (no tariff configured):
            PEA = Spot - TWAP - BPEA
            Final = Base + PEA
        """
        wholesale_cents = self._get_wholesale_price_cents()
        if wholesale_cents is None:
            return None

        # Get config values
        pea_enabled = self._get_config_value(CONF_PEA_ENABLED, True)
        base_rate = self._get_config_value(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE)
        custom_pea = self._get_config_value(CONF_PEA_CUSTOM_VALUE)

        if pea_enabled:
            if custom_pea is not None and custom_pea != "":
                try:
                    pea = float(custom_pea)
                except (ValueError, TypeError):
                    pea = self._calculate_pea_auto(wholesale_cents)
            else:
                pea = self._calculate_pea_auto(wholesale_cents)

            # Final rate = base_rate + PEA (in c/kWh)
            final_cents = base_rate + pea
        else:
            # No PEA - just use base rate
            final_cents = base_rate

        # Convert to $/kWh and clamp to 0 (no negative prices)
        return max(0, final_cents / 100)

    def _calculate_pea_auto(self, wholesale_cents: float) -> float:
        """Calculate PEA automatically using v2 or legacy formula."""
        twap = self._get_effective_twap()
        tariff_rate, avg_daily_tariff = self._get_tariff_data()

        if tariff_rate is not None and avg_daily_tariff is not None:
            # V2 formula: GST*Spot + Tariff - GST*TWAP - AvgDailyTariff - BPEA
            return (
                FLOW_POWER_GST * wholesale_cents
                + tariff_rate
                - FLOW_POWER_GST * twap
                - avg_daily_tariff
                - FLOW_POWER_BENCHMARK
            )
        else:
            # Legacy formula: Spot - TWAP - BPEA
            return wholesale_cents - twap - FLOW_POWER_BENCHMARK

    def _calculate_export_price(self) -> float:
        """Calculate Flow Power export price in $/kWh."""
        state = self._get_config_value(CONF_FLOW_POWER_STATE, "QLD1")

        if self._is_happy_hour():
            # Happy Hour rate
            return FLOW_POWER_EXPORT_RATES.get(state, 0.45)
        else:
            # Outside Happy Hour - no export credit
            return 0.0

    @property
    def native_value(self) -> float | None:
        """Return the current price in $/kWh."""
        if self._sensor_type == SENSOR_TYPE_FLOW_POWER_PRICE:
            return self._calculate_import_price()
        else:
            return self._calculate_export_price()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        wholesale_cents = self._get_wholesale_price_cents()
        pea_enabled = self._get_config_value(CONF_PEA_ENABLED, True)
        base_rate = self._get_config_value(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE)
        custom_pea = self._get_config_value(CONF_PEA_CUSTOM_VALUE)
        state = self._get_config_value(CONF_FLOW_POWER_STATE, "QLD1")

        attributes = {
            "state": state,
            "pea_enabled": pea_enabled,
            "base_rate_cents": base_rate,
        }

        if self._sensor_type == SENSOR_TYPE_FLOW_POWER_PRICE:
            # Import price attributes
            tracker = self._get_twap_tracker()
            twap = self._get_effective_twap()
            attributes["twap_used"] = round(twap, 2)
            attributes["twap_source"] = "dynamic" if (tracker and not tracker.using_fallback) else "fallback"

            # TWAP override info
            override = self._get_config_value(CONF_FP_TWAP_OVERRIDE)
            if override is not None and override != "":
                attributes["twap_override"] = override

            # Tariff info
            tariff_rate, avg_daily_tariff = self._get_tariff_data()
            has_tariff = tariff_rate is not None and avg_daily_tariff is not None
            attributes["formula_version"] = "v2" if has_tariff else "v1"

            if has_tariff:
                attributes["network_cents"] = round(tariff_rate, 2)
                attributes["avg_daily_tariff"] = round(avg_daily_tariff, 2)
                attributes["gst_multiplier"] = FLOW_POWER_GST

            if wholesale_cents is not None:
                attributes["wholesale_cents"] = round(wholesale_cents, 2)

                if pea_enabled:
                    if custom_pea is not None and custom_pea != "":
                        try:
                            pea = float(custom_pea)
                        except (ValueError, TypeError):
                            pea = self._calculate_pea_auto(wholesale_cents)
                    else:
                        pea = self._calculate_pea_auto(wholesale_cents)

                    attributes["pea_cents"] = round(pea, 2)
                    attributes["final_rate_cents"] = round(base_rate + pea, 2)
                else:
                    attributes["pea_cents"] = 0
                    attributes["final_rate_cents"] = base_rate
        else:
            # Export price attributes
            attributes["is_happy_hour"] = self._is_happy_hour()
            attributes["happy_hour_rate"] = FLOW_POWER_EXPORT_RATES.get(state, 0.45)

        return attributes


class FlowPowerTWAPSensor(SensorEntity):
    """Sensor exposing the 30-day rolling TWAP used in PEA calculation.

    Shows the dynamic Time Weighted Average Price that replaces the
    hardcoded 8.0 c/kWh in the PEA formula. Falls back to 8.0 when
    insufficient data is available (< 12 samples).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the TWAP sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_FLOW_POWER_TWAP}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_FLOW_POWER_TWAP}"
        self._attr_name = "Flow Power TWAP 30-Day Average"
        self._attr_icon = "mdi:chart-line"
        self._attr_native_unit_of_measurement = "c/kWh"
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _get_tracker(self):
        """Get the FlowPowerTWAPTracker from hass.data."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return domain_data.get("flow_power_twap_tracker")

    @property
    def native_value(self) -> float | None:
        """Return the current TWAP value (or fallback)."""
        tracker = self._get_tracker()
        if tracker and tracker.twap is not None:
            return tracker.twap
        return FLOW_POWER_MARKET_AVG

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return TWAP tracking attributes."""
        tracker = self._get_tracker()
        override = self._get_config_value(CONF_FP_TWAP_OVERRIDE)

        attrs = {}
        if override is not None and override != "":
            attrs["twap_override"] = override

        if tracker:
            twap_value = tracker.twap if tracker.twap is not None else FLOW_POWER_MARKET_AVG
            attrs.update({
                "days_of_data": tracker.twap_days,
                "sample_count": tracker.sample_count,
                "using_fallback": tracker.using_fallback,
                "twap_dollars": round(twap_value / 100, 4),
            })
        else:
            attrs.update({
                "days_of_data": 0,
                "sample_count": 0,
                "using_fallback": True,
                "twap_dollars": round(FLOW_POWER_MARKET_AVG / 100, 4),
            })
        return attrs


class FlowPowerNetworkTariffSensor(SensorEntity):
    """Sensor showing the current TOU network tariff rate.

    Displays the network charge component from the aemo_to_tariff library
    for the configured DNSP and tariff code. Updates via hass.data populated
    by the coordinator/init.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the network tariff sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_NETWORK_TARIFF}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_NETWORK_TARIFF}"
        self._attr_name = "Flow Power Network Tariff"
        self._attr_icon = "mdi:transmission-tower"
        self._attr_native_unit_of_measurement = "c/kWh"
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    @property
    def native_value(self) -> float | None:
        """Return the current network tariff rate in c/kWh."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        rate = domain_data.get("fp_tariff_rate")
        if rate is not None:
            return round(rate, 2)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return tariff details."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        network = self._get_config_value(CONF_FP_NETWORK, "")
        tariff_code = self._get_config_value(CONF_FP_TARIFF_CODE, "")
        avg_daily = domain_data.get("fp_avg_daily_tariff")

        attrs = {
            "network": network,
            "tariff_code": tariff_code,
        }
        if avg_daily is not None:
            attrs["avg_daily_tariff"] = round(avg_daily, 2)
        return attrs


class FlowPowerAmberComparisonSensor(SensorEntity):
    """Sensor showing what the current price would be on Amber Electric.

    Calculates: 1.1 * Spot + Tariff + Markup
    Useful for comparing Flow Power vs Amber pricing.
    Only available when tariff is configured (needs network charge component).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        """Initialize the Amber comparison sensor."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_AMBER_COMPARISON}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_AMBER_COMPARISON}"
        self._attr_name = "Flow Power Amber Comparison"
        self._attr_icon = "mdi:compare-horizontal"
        self._attr_native_unit_of_measurement = f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_suggested_display_precision = 4

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _get_wholesale_price_cents(self) -> float | None:
        """Extract current wholesale price in cents from coordinator data."""
        if not self._coordinator or not self._coordinator.data:
            return None
        current_prices = self._coordinator.data.get("current", [])
        for price in current_prices:
            if price.get("channelType") == "general":
                wholesale = price.get("wholesaleKWHPrice")
                if wholesale is not None:
                    return wholesale
                return price.get("perKwh", 0)
        return None

    @property
    def native_value(self) -> float | None:
        """Return Amber-equivalent price in $/kWh."""
        wholesale_cents = self._get_wholesale_price_cents()
        if wholesale_cents is None:
            return None

        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tariff_rate = domain_data.get("fp_tariff_rate")
        if tariff_rate is None:
            return None

        state = self._get_config_value(CONF_FLOW_POWER_STATE, "QLD1")
        markup = self._get_config_value(CONF_FP_AMBER_MARKUP)
        if markup is None or markup == "":
            markup = DEFAULT_FP_AMBER_MARKUP.get(state, 4.0)
        else:
            try:
                markup = float(markup)
            except (ValueError, TypeError):
                markup = DEFAULT_FP_AMBER_MARKUP.get(state, 4.0)

        # Amber comparison: GST*Spot + Tariff + Markup (all in c/kWh)
        amber_cents = FLOW_POWER_GST * wholesale_cents + tariff_rate + markup
        return max(0, amber_cents / 100)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return breakdown of the Amber comparison price."""
        wholesale_cents = self._get_wholesale_price_cents()
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tariff_rate = domain_data.get("fp_tariff_rate")
        state = self._get_config_value(CONF_FLOW_POWER_STATE, "QLD1")

        markup = self._get_config_value(CONF_FP_AMBER_MARKUP)
        if markup is None or markup == "":
            markup = DEFAULT_FP_AMBER_MARKUP.get(state, 4.0)
        else:
            try:
                markup = float(markup)
            except (ValueError, TypeError):
                markup = DEFAULT_FP_AMBER_MARKUP.get(state, 4.0)

        attrs = {"markup_cents": markup}

        if wholesale_cents is not None:
            attrs["wholesale_cents"] = round(wholesale_cents, 2)
        if tariff_rate is not None:
            attrs["tariff_rate_cents"] = round(tariff_rate, 2)
        if wholesale_cents is not None and tariff_rate is not None:
            amber_cents = FLOW_POWER_GST * wholesale_cents + tariff_rate + markup
            attrs["price_cents"] = round(amber_cents, 2)

        return attrs


class BatteryHealthSensor(SensorEntity):
    """Sensor for battery health data from mobile app TEDAPI scans.

    This sensor receives data from the sync_battery_health service call
    made by the mobile app after scanning the Powerwall via TEDAPI.

    Shows battery health as a percentage of original capacity.
    Can be > 100% if batteries have more capacity than rated spec.
    Individual battery data is available in attributes.
    """

    _attr_has_entity_name = True
    _attr_name = "Battery Health"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_BATTERY_HEALTH}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_BATTERY_HEALTH}"

        # Battery health data (from service call)
        self._original_capacity_wh: float | None = None
        self._current_capacity_wh: float | None = None
        self._degradation_percent: float | None = None
        self._battery_count: int | None = None
        self._scanned_at: str | None = None
        self._individual_batteries: list | None = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to battery health updates when added to hass."""
        # Register for updates via dispatcher
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_battery_health_update_{self._entry.entry_id}",
                self._handle_battery_health_update,
            )
        )

        # Try to restore from storage
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        stored_health = domain_data.get("battery_health")
        if stored_health:
            self._original_capacity_wh = stored_health.get("original_capacity_wh")
            self._current_capacity_wh = stored_health.get("current_capacity_wh")
            self._degradation_percent = stored_health.get("degradation_percent")
            self._battery_count = stored_health.get("battery_count")
            self._scanned_at = stored_health.get("scanned_at")
            self._individual_batteries = stored_health.get("individual_batteries")
            _LOGGER.info(f"Restored battery health from storage: {self._calculate_health_percent()}% health")

    @callback
    def _handle_battery_health_update(self, data: dict[str, Any]) -> None:
        """Handle battery health update from service call."""
        self._original_capacity_wh = data.get("original_capacity_wh")
        self._current_capacity_wh = data.get("current_capacity_wh")
        self._degradation_percent = data.get("degradation_percent")
        self._battery_count = data.get("battery_count")
        self._scanned_at = data.get("scanned_at")
        self._individual_batteries = data.get("individual_batteries")

        _LOGGER.info(
            f"Battery health updated: {self._calculate_health_percent()}% health, "
            f"{self._current_capacity_wh}Wh / {self._original_capacity_wh}Wh"
        )
        self.async_write_ha_state()

    def _calculate_health_percent(self) -> float | None:
        """Calculate health as percentage of original capacity."""
        if self._current_capacity_wh is not None and self._original_capacity_wh is not None and self._original_capacity_wh > 0:
            return round((self._current_capacity_wh / self._original_capacity_wh) * 100, 1)
        return None

    @property
    def native_value(self) -> float | None:
        """Return the battery health as percentage of original capacity.

        Can be > 100% if batteries have more capacity than rated spec.
        """
        return self._calculate_health_percent()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attributes = {}

        if self._original_capacity_wh is not None:
            attributes["original_capacity_wh"] = self._original_capacity_wh
            attributes["original_capacity_kwh"] = round(self._original_capacity_wh / 1000, 2)

        if self._current_capacity_wh is not None:
            attributes["current_capacity_wh"] = self._current_capacity_wh
            attributes["current_capacity_kwh"] = round(self._current_capacity_wh / 1000, 2)

        if self._degradation_percent is not None:
            attributes["degradation_percent"] = self._degradation_percent

        if self._battery_count is not None:
            attributes["battery_count"] = self._battery_count

        if self._scanned_at is not None:
            attributes["last_scan"] = self._scanned_at

        # Add individual battery data if available
        if self._individual_batteries:
            for i, battery in enumerate(self._individual_batteries):
                prefix = f"battery_{i + 1}"
                if isinstance(battery, dict):
                    if battery.get("din"):
                        attributes[f"{prefix}_din"] = battery.get("din")
                    if battery.get("serialNumber"):
                        attributes[f"{prefix}_serial"] = battery.get("serialNumber")
                    if battery.get("nominalFullPackEnergyWh") is not None:
                        orig_wh = battery.get("nominalFullPackEnergyWh")
                        # Actual measured usable capacity of the battery
                        attributes[f"{prefix}_original_kwh"] = round(orig_wh / 1000, 2)
                    if battery.get("nominalEnergyRemainingWh") is not None:
                        curr_wh = battery.get("nominalEnergyRemainingWh")
                        # Current charge level (SOC)
                        attributes[f"{prefix}_current_kwh"] = round(curr_wh / 1000, 2)
                    # Calculate individual battery health as % of rated 13.5 kWh capacity
                    # nominalFullPackEnergyWh = actual measured capacity (can be > rated for new batteries)
                    # Health = actual_capacity / rated_capacity * 100
                    orig_wh = battery.get("nominalFullPackEnergyWh", 0)
                    if orig_wh > 0:
                        RATED_CAPACITY_WH = 13500  # 13.5 kWh per Powerwall
                        health = round((orig_wh / RATED_CAPACITY_WH) * 100, 1)
                        attributes[f"{prefix}_health_percent"] = health
                    if battery.get("isExpansion") is not None:
                        attributes[f"{prefix}_is_expansion"] = battery.get("isExpansion")

        attributes["source"] = "mobile_app_tedapi"

        return attributes


class BatteryModeSensor(SensorEntity):
    """Sensor for displaying battery mode (normal/force_charge/force_discharge).

    This sensor allows users to build automations that trigger when the battery
    mode changes, e.g., to exit force charge when electricity prices spike.

    States:
        - normal: Battery operating in normal self-consumption mode
        - force_charge: Battery is being force charged
        - force_discharge: Battery is being force discharged
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_BATTERY_MODE}"
        self._attr_has_entity_name = True
        self._attr_name = "Battery Mode"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_BATTERY_MODE}"
        self._attr_icon = "mdi:battery-sync"
        self._unsub_force_charge = None
        self._unsub_force_discharge = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        _LOGGER.info(
            "Battery mode sensor registered with entity_id: %s",
            self.entity_id
        )

        @callback
        def _handle_mode_update(data=None):
            """Handle battery mode update signal."""
            _LOGGER.debug("Battery mode sensor received update signal: %s", data)
            self.async_write_ha_state()

        # Subscribe to existing force charge/discharge signals
        self._unsub_force_charge = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_force_charge_state",
            _handle_mode_update,
        )
        self._unsub_force_discharge = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_force_discharge_state",
            _handle_mode_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_force_charge:
            self._unsub_force_charge()
        if self._unsub_force_discharge:
            self._unsub_force_discharge()

    def _get_current_mode(self) -> str:
        """Determine current battery mode from hass.data state."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

        # Check force charge state
        force_charge_state = entry_data.get("force_charge_state", {})
        if force_charge_state.get("active", False):
            return BATTERY_MODE_STATE_FORCE_CHARGE

        # Check force discharge state
        force_discharge_state = entry_data.get("force_discharge_state", {})
        if force_discharge_state.get("active", False):
            return BATTERY_MODE_STATE_FORCE_DISCHARGE

        # Default to normal
        return BATTERY_MODE_STATE_NORMAL

    @property
    def native_value(self) -> str:
        """Return the current battery mode."""
        return self._get_current_mode()

    @property
    def icon(self) -> str:
        """Return the icon based on current mode."""
        mode = self._get_current_mode()
        if mode == BATTERY_MODE_STATE_FORCE_CHARGE:
            return "mdi:battery-charging"
        elif mode == BATTERY_MODE_STATE_FORCE_DISCHARGE:
            return "mdi:battery-arrow-down"
        return "mdi:battery-sync"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        force_charge_state = entry_data.get("force_charge_state", {})
        force_discharge_state = entry_data.get("force_discharge_state", {})

        mode = self._get_current_mode()

        attributes = {
            "mode": mode,
        }

        # Add mode-specific attributes
        if mode == BATTERY_MODE_STATE_FORCE_CHARGE:
            attributes["description"] = "Battery is being force charged"
            if force_charge_state.get("expiry"):
                attributes["expires_at"] = force_charge_state["expiry"]
            if force_charge_state.get("duration_minutes"):
                attributes["duration_minutes"] = force_charge_state["duration_minutes"]
        elif mode == BATTERY_MODE_STATE_FORCE_DISCHARGE:
            attributes["description"] = "Battery is being force discharged"
            if force_discharge_state.get("expiry"):
                attributes["expires_at"] = force_discharge_state["expiry"]
            if force_discharge_state.get("duration_minutes"):
                attributes["duration_minutes"] = force_discharge_state["duration_minutes"]
        else:
            attributes["description"] = "Battery operating in normal self-consumption mode"

        return attributes


class AmberUsageSensor(SensorEntity):
    """Sensor for actual metered usage/cost data from Amber Usage API.

    Reads from AmberUsageCoordinator via hass.data. Refreshes hourly.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash-check"

    def __init__(
        self,
        entry: ConfigEntry,
        sensor_type: str,
        name: str,
        period: str,
        value_key: str,
    ) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._sensor_type = sensor_type
        self._period = period
        self._value_key = value_key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        self._attr_suggested_object_id = f"power_sync_{sensor_type}"
        self._unsub_interval: Any = None

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_added_to_hass(self) -> None:
        """Start periodic updates when added to HA."""
        @callback
        def _periodic_update(_now=None) -> None:
            self.async_write_ha_state()

        self._unsub_interval = async_track_time_interval(
            self.hass,
            _periodic_update,
            timedelta(hours=1),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the update timer."""
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None

    def _get_usage_coordinator(self):
        """Get the AmberUsageCoordinator from hass.data."""
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("amber_usage_coordinator")
        )

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        coord = self._get_usage_coordinator()
        if not coord:
            return None
        summary = coord.get_savings_summary(self._period)
        val = summary.get(self._value_key)
        if val is None:
            return None
        return round(val, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        coord = self._get_usage_coordinator()
        if not coord:
            return {"source": "amber_usage_api"}
        summary = coord.get_savings_summary(self._period)
        attrs = {
            "import_kwh": summary.get("import_kwh"),
            "export_kwh": summary.get("export_kwh"),
            "supply_charge": summary.get("supply_charge"),
            "quality": summary.get("quality"),
            "days_count": summary.get("days_count"),
            "source": "amber_usage_api",
            "last_fetch": coord.last_fetch_iso,
        }
        if self._value_key == "savings":
            attrs["baseline_cost"] = summary.get("baseline_cost")
            attrs["net_cost"] = summary.get("net_cost")
        return attrs
