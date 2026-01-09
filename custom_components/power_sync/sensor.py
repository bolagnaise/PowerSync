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
    SENSOR_TYPE_TARIFF_SCHEDULE,
    SENSOR_TYPE_SOLAR_CURTAILMENT,
    SENSOR_TYPE_FLOW_POWER_PRICE,
    SENSOR_TYPE_FLOW_POWER_EXPORT_PRICE,
    SENSOR_TYPE_BATTERY_HEALTH,
    SENSOR_TYPE_INVERTER_STATUS,
    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
    CONF_INVERTER_BRAND,
    CONF_INVERTER_MODEL,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SLAVE_ID,
    CONF_INVERTER_TOKEN,
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
    FLOW_POWER_PEA_OFFSET,
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
from .coordinator import AmberPriceCoordinator, TeslaEnergyCoordinator, DemandChargeCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PowerSyncSensorEntityDescription(SensorEntityDescription):
    """Describes PowerSync sensor entity."""

    value_fn: Callable[[Any], Any] | None = None
    attr_fn: Callable[[Any], dict[str, Any]] | None = None


def _get_import_price(data):
    """Extract import (general) price from Amber data."""
    if not data or not data.get("current"):
        return None
    for price in data.get("current", []):
        if price.get("channelType") == "general":
            return price.get("perKwh", 0) / 100
    return None


def _get_export_price(data):
    """Extract export earnings from Amber feedIn data.

    Amber convention: feedIn.perKwh is NEGATIVE when you earn money (good)
                      feedIn.perKwh is POSITIVE when you pay to export (bad)

    We negate to show user-friendly "export earnings":
        Positive = earning money per kWh exported
        Negative = paying money per kWh exported
    """
    if not data or not data.get("current"):
        return None
    for price in data.get("current", []):
        if price.get("channelType") == "feedIn":
            # Negate to convert from Amber feedIn to export earnings
            # Amber feedIn +10 (paying) → sensor -0.10 (negative earnings)
            # Amber feedIn -10 (earning) → sensor +0.10 (positive earnings)
            return -price.get("perKwh", 0) / 100
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
    demand_charge_coordinator: DemandChargeCoordinator | None = domain_data.get("demand_charge_coordinator")
    aemo_spike_manager = domain_data.get("aemo_spike_manager")
    is_sigenergy = domain_data.get("is_sigenergy", False)

    entities: list[SensorEntity] = []

    # Add price sensors (only if Amber mode - requires amber_coordinator)
    if amber_coordinator:
        for description in PRICE_SENSORS:
            entities.append(
                AmberPriceSensor(
                    coordinator=amber_coordinator,
                    description=description,
                    entry=entry,
                )
            )

    # Add energy sensors - use Tesla or Sigenergy coordinator depending on battery system
    # Both coordinators return data with same field names (solar_power, grid_power, etc.)
    energy_coordinator = sigenergy_coordinator if is_sigenergy else tesla_coordinator
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
        price_coordinator = amber_coordinator or domain_data.get("aemo_coordinator")
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
            _LOGGER.info("Flow Power price sensors added (import and export)")

    # Always add battery health sensor (receives data from mobile app)
    entities.append(BatteryHealthSensor(entry=entry))
    _LOGGER.info("Battery health sensor added")

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

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(self.coordinator.data)
        return None


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
        self._entry = entry

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
        self._attr_name = "Tariff Schedule"
        self._attr_icon = "mdi:calendar-clock"
        self._unsub_dispatcher = None

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

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()

    @property
    def native_value(self) -> Any:
        """Return the state - number of periods in schedule."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if tariff_data:
            return tariff_data.get("last_sync", "Unknown")
        return "Not synced"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the tariff schedule as attributes for visualization."""
        tariff_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tariff_schedule")
        if not tariff_data:
            return {}

        attributes = {
            "last_sync": tariff_data.get("last_sync"),
            "period_count": len(tariff_data.get("buy_prices", {})),
        }

        # Add buy and sell prices as individual attributes for easy access
        buy_prices = tariff_data.get("buy_prices", {})
        sell_prices = tariff_data.get("sell_prices", {})

        # Create a list format suitable for apexcharts-card visualization
        # Format: list of {time: "HH:MM", buy: price, sell: price}
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

        # Also add raw dicts for flexibility
        attributes["buy_prices"] = buy_prices
        attributes["sell_prices"] = sell_prices

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
        self._attr_icon = "mdi:solar-power-variant"
        self._unsub_dispatcher = None

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
        self._attr_icon = "mdi:solar-panel"
        self._unsub_dispatcher = None
        self._unsub_interval = None
        self._cached_state = None
        self._cached_attrs = {}

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

        if not inverter_host:
            _LOGGER.debug("Inverter host not configured - skipping poll")
            self._cached_state = "not_configured"
            self.async_write_ha_state()
            return

        _LOGGER.debug(f"Polling inverter: {inverter_brand} at {inverter_host}:{inverter_port}")

        try:
            controller = get_inverter_controller(
                brand=inverter_brand,
                host=inverter_host,
                port=inverter_port,
                slave_id=inverter_slave_id,
                model=inverter_model,
                token=inverter_token,
            )

            if not controller:
                _LOGGER.warning(f"Failed to create controller for {inverter_brand}")
                self._cached_state = "error"
                self._cached_attrs = {"error": f"Unsupported brand: {inverter_brand}"}
                self.async_write_ha_state()
                return

            # Get status from inverter
            state = await controller.get_status()
            await controller.disconnect()

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

    def _calculate_import_price(self) -> float | None:
        """Calculate Flow Power import price with PEA in $/kWh."""
        wholesale_cents = self._get_wholesale_price_cents()
        if wholesale_cents is None:
            return None

        # Get config values
        pea_enabled = self._get_config_value(CONF_PEA_ENABLED, True)
        base_rate = self._get_config_value(CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE)
        custom_pea = self._get_config_value(CONF_PEA_CUSTOM_VALUE)

        if pea_enabled:
            # PEA = wholesale - 9.7c
            if custom_pea is not None and custom_pea != "":
                try:
                    pea = float(custom_pea)
                except (ValueError, TypeError):
                    pea = wholesale_cents - FLOW_POWER_PEA_OFFSET
            else:
                pea = wholesale_cents - FLOW_POWER_PEA_OFFSET

            # Final rate = base_rate + PEA (in c/kWh)
            final_cents = base_rate + pea
        else:
            # No PEA - just use base rate
            final_cents = base_rate

        # Convert to $/kWh and clamp to 0 (no negative prices)
        return max(0, final_cents / 100)

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
            if wholesale_cents is not None:
                attributes["wholesale_cents"] = round(wholesale_cents, 2)

                if pea_enabled:
                    if custom_pea is not None and custom_pea != "":
                        try:
                            pea = float(custom_pea)
                        except (ValueError, TypeError):
                            pea = wholesale_cents - FLOW_POWER_PEA_OFFSET
                    else:
                        pea = wholesale_cents - FLOW_POWER_PEA_OFFSET

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

        # Battery health data (from service call)
        self._original_capacity_wh: float | None = None
        self._current_capacity_wh: float | None = None
        self._degradation_percent: float | None = None
        self._battery_count: int | None = None
        self._scanned_at: str | None = None
        self._individual_batteries: list | None = None

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
