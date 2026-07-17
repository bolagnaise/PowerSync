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
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from datetime import timedelta

from .const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    DOMAIN,
    SENSOR_TYPE_CURRENT_PRICE,
    SENSOR_TYPE_CURRENT_IMPORT_PRICE,
    SENSOR_TYPE_CURRENT_EXPORT_PRICE,
    SENSOR_TYPE_SOLAR_POWER,
    SENSOR_TYPE_GRID_POWER,
    SENSOR_TYPE_GRID_STATUS,
    SENSOR_TYPE_BATTERY_POWER,
    SENSOR_TYPE_HOME_LOAD,
    SENSOR_TYPE_BATTERY_LEVEL,
    SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER,
    SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER,
    SENSOR_TYPE_DAILY_SOLAR_ENERGY,
    SENSOR_TYPE_DAILY_GRID_IMPORT,
    SENSOR_TYPE_DAILY_GRID_EXPORT,
    SENSOR_TYPE_DAILY_BATTERY_CHARGE,
    SENSOR_TYPE_DAILY_BATTERY_DISCHARGE,
    SENSOR_TYPE_DAILY_LOAD,
    SENSOR_TYPE_DAILY_IMPORT_COST,
    SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
    SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
    SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
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
    SENSOR_TYPE_SAVING_SESSION_ACTIVE,
    SENSOR_TYPE_NEXT_SAVING_SESSION,
    SENSOR_TYPE_SAVING_SESSION_RATE,
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
    SENSOR_TYPE_FIRMWARE,
    SENSOR_TYPE_LIFETIME_SOLAR,
    SENSOR_TYPE_LIFETIME_GRID_IMPORT,
    SENSOR_TYPE_LIFETIME_GRID_EXPORT,
    SENSOR_TYPE_LIFETIME_BATTERY_CHARGED,
    SENSOR_TYPE_LIFETIME_BATTERY_DISCHARGED,
    SENSOR_TYPE_LIFETIME_HOME_CONSUMPTION,
    SENSOR_TYPE_BACKUP_TIME_REMAINING,
    SENSOR_TYPE_TOTAL_PACK_ENERGY,
    SENSOR_TYPE_ENERGY_LEFT,
    SENSOR_TYPE_GRID_SERVICES_POWER,
    SENSOR_TYPE_INVERTER_STATUS,
    SENSOR_TYPE_BATTERY_MODE,
    SENSOR_TYPE_PV1_POWER,
    SENSOR_TYPE_PV2_POWER,
    SENSOR_TYPE_PV3_POWER,
    SENSOR_TYPE_PV4_POWER,
    SENSOR_TYPE_PV5_POWER,
    SENSOR_TYPE_PV6_POWER,
    SENSOR_TYPE_CT2_POWER,
    SENSOR_TYPE_WORK_MODE,
    SENSOR_TYPE_MIN_SOC,
    SENSOR_TYPE_DAILY_BATTERY_CHARGE_FOXESS,
    SENSOR_TYPE_DAILY_BATTERY_DISCHARGE_FOXESS,
    SENSOR_TYPE_BATTERY_LEVEL_1,
    SENSOR_TYPE_BATTERY_LEVEL_2,
    SENSOR_TYPE_OPTIMIZATION_STATUS,
    SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION,
    SENSOR_TYPE_OPTIMIZATION_FORCE_CHARGE_WINDOWS,
    SENSOR_TYPE_OPTIMIZATION_FORCE_DISCHARGE_WINDOWS,
    SENSOR_TYPE_NEOVOLT_SURPLUS_BALANCER,
    SENSOR_TYPE_LP_SOLAR_FORECAST,
    SENSOR_TYPE_LP_LOAD_FORECAST,
    SENSOR_TYPE_LP_BATTERY_POWER_FORECAST,
    SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST,
    SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST,
    SENSOR_TYPE_LOAD_FORECAST_TODAY_REMAINING,
    SENSOR_TYPE_LOAD_FORECAST_TOMORROW,
    SENSOR_TYPE_AMBER_USAGE_YESTERDAY_COST,
    SENSOR_TYPE_AMBER_USAGE_YESTERDAY_SAVINGS,
    SENSOR_TYPE_AMBER_USAGE_MONTH_COST,
    SENSOR_TYPE_AMBER_USAGE_MONTH_SAVINGS,
    SENSOR_TYPE_EV_POWER,
    SENSOR_TYPE_EV_BATTERY_LEVEL,
    SENSOR_TYPE_PV_DC_POWER,
    SENSOR_TYPE_PV_AC_POWER,
    CONF_EV_CHARGING_ENABLED,
    CONF_GENERIC_CHARGER_ENABLED,
    CONF_SIGENERGY_CHARGER_ENABLED,
    CONF_BATTERY_SYSTEM,
    BATTERY_SYSTEM_SUNGROW,
    BATTERY_MODE_STATE_NORMAL,
    BATTERY_MODE_STATE_FORCE_CHARGE,
    BATTERY_MODE_STATE_FORCE_DISCHARGE,
    BATTERY_MODE_STATE_HOLD_SOC,
    BATTERY_MODE_STATE_SELF_CONSUMPTION,
    INVERTER_CONTROL_MODE_NORMAL,
    INVERTER_CONTROL_MODE_LOAD_FOLLOWING,
    INVERTER_CONTROL_MODE_SHUTDOWN,
    INVERTER_CONTROL_MODE_CURTAILED,
    INVERTER_CONTROL_MODES,
    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_MODEL,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SLAVE_ID,
    CONF_INVERTER_TOKEN,
    CONF_SUNGROW_HOST,
    CONF_SUNGROW_PORT,
    CONF_SUNGROW_SLAVE_ID,
    DEFAULT_SUNGROW_PORT,
    DEFAULT_SUNGROW_SLAVE_ID,
    DEFAULT_INVERTER_PORT,
    DEFAULT_INVERTER_SLAVE_ID,
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
    CONF_FLOWPOWER_API_KEY,
    CONF_FLOWPOWER_NETWORK_TARIFF,
    CONF_PEA_ENABLED,
    CONF_FLOW_POWER_BASE_RATE,
    CONF_FLOW_POWER_EXPORT_RATE,
    CONF_PEA_CUSTOM_VALUE,
    FLOW_POWER_MARKET_AVG,
    FLOW_POWER_DEFAULT_BASE_RATE,
    FLOW_POWER_EXPORT_RATES,
    FLOW_POWER_HAPPY_HOUR_PERIODS,
    ATTR_PRICE_SPIKE,
    ATTR_WHOLESALE_PRICE,
    ATTR_NETWORK_PRICE,
    ATTR_AEMO_REGION,
    ATTR_AEMO_THRESHOLD,
    ATTR_SPIKE_START_TIME,
    family_device_info,
    provider_pricing_device_info,
    powerwall_device_info,
    SENSOR_KEY_TO_FAMILY,
    SENSOR_FAMILY_LP_OPTIMIZER,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_SOLAR_INVERTER,
    SENSOR_FAMILY_GRID_HOME,
    SENSOR_FAMILY_PRICING,
    SENSOR_FAMILY_FLOW_POWER,
    SENSOR_FAMILY_AEMO,
    SENSOR_FAMILY_EV_CHARGING,
    SENSOR_FAMILY_OCTOPUS,
    TESLA_INTEGRATIONS,
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
)
from .coordinator import (
    AmberPriceCoordinator,
    LocalvoltsPriceCoordinator,
    OctopusPriceCoordinator,
    TeslaEnergyCoordinator,
    DemandChargeCoordinator,
    SolcastForecastCoordinator,
)
from .currency import (
    currency_for_entry,
    currency_metadata,
    major_price_unit,
    minor_price_unit,
    money_unit,
    normalize_currency,
)
from .flow_power_pricing import (
    FlowPowerPricingContext,
    calculate_flow_power_pea,
    resolve_flow_power_pricing_context,
)
from .network_envelope import HANetworkEnvelopeManager, NetworkExportEnvelope
from . import get_current_price_from_tariff_schedule

_LOGGER = logging.getLogger(__name__)


def _has_tesla_ev_device(hass: HomeAssistant) -> bool:
    """Return true when a Tesla/Teslemetry vehicle device is registered."""
    try:
        device_registry = dr.async_get(hass)
    except Exception:
        return False

    for device in device_registry.devices.values():
        for identifier_entry in device.identifiers:
            if not isinstance(identifier_entry, (tuple, list)) or len(identifier_entry) < 2:
                continue
            domain, identifier = identifier_entry[0], identifier_entry[1]
            if domain not in TESLA_INTEGRATIONS:
                continue
            identifier_text = str(identifier)
            if len(identifier_text) == 17 and not identifier_text.isdigit():
                return True
    return False


def _has_solaredge_ev_power(hass: HomeAssistant) -> bool:
    """Return true when the SolarEdge EV charger integration exposes power."""
    try:
        states = hass.states.async_all("sensor")
    except TypeError:
        states = hass.states.async_all()
    except Exception:
        return False

    for state in states:
        entity_id = str(getattr(state, "entity_id", "")).lower()
        if not entity_id.startswith("sensor."):
            continue
        body = entity_id.split(".", 1)[-1]
        if body not in {"ev_charger_power", "ev_charging_power"} and not body.endswith(
            ("_ev_charger_power", "_ev_charging_power")
        ):
            continue

        attrs = getattr(state, "attributes", {}) or {}
        friendly_name = str(attrs.get("friendly_name", "")).lower()
        if (
            body in {"ev_charger_power", "ev_charging_power"}
            or "solaredge" in body
            or "solar edge" in friendly_name
            or "solaredge" in friendly_name
        ):
            return True
    return False


def _sungrow_ac_inverter_power_kw(entry: ConfigEntry, hass: HomeAssistant) -> float:
    """Return separately configured Sungrow SG inverter output in kW."""
    if entry.data.get(CONF_BATTERY_SYSTEM) != BATTERY_SYSTEM_SUNGROW:
        return 0.0
    if _sungrow_ac_inverter_matches_battery(entry):
        return 0.0
    if not entry.options.get(
        CONF_AC_INVERTER_CURTAILMENT_ENABLED,
        entry.data.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False),
    ):
        return 0.0
    if (
        entry.options.get(CONF_INVERTER_BRAND, entry.data.get(CONF_INVERTER_BRAND))
        != "sungrow"
    ):
        return 0.0

    attrs = (
        hass.data.get(DOMAIN, {})
        .get(entry.entry_id, {})
        .get("inverter_attributes")
        or {}
    )
    power_w = attrs.get("power_output_w")
    if power_w is None:
        power_w = attrs.get("dc_power")
    try:
        return max(0.0, float(power_w or 0) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def _sungrow_ac_inverter_matches_battery(entry: ConfigEntry) -> bool:
    """Return true when AC inverter config points at the Sungrow battery endpoint."""
    if entry.data.get(CONF_BATTERY_SYSTEM) != BATTERY_SYSTEM_SUNGROW:
        return False
    if (
        entry.options.get(CONF_INVERTER_BRAND, entry.data.get(CONF_INVERTER_BRAND))
        != "sungrow"
    ):
        return False

    inverter_host = entry.options.get(
        CONF_INVERTER_HOST, entry.data.get(CONF_INVERTER_HOST, "")
    )
    inverter_port = entry.options.get(
        CONF_INVERTER_PORT,
        entry.data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT),
    )
    inverter_slave_id = entry.options.get(
        CONF_INVERTER_SLAVE_ID,
        entry.data.get(CONF_INVERTER_SLAVE_ID, DEFAULT_INVERTER_SLAVE_ID),
    )
    return (
        inverter_host == entry.data.get(CONF_SUNGROW_HOST, "")
        and inverter_port == entry.data.get(CONF_SUNGROW_PORT, DEFAULT_SUNGROW_PORT)
        and inverter_slave_id
        == entry.data.get(CONF_SUNGROW_SLAVE_ID, DEFAULT_SUNGROW_SLAVE_ID)
    )


def _home_load_power_kw(data: Any) -> float | None:
    """Return Home Load in kW, clamped to its physical lower bound."""
    if not data:
        return None
    value = data.get("load_power")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


# Large rolling prediction arrays exposed as sensor attributes (≈48h @ 5min /
# price-period series). They exceed Home Assistant's 16 KB per-state recorder
# attribute cap and are regenerated each cycle (not history), so the recorder
# is told to skip them via Entity._unrecorded_attributes while the scalar state
# is still recorded. Keys cover the LP optimizer, Solcast and Amber forecast
# sensors (different sensors use different key names for their array).
_FORECAST_ARRAY_ATTRS = frozenset({
    "forecast",
    "forecast_values_kw",
    "charge_values_kw",
    "discharge_values_kw",
    "home_consumption_values_kw",
    "export_values_kw",
    "power_values_kw",
    "price_values",
    "hourly_forecast",
    "forecast_periods",
})


@dataclass
class PowerSyncSensorEntityDescription(SensorEntityDescription):
    """Describes PowerSync sensor entity."""

    value_fn: Callable[[Any], Any] | None = None
    attr_fn: Callable[[Any], dict[str, Any]] | None = None
    # Optional override that pulls the sensor onto a separate HA device.
    # Currently only "powerwall" is recognised — anything else falls back to
    # the default family_device_info routing so existing sensors are unaffected.
    device_section: str | None = None
    # Currency unit kind. "money" is a pure monetary total, "major_rate" is
    # ISO/kWh, "market_rate" is ISO/MWh, and "minor_rate" is p/ct/c per kWh.
    currency_unit: str | None = None
    currency_attrs: bool = False


RATE_CURRENCY_UNITS = {"major_rate", "market_rate", "minor_rate"}
_RESTORED_NUMERIC_SENSOR_KEYS = {
    SENSOR_TYPE_CURRENT_IMPORT_PRICE,
    SENSOR_TYPE_CURRENT_EXPORT_PRICE,
    SENSOR_TYPE_DAILY_IMPORT_COST,
    SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
    SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
    SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
}


def _restored_numeric_state_value(state: Any) -> float | None:
    """Return a numeric value from a restored HA state, if it is usable."""
    raw = getattr(state, "state", None)
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class RestoredNumericStateMixin(RestoreEntity):
    """Restore the last valid numeric state while startup data is unavailable."""

    _restored_native_value: float | None = None

    async def _async_restore_numeric_state(self) -> None:
        last_state = await self.async_get_last_state()
        self._restored_native_value = _restored_numeric_state_value(last_state)

    def _restored_numeric_value(self, sensor_key: str) -> float | None:
        if sensor_key not in _RESTORED_NUMERIC_SENSOR_KEYS:
            return None
        return self._restored_native_value


def _currency_unit_for_kind(kind: str | None, currency: str) -> str | None:
    """Return a unit string for a PowerSync currency unit kind."""
    if kind == "money":
        return money_unit(currency)
    if kind == "major_rate":
        return major_price_unit(currency)
    if kind == "market_rate":
        return major_price_unit(currency, "MWh")
    if kind == "minor_rate":
        return minor_price_unit(currency)
    return None


def _entity_currency(entity: Any, tariff_data: dict[str, Any] | None = None) -> str:
    """Return the currency for an entity, optionally preferring tariff metadata."""
    if tariff_data:
        tariff_currency = normalize_currency(tariff_data.get("currency"), "")
        if tariff_currency:
            return tariff_currency
    return currency_for_entry(getattr(entity, "_entry", None), getattr(entity, "hass", None))


def _entity_currency_attrs(
    entity: Any,
    attrs: dict[str, Any] | None,
    tariff_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge currency metadata into attributes for currency-aware sensors."""
    base = dict(attrs or {})
    description = getattr(entity, "entity_description", None)
    kind = getattr(description, "currency_unit", None) or getattr(entity, "_attr_currency_unit", None)
    include = (
        getattr(description, "currency_attrs", False)
        or getattr(entity, "_attr_currency_attrs", False)
    )
    if kind and include:
        base.update(currency_metadata(_entity_currency(entity, tariff_data)))
    return base


class PowerSyncCurrencyMixin:
    """Mixin for dynamic currency units on PowerSync sensors."""

    def _currency_source_data(self) -> dict[str, Any] | None:
        """Return optional tariff data that should override entry currency."""
        return None

    @property
    def _currency_unit_kind(self) -> str | None:
        description = getattr(self, "entity_description", None)
        return getattr(description, "currency_unit", None) or getattr(self, "_attr_currency_unit", None)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return a provider/HA currency-aware unit when requested."""
        unit = _currency_unit_for_kind(
            self._currency_unit_kind,
            _entity_currency(self, self._currency_source_data()),
        )
        if unit:
            return unit
        description = getattr(self, "entity_description", None)
        return (
            getattr(self, "_attr_native_unit_of_measurement", None)
            or getattr(description, "native_unit_of_measurement", None)
        )

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Avoid monetary device class for price-rate sensors."""
        if self._currency_unit_kind in RATE_CURRENCY_UNITS:
            return None
        description = getattr(self, "entity_description", None)
        return (
            getattr(self, "_attr_device_class", None)
            or getattr(description, "device_class", None)
        )


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
        currency_unit="major_rate",
        currency_attrs=True,
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
        currency_unit="major_rate",
        currency_attrs=True,
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
        key=SENSOR_TYPE_GRID_STATUS,
        name="Grid Status",
        icon="mdi:transmission-tower",
        value_fn=lambda data: data.get("grid_status", "Active") if data else None,
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
        value_fn=_home_load_power_kw,
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
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_SOLAR_ENERGY,
        name="Daily Solar Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:solar-power",
        value_fn=lambda data: data.get("energy_summary", {}).get("pv_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_GRID_IMPORT,
        name="Daily Grid Import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: data.get("energy_summary", {}).get("grid_import_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_GRID_EXPORT,
        name="Daily Grid Export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: data.get("energy_summary", {}).get("grid_export_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_CHARGE,
        name="Daily Battery Charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-charging",
        value_fn=lambda data: data.get("energy_summary", {}).get("charge_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_DISCHARGE,
        name="Daily Battery Discharge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: data.get("energy_summary", {}).get("discharge_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_LOAD,
        name="Daily Home Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: data.get("energy_summary", {}).get("load_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_IMPORT_COST,
        name="Daily Import Cost",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-minus",
        value_fn=lambda data: data.get("energy_summary", {}).get("import_cost_today") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
        name="Daily Export Earnings",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value_fn=lambda data: data.get("energy_summary", {}).get("export_earnings_today") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
        name="Average Cost per kWh Today",
        currency_unit="major_rate",
        currency_attrs=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.get("energy_summary", {}).get("avg_cost_per_kwh_today") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
        name="Average Cost per kWh Month to Date",
        currency_unit="major_rate",
        currency_attrs=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:calendar-month",
        value_fn=lambda data: data.get("energy_summary", {}).get("avg_cost_per_kwh_mtd") if data else None,
    ),
)

FOXESS_PV_POWER_SENSOR_TYPES = (
    SENSOR_TYPE_PV1_POWER,
    SENSOR_TYPE_PV2_POWER,
    SENSOR_TYPE_PV3_POWER,
    SENSOR_TYPE_PV4_POWER,
    SENSOR_TYPE_PV5_POWER,
    SENSOR_TYPE_PV6_POWER,
)

FOXESS_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    *(
        PowerSyncSensorEntityDescription(
            key=sensor_type,
            name=f"PV{idx} Power",
            native_unit_of_measurement=UnitOfPower.KILO_WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            icon="mdi:solar-panel",
            value_fn=lambda data, key=sensor_type: data.get(key) if data else None,
        )
        for idx, sensor_type in enumerate(FOXESS_PV_POWER_SENSOR_TYPES, start=1)
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
        name="FoxESS Daily Battery Charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-charging",
        value_fn=lambda data: data.get("energy_summary", {}).get("charge_today_kwh") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_DISCHARGE_FOXESS,
        name="FoxESS Daily Battery Discharge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: data.get("energy_summary", {}).get("discharge_today_kwh") if data else None,
    ),
)

SOLAX_PV_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key="pv1_power",
        name="PV1 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: data.get("pv1_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv1_voltage",
        name="PV1 Voltage",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:sine-wave",
        value_fn=lambda data: data.get("pv1_voltage") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv1_current",
        name="PV1 Current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:current-dc",
        value_fn=lambda data: data.get("pv1_current") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv2_power",
        name="PV2 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: data.get("pv2_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv2_voltage",
        name="PV2 Voltage",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:sine-wave",
        value_fn=lambda data: data.get("pv2_voltage") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv2_current",
        name="PV2 Current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:current-dc",
        value_fn=lambda data: data.get("pv2_current") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv3_power",
        name="PV3 Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: data.get("pv3_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv3_voltage",
        name="PV3 Voltage",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:sine-wave",
        value_fn=lambda data: data.get("pv3_voltage") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="pv3_current",
        name="PV3 Current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:current-dc",
        value_fn=lambda data: data.get("pv3_current") if data else None,
    ),
)

TESLA_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_FIRMWARE,
        name="Firmware",
        icon="mdi:chip",
        value_fn=lambda data: data.get("firmware") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_TOTAL_PACK_ENERGY,
        name="Battery Pack Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-high",
        value_fn=lambda data: data.get("total_pack_energy_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_ENERGY_LEFT,
        name="Battery Energy Left",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-50",
        value_fn=lambda data: data.get("energy_left_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BACKUP_TIME_REMAINING,
        name="Backup Time Remaining",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:timer-sand",
        value_fn=lambda data: data.get("backup_time_remaining_hours") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_GRID_SERVICES_POWER,
        name="Grid Services Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:transmission-tower",
        value_fn=lambda data: data.get("grid_services_power_kw") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_SOLAR,
        name="Lifetime Solar Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_solar_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_GRID_IMPORT,
        name="Lifetime Grid Import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_grid_import_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_GRID_EXPORT,
        name="Lifetime Grid Export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_grid_export_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_BATTERY_CHARGED,
        name="Lifetime Battery Charged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:battery-charging-100",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_battery_charged_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_BATTERY_DISCHARGED,
        name="Lifetime Battery Discharged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_battery_discharged_kwh") if data else None,
        device_section="powerwall",
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_HOME_CONSUMPTION,
        name="Lifetime Home Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: (data.get("lifetime_totals") or {}).get("lifetime_home_kwh") if data else None,
        device_section="powerwall",
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

EV_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_EV_POWER,
        name="EV Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:ev-station",
        value_fn=lambda data: data.get("ev_power_kw") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_EV_BATTERY_LEVEL,
        name="EV Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:car-electric",
        value_fn=lambda data: data.get("ev_soc") if data else None,
    ),
)

SIGENERGY_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_PV_DC_POWER,
        name="PV DC Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel",
        value_fn=lambda data: (data.get("solar_power", 0) - data.get("third_party_pv_power_kw", 0)) if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_PV_AC_POWER,
        name="PV AC Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:solar-panel-large",
        value_fn=lambda data: data.get("third_party_pv_power_kw") if data else None,
    ),
)

NEOVOLT_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_NEOVOLT_SURPLUS_BALANCER,
        name="NeoVolt Surplus Balancer",
        icon="mdi:battery-sync",
        value_fn=lambda data: (data.get("neovolt_surplus_balancer") or {}).get("status") if data else None,
        attr_fn=lambda data: dict(data.get("neovolt_surplus_balancer") or {}) if data else {},
    ),
)

# Shared sensors exposing BMS/inverter-reported power ceilings. Coordinators
# populate the same battery_max_* fields even when the brand-specific source
# differs (for example AlphaESS BMS registers vs FoxESS nominal inverter power).
BMS_POWER_LIMIT_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER,
        name="Battery Max Charge Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-plus",
        value_fn=lambda data: data.get("battery_max_charge_power") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER,
        name="Battery Max Discharge Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-minus",
        value_fn=lambda data: data.get("battery_max_discharge_power") if data else None,
    ),
)

ESY_SUNHOME_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key="esy_inverter_temperature",
        name="Inverter Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:thermometer",
        value_fn=lambda data: data.get("inverter_temperature") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="esy_battery_status",
        name="Battery Status",
        icon="mdi:battery-heart",
        value_fn=lambda data: data.get("battery_status_text") if data else None,
    ),
    PowerSyncSensorEntityDescription(
        key="esy_battery_soh",
        name="Battery Health (SOH)",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-check",
        suggested_display_precision=0,
        value_fn=lambda data: data.get("battery_soh") if data else None,
    ),
)


def _parse_optimizer_time(value: Any) -> datetime | None:
    """Parse an optimizer ISO timestamp."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_optimizer_window(start: datetime | None, end: datetime | None) -> str:
    """Format a compact time range for HA state display."""
    if not start or not end:
        return "unknown"

    start_local = dt_util.as_local(start)
    end_local = dt_util.as_local(end)
    prefix = "" if start_local.date() == dt_util.now().date() else f"{start_local:%a} "
    return f"{prefix}{start_local:%H:%M}-{end_local:%H:%M}"


def _optimizer_action_names(action: str | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize one or more optimizer action names."""
    return (action,) if isinstance(action, str) else action


def _future_optimizer_action_windows(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return future consolidated optimizer windows for an action."""
    if not data:
        return []

    action_names = _optimizer_action_names(action)
    windows: list[dict[str, Any]] = []
    now = dt_util.now()
    for item in data.get("next_actions") or []:
        item_action = item.get("action")
        if item_action not in action_names:
            continue

        start = _parse_optimizer_time(item.get("timestamp"))
        end = _parse_optimizer_time(item.get("end_time"))
        if end and end <= now:
            continue

        planned_power_w = item.get("power_w")
        display_power_w = planned_power_w
        if start and end and start <= now < end:
            effective_action = (
                data.get("effective_current_action") or data.get("current_action")
            )
            if item_action == effective_action:
                try:
                    current_power_w = float(data.get("current_power_w") or 0)
                except (TypeError, ValueError):
                    current_power_w = 0
                if current_power_w > 0:
                    display_power_w = current_power_w

        window: dict[str, Any] = {
            "action": item_action,
            "start_time": item.get("timestamp"),
            "end_time": item.get("end_time"),
            "label": _format_optimizer_window(start, end),
            "power_w": display_power_w,
            "planned_power_w": planned_power_w,
            "soc": item.get("soc"),
        }

        if start and end:
            window["duration_minutes"] = round((end - start).total_seconds() / 60)

        windows.append(window)

    return windows


def _optimizer_window_state(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> str:
    """Return a short sensor state for upcoming optimizer windows."""
    windows = _future_optimizer_action_windows(data, action)
    if not windows:
        return "none"

    labels = [w["label"] for w in windows if w.get("label")]
    state = ", ".join(labels)
    if len(state) <= 255:
        return state

    return f"{labels[0]} (+{len(labels) - 1} more)"


def _optimizer_window_attributes(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> dict[str, Any]:
    """Return attributes for upcoming optimizer windows."""
    action_names = _optimizer_action_names(action)
    windows = _future_optimizer_action_windows(data, action)
    total_minutes = sum(w.get("duration_minutes", 0) or 0 for w in windows)
    attrs: dict[str, Any] = {
        "actions": list(action_names),
        "count": len(windows),
        "total_minutes": total_minutes,
        "windows": windows,
    }
    if windows:
        attrs["next_start"] = windows[0].get("start_time")
        attrs["next_end"] = windows[0].get("end_time")
        attrs["next_label"] = windows[0].get("label")
        attrs["next_power_w"] = windows[0].get("power_w")
    return attrs


OPTIMIZER_ACTION_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_STATUS,
        name="Current Action",
        icon="mdi:battery-sync",
        value_fn=lambda data: data.get("current_action") if data else None,
        attr_fn=lambda data: {
            "power_w": data.get("current_power_w"),
            "planned_action": data.get("planned_current_action"),
            "planned_power_w": data.get("planned_current_power_w"),
            "effective_action": data.get("effective_current_action"),
            "actual_battery_power_w": data.get("actual_battery_power_w"),
            "status": data.get("status"),
            "until": data.get("current_action_end_time"),
            "lp_stats": data.get("lp_stats", {}),
            "reserve_recommendation": data.get("reserve_recommendation", {}),
            "idle_hold_active": data.get("idle_hold_active", False),
            "idle_hold_reserve": data.get("idle_hold_reserve"),
            "idle_hold_reserve_percent": data.get("idle_hold_reserve_percent"),
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION,
        name="Next Scheduled Change",
        icon="mdi:clock-fast",
        value_fn=lambda data: data.get("next_action") if data else None,
        attr_fn=lambda data: {
            "time": data.get("next_action_time"),
            "power_w": data.get("next_action_power_w"),
            "current_action": data.get("current_action"),
            "current_until": data.get("current_action_end_time"),
            "next_actions": data.get("next_actions", []),
            "force_charge_windows": _future_optimizer_action_windows(data, "charge"),
            "force_discharge_windows": _future_optimizer_action_windows(
                data, ("discharge", "export")
            ),
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_FORCE_CHARGE_WINDOWS,
        name="Optimizer Force Charge Windows",
        icon="mdi:battery-clock",
        value_fn=lambda data: _optimizer_window_state(data, "charge"),
        attr_fn=lambda data: _optimizer_window_attributes(data, "charge"),
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_OPTIMIZATION_FORCE_DISCHARGE_WINDOWS,
        name="Optimizer Force Discharge Windows",
        icon="mdi:battery-arrow-down-clock",
        value_fn=lambda data: _optimizer_window_state(data, ("discharge", "export")),
        attr_fn=lambda data: _optimizer_window_attributes(data, ("discharge", "export")),
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
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("estimated_cost", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_SUPPLY_CHARGE_COST,
        name="Daily Supply Charge Cost This Month",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,  # MONETARY only supports 'total', not 'total_increasing'
        suggested_display_precision=2,
        value_fn=lambda data: data.get("daily_supply_charge_cost", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_MONTHLY_SUPPLY_CHARGE,
        name="Monthly Supply Charge",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("monthly_supply_charge", 0.0) if data else 0.0,
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_TOTAL_MONTHLY_COST,
        name="Total Estimated Monthly Cost",
        currency_unit="money",
        currency_attrs=True,
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
        currency_unit="market_rate",
        currency_attrs=True,
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


# Octopus Saving Session Sensors
SAVING_SESSION_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SAVING_SESSION_ACTIVE,
        name="Saving Session Active",
        icon="mdi:lightning-bolt",
        value_fn=lambda data: "Active" if data and data.get("active_session") else "Inactive",
        attr_fn=lambda data: {
            "session_code": data["active_session"].code if data and data.get("active_session") else None,
            "session_start": data["active_session"].start.isoformat() if data and data.get("active_session") else None,
            "session_end": data["active_session"].end.isoformat() if data and data.get("active_session") else None,
            "session_type": data["active_session"].session_type if data and data.get("active_session") else None,
            "octopoints_per_kwh": data["active_session"].octopoints_per_kwh if data and data.get("active_session") else None,
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_NEXT_SAVING_SESSION,
        name="Next Saving Session",
        icon="mdi:calendar-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data["next_session"].start if data and data.get("next_session") else None,
        attr_fn=lambda data: {
            "session_code": data["next_session"].code if data and data.get("next_session") else None,
            "session_end": data["next_session"].end.isoformat() if data and data.get("next_session") else None,
            "session_type": data["next_session"].session_type if data and data.get("next_session") else None,
            "octopoints_per_kwh": data["next_session"].octopoints_per_kwh if data and data.get("next_session") else None,
            "rate_pence_per_kwh": data["next_session"].rate_pence_per_kwh if data and data.get("next_session") else None,
        } if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_SAVING_SESSION_RATE,
        name="Saving Session Rate",
        icon="mdi:currency-gbp",
        native_unit_of_measurement="p/kWh",
        suggested_display_precision=1,
        value_fn=lambda data: (
            data["active_session"].rate_pence_per_kwh
            if data and data.get("active_session")
            else (
                data["next_session"].rate_pence_per_kwh
                if data and data.get("next_session")
                else None
            )
        ),
        attr_fn=lambda data: {
            "source": "active" if data and data.get("active_session") else ("next" if data and data.get("next_session") else None),
            "total_sessions": len(data.get("sessions", [])) if data else 0,
        } if data else {},
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


class NetworkExportLimitSensor(SensorEntity):
    """Expose the certified controller's read-only network export envelope."""

    _attr_has_entity_name = True
    _attr_name = "Network Export Limit"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(
        self,
        manager: HANetworkEnvelopeManager,
        entry: ConfigEntry,
    ) -> None:
        self._manager = manager
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_network_export_limit"
        self._attr_suggested_object_id = "power_sync_network_export_limit"

    @property
    def device_info(self) -> dict[str, Any]:
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_GRID_HOME)

    @property
    def native_value(self) -> float | None:
        return self._manager.snapshot.effective_limit_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._manager.snapshot
        attributes = snapshot.to_dict()
        next_change = snapshot.next_change_at
        attributes["next_limit_w"] = (
            snapshot.limit_for_interval(
                next_change,
                next_change + timedelta(seconds=1),
            )
            if next_change is not None
            else None
        )
        return attributes

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.add_listener(self._handle_envelope_update))

    @callback
    def _handle_envelope_update(
        self,
        _old: NetworkExportEnvelope,
        _new: NetworkExportEnvelope,
    ) -> None:
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync sensor entities."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    amber_coordinator: AmberPriceCoordinator | None = domain_data.get("amber_coordinator")
    localvolts_coordinator: LocalvoltsPriceCoordinator | None = domain_data.get("localvolts_coordinator")
    octopus_coordinator: OctopusPriceCoordinator | None = domain_data.get("octopus_coordinator")
    tesla_coordinator: TeslaEnergyCoordinator | None = domain_data.get("tesla_coordinator")
    sigenergy_coordinator = domain_data.get("sigenergy_coordinator")
    sungrow_coordinator = domain_data.get("sungrow_coordinator")
    foxess_coordinator = domain_data.get("foxess_coordinator")
    goodwe_coordinator = domain_data.get("goodwe_coordinator")
    alphaess_coordinator = domain_data.get("alphaess_coordinator")
    esy_sunhome_coordinator = domain_data.get("esy_sunhome_coordinator")
    solax_coordinator = domain_data.get("solax_coordinator")
    saj_h2_coordinator = domain_data.get("saj_h2_coordinator")
    fronius_reserva_coordinator = domain_data.get("fronius_reserva_coordinator")
    neovolt_coordinator = domain_data.get("neovolt_coordinator")
    solaredge_coordinator = domain_data.get("solaredge_coordinator")
    anker_solix_coordinator = domain_data.get("anker_solix_coordinator")
    custom_energy_coordinator = domain_data.get("custom_energy_coordinator")
    demand_charge_coordinator: DemandChargeCoordinator | None = domain_data.get("demand_charge_coordinator")
    aemo_spike_manager = domain_data.get("aemo_spike_manager")
    is_sigenergy = domain_data.get("is_sigenergy", False)
    is_sungrow = domain_data.get("is_sungrow", False)
    is_foxess = domain_data.get("is_foxess", False)
    is_goodwe = domain_data.get("is_goodwe", False)
    is_alphaess = domain_data.get("is_alphaess", False)
    is_esy_sunhome = domain_data.get("is_esy_sunhome", False)
    is_solax = domain_data.get("is_solax", False)
    is_saj_h2 = domain_data.get("is_saj_h2", False)
    is_fronius_reserva = domain_data.get("is_fronius_reserva", False)
    is_neovolt = domain_data.get("is_neovolt", False)
    is_solaredge = domain_data.get("is_solaredge", False)
    is_anker_solix = domain_data.get("is_anker_solix", False)
    is_custom_battery = domain_data.get("is_custom_battery", False)

    entities: list[SensorEntity] = []
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
    )

    network_envelope_manager = domain_data.get("network_envelope_manager")
    if network_envelope_manager is not None:
        entities.append(NetworkExportLimitSensor(network_envelope_manager, entry))
        _LOGGER.info("Network export limit sensor added")

    # Add price sensors
    # For Amber/Localvolts users: use AmberPriceSensor with live API data
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
    elif localvolts_coordinator:
        _LOGGER.info("Adding Localvolts price sensors (import and export)")
        for description in PRICE_SENSORS:
            entities.append(
                AmberPriceSensor(
                    coordinator=localvolts_coordinator,
                    description=description,
                    entry=entry,
                )
            )
    elif octopus_coordinator:
        _LOGGER.info("Adding Octopus price sensors (import and export)")
        for description in PRICE_SENSORS:
            entities.append(
                AmberPriceSensor(
                    coordinator=octopus_coordinator,
                    description=description,
                    entry=entry,
                )
            )
    else:
        # For non-Amber providers (Globird, Flow Power, etc.), always create TariffPriceSensor.
        # The sensor handles missing tariff_schedule gracefully (returns None until
        # the tariff is fetched later during setup). This avoids a race condition
        # where sensors were skipped because tariff_schedule hadn't been fetched yet.
        tou_providers = (
            "globird",
            "covau",
            "aemo_vpp",
            "other",
            "tou_only",
            "octopus",
        )
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

    if electricity_provider == "covau":
        entities.extend(
            CovaUProviderSensor(hass, entry, sensor_type)
            for sensor_type in (
                COVAU_SENSOR_PLAN,
                COVAU_SENSOR_IMPORT_REMAINING,
                COVAU_SENSOR_EXPORT_REMAINING,
            )
        )
        _LOGGER.info("CovaU plan and quota sensors added")

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
    elif is_alphaess:
        energy_coordinator = alphaess_coordinator
    elif is_esy_sunhome:
        energy_coordinator = esy_sunhome_coordinator
    elif is_solax:
        energy_coordinator = solax_coordinator
    elif is_saj_h2:
        energy_coordinator = saj_h2_coordinator
    elif is_fronius_reserva:
        energy_coordinator = fronius_reserva_coordinator
    elif is_neovolt:
        energy_coordinator = neovolt_coordinator
    elif is_solaredge:
        energy_coordinator = solaredge_coordinator
    elif is_anker_solix:
        energy_coordinator = anker_solix_coordinator
    elif is_custom_battery:
        energy_coordinator = custom_energy_coordinator
    else:
        energy_coordinator = tesla_coordinator
    if energy_coordinator:
        for description in ENERGY_SENSORS:
            if (
                is_custom_battery
                and description.key == SENSOR_TYPE_GRID_STATUS
            ):
                continue
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
    else:
        _LOGGER.warning("No energy coordinator available - energy sensors will not be created")

    # Add Tesla-specific sensors (gateway firmware, etc.)
    if tesla_coordinator:
        for description in TESLA_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=tesla_coordinator,
                    description=description,
                    entry=entry,
                )
            )

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
        _LOGGER.info("FoxESS-specific sensors added (PV1-PV6, CT2, work mode, min SOC, daily energy)")

    # Add Solax PV string sensors, including X3 Ultra PV3 and voltage/current detail.
    if is_solax and energy_coordinator:
        for description in SOLAX_PV_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Solax PV string sensors added (PV1/PV2/PV3 power, voltage, current)")

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

    # Add Sigenergy-specific PV sensors (DC-coupled and AC-coupled/Smart Port)
    if is_sigenergy and energy_coordinator:
        for description in SIGENERGY_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Sigenergy PV sensors added (DC and AC-coupled)")

    if is_neovolt and energy_coordinator:
        for description in NEOVOLT_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("NeoVolt surplus balancer diagnostic sensor added")

    # Add power ceiling sensors for brands that publish battery_max_* fields.
    if (is_alphaess or is_foxess or is_fronius_reserva) and energy_coordinator:
        for description in BMS_POWER_LIMIT_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Battery power ceiling sensors added")

    # Add ESY Sunhome-specific sensors (inverter temp, battery status, SOH)
    if is_esy_sunhome and energy_coordinator:
        for description in ESY_SUNHOME_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=energy_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("ESY Sunhome-specific sensors added (inverter temp, battery status, SOH)")

    # Add EV sensors if EV charging is configured or Tesla vehicle telemetry is
    # available. The HA energy-flow dashboard depends on these sensors for
    # passive/self-scheduled Tesla charging, even when PowerSync is not
    # controlling the charge.
    ev_enabled = entry.options.get(
        CONF_EV_CHARGING_ENABLED,
        entry.data.get(CONF_EV_CHARGING_ENABLED, False),
    )
    ocpp_enabled = entry.options.get("ocpp_enabled", entry.data.get("ocpp_enabled", False))
    zaptec_entity = entry.options.get(
        "zaptec_charger_entity", entry.data.get("zaptec_charger_entity", "")
    )
    zaptec_standalone = entry.options.get(
        "zaptec_standalone_enabled", entry.data.get("zaptec_standalone_enabled", False)
    )
    sigenergy_charger_enabled = entry.options.get(
        CONF_SIGENERGY_CHARGER_ENABLED,
        entry.data.get(CONF_SIGENERGY_CHARGER_ENABLED, False),
    )
    generic_charger_enabled = entry.options.get(
        CONF_GENERIC_CHARGER_ENABLED,
        entry.data.get(CONF_GENERIC_CHARGER_ENABLED, False),
    )
    has_ev = (
        ev_enabled
        or ocpp_enabled
        or bool(zaptec_entity)
        or zaptec_standalone
        or sigenergy_charger_enabled
        or generic_charger_enabled
        or _has_tesla_ev_device(hass)
        or (is_solaredge and _has_solaredge_ev_power(hass))
    )
    if has_ev:
        for description in EV_SENSORS:
            entities.append(
                EVStatusSensor(
                    hass=hass,
                    entry=entry,
                    description=description,
                )
            )
        _LOGGER.info("EV sensors added (power and battery level)")

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

    # Add Saving Session sensors if coordinator exists
    saving_session_coordinator = domain_data.get("saving_session_coordinator")
    if saving_session_coordinator:
        _LOGGER.info("Octopus Saving Sessions enabled - adding sensors")
        for description in SAVING_SESSION_SENSORS:
            entities.append(
                SavingSessionSensor(
                    coordinator=saving_session_coordinator,
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
        # Skip price forecast sensors for fixed-price providers (prices never change)
        electricity_provider = entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            entry.data.get(CONF_ELECTRICITY_PROVIDER, "")
        )
        fixed_price_providers = ("globird", "nz_retailer", "nz_custom")
        has_dynamic_prices = electricity_provider not in fixed_price_providers
        for description in LP_FORECAST_SENSORS:
            # Skip price forecast sensors for fixed-price providers
            if not has_dynamic_prices and description.key in (
                SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST,
                SENSOR_TYPE_LP_EXPORT_PRICE_FORECAST,
            ):
                continue
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
        _LOGGER.info("Optimizer action sensors added (current, next, charge/discharge windows)")
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
    if inverter_enabled and not _sungrow_ac_inverter_matches_battery(entry):
        entities.append(
            InverterStatusSensor(
                hass=hass,
                entry=entry,
            )
        )
        _LOGGER.info("Inverter status sensor added")
    elif inverter_enabled:
        _LOGGER.warning(
            "Skipping AC inverter status poller because the Sungrow inverter "
            "curtailment endpoint matches the configured Sungrow battery endpoint"
        )

    # Add Flow Power price sensors if Flow Power provider is selected
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER)
    )
    if electricity_provider == "flow_power":
        # Get the price coordinator (Amber or AEMO)
        price_coordinator = (
            amber_coordinator
            or domain_data.get("aemo_sensor_coordinator")
            or domain_data.get("flow_power_kwatch_coordinator")
        )
        if price_coordinator:
            # Publish Flow Power-adjusted rates under the standard current_* sensor
            # ids so the mobile app and default dashboard read the retail price
            # instead of the generic tariff-schedule value.
            entities.append(
                FlowPowerPriceSensor(
                    coordinator=price_coordinator,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_CURRENT_IMPORT_PRICE,
                )
            )
            entities.append(
                FlowPowerPriceSensor(
                    coordinator=price_coordinator,
                    entry=entry,
                    sensor_type=SENSOR_TYPE_CURRENT_EXPORT_PRICE,
                )
            )
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

            # Add portal sensors if portal is connected
            from .const import CONF_FLOWPOWER_EMAIL, FLOW_POWER_PORTAL_SENSORS
            fp_email = entry.options.get(
                CONF_FLOWPOWER_EMAIL, entry.data.get(CONF_FLOWPOWER_EMAIL)
            )
            fp_api_key = entry.options.get(
                CONF_FLOWPOWER_API_KEY, entry.data.get(CONF_FLOWPOWER_API_KEY)
            )
            if fp_email or fp_api_key:
                for sensor_type, name, data_key, unit, icon, source in FLOW_POWER_PORTAL_SENSORS:
                    entities.append(
                        FlowPowerPortalSensor(
                            hass=hass,
                            entry=entry,
                            sensor_type=sensor_type,
                            name=name,
                            data_key=data_key,
                            unit=unit,
                            icon=icon,
                            source_label=source,
                        )
                    )
                _LOGGER.info("Flow Power portal sensors added (%d sensors)", len(FLOW_POWER_PORTAL_SENSORS))

    # Add GloBird portal/account sensors if the provider account is connected.
    globird_coordinator = domain_data.get("globird_coordinator")
    if electricity_provider == "globird" and globird_coordinator:
        from .globird_sensors import build_globird_entities

        globird_entities = build_globird_entities(globird_coordinator, entry)
        entities.extend(globird_entities)
        _LOGGER.info("GloBird portal sensors added (%d sensors)", len(globird_entities))

    # Always add battery health sensor
    # For non-Tesla systems, pass coordinator so it can read battery_soh
    battery_system = "tesla"
    if is_foxess:
        battery_system = "foxess"
    elif is_goodwe:
        battery_system = "goodwe"
    elif is_sungrow:
        battery_system = "sungrow"
    elif is_sigenergy:
        battery_system = "sigenergy"
    elif is_alphaess:
        battery_system = "alphaess"
    elif is_saj_h2:
        battery_system = "saj_h2"
    elif is_fronius_reserva:
        battery_system = "fronius_reserva"
    elif is_neovolt:
        battery_system = "neovolt"
    elif is_anker_solix:
        battery_system = "anker_solix"
    entities.append(BatteryHealthSensor(
        entry=entry,
        coordinator=energy_coordinator,
        battery_system=battery_system,
    ))
    _LOGGER.info("Battery health sensor added")

    # Always add battery mode sensor (for automation triggers)
    entities.append(BatteryModeSensor(hass=hass, entry=entry))
    _LOGGER.info("Battery mode sensor added")

    # Powerwall local TEDAPI sensors — gated on completed pairing.
    # System-level sensors come from the live local snapshot.
    if entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        local_coord = (
            domain_data.get("powerwall_local", {}).get("coordinator")
        )
        if local_coord is not None:
            entities.extend([
                PowerwallSystemIslandStateSensor(local_coord, entry),
                PowerwallCountSensor(local_coord, entry),
                PowerwallActiveAlertsSensor(local_coord, entry),
            ])
    # Pack-level sensors come from the richer BMS health scan because
    # batteryBlocks only contains shallow block identity/count data on PW3 sites.
    if battery_system == "tesla":
        _setup_powerwall_pack_sensor_additions(hass, entry, async_add_entities)
        _setup_powerwall_solar_string_sensor_additions(hass, entry, async_add_entities)

    async_add_entities(entities)


def _powerwall_pack_data(health_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return BMS-scanned packs, including expansion packs, in scan order."""
    if not isinstance(health_data, dict):
        return []
    packs = health_data.get("individual_batteries") or []
    if not isinstance(packs, list):
        return []
    return [pack for pack in packs if isinstance(pack, dict)]


def _pack_value(pack: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = pack.get(key)
        if value is not None:
            return value
    return None


def _pack_float(pack: dict[str, Any], *keys: str) -> float | None:
    value = _pack_value(pack, *keys)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pack_has_value(pack: dict[str, Any], *keys: str) -> bool:
    return _pack_value(pack, *keys) is not None


def _pack_label(packs: list[dict[str, Any]], index: int) -> str:
    """Human label for a BMS pack: base Powerwalls first, expansions separately."""
    pack = packs[index]
    role = pack.get("role")
    if role == "powerwall":
        powerwall_number = sum(1 for prior in packs[: index + 1] if prior.get("role") == "powerwall")
        return f"Powerwall {powerwall_number}"
    if role == "leader":
        return "Leader Powerwall"
    if role == "follower" or pack.get("isFollower"):
        follower_number = sum(
            1
            for prior in packs[: index + 1]
            if prior.get("role") == "follower" or prior.get("isFollower")
        )
        return (
            "Follower Powerwall"
            if follower_number == 1
            else f"Follower Powerwall {follower_number}"
        )
    if pack.get("isExpansion"):
        expansion_number = sum(1 for prior in packs[: index + 1] if prior.get("isExpansion"))
        return f"Expansion Pack {expansion_number}"

    base_number = sum(1 for prior in packs[: index + 1] if not prior.get("isExpansion"))
    return "Leader Powerwall" if base_number == 1 else f"Follower Powerwall {base_number - 1}"


def _pack_metric_available(packs: list[dict[str, Any]], metric: str) -> bool:
    if metric in ("soc", "capacity", "soh"):
        return any(_pack_float(pack, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh") for pack in packs)
    if metric == "current_energy":
        return any(
            _pack_float(
                pack,
                "nominalEnergyRemainingWh",
                "nominal_energy_remaining_wh",
            )
            is not None
            for pack in packs
        )
    if metric == "voltage":
        return any(
            _pack_has_value(
                pack,
                "voltage_v",
                "voltage",
                "battery_voltage",
                "BMS_packVoltage",
            )
            for pack in packs
        )
    if metric == "temperature":
        return any(
            _pack_has_value(
                pack,
                "temperature_c",
                "temperature",
                "battery_temp",
                "BMS_maxCellTemp",
                "BMS_minCellTemp",
            )
            for pack in packs
        )
    return False


def _pack_sensor_classes_for(packs: list[dict[str, Any]]) -> tuple[type[SensorEntity], ...]:
    classes: list[type[SensorEntity]] = [
        PowerwallBlockSocSensor,
        PowerwallBlockCurrentEnergySensor,
        PowerwallBlockCapacitySensor,
        PowerwallBlockSohSensor,
    ]
    if _pack_metric_available(packs, "voltage"):
        classes.append(PowerwallBlockVoltageSensor)
    if _pack_metric_available(packs, "temperature"):
        classes.append(PowerwallBlockTemperatureSensor)
    return tuple(classes)


def _build_powerwall_pack_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    packs: list[dict[str, Any]],
    added_keys: set[tuple[int, str]],
) -> list[SensorEntity]:
    """Build pack-level entities for BMS metrics that are present."""
    entities: list[SensorEntity] = []
    for index, _pack in enumerate(packs):
        for sensor_cls in _pack_sensor_classes_for(packs):
            if not _pack_metric_available([_pack], sensor_cls.metric_key):
                continue
            key = (index, sensor_cls.metric_key)
            if key in added_keys:
                continue
            added_keys.add(key)
            entities.append(sensor_cls(hass, entry, index))
    return entities


def _setup_powerwall_pack_sensor_additions(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create pack sensors from BMS health data now and after future scans."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    added_keys: set[tuple[int, str]] = domain_data.setdefault("powerwall_pack_sensor_keys", set())

    def _add_from_health(health_data: dict[str, Any] | None) -> None:
        packs = _powerwall_pack_data(health_data)
        if not packs:
            return
        new_entities = _build_powerwall_pack_sensors(hass, entry, packs, added_keys)
        if new_entities:
            async_add_entities(new_entities)
            _LOGGER.info(
                "Added %d Powerwall pack sensors across %d BMS pack(s)",
                len(new_entities),
                len(packs),
            )

    _add_from_health(domain_data.get("battery_health"))

    if domain_data.get("powerwall_pack_sensor_unsub") is not None:
        return

    @callback
    def _handle_battery_health_update(data: dict[str, Any]) -> None:
        _add_from_health(data)

    domain_data["powerwall_pack_sensor_unsub"] = async_dispatcher_connect(
        hass,
        f"{DOMAIN}_battery_health_update_{entry.entry_id}",
        _handle_battery_health_update,
    )

    try:
        _cleanup_legacy_powerwall_pack_registry(hass, entry)
    except Exception:
        _LOGGER.warning("Could not clean up legacy Powerwall pack registry entries", exc_info=True)


def _solar_string_data(diagnostics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(diagnostics, dict):
        return []
    strings = diagnostics.get("strings")
    if not isinstance(strings, list):
        return []
    return [string for string in strings if isinstance(string, dict)]


def _solar_string_label(reading: dict[str, Any], index: int) -> str:
    label = reading.get("label")
    if isinstance(label, str) and label:
        return label
    mppt = reading.get("mppt")
    if isinstance(mppt, str) and mppt:
        return mppt
    return str(index + 1)


def _solar_string_key(reading: dict[str, Any], index: int) -> str:
    raw = reading.get("id") or reading.get("label") or f"string_{index + 1}"
    key = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(raw)).strip("_")
    return key or f"string_{index + 1}"


def _build_powerwall_solar_string_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    diagnostics: dict[str, Any] | None,
    added_keys: set[str],
) -> list[SensorEntity]:
    """Build DC string voltage entities for strings reported by TEDAPI scans."""
    entities: list[SensorEntity] = []
    for index, reading in enumerate(_solar_string_data(diagnostics)):
        key = _solar_string_key(reading, index)
        if key in added_keys:
            continue
        added_keys.add(key)
        entities.append(
            PowerwallSolarStringVoltageSensor(
                hass,
                entry,
                key,
                reading.get("id"),
                _solar_string_label(reading, index),
            )
        )
    return entities


def _setup_powerwall_solar_string_sensor_additions(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create Powerwall string voltage sensors now and after future scans."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    added_keys: set[str] = domain_data.setdefault("powerwall_solar_string_sensor_keys", set())

    def _add_from_diagnostics(diagnostics: dict[str, Any] | None) -> None:
        new_entities = _build_powerwall_solar_string_sensors(
            hass,
            entry,
            diagnostics,
            added_keys,
        )
        if new_entities:
            async_add_entities(new_entities)
            _LOGGER.info(
                "Added %d Powerwall solar string voltage sensor(s)",
                len(new_entities),
            )

    _add_from_diagnostics(domain_data.get("solar_string_diagnostics"))

    if domain_data.get("powerwall_solar_string_sensor_unsub") is not None:
        return

    @callback
    def _handle_solar_strings_update(data: dict[str, Any]) -> None:
        _add_from_diagnostics(data)

    domain_data["powerwall_solar_string_sensor_unsub"] = async_dispatcher_connect(
        hass,
        f"{DOMAIN}_solar_strings_update_{entry.entry_id}",
        _handle_solar_strings_update,
    )


def _cleanup_legacy_powerwall_pack_registry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove stale standalone Powerwall N registry entries from older releases."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
    except Exception as err:
        _LOGGER.debug("Unable to access HA registries for Powerwall pack cleanup: %s", err)
        return

    legacy_device_ids: set[str] = set()
    legacy_identifier_prefix = f"{entry.entry_id}_pw_"
    for device in list(device_registry.devices.values()):
        identifiers = getattr(device, "identifiers", set()) or set()
        for identifier_entry in identifiers:
            if not isinstance(identifier_entry, (tuple, list)) or len(identifier_entry) < 2:
                continue
            domain, identifier = identifier_entry[0], identifier_entry[1]
            if domain == DOMAIN and str(identifier).startswith(legacy_identifier_prefix):
                legacy_device_ids.add(device.id)
                break

    for entity in list(entity_registry.entities.values()):
        if (
            entity.platform == DOMAIN
            and entity.device_id in legacy_device_ids
            and str(entity.unique_id).startswith(f"{entry.entry_id}_pw")
            and str(entity.unique_id).endswith(("_temperature", "_voltage"))
        ):
            entity_registry.async_remove(entity.entity_id)

    for device_id in legacy_device_ids:
        try:
            device_registry.async_update_device(
                device_id=device_id,
                remove_config_entry_id=entry.entry_id,
            )
        except Exception as err:
            _LOGGER.debug("Unable to remove legacy Powerwall pack device %s: %s", device_id, err)


class AmberPriceSensor(PowerSyncCurrencyMixin, CoordinatorEntity, RestoredNumericStateMixin, SensorEntity):
    """Sensor for Amber electricity prices."""

    # The "forecast" attribute is a large rolling price-forecast array that
    # exceeds the recorder's 16 KB per-state attribute cap and is not history.
    # Keep the scalar state recorded but exclude the bulky attribute.
    _unrecorded_attributes = _FORECAST_ARRAY_ATTRS

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
        return family_device_info(
            self._entry.entry_id,
            SENSOR_KEY_TO_FAMILY.get(self.entity_description.key, SENSOR_FAMILY_PRICING),
        )

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            value = self.entity_description.value_fn(self.coordinator.data)
            _LOGGER.debug("AmberPriceSensor %s native_value: %s", self.entity_description.key, value)
            return value if value is not None else self._restored_numeric_value(self.entity_description.key)
        return None

    async def async_added_to_hass(self) -> None:
        """Restore the last price while coordinator data warms up."""
        await super().async_added_to_hass()
        await self._async_restore_numeric_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            attrs = self.entity_description.attr_fn(self.coordinator.data)
        else:
            attrs = {}
        return _entity_currency_attrs(self, attrs)


_LOCAL_GRID_STATUS_TO_CLOUD = {
    "SystemGridConnected": "Active",
    "SystemIslandedReady": "Active",
    "SystemTransitionToGrid": "Active",
    "SystemTransitionToIsland": "Off-Grid",
    "SystemIslandedActive": "Off-Grid",
    "SystemMicroGridFaulted": "Off-Grid",
    "SystemWaitForUser": "Off-Grid",
}


def _local_value_for(
    sensor_key: str,
    snap: Any,
    *,
    ev_power_kw: float = 0.0,
) -> Any:
    """Map a sensor key to its equivalent on the local PowerwallSnapshot.

    Returns the locally-derived value (in the same units the cloud value_fn
    produces) or ``None`` to indicate "no local equivalent — fall through to cloud".
    """
    if snap is None:
        return None
    if sensor_key == SENSOR_TYPE_BATTERY_POWER:
        return None if snap.battery_w is None else snap.battery_w / 1000.0
    if sensor_key == SENSOR_TYPE_GRID_POWER:
        return None if snap.grid_w is None else snap.grid_w / 1000.0
    if sensor_key == SENSOR_TYPE_SOLAR_POWER:
        return None if snap.solar_w is None else snap.solar_w / 1000.0
    if sensor_key == SENSOR_TYPE_HOME_LOAD:
        if snap.load_w is None:
            return None
        # Powerwall local TEDAPI reports total behind-the-meter load, which
        # includes EV charging. Keep Home Load aligned with the cloud
        # coordinator and Tesla app by removing observed EV charging power.
        return max(0.0, (snap.load_w / 1000.0) - max(0.0, ev_power_kw))
    if sensor_key == SENSOR_TYPE_BATTERY_LEVEL:
        return snap.soc
    if sensor_key == SENSOR_TYPE_GRID_STATUS:
        if snap.grid_status is None:
            return None
        return _LOCAL_GRID_STATUS_TO_CLOUD.get(snap.grid_status, "Active")
    return None


_LOCAL_OVERRIDABLE = {
    SENSOR_TYPE_BATTERY_POWER,
    SENSOR_TYPE_GRID_POWER,
    SENSOR_TYPE_SOLAR_POWER,
    SENSOR_TYPE_HOME_LOAD,
    SENSOR_TYPE_BATTERY_LEVEL,
    SENSOR_TYPE_GRID_STATUS,
}

# How recently the local coordinator must have ticked for its data to be
# trusted by the local-prefer override. The local coord polls every 2s, so
# 30s is ~15 missed ticks — comfortably past transient blips, well before
# the data turns into a "stuck at 41%" disaster.
_LOCAL_STALE_SECONDS = TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS
_ENERGY_COORDINATOR_STALE_FACTOR = 4
_ENERGY_COORDINATOR_MIN_STALE_SECONDS = 60


def _local_data_is_fresh(local_coord: Any) -> bool:
    """True iff the local coordinator's last successful update is recent."""
    if local_coord is None or local_coord.data is None:
        return False
    last_ts = getattr(local_coord, "last_success_monotonic", None)
    if last_ts is None:
        return False
    import time as _time
    return (_time.monotonic() - last_ts) <= _LOCAL_STALE_SECONDS


def _coordinator_data_is_fresh(coordinator: Any) -> bool:
    """Return False when a coordinator is serving stale energy data."""
    if not getattr(coordinator, "last_update_success", True):
        return False
    if getattr(coordinator, "data", None) is None:
        return False

    last_update = getattr(coordinator, "last_update_success_time", None)
    update_interval = getattr(coordinator, "update_interval", None)
    if last_update is None or update_interval is None:
        return True

    try:
        stale_after = max(
            update_interval * _ENERGY_COORDINATOR_STALE_FACTOR,
            timedelta(seconds=_ENERGY_COORDINATOR_MIN_STALE_SECONDS),
        )
        now = dt_util.utcnow()
        if getattr(now, "tzinfo", None) is None and getattr(last_update, "tzinfo", None) is not None:
            now = now.replace(tzinfo=last_update.tzinfo)
        elif getattr(now, "tzinfo", None) is not None and getattr(last_update, "tzinfo", None) is None:
            last_update = last_update.replace(tzinfo=now.tzinfo)
        age = now - last_update
    except Exception:
        return True

    return age <= stale_after


class TeslaEnergySensor(PowerSyncCurrencyMixin, CoordinatorEntity, RestoredNumericStateMixin, SensorEntity):
    """Sensor for Tesla energy data.

    Reads cloud-coordinator data via the entity description's ``value_fn`` by
    default. When the entry is paired and the local coordinator has a fresh
    snapshot, the locally-derived value wins for keys in ``_LOCAL_OVERRIDABLE``
    — and the entity also subscribes to local coordinator updates so it
    refreshes at the local 2s cadence instead of the cloud 30-60s cadence.
    """

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
        self._local_unsub = None

    @property
    def device_info(self):
        if self.entity_description.device_section == "powerwall":
            return powerwall_device_info(self._entry.entry_id)
        return family_device_info(
            self._entry.entry_id,
            SENSOR_KEY_TO_FAMILY.get(self.entity_description.key, SENSOR_FAMILY_BATTERY),
        )

    def _local_coordinator(self):
        """Return the PowerwallLocalCoordinator if paired and built, else None."""
        if not self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
            return None
        bucket = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("powerwall_local", {})
        )
        return bucket.get("coordinator")

    @property
    def available(self) -> bool:
        """Return False when the backing energy coordinator is stale."""
        return _coordinator_data_is_fresh(self.coordinator)

    @property
    def native_value(self) -> Any:
        """Prefer local snapshot value when paired AND fresh; else cloud value_fn.

        Freshness guard: if the local coordinator's last successful update is
        older than ``_LOCAL_STALE_SECONDS``, fall through to cloud. The local
        coordinator can die silently (eg gateway unreachable, key rejection,
        unhandled exception in update loop) and its ``data`` attribute keeps
        the last successful snapshot. Without this guard, sensors would
        cling to that stale value indefinitely.
        """
        if self.entity_description.key in _LOCAL_OVERRIDABLE:
            local_coord = self._local_coordinator()
            if local_coord is not None and _local_data_is_fresh(local_coord):
                local_v = _local_value_for(
                    self.entity_description.key,
                    local_coord.data,
                    ev_power_kw=(
                        (self.coordinator.data or {}).get("ev_power", 0.0) or 0.0
                    ),
                )
                if local_v is not None:
                    return local_v
        if self.entity_description.value_fn:
            value = self.entity_description.value_fn(self.coordinator.data)
            if (
                self.entity_description.key == SENSOR_TYPE_SOLAR_POWER
                and value is not None
            ):
                value += _sungrow_ac_inverter_power_kw(self._entry, self.hass)
            return value if value is not None else self._restored_numeric_value(self.entity_description.key)
        return None

    async def async_added_to_hass(self) -> None:
        """Subscribe to both cloud and local coordinator updates."""
        await super().async_added_to_hass()
        await self._async_restore_numeric_state()
        if self.entity_description.key in _LOCAL_OVERRIDABLE:
            local_coord = self._local_coordinator()
            if local_coord is not None:
                self._local_unsub = local_coord.async_add_listener(
                    self.async_write_ha_state
                )

    async def async_will_remove_from_hass(self) -> None:
        """Drop the local coordinator listener cleanly."""
        if self._local_unsub is not None:
            self._local_unsub()
            self._local_unsub = None
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if self.entity_description.attr_fn:
            attrs = self.entity_description.attr_fn(self.coordinator.data)
        else:
            attrs = {}
        if self.entity_description.key == SENSOR_TYPE_SOLAR_POWER:
            battery_solar_kw = (
                self.coordinator.data.get("solar_power")
                if self.coordinator.data
                else None
            )
            ac_solar_kw = _sungrow_ac_inverter_power_kw(self._entry, self.hass)
            if ac_solar_kw > 0:
                attrs.update(
                    {
                        "battery_inverter_solar_power_kw": battery_solar_kw,
                        "ac_inverter_solar_power_kw": round(ac_solar_kw, 3),
                        "total_solar_power_kw": round(
                            float(battery_solar_kw or 0) + ac_solar_kw, 3
                        ),
                    }
                )
        return _entity_currency_attrs(self, attrs)


class _PowerwallLocalSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for sensors that read directly from the local TEDAPI snapshot."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _snap(self):
        return self.coordinator.data


class PowerwallSystemIslandStateSensor(_PowerwallLocalSensorBase):
    """Powerwall-reported island state (richer than the simple grid_status sensor)."""

    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_system_island_state", "System Island State")

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None:
            return None
        return snap.system_island_state or snap.grid_status


class PowerwallCountSensor(_PowerwallLocalSensorBase):
    """Number of in-service Powerwalls reported by the gateway."""

    _attr_icon = "mdi:battery-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_count", "Powerwall Count")

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None:
            return None
        if snap.pw_count is not None:
            return snap.pw_count
        return len(snap.battery_blocks) if snap.battery_blocks else None


class PowerwallActiveAlertsSensor(_PowerwallLocalSensorBase):
    """Count of active alerts; alert names + severities exposed as attributes."""

    _attr_icon = "mdi:alert-circle"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_active_alerts", "Powerwall Active Alerts")

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None or snap.alerts is None:
            return None
        return len(snap.alerts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snap = self._snap
        if snap is None or not snap.alerts:
            return {}
        names = []
        severities = {}
        for alert in snap.alerts:
            name = alert.get("name") or alert.get("alert_name") or "Unknown"
            sev = alert.get("severity") or alert.get("alert_severity")
            names.append(name)
            if sev:
                severities[name] = sev
        return {"alerts": names, "severities": severities}


class _PowerwallBlockSensorBase(SensorEntity):
    """Base class for BMS-scanned pack sensors.

    The class name is retained so existing entity unique IDs keep migrating
    cleanly, but the data now comes from battery_health.individual_batteries
    rather than local batteryBlocks.
    """

    _attr_has_entity_name = False
    metric_key = ""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int, key: str, name: str) -> None:
        self._hass = hass
        self._entry = entry
        self._index = index
        self._metric_name = name
        self._attr_unique_id = f"{entry.entry_id}_pw{index + 1}_{key}"
        self._attr_suggested_object_id = f"powerwall_{index + 1}_{key}"
        self._attr_name = f"{self._label} {name}"

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _health_data(self) -> dict[str, Any] | None:
        return (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("battery_health")
        )

    @property
    def _packs(self) -> list[dict[str, Any]]:
        return _powerwall_pack_data(self._health_data)

    @property
    def _label(self) -> str:
        packs = self._packs
        if self._index >= len(packs):
            return f"Powerwall {self._index + 1}"
        return _pack_label(packs, self._index)

    @property
    def _block(self) -> dict[str, Any] | None:
        packs = self._packs
        if self._index >= len(packs):
            return None
        return packs[self._index]

    @property
    def available(self) -> bool:
        return self._block is not None

    async def async_added_to_hass(self) -> None:
        """Refresh state when a new BMS health scan lands."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_battery_health_update_{self._entry.entry_id}",
                self._handle_battery_health_update,
            )
        )

    @callback
    def _handle_battery_health_update(self, data: dict[str, Any]) -> None:
        self._attr_name = f"{self._label} {self._metric_name}"
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pack = self._block
        if not pack:
            return {}

        is_expansion = bool(pack.get("isExpansion"))
        is_follower = bool(pack.get("isFollower"))
        role = pack.get("role") or (
            "expansion" if is_expansion else "follower" if is_follower else "leader"
        )
        attrs: dict[str, Any] = {
            "pack_index": self._index + 1,
            "pack_label": self._label,
            "pack_role": role,
            "is_expansion": is_expansion,
            "is_follower": is_follower,
        }
        serial = pack.get("serialNumber") or pack.get("serial_number")
        if serial:
            attrs["serial_number"] = serial
        physical_din = pack.get("physicalDin") or pack.get("physical_din") or pack.get("din")
        if physical_din:
            attrs["physical_din"] = physical_din
        bms_serial = pack.get("bmsSerialNumber") or pack.get("bms_serial_number")
        if bms_serial and bms_serial != serial:
            attrs["bms_serial_number"] = bms_serial

        full = _pack_float(pack, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh")
        remaining = _pack_float(pack, "nominalEnergyRemainingWh", "nominal_energy_remaining_wh")
        if full is not None:
            attrs["capacity_kwh"] = round(full / 1000.0, 2)
        if remaining is not None:
            attrs["energy_remaining_kwh"] = round(remaining / 1000.0, 2)
        if full and full > 0 and remaining is not None:
            attrs["soc_percent"] = round(remaining / full * 100.0, 1)

        health_data = self._health_data or {}
        if health_data.get("source"):
            attrs["source"] = health_data["source"]
        return attrs


class PowerwallBlockSocSensor(_PowerwallBlockSensorBase):
    metric_key = "soc"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "soc", "SOC")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh")
        rem = _pack_float(block, "nominalEnergyRemainingWh", "nominal_energy_remaining_wh")
        if full and rem is not None and full > 0:
            return round(rem / full * 100.0, 1)
        return None


class PowerwallBlockCurrentEnergySensor(_PowerwallBlockSensorBase):
    metric_key = "current_energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:battery-50"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "current_energy", "Current Energy")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        remaining = _pack_float(
            block,
            "nominalEnergyRemainingWh",
            "nominal_energy_remaining_wh",
        )
        return round(remaining / 1000.0, 2) if remaining is not None else None


class PowerwallBlockCapacitySensor(_PowerwallBlockSensorBase):
    metric_key = "capacity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:battery-high"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "capacity", "Capacity")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh")
        return round(full / 1000.0, 2) if full else None


class PowerwallBlockVoltageSensor(_PowerwallBlockSensorBase):
    metric_key = "voltage"
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "voltage", "Voltage")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        v = _pack_float(block, "voltage_v", "voltage", "battery_voltage", "BMS_packVoltage")
        return round(float(v), 1) if v is not None else None


class PowerwallBlockTemperatureSensor(_PowerwallBlockSensorBase):
    metric_key = "temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "temperature", "Temperature")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        value = _pack_float(
            block,
            "temperature_c",
            "temperature",
            "battery_temp",
            "BMS_maxCellTemp",
            "BMS_minCellTemp",
        )
        return round(value, 1) if value is not None else None


class PowerwallBlockSohSensor(_PowerwallBlockSensorBase):
    """State of Health: pack capacity vs nameplate. PW2 nameplate = 13.5 kWh."""

    metric_key = "soh"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:battery-heart"

    _NAMEPLATE_WH = 13500.0  # PW2 baseline; PW3 reports its own nominal

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "soh", "State of Health")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh")
        if not full:
            return None
        return round(float(full) / self._NAMEPLATE_WH * 100.0, 1)


class PowerwallSolarStringVoltageSensor(SensorEntity):
    """Voltage for a single Powerwall DC-coupled solar string."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-power-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        string_id: Any,
        label: str,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._key = key
        self._string_id = string_id
        self._label = label
        self._attr_unique_id = f"{entry.entry_id}_solar_string_{key}_voltage"
        self._attr_suggested_object_id = f"powerwall_solar_string_{key}_voltage"
        self._attr_name = f"Solar String {label} Voltage"

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _diagnostics(self) -> dict[str, Any] | None:
        return (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("solar_string_diagnostics")
        )

    @property
    def _reading(self) -> dict[str, Any] | None:
        strings = _solar_string_data(self._diagnostics)
        for index, reading in enumerate(strings):
            if reading.get("id") == self._string_id or _solar_string_key(reading, index) == self._key:
                return reading
        return None

    @property
    def available(self) -> bool:
        reading = self._reading
        return reading is not None and reading.get("voltage_v") is not None

    @property
    def native_value(self) -> Any:
        reading = self._reading
        if not reading:
            return None
        value = _pack_float(reading, "voltage_v")
        return round(value, 1) if value is not None else None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_solar_strings_update_{self._entry.entry_id}",
                self._handle_solar_strings_update,
            )
        )

    @callback
    def _handle_solar_strings_update(self, data: dict[str, Any]) -> None:
        reading = self._reading
        if reading:
            self._label = str(reading.get("label") or self._label)
            self._attr_name = f"Solar String {self._label} Voltage"
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self._reading
        diagnostics = self._diagnostics or {}
        if not reading:
            return {
                "string_id": self._string_id,
                "source": diagnostics.get("source"),
                "last_scan": diagnostics.get("last_scan"),
            }

        attrs: dict[str, Any] = {
            "string_id": reading.get("id"),
            "string_label": reading.get("label"),
            "mppt": reading.get("mppt"),
            "connected": reading.get("connected"),
            "source": diagnostics.get("source"),
            "transport_source": diagnostics.get("transport_source"),
            "last_scan": diagnostics.get("last_scan"),
        }
        for attr_key in ("current_a", "power_w", "state", "device_id"):
            if reading.get(attr_key) is not None:
                attrs[attr_key] = reading.get(attr_key)

        string_id = reading.get("id")
        groups = diagnostics.get("groups") if isinstance(diagnostics, dict) else None
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                if string_id in (group.get("string_ids") or []):
                    attrs["group_id"] = group.get("id")
                    attrs["group_label"] = group.get("label")
                    if group.get("total_power_w") is not None:
                        attrs["group_total_power_w"] = group.get("total_power_w")
                    break
        return attrs


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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

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


class DemandChargeSensor(PowerSyncCurrencyMixin, CoordinatorEntity, SensorEntity):
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_PRICING)

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

        return _entity_currency_attrs(self, attributes)


class AEMOSpikeSensor(PowerSyncCurrencyMixin, SensorEntity):
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_AEMO)

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
            attrs = self.entity_description.attr_fn(self._spike_manager.get_status())
        else:
            attrs = {}
        return _entity_currency_attrs(self, attrs)


class SavingSessionSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Octopus Saving Sessions status."""

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        coordinator,  # OctopusSavingSessionCoordinator
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_OCTOPUS)

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


class SolcastForecastSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Solcast solar production forecasts."""

    # The "forecast" attribute is a large rolling prediction array (≈48h @ 5min)
    # that exceeds the recorder's 16 KB per-state attribute cap and is not
    # history (it's regenerated each cycle). Keep the scalar state recorded but
    # exclude the bulky attribute so the recorder doesn't drop it with a warning
    # or bloat the database.
    _unrecorded_attributes = _FORECAST_ARRAY_ATTRS

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_SOLAR_INVERTER)

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
            "planned_ev_load_peak_kw": data.get("planned_ev_load_peak_kw"),
            "planned_ev_load_kwh": data.get("planned_ev_load_kwh"),
            "planned_ev_load_forecast_w": data.get("planned_ev_load_forecast_w"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_BATTERY_POWER_FORECAST,
        name="Battery Power Forecast",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=2,
        icon="mdi:battery-clock",
        value_fn=lambda data: data.get("battery_power_now_kw") if data and data.get("battery_schedule_available") else None,
        attr_fn=lambda data: {
            "max_charge_kw": data.get("battery_charge_peak_kw"),
            "max_discharge_kw": data.get("battery_discharge_peak_kw"),
            "charge_values_kw": data.get("battery_charge_forecast"),
            "discharge_values_kw": data.get("battery_discharge_forecast"),
            "home_consumption_values_kw": data.get("battery_home_consumption_forecast"),
            "export_values_kw": data.get("battery_export_forecast"),
            "power_values_kw": data.get("battery_power_forecast"),
        } if data and data.get("battery_schedule_available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LP_IMPORT_PRICE_FORECAST,
        name="Import Price Forecast",
        currency_unit="major_rate",
        currency_attrs=True,
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
        currency_unit="major_rate",
        currency_attrs=True,
        suggested_display_precision=4,
        icon="mdi:cash-clock",
        value_fn=lambda data: data.get("export_price_avg") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "min_price": data.get("export_price_min"),
            "max_price": data.get("export_price_max"),
            "price_values": data.get("export_prices"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LOAD_FORECAST_TODAY_REMAINING,
        name="Load Forecast Today (Remaining)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda data: data.get("load_today_remaining_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("load_peak_kw"),
            "hourly_forecast": data.get("load_hourly_today_remaining"),
            "temperature_adjusted": data.get("load_temperature_adjusted", False),
            "away_mode": data.get("load_away_mode", False),
            "away_in_recovery": data.get("load_away_in_recovery", False),
            "away_recovery_remaining_hours": data.get("load_away_recovery_remaining_hours"),
            "away_enabled_at": data.get("load_away_enabled_at"),
            "away_disabled_at": data.get("load_away_disabled_at"),
        } if data and data.get("available") else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_LOAD_FORECAST_TOMORROW,
        name="Load Forecast Tomorrow",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        icon="mdi:home-clock-outline",
        value_fn=lambda data: data.get("load_tomorrow_kwh") if data and data.get("available") else None,
        attr_fn=lambda data: {
            "peak_kw": data.get("load_peak_kw"),
            "hourly_forecast": data.get("load_hourly_tomorrow"),
            "temperature_adjusted": data.get("load_temperature_adjusted", False),
            "away_mode": data.get("load_away_mode", False),
            "away_in_recovery": data.get("load_away_in_recovery", False),
            "away_recovery_remaining_hours": data.get("load_away_recovery_remaining_hours"),
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


class LPForecastSensor(PowerSyncCurrencyMixin, CoordinatorEntity, SensorEntity):
    """Sensor for LP optimizer forecast data (solar, load, prices).

    Reads forecast data stored by the OptimizationCoordinator each
    optimization cycle via get_forecast_data().
    """

    # The "forecast" attribute is a large rolling prediction array (≈48h @ 5min)
    # that exceeds the recorder's 16 KB per-state attribute cap and is not
    # history. Keep the scalar state recorded but exclude the bulky attribute.
    _unrecorded_attributes = _FORECAST_ARRAY_ATTRS

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

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
            attrs = self.entity_description.attr_fn(self._forecast_data)
        else:
            attrs = {}
        return _entity_currency_attrs(self, attrs)


SIGNAL_TARIFF_UPDATED = "power_sync_tariff_updated_{}"
COVAU_SENSOR_PLAN = "covau_plan"
COVAU_SENSOR_IMPORT_REMAINING = "covau_free_import_remaining"
COVAU_SENSOR_EXPORT_REMAINING = "covau_premium_export_remaining"


def _covau_provider_contract_for_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any] | None:
    """Read the live CovaU contract, with a conservative config fallback."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    quota_runtime = runtime.get("covau_quota_runtime")
    if quota_runtime is not None:
        return quota_runtime.contract()
    coordinator = runtime.get("optimization_coordinator")
    if coordinator is not None and hasattr(coordinator, "get_provider_contract"):
        contract = coordinator.get_provider_contract()
        if contract is not None:
            return contract

    from .const import (
        CONF_COVAU_EXPORT_ENERGY_ENTITY,
        CONF_COVAU_IMPORT_ENERGY_ENTITY,
        CONF_COVAU_PLAN_SNAPSHOT,
    )

    raw = entry.options.get(
        CONF_COVAU_PLAN_SNAPSHOT,
        entry.data.get(CONF_COVAU_PLAN_SNAPSHOT),
    )
    if not isinstance(raw, dict):
        return None
    try:
        from .covau import (
            CovaUPlanSnapshot,
            covau_provider_contract,
            covau_quota_rules,
        )
        from .quota import QuotaLedger

        snapshot = CovaUPlanSnapshot.from_dict(raw)
        return covau_provider_contract(
            snapshot,
            QuotaLedger(covau_quota_rules(snapshot)),
            import_energy_entity=entry.options.get(
                CONF_COVAU_IMPORT_ENERGY_ENTITY,
                entry.data.get(CONF_COVAU_IMPORT_ENERGY_ENTITY),
            ),
            export_energy_entity=entry.options.get(
                CONF_COVAU_EXPORT_ENERGY_ENTITY,
                entry.data.get(CONF_COVAU_EXPORT_ENERGY_ENTITY),
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


class CovaUProviderSensor(SensorEntity):
    """Expose the selected SolarMax plan and measured daily quota balances."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sensor_type: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{sensor_type}"
        self._unsub_time_interval = None
        if sensor_type == COVAU_SENSOR_PLAN:
            self._attr_name = "CovaU Plan"
            self._attr_icon = "mdi:file-document-outline"
        elif sensor_type == COVAU_SENSOR_IMPORT_REMAINING:
            self._attr_name = "CovaU Free Import Remaining"
            self._attr_icon = "mdi:transmission-tower-import"
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_suggested_display_precision = 2
        else:
            self._attr_name = "CovaU Premium Export Remaining"
            self._attr_icon = "mdi:transmission-tower-export"
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_suggested_display_precision = 2

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, "covau")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _periodic_update(_now=None):
            self.async_write_ha_state()

        self._unsub_time_interval = async_track_time_interval(
            self.hass,
            _periodic_update,
            timedelta(minutes=1),
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_time_interval:
            self._unsub_time_interval()

    @property
    def native_value(self) -> str | float | None:
        contract = _covau_provider_contract_for_entry(self.hass, self._entry)
        if not contract:
            return None
        if self._sensor_type == COVAU_SENSOR_PLAN:
            return contract.get("plan", {}).get("display_name")
        direction = (
            "import"
            if self._sensor_type == COVAU_SENSOR_IMPORT_REMAINING
            else "export"
        )
        value = contract.get("quotas", {}).get(direction, {}).get("remaining_kwh")
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        contract = _covau_provider_contract_for_entry(self.hass, self._entry)
        if not contract:
            return {}
        attributes: dict[str, Any] = {
            "tariff_day": contract.get("tariff_day"),
            "settlement_confidence": contract.get("settlement_confidence"),
            "settlement_reason": contract.get("settlement_reason"),
            "plan": contract.get("plan"),
        }
        if self._sensor_type == COVAU_SENSOR_PLAN:
            attributes["prices"] = contract.get("prices")
            attributes["quotas"] = contract.get("quotas")
            attributes["import_energy_entity"] = contract.get("import_energy_entity")
            attributes["export_energy_entity"] = contract.get("export_energy_entity")
        else:
            direction = (
                "import"
                if self._sensor_type == COVAU_SENSOR_IMPORT_REMAINING
                else "export"
            )
            attributes.update(contract.get("quotas", {}).get(direction, {}))
        return attributes


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
        # Cache for the schedule-list / buy_prices / sell_prices dicts, which are
        # expensive to rebuild and only change when the tariff data changes (every
        # ~5 minutes on Amber). Rebuilt only when last_sync changes; the
        # time-sensitive fields (current_period, buy_price, current_time) are
        # computed fresh on every write from the cached tariff data.
        self._schedule_cache: dict = {}
        self._schedule_cache_sync: str | None = None
        self._schedule_cache_dow: int = -1  # weekday the cache was built on (0=Mon)
        # Last computed price tuple — shared between native_value and
        # extra_state_attributes within the same HA state-write cycle to avoid
        # calling get_current_price_from_tariff_schedule twice per update.
        self._last_price_result: tuple[float, float, str] = (0.0, 0.0, "UNKNOWN")

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

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

    def _refresh_price(self, tariff_data: dict) -> tuple[float, float, str]:
        """Compute current price once and cache on the instance for this write cycle."""
        result = get_current_price_from_tariff_schedule(tariff_data)
        self._last_price_result = result
        return result

    def _tariff_currency(self, tariff_data: dict[str, Any] | None = None) -> str:
        """Return tariff currency, falling back to provider/HA metadata."""
        return _entity_currency(self, tariff_data)

    def _rebuild_schedule_cache(self, tariff_data: dict) -> None:
        """Rebuild the static parts of extra_state_attributes (schedule lists, raw dicts).

        Called when last_sync changes (~5 min on Amber) OR when the weekday
        changes (midnight rollover). Day-of-week is part of the cache key so
        TOU tariffs with weekday/weekend rate differences stay correct.
        The time-sensitive fields (current_period, buy_price, current_time) are
        computed separately on every write.
        """
        buy_prices = tariff_data.get("buy_prices", {})
        sell_prices = tariff_data.get("sell_prices", {})
        buy_rates = tariff_data.get("buy_rates", {})
        sell_rates = tariff_data.get("sell_rates", {})
        tou_periods = tariff_data.get("tou_periods", {})
        today_dow = dt_util.now().weekday()  # 0=Monday; used for TOU day filtering — must use HA tz, not container UTC

        attrs: dict[str, Any] = {
            "last_sync": tariff_data.get("last_sync"),
            "utility": tariff_data.get("utility"),
            "plan_name": tariff_data.get("plan_name"),
            "current_season": tariff_data.get("current_season"),
        }

        if buy_prices:
            schedule_list = []
            now = dt_util.now()
            today = now.date()
            for period_key in sorted(buy_prices.keys()):
                parts = period_key.replace("PERIOD_", "").split("_")
                time_str = f"{parts[0]}:{parts[1]}"
                schedule_list.append({
                    "time": time_str,
                    "date": today.isoformat(),
                    "date_label": "Today",
                    "buy": buy_prices.get(period_key, 0),
                    "sell": sell_prices.get(period_key, 0),
                })
            attrs["period_count"] = len(buy_prices)
            attrs["schedule"] = schedule_list
            attrs["buy_prices"] = buy_prices
            attrs["sell_prices"] = sell_prices
        elif buy_rates:
            tou_schedule = []
            for period_name, rate in buy_rates.items():
                buy_cents = rate * 100
                sell_rate = sell_rates.get(period_name, 0)
                sell_cents = sell_rate * 100
                period_times = tou_periods.get(period_name, [])
                if isinstance(period_times, dict) and "periods" in period_times:
                    periods_list = period_times["periods"]
                elif isinstance(period_times, list):
                    periods_list = period_times
                else:
                    periods_list = []
                time_windows = [
                    {
                        "from_hour": w.get("fromHour", 0),
                        "to_hour": w.get("toHour", 24),
                        "from_day": w.get("fromDayOfWeek", 0),
                        "to_day": w.get("toDayOfWeek", 6),
                    }
                    for w in periods_list
                ]
                tou_schedule.append({
                    "period": period_name,
                    "buy": round(buy_cents, 2),
                    "sell": round(sell_cents, 2),
                    "windows": time_windows,
                })

            attrs["period_count"] = len(buy_rates)
            attrs["tou_schedule"] = tou_schedule
            attrs["buy_rates"] = {k: round(v * 100, 2) for k, v in buy_rates.items()}
            attrs["sell_rates"] = {k: round(v * 100, 2) for k, v in sell_rates.items()}

            # 48-slot schedule list for price chart compatibility
            sorted_tou = sorted(
                tou_schedule,
                key=lambda e: (
                    0 if e["period"].startswith("SUPER_OFF_PEAK") else
                    1 if e["period"].startswith("PEAK_") else
                    2 if e["period"] == "PEAK" else
                    3 if e["period"].startswith("SHOULDER") else 4
                ),
            )
            tesla_dow = (today_dow + 1) % 7  # Tesla: 0=Sunday
            schedule_list = []
            for slot in range(48):
                hour = slot // 2
                minute = (slot % 2) * 30
                time_str = f"{hour:02d}:{minute:02d}"
                slot_buy = 0.0
                slot_sell = 0.0
                matched = False
                for entry in sorted_tou:
                    for w in entry.get("windows", []):
                        fd = w.get("from_day", 0)
                        td = w.get("to_day", 6)
                        fh = w.get("from_hour", 0)
                        th = w.get("to_hour", 24)
                        if fd <= tesla_dow <= td:
                            if (fh <= th and fh <= hour < th) or (fh > th and (hour >= fh or hour < th)):
                                slot_buy = entry["buy"] / 100
                                slot_sell = entry["sell"] / 100
                                matched = True
                                break
                    if matched:
                        break
                schedule_list.append({"time": time_str, "buy": slot_buy, "sell": slot_sell})
            attrs["schedule"] = schedule_list

        self._schedule_cache = attrs
        self._schedule_cache_sync = tariff_data.get("last_sync")
        self._schedule_cache_dow = today_dow

    @property
    def native_value(self) -> Any:
        """Return the state — current tariff period and price (recalculated in real-time)."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if tariff_data:
            buy_price_cents, _, current_period = self._refresh_price(tariff_data)
            if current_period and current_period != "UNKNOWN":
                unit = minor_price_unit(self._tariff_currency(tariff_data))
                return f"{current_period} ({buy_price_cents:.1f}{unit})"
            return tariff_data.get("last_sync", "Unknown")
        return "Not synced"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the tariff schedule as attributes for visualization."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return {}

        # Rebuild when tariff changes OR when the day rolls over (TOU tariffs have
        # weekday/weekend rate differences that depend on the current day).
        if (
            tariff_data.get("last_sync") != self._schedule_cache_sync
            or dt_util.now().weekday() != self._schedule_cache_dow
        ):
            self._rebuild_schedule_cache(tariff_data)

        # Reuse price already computed by native_value in this write cycle
        buy_price_cents, sell_price_cents, current_period = self._last_price_result
        now = dt_util.now()  # HA tz; naive datetime.now() returns UTC in containers

        return {
            **self._schedule_cache,
            **currency_metadata(self._tariff_currency(tariff_data)),
            "current_period": current_period,
            "buy_price": round(buy_price_cents, 2),
            "sell_price": round(sell_price_cents, 2),
            "current_time": now.strftime("%H:%M"),
            "current_hour": now.hour,
            "current_minute": now.minute,
        }


class TariffPriceSensor(PowerSyncCurrencyMixin, RestoredNumericStateMixin, SensorEntity):
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
        self._attr_currency_unit = "major_rate"
        self._attr_currency_attrs = True
        self._attr_suggested_display_precision = 4
        self._attr_icon = "mdi:cash" if "import" in sensor_type else "mdi:transmission-tower-export"
        self._unsub_dispatcher = None
        self._unsub_time_interval = None
        self._current_period = None

    def _currency_source_data(self) -> dict[str, Any] | None:
        """Use tariff schedule currency for this tariff-backed price sensor."""
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_PRICING)

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        await self._async_restore_numeric_state()

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
        electricity_provider = self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        if electricity_provider == "covau":
            contract = _covau_provider_contract_for_entry(self.hass, self._entry)
            if contract:
                direction = (
                    "import"
                    if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE
                    else "export"
                )
                price = contract.get("prices", {}).get(direction, {}).get("c_per_kwh")
                if price is not None:
                    return round(float(price) / 100.0, 4)

        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return self._restored_numeric_value(self._sensor_type)

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
        electricity_provider = self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        if electricity_provider == "covau":
            contract = _covau_provider_contract_for_entry(self.hass, self._entry)
            if not contract:
                return {}
            direction = (
                "import"
                if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE
                else "export"
            )
            price = contract.get("prices", {}).get(direction, {})
            quota = contract.get("quotas", {}).get(direction, {})
            return _entity_currency_attrs(
                self,
                {
                    "source": "covau_aer_cdr",
                    "current_period": price.get("period") or quota.get("rule_id"),
                    "plan_name": contract.get("plan", {}).get("display_name"),
                    "plan_id": contract.get("plan", {}).get("plan_id"),
                    "settlement_confidence": contract.get("settlement_confidence"),
                    "quota": quota,
                },
            )

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

        return _entity_currency_attrs(self, attributes, tariff_data)


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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_SOLAR_INVERTER)

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_SOLAR_INVERTER)

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
                # Fronius simple mode uses a soft export limit that does not
                # always show up in inverter status registers, so keep the
                # curtailment command state only for that mode.
                entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
                cached_curtail_state = entry_data.get("inverter_last_state")
                if (
                    inverter_brand == "fronius"
                    and cached_curtail_state == "curtailed"
                    and not fronius_load_following
                ):
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

            # Also update hass.data for consistency with curtailment logic.
            # Keep explicit manual/automatic control modes more specific than
            # the inverter's generic curtailed/running status.
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            if entry_data:
                control_mode = entry_data.get("inverter_control_mode")
                if control_mode in (
                    INVERTER_CONTROL_MODE_LOAD_FOLLOWING,
                    INVERTER_CONTROL_MODE_SHUTDOWN,
                ):
                    entry_data["inverter_last_state"] = "curtailed"
                else:
                    entry_data["inverter_last_state"] = self._cached_state
                    if self._cached_state == "running":
                        entry_data["inverter_control_mode"] = INVERTER_CONTROL_MODE_NORMAL
                    elif self._cached_state == "curtailed" and control_mode not in INVERTER_CONTROL_MODES:
                        entry_data["inverter_control_mode"] = INVERTER_CONTROL_MODE_CURTAILED
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

    def _entry_data(self) -> dict[str, Any]:
        """Return runtime data for this config entry."""
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

    def _control_mode(self) -> str:
        """Return the current runtime AC inverter control mode."""
        mode = self._entry_data().get("inverter_control_mode")
        if mode in INVERTER_CONTROL_MODES:
            return mode
        if self._cached_state == "curtailed":
            return INVERTER_CONTROL_MODE_CURTAILED
        return INVERTER_CONTROL_MODE_NORMAL

    def _target_power_w(self) -> int | None:
        """Return the runtime inverter target limit when one is known."""
        value = self._entry_data().get("inverter_power_limit_w")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def native_value(self) -> str:
        """Return the inverter status."""
        if self._cached_state == "offline":
            return "Offline"
        elif self._cached_state == "disabled":
            return "Disabled"
        elif self._cached_state == "not_configured":
            return "Not Configured"
        elif self._cached_state == "error":
            return "Error"

        control_mode = self._control_mode()
        if control_mode == INVERTER_CONTROL_MODE_LOAD_FOLLOWING:
            return "Load Following"
        elif control_mode == INVERTER_CONTROL_MODE_SHUTDOWN:
            return "Shutdown"
        elif control_mode == INVERTER_CONTROL_MODE_CURTAILED or self._cached_state == "curtailed":
            return "Curtailed"
        elif self._cached_state == "running":
            return "Normal"
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

        control_mode = self._control_mode()
        target_power_w = self._target_power_w()

        # Base attributes
        attrs = {
            "enabled": inverter_enabled,
            "brand": inverter_brand,
            "host": inverter_host,
            "model": inverter_model,
            "state": self._cached_state,
            "control_mode": control_mode,
            "target_power_w": target_power_w,
        }

        # Add cached attributes from inverter polling
        attrs.update(self._cached_attrs)
        attrs["control_mode"] = control_mode
        attrs["target_power_w"] = target_power_w

        # Add description based on state after cached attrs so the public
        # dashboard text remains specific to PowerSync's active control mode.
        if control_mode == INVERTER_CONTROL_MODE_LOAD_FOLLOWING:
            if target_power_w is not None and target_power_w > 0:
                attrs["description"] = f"Inverter curtailed - load following at {target_power_w}W"
            else:
                attrs["description"] = "Inverter curtailed - load following"
        elif control_mode == INVERTER_CONTROL_MODE_SHUTDOWN:
            attrs["description"] = "Inverter curtailed - shutdown mode"
        elif self._cached_state == "curtailed":
            attrs["description"] = "Inverter curtailed"
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

        return attrs


class FlowPowerPriceSensor(PowerSyncCurrencyMixin, CoordinatorEntity, RestoredNumericStateMixin, SensorEntity):
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
        if sensor_type in (
            SENSOR_TYPE_FLOW_POWER_PRICE,
            SENSOR_TYPE_CURRENT_IMPORT_PRICE,
        ):
            self._is_import_sensor = True
        else:
            self._is_import_sensor = False

        if sensor_type == SENSOR_TYPE_FLOW_POWER_PRICE:
            self._attr_name = "Flow Power Import Price"
            self._attr_icon = "mdi:lightning-bolt"
        elif sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE:
            self._attr_name = "Current Import Price"
            self._attr_icon = "mdi:cash"
        elif sensor_type == SENSOR_TYPE_CURRENT_EXPORT_PRICE:
            self._attr_name = "Current Export Price"
            self._attr_icon = "mdi:transmission-tower-export"
        else:
            self._attr_name = "Flow Power Export Price"
            self._attr_icon = "mdi:solar-power"

        self._attr_currency_unit = "major_rate"
        self._attr_currency_attrs = True
        self._attr_suggested_display_precision = 4
        self._current_period = None

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)

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

    def _get_tariff_data(self) -> tuple[float | None, float | None]:
        """Get tariff_rate and avg_daily_tariff from hass.data if available."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tariff_rate = domain_data.get("fp_tariff_rate")
        avg_daily_tariff = domain_data.get("fp_avg_daily_tariff")
        return tariff_rate, avg_daily_tariff

    def _get_pricing_context(self) -> FlowPowerPricingContext:
        """Resolve TWAP/BPEA/GST inputs shared with the optimizer."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return resolve_flow_power_pricing_context(
            self._entry.options,
            self._entry.data,
            domain_data,
        )

    def _coordinator_source_attributes(self) -> dict[str, Any]:
        """Expose the effective dynamic price source used by the coordinator."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return {}

        attrs: dict[str, Any] = {}
        source = data.get("source")
        if source:
            attrs["price_source"] = source
        attrs["price_update_success"] = bool(
            getattr(self.coordinator, "last_update_success", True)
        )
        attrs["using_price_fallback"] = bool(data.get("using_fallback"))
        for key in (
            "fallback_reason",
            "fallback_source",
            "primary_source",
            "kwatch_consecutive_failures",
            "kwatch_last_attempt",
            "kwatch_last_success",
        ):
            value = data.get(key)
            if value not in (None, ""):
                attrs[key] = value

        # Read live coordinator fields after the last-good data snapshot. If a
        # refresh fails completely, DataUpdateCoordinator intentionally keeps
        # serving that snapshot, but these values still reveal the new attempt.
        last_attempt = getattr(self.coordinator, "_kwatch_last_attempt", None)
        if last_attempt is not None:
            attrs["kwatch_last_attempt"] = last_attempt.isoformat()
        failures = getattr(self.coordinator, "_kwatch_consecutive_failures", None)
        if failures is not None:
            attrs["kwatch_consecutive_failures"] = failures
        return attrs

    @property
    def _uses_standard_current_price_id(self) -> bool:
        """Return true for standard dashboard/mobile current price entities."""
        return self._sensor_type in (
            SENSOR_TYPE_CURRENT_IMPORT_PRICE,
            SENSOR_TYPE_CURRENT_EXPORT_PRICE,
        )

    def _get_current_tariff_price(self) -> tuple[float, dict[str, Any]] | None:
        """Read the canonical tariff schedule for standard current price sensors."""
        if not self._uses_standard_current_price_id or not hasattr(self, "hass"):
            return None

        tariff_data = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tariff_schedule")
        )
        if not tariff_data:
            return None

        buy_price_cents, sell_price_cents, current_period = (
            get_current_price_from_tariff_schedule(tariff_data)
        )
        self._current_period = current_period
        price_cents = (
            buy_price_cents
            if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE
            else sell_price_cents
        )
        return max(0.0, price_cents / 100), tariff_data

    def _get_effective_twap(self) -> float:
        """Get effective raw wholesale TWAP: override -> tracker -> fallback."""
        return self._get_pricing_context().twap

    def _get_twap_source(self) -> str:
        """Return a label describing which TWAP source is active."""
        return self._get_pricing_context().twap_source

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
        pricing = self._get_pricing_context()
        tariff_rate, avg_daily_tariff = self._get_tariff_data()

        return calculate_flow_power_pea(
            wholesale_cents,
            pricing,
            tariff_rate=tariff_rate,
            avg_daily_tariff=avg_daily_tariff,
        )

    def _calculate_export_price(self) -> float:
        """Calculate Flow Power export price in $/kWh."""
        if self._is_happy_hour():
            # Happy Hour rate
            return self._get_export_rate()
        else:
            # Outside Happy Hour - no export credit
            return 0.0

    def _get_export_rate(self) -> float:
        """Return configured Flow Power Happy Hour export rate in $/kWh."""
        configured_rate = self._get_config_value(CONF_FLOW_POWER_EXPORT_RATE)
        if configured_rate not in (None, ""):
            try:
                return max(0.0, float(configured_rate) / 100)
            except (ValueError, TypeError):
                pass

        state = self._get_config_value(CONF_FLOW_POWER_STATE, "QLD1")
        return FLOW_POWER_EXPORT_RATES.get(state, 0.0)

    @property
    def native_value(self) -> float | None:
        """Return the current price in $/kWh."""
        tariff_price = self._get_current_tariff_price()
        if tariff_price is not None:
            value, _tariff_data = tariff_price
            return round(value, 4)

        if self._is_import_sensor:
            value = self._calculate_import_price()
        else:
            value = self._calculate_export_price()
        return value if value is not None else self._restored_numeric_value(self._sensor_type)

    async def async_added_to_hass(self) -> None:
        """Restore the last price while coordinator data warms up."""
        await super().async_added_to_hass()
        await self._async_restore_numeric_state()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id),
                self._handle_flow_power_tariff_update,
            )
        )

    @callback
    def _handle_flow_power_tariff_update(self) -> None:
        """Handle Flow Power tariff data updates."""
        self.async_write_ha_state()

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
            **self._coordinator_source_attributes(),
        }

        tariff_price = self._get_current_tariff_price()
        if tariff_price is not None:
            value, tariff_data = tariff_price
            attributes.update(
                {
                    "source": "tariff_schedule",
                    "price_source": tariff_data.get(
                        "price_source",
                        attributes.get("price_source", "tariff_schedule"),
                    ),
                    "using_price_fallback": bool(
                        tariff_data.get(
                            "using_price_fallback",
                            attributes.get("using_price_fallback", False),
                        )
                    ),
                    "current_period": self._current_period,
                    "final_rate_cents": round(value * 100, 2),
                    "utility": tariff_data.get("utility"),
                    "plan_name": tariff_data.get("plan_name"),
                }
            )
            for key in (
                "fallback_reason",
                "fallback_source",
                "primary_source",
                "kwatch_consecutive_failures",
                "kwatch_last_attempt",
                "kwatch_last_success",
            ):
                value = tariff_data.get(key)
                if value not in (None, ""):
                    attributes[key] = value
            if self._sensor_type == SENSOR_TYPE_CURRENT_IMPORT_PRICE:
                attributes["price_spike"] = None
            else:
                attributes["channel_type"] = "feedIn"
            return _entity_currency_attrs(self, attributes, tariff_data)

        if self._is_import_sensor:
            # Import price attributes
            pricing = self._get_pricing_context()
            twap = pricing.twap
            attributes["twap_used"] = round(twap, 2)
            attributes["twap_source"] = pricing.twap_source
            attributes["bpea_cents"] = round(pricing.bpea, 2)
            attributes["bpea_source"] = pricing.bpea_source
            attributes["gst_multiplier"] = pricing.gst_multiplier
            attributes["gst_source"] = pricing.gst_source
            attributes["portal_pricing_active"] = pricing.portal_active

            # TWAP override info
            override = self._get_config_value(CONF_FP_TWAP_OVERRIDE)
            if override is not None and override != "":
                attributes["twap_override"] = override

            # Tariff info
            tariff_rate, avg_daily_tariff = self._get_tariff_data()
            has_tariff = tariff_rate is not None and avg_daily_tariff is not None
            attributes["formula_version"] = "v2" if has_tariff else "v1"
            fp_tc = self._get_config_value(CONF_FP_TARIFF_CODE)
            fp_net = self._get_config_value(CONF_FP_NETWORK)
            if fp_tc:
                attributes["tariff_code"] = fp_tc
            if fp_net:
                attributes["network"] = fp_net

            if has_tariff:
                attributes["network_cents"] = round(tariff_rate, 2)
                attributes["avg_daily_tariff"] = round(avg_daily_tariff, 2)
                attributes["network_tou_adjustment_cents"] = round(
                    tariff_rate - avg_daily_tariff,
                    2,
                )

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
                    final_rate_cents = base_rate + pea
                    attributes["final_rate_cents"] = round(final_rate_cents, 2)
                    if has_tariff:
                        without_network_tou = final_rate_cents - (
                            tariff_rate - avg_daily_tariff
                        )
                        attributes[
                            "price_without_network_tou_adjustment_cents"
                        ] = round(without_network_tou, 2)
                        attributes[
                            "price_without_network_tou_adjustment_dollars"
                        ] = round(without_network_tou / 100, 4)
                else:
                    attributes["pea_cents"] = 0
                    attributes["final_rate_cents"] = base_rate
        else:
            # Export price attributes
            attributes["is_happy_hour"] = self._is_happy_hour()
            attributes["happy_hour_rate"] = self._get_export_rate()

        return _entity_currency_attrs(self, attributes)


class FlowPowerTWAPSensor(PowerSyncCurrencyMixin, SensorEntity):
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
        self._attr_currency_unit = "minor_rate"
        self._attr_currency_attrs = True
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)

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
        return _entity_currency_attrs(self, attrs)


class FlowPowerNetworkTariffSensor(PowerSyncCurrencyMixin, SensorEntity):
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
        self._attr_currency_unit = "minor_rate"
        self._attr_currency_attrs = True
        self._attr_suggested_display_precision = 2

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_added_to_hass(self) -> None:
        """Subscribe to Flow Power tariff data updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id),
                self._handle_flow_power_tariff_update,
            )
        )

    @callback
    def _handle_flow_power_tariff_update(self) -> None:
        """Handle Flow Power tariff data updates."""
        self.async_write_ha_state()

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
        return _entity_currency_attrs(self, attrs)


class FlowPowerAmberComparisonSensor(PowerSyncCurrencyMixin, SensorEntity):
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
        self._attr_currency_unit = "major_rate"
        self._attr_currency_attrs = True
        self._attr_suggested_display_precision = 4

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)

    def _get_config_value(self, key: str, default=None):
        """Get config value from options first, then data."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_added_to_hass(self) -> None:
        """Subscribe to Flow Power tariff data updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id),
                self._handle_flow_power_tariff_update,
            )
        )

    @callback
    def _handle_flow_power_tariff_update(self) -> None:
        """Handle Flow Power tariff data updates."""
        self.async_write_ha_state()

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

        return _entity_currency_attrs(self, attrs)


class FlowPowerPortalSensor(SensorEntity):
    """Sensor for individual Flow Power portal account metrics.

    Reads from hass.data[DOMAIN][entry_id]["flow_power_portal_data"] which is
    populated by the portal client every 30 minutes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sensor_type: str,
        name: str,
        data_key: str,
        unit: str | None,
        icon: str,
        source_label: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._sensor_type = sensor_type
        self._data_key = data_key
        self._source_label = source_label
        self._attr_name = f"Power Sync {name}"
        self._attr_unique_id = f"power_sync_{entry.entry_id}_{sensor_type}"
        self._attr_suggested_object_id = f"power_sync_{sensor_type}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = SensorStateClass.MEASUREMENT if unit else None

    @property
    def device_info(self):
        return provider_pricing_device_info(self._entry.entry_id, SENSOR_FAMILY_FLOW_POWER)

    @property
    def native_value(self) -> float | None:
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        portal_data = domain_data.get("flow_power_portal_data")
        if not portal_data:
            return None
        val = portal_data.get(self._data_key)
        if val is not None:
            try:
                return round(float(val), 3)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        portal_data = domain_data.get("flow_power_portal_data") or {}
        source = portal_data.get("source", self._source_label)
        attrs = {"source": source}
        raw_network_tariff = self._entry.options.get(
            CONF_FLOWPOWER_NETWORK_TARIFF,
            self._entry.data.get(CONF_FLOWPOWER_NETWORK_TARIFF),
        )
        if raw_network_tariff:
            attrs["network_tariff_raw"] = raw_network_tariff
        return attrs

    @property
    def should_poll(self) -> bool:
        return True


class BatteryHealthSensor(SensorEntity):
    """Sensor for battery health / state of health.

    Data sources:
    - Tesla: TEDAPI / Fleet API BMS scan (capacity-based, with per-battery breakdown)
    - Sungrow/Sigenergy/GoodWe: battery_soh from coordinator (Modbus SOH%)
    - FoxESS: no SOH register available (shows Unknown)

    Shows battery health as a percentage. Tesla can be > 100% if batteries
    have more capacity than rated spec.
    """

    _attr_has_entity_name = True
    _attr_name = "Battery Health"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator=None,
        battery_system: str = "tesla",
    ) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_BATTERY_HEALTH}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_BATTERY_HEALTH}"

        # Energy coordinator for reading battery_soh (non-Tesla systems)
        self._coordinator = coordinator
        self._battery_system = battery_system
        self._soh_percent: float | None = None

        # Battery health data (from TEDAPI service call)
        self._original_capacity_wh: float | None = None
        self._current_capacity_wh: float | None = None
        self._degradation_percent: float | None = None
        self._battery_count: int | None = None
        self._scanned_at: str | None = None
        self._source: str | None = None
        self._individual_batteries: list | None = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

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
            self._source = stored_health.get("source")
            self._individual_batteries = stored_health.get("individual_batteries")
            _LOGGER.info(f"Restored battery health from storage: {self._calculate_health_percent()}% health")

        # For non-Tesla systems: listen to coordinator updates for battery_soh
        if self._coordinator is not None and self._battery_system != "tesla":
            self.async_on_remove(
                self._coordinator.async_add_listener(self._handle_coordinator_update)
            )
            # Read initial value if coordinator already has data
            if self._coordinator.data:
                self._handle_coordinator_update()

    @callback
    def _handle_battery_health_update(self, data: dict[str, Any]) -> None:
        """Handle battery health update from service call."""
        self._original_capacity_wh = data.get("original_capacity_wh")
        self._current_capacity_wh = data.get("current_capacity_wh")
        self._degradation_percent = data.get("degradation_percent")
        self._battery_count = data.get("battery_count")
        self._scanned_at = data.get("scanned_at")
        self._source = data.get("source")
        self._individual_batteries = data.get("individual_batteries")

        _LOGGER.info(
            f"Battery health updated: {self._calculate_health_percent()}% health, "
            f"{self._current_capacity_wh}Wh / {self._original_capacity_wh}Wh"
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update — read battery_soh."""
        if not self._coordinator or not self._coordinator.data:
            return
        data = self._coordinator.data
        soh = data.get("battery_soh")
        if soh is not None and soh > 0:
            self._soh_percent = round(float(soh), 1)
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
        Falls back to direct SOH% from coordinator for non-Tesla systems.
        """
        # TEDAPI capacity-based health (Tesla)
        health = self._calculate_health_percent()
        if health is not None:
            return health
        # Direct SOH from coordinator (Sungrow, Sigenergy, GoodWe)
        return self._soh_percent

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
                    attributes[f"{prefix}_label"] = _pack_label(self._individual_batteries, i)
                    din = battery.get("physicalDin") or battery.get("physical_din") or battery.get("din")
                    if din:
                        attributes[f"{prefix}_din"] = din
                    if battery.get("serialNumber"):
                        attributes[f"{prefix}_serial"] = battery.get("serialNumber")
                    bms_serial = battery.get("bmsSerialNumber") or battery.get("bms_serial_number")
                    if bms_serial:
                        attributes[f"{prefix}_bms_serial"] = bms_serial
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
                    if battery.get("isFollower") is not None:
                        attributes[f"{prefix}_is_follower"] = battery.get("isFollower")
                    if battery.get("role") is not None:
                        attributes[f"{prefix}_role"] = battery.get("role")

        # Source attribution
        if self._original_capacity_wh is not None:
            attributes["source"] = self._source or "mobile_app_tedapi"
        elif self._soh_percent is not None:
            attributes["source"] = "inverter_modbus"
            attributes["state_of_health_percent"] = self._soh_percent

        return attributes


class EVStatusSensor(SensorEntity):
    """Polling sensor for EV charging power and SOC.

    Reads EV data from Tesla vehicle sensors (Fleet API / BLE) and
    Wall Connector data. Updates every 30 seconds.
    """

    entity_description: PowerSyncSensorEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: PowerSyncSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._ev_data: dict | None = None
        self._unsub_timer = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_EV_CHARGING)

    async def async_added_to_hass(self) -> None:
        """Start polling when added to hass."""
        await super().async_added_to_hass()
        # Also listen to energy coordinator updates for faster refresh
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        self._unsub_coordinators = []
        for key in ("tesla_coordinator", "sigenergy_coordinator", "solaredge_coordinator"):
            coordinator = entry_data.get(key)
            if coordinator:
                self._unsub_coordinators.append(
                    coordinator.async_add_listener(self._handle_coordinator_update)
                )
        # Poll on a 30s timer for non-Tesla sources
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_update_ev, timedelta(seconds=30)
        )
        # Initial fetch
        await self._async_update_ev()

    async def async_will_remove_from_hass(self) -> None:
        """Stop polling when removed."""
        if self._unsub_timer:
            self._unsub_timer()
        for unsub in getattr(self, "_unsub_coordinators", []):
            unsub()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle energy coordinator updates with embedded EV telemetry."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        for key in ("tesla_coordinator", "sigenergy_coordinator", "solaredge_coordinator"):
            coordinator = entry_data.get(key)
            if not coordinator or not coordinator.data:
                continue
            coord_data = coordinator.data
            ev_power = coord_data.get("ev_power")
            if ev_power is None:
                continue
            if self._ev_data is None:
                self._ev_data = {}
            if key == "solaredge_coordinator":
                self._ev_data["ev_power_kw"] = ev_power
                self._ev_data["vehicle_id"] = "solaredge_ev_charger"
                self._ev_data["vehicle_name"] = "SolarEdge EV Charger"
                self._ev_data["is_connected"] = coord_data.get(
                    "ev_charger_connected", ev_power > 0.05
                )
                self._ev_data["is_charging"] = coord_data.get(
                    "ev_charger_charging", ev_power > 0.05
                )
                self._ev_data["is_discharging"] = coord_data.get(
                    "ev_charger_discharging", False
                )
            elif coord_data.get("ev_charger_type"):
                self._apply_sigenergy_charger_context(
                    charger_type=coord_data.get("ev_charger_type"),
                    ev_power_kw=ev_power,
                    is_connected=coord_data.get("ev_charger_connected", False),
                    is_charging=coord_data.get("ev_charger_charging", False),
                    is_discharging=coord_data.get("ev_charger_discharging", False),
                    ev_soc=coord_data.get("ev_soc"),
                )
            elif abs(ev_power) > 0.05 or self._ev_data.get("ev_power_kw", 0) == 0:
                self._ev_data["ev_power_kw"] = ev_power
        self.async_write_ha_state()

    @staticmethod
    def _active_vehicle(vehicles: list[dict]) -> dict | None:
        """Return the most relevant vehicle for dashboard attribution."""
        if not vehicles:
            return None
        return (
            next(
                (
                    vehicle
                    for vehicle in vehicles
                    if vehicle.get("is_charging")
                    or (vehicle.get("ev_power_kw") or 0) > 0.05
                ),
                None,
            )
            or next(
                (vehicle for vehicle in vehicles if vehicle.get("is_connected")),
                None,
            )
            or vehicles[0]
        )

    def _apply_vehicle_context(self, vehicles: list[dict]) -> None:
        """Attach backend-matched vehicle context to the EV sensor data."""
        if self._ev_data is None:
            self._ev_data = {}

        active_vehicle = self._active_vehicle(vehicles)
        if active_vehicle is None:
            self._ev_data["vehicle_count"] = len(vehicles)
            return

        self._ev_data["vehicle_count"] = len(vehicles)
        for source_key, target_key in (
            ("vehicle_id", "vehicle_id"),
            ("vehicle_name", "vehicle_name"),
            ("is_connected", "is_connected"),
            ("is_charging", "is_charging"),
            ("is_discharging", "is_discharging"),
        ):
            if source_key in active_vehicle:
                self._ev_data[target_key] = active_vehicle.get(source_key)

        active_power = active_vehicle.get("ev_power_kw") or 0
        if abs(active_power) > 0.05 or self._ev_data.get("ev_power_kw", 0) == 0:
            self._ev_data["ev_power_kw"] = active_power

        active_soc = active_vehicle.get("ev_soc")
        if active_soc is not None:
            self._ev_data["ev_soc"] = active_soc

    @staticmethod
    def _sigenergy_vehicle_name(charger_type: Any) -> str:
        """Return a dashboard label for a Sigenergy charger type."""
        if str(charger_type or "").lower() == "evdc":
            return "Sigenergy EVDC"
        return "Sigenergy EVAC"

    def _apply_sigenergy_charger_context(
        self,
        *,
        charger_type: Any,
        ev_power_kw: Any,
        is_connected: Any,
        is_charging: Any,
        is_discharging: Any = False,
        ev_soc: Any = None,
    ) -> None:
        """Attach Sigenergy charger presence so the dashboard can show idle EVs."""
        if self._ev_data is None:
            self._ev_data = {}

        try:
            power_kw = float(ev_power_kw if ev_power_kw is not None else 0.0)
        except (TypeError, ValueError):
            power_kw = 0.0

        self._ev_data["ev_power_kw"] = power_kw
        self._ev_data["vehicle_id"] = "sigenergy_charger"
        self._ev_data["vehicle_name"] = self._sigenergy_vehicle_name(charger_type)
        self._ev_data["is_connected"] = bool(is_connected)
        self._ev_data["is_charging"] = bool(is_charging)
        self._ev_data["is_discharging"] = bool(is_discharging)
        if ev_soc is not None:
            self._ev_data["ev_soc"] = ev_soc

    async def _async_update_ev(self, _now=None) -> None:
        """Poll EV status from vehicle sensors."""
        from . import (
            _get_ev_vehicle_status,
            _get_ev_vehicles_status,
            _read_sigenergy_charger_state_for_entry,
        )
        try:
            self._ev_data = _get_ev_vehicle_status(self.hass, self._entry)
            self._apply_vehicle_context(_get_ev_vehicles_status(self.hass, self._entry))
            # Supplement with Tesla coordinator Wall Connector data
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            tesla_coordinator = entry_data.get("tesla_coordinator")
            if tesla_coordinator and tesla_coordinator.data:
                wc_power = tesla_coordinator.data.get("ev_power", 0)
                if wc_power > 0 and self._ev_data.get("ev_power_kw", 0) <= 0:
                    self._ev_data["ev_power_kw"] = wc_power
            sigenergy_context_applied = False
            sigenergy_coordinator = entry_data.get("sigenergy_coordinator")
            if sigenergy_coordinator and sigenergy_coordinator.data:
                coord_data = sigenergy_coordinator.data
                ev_power = coord_data.get("ev_power")
                if ev_power is not None and coord_data.get("ev_charger_type"):
                    sigenergy_context_applied = True
                    self._apply_sigenergy_charger_context(
                        charger_type=coord_data.get("ev_charger_type"),
                        ev_power_kw=ev_power,
                        is_connected=coord_data.get("ev_charger_connected", False),
                        is_charging=coord_data.get("ev_charger_charging", False),
                        is_discharging=coord_data.get("ev_charger_discharging", False),
                        ev_soc=coord_data.get("ev_soc"),
                    )
                elif ev_power is not None and (
                    abs(ev_power) > 0.05 or self._ev_data.get("ev_power_kw", 0) == 0
                ):
                    self._ev_data["ev_power_kw"] = ev_power

            if not sigenergy_context_applied:
                sigenergy_state = await _read_sigenergy_charger_state_for_entry(
                    self._entry,
                    self.hass,
                )
            else:
                sigenergy_state = None
            if sigenergy_state is not None:
                self._apply_sigenergy_charger_context(
                    charger_type=getattr(sigenergy_state, "charger_type", None),
                    ev_power_kw=getattr(sigenergy_state, "power_kw", 0.0),
                    is_connected=getattr(sigenergy_state, "is_connected", False),
                    is_charging=getattr(sigenergy_state, "is_charging", False),
                    is_discharging=getattr(sigenergy_state, "is_discharging", False),
                    ev_soc=getattr(sigenergy_state, "vehicle_soc", None),
                )
            solaredge_coordinator = entry_data.get("solaredge_coordinator")
            if solaredge_coordinator and solaredge_coordinator.data:
                coord_data = solaredge_coordinator.data
                ev_power = coord_data.get("ev_power")
                if ev_power is not None:
                    self._ev_data["ev_power_kw"] = ev_power
                    self._ev_data["vehicle_id"] = "solaredge_ev_charger"
                    self._ev_data["vehicle_name"] = "SolarEdge EV Charger"
                    self._ev_data["is_connected"] = coord_data.get(
                        "ev_charger_connected", ev_power > 0.05
                    )
                    self._ev_data["is_charging"] = coord_data.get(
                        "ev_charger_charging", ev_power > 0.05
                    )
                    self._ev_data["is_discharging"] = coord_data.get(
                        "ev_charger_discharging", False
                    )
        except Exception:
            _LOGGER.debug("Error polling EV status", exc_info=True)
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self._ev_data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return backend-matched EV attribution for dashboards."""
        if not self._ev_data:
            return {}

        attrs: dict[str, Any] = {}
        for key in (
            "vehicle_id",
            "vehicle_name",
            "is_connected",
            "is_charging",
            "is_discharging",
            "vehicle_count",
        ):
            value = self._ev_data.get(key)
            if value is not None:
                attrs[key] = value
        return attrs

    @property
    def available(self) -> bool:
        """Return True if sensor data is available."""
        return self._ev_data is not None


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
        self._unsub_hold_soc = None
        self._unsub_self_consumption = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

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
        self._unsub_hold_soc = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_hold_soc_state",
            _handle_mode_update,
        )
        self._unsub_self_consumption = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_self_consumption_state",
            _handle_mode_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_force_charge:
            self._unsub_force_charge()
        if self._unsub_force_discharge:
            self._unsub_force_discharge()
        if self._unsub_hold_soc:
            self._unsub_hold_soc()
        if self._unsub_self_consumption:
            self._unsub_self_consumption()

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

        # Check Hold SoC state (locks battery at current SoC for a duration)
        hold_soc_state = entry_data.get("hold_soc_state", {})
        if hold_soc_state.get("active", False):
            return BATTERY_MODE_STATE_HOLD_SOC

        # Check Self-Consumption override (duration-based manual override)
        self_consumption_state = entry_data.get("self_consumption_state", {})
        if self_consumption_state.get("active", False):
            return BATTERY_MODE_STATE_SELF_CONSUMPTION

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
        elif mode == BATTERY_MODE_STATE_HOLD_SOC:
            return "mdi:battery-lock"
        elif mode == BATTERY_MODE_STATE_SELF_CONSUMPTION:
            return "mdi:home-lightning-bolt"
        return "mdi:battery-sync"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        force_charge_state = entry_data.get("force_charge_state", {})
        force_discharge_state = entry_data.get("force_discharge_state", {})
        hold_soc_state = entry_data.get("hold_soc_state", {})
        self_consumption_state = entry_data.get("self_consumption_state", {})

        mode = self._get_current_mode()

        attributes = {
            "mode": mode,
        }

        def _populate_timer_attrs(target: dict, src: dict) -> None:
            """Fill in expires_at + remaining_minutes from a state dict."""
            target["force_duration_minutes"] = src.get("duration", 0)
            if src.get("expires_at"):
                expires_at = src["expires_at"]
                target["expires_at"] = (
                    expires_at.isoformat()
                    if hasattr(expires_at, "isoformat")
                    else str(expires_at)
                )
                target["force_expires_at"] = target["expires_at"]
                from homeassistant.util import dt as dt_util
                remaining = (expires_at - dt_util.utcnow()).total_seconds() / 60
                target["remaining_minutes"] = max(0, int(remaining))
                target["force_remaining_minutes"] = target["remaining_minutes"]

        # Add mode-specific attributes
        if mode == BATTERY_MODE_STATE_FORCE_CHARGE:
            attributes["description"] = "Battery is being force charged"
            _populate_timer_attrs(attributes, force_charge_state)
        elif mode == BATTERY_MODE_STATE_FORCE_DISCHARGE:
            attributes["description"] = "Battery is being force discharged"
            _populate_timer_attrs(attributes, force_discharge_state)
        elif mode == BATTERY_MODE_STATE_HOLD_SOC:
            attributes["description"] = "Battery locked at current state of charge"
            _populate_timer_attrs(attributes, hold_soc_state)
            if hold_soc_state.get("locked_soc") is not None:
                attributes["locked_soc"] = hold_soc_state["locked_soc"]
        elif mode == BATTERY_MODE_STATE_SELF_CONSUMPTION:
            attributes["description"] = "Pure self-consumption (TOU optimisation off)"
            _populate_timer_attrs(attributes, self_consumption_state)
            engaged_at = self_consumption_state.get("engaged_at")
            if engaged_at:
                attributes["engaged_at"] = (
                    engaged_at.isoformat()
                    if hasattr(engaged_at, "isoformat")
                    else str(engaged_at)
                )
        else:
            attributes["description"] = "Battery operating in normal self-consumption mode"

        return attributes


class AmberUsageSensor(PowerSyncCurrencyMixin, SensorEntity):
    """Sensor for actual metered usage/cost data from Amber Usage API.

    Reads from AmberUsageCoordinator via hass.data. Refreshes hourly.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_currency_unit = "money"
    _attr_currency_attrs = True
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_PRICING)

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
            return _entity_currency_attrs(self, {"source": "amber_usage_api"})
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
        return _entity_currency_attrs(self, attrs)
