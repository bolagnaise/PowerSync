"""Sensor platform for Tesla Sync integration."""
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
from homeassistant.util import dt as dt_util

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
    CONF_DEMAND_CHARGE_ENABLED,
    CONF_DEMAND_CHARGE_RATE,
    CONF_DEMAND_CHARGE_START_TIME,
    CONF_DEMAND_CHARGE_END_TIME,
    CONF_DEMAND_CHARGE_DAYS,
    CONF_DEMAND_CHARGE_BILLING_DAY,
    CONF_AEMO_SPIKE_ENABLED,
    CONF_SOLAR_CURTAILMENT_ENABLED,
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
class TeslaSyncSensorEntityDescription(SensorEntityDescription):
    """Describes Tesla Sync sensor entity."""

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
    """Extract export (feedIn) price from Amber data."""
    if not data or not data.get("current"):
        return None
    for price in data.get("current", []):
        if price.get("channelType") == "feedIn":
            # Export price should always be positive (what you earn)
            # Amber uses negative to indicate earnings, so use abs()
            return abs(price.get("perKwh", 0)) / 100
    return None


PRICE_SENSORS: tuple[TeslaSyncSensorEntityDescription, ...] = (
    TeslaSyncSensorEntityDescription(
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
    TeslaSyncSensorEntityDescription(
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

ENERGY_SENSORS: tuple[TeslaSyncSensorEntityDescription, ...] = (
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_SOLAR_POWER,
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("solar_power") if data else None,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_GRID_POWER,
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("grid_power") if data else None,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_POWER,
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("battery_power") if data else None,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_HOME_LOAD,
        name="Home Load",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("load_power") if data else None,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_LEVEL,
        name="Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("battery_level") if data else None,
    ),
)

DEMAND_CHARGE_SENSORS: tuple[TeslaSyncSensorEntityDescription, ...] = (
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_IN_DEMAND_CHARGE_PERIOD,
        name="In Demand Charge Period",
        value_fn=lambda data: data.get("in_peak_period", False) if data else False,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_PEAK_DEMAND_THIS_CYCLE,
        name="Peak Demand This Cycle",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("peak_demand_kw", 0.0) if data else 0.0,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_DEMAND_CHARGE_COST,
        name="Estimated Demand Charge Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("estimated_cost", 0.0) if data else 0.0,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_SUPPLY_CHARGE_COST,
        name="Daily Supply Charge Cost This Month",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("daily_supply_charge_cost", 0.0) if data else 0.0,
    ),
    TeslaSyncSensorEntityDescription(
        key=SENSOR_TYPE_MONTHLY_SUPPLY_CHARGE,
        name="Monthly Supply Charge",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("monthly_supply_charge", 0.0) if data else 0.0,
    ),
    TeslaSyncSensorEntityDescription(
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
AEMO_SENSORS: tuple[TeslaSyncSensorEntityDescription, ...] = (
    TeslaSyncSensorEntityDescription(
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
    TeslaSyncSensorEntityDescription(
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
    """Set up Tesla Sync sensor entities."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    amber_coordinator: AmberPriceCoordinator | None = domain_data.get("amber_coordinator")
    tesla_coordinator: TeslaEnergyCoordinator = domain_data["tesla_coordinator"]
    demand_charge_coordinator: DemandChargeCoordinator | None = domain_data.get("demand_charge_coordinator")
    aemo_spike_manager = domain_data.get("aemo_spike_manager")

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

    # Add energy sensors
    for description in ENERGY_SENSORS:
        entities.append(
            TeslaEnergySensor(
                coordinator=tesla_coordinator,
                description=description,
                entry=entry,
            )
        )

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
        CONF_SOLAR_CURTAILMENT_ENABLED,
        entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
    )
    if curtailment_enabled:
        entities.append(
            SolarCurtailmentSensor(
                hass=hass,
                entry=entry,
            )
        )
        _LOGGER.info("Solar curtailment sensor added")

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

    async_add_entities(entities)


class AmberPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Amber electricity prices."""

    entity_description: TeslaSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: AmberPriceCoordinator,
        description: TeslaSyncSensorEntityDescription,
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

    entity_description: TeslaSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: TeslaEnergyCoordinator,
        description: TeslaSyncSensorEntityDescription,
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

    entity_description: TeslaSyncSensorEntityDescription

    def __init__(
        self,
        coordinator: DemandChargeCoordinator,
        description: TeslaSyncSensorEntityDescription,
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

    entity_description: TeslaSyncSensorEntityDescription

    def __init__(
        self,
        spike_manager,  # AEMOSpikeManager from __init__.py
        description: TeslaSyncSensorEntityDescription,
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


SIGNAL_TARIFF_UPDATED = "tesla_sync_tariff_updated_{}"


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


SIGNAL_CURTAILMENT_UPDATED = "tesla_sync_curtailment_updated_{}"


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
        self._attr_name = "Solar Curtailment"
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

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()

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
            CONF_SOLAR_CURTAILMENT_ENABLED,
            self._entry.data.get(CONF_SOLAR_CURTAILMENT_ENABLED, False)
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
