"""
Forecast Data Bridge for PowerSync.

Creates Home Assistant sensor entities with forecast attributes for
dashboard visibility and external integration compatibility.

Sensors created:
- sensor.powersync_price_import_forecast
- sensor.powersync_price_export_forecast
- sensor.powersync_solar_forecast
- sensor.powersync_load_forecast

These sensors use the standard forecast format:
{
  "state": <current_value>,
  "attributes": {
    "forecast": [
      {"time": "2024-01-01T00:00:00+00:00", "value": 1.234},
      ...
    ],
    "unit_of_measurement": "...",
    "device_class": "...",
    ...
  }
}

The built-in LP optimizer reads data directly via callbacks, but these
sensors are kept for dashboard charts and external tool compatibility.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Forecast horizon (48 hours at 5-minute intervals = 576 points)
FORECAST_HORIZON_HOURS = 48
FORECAST_INTERVAL_MINUTES = 5
FORECAST_POINTS = FORECAST_HORIZON_HOURS * 60 // FORECAST_INTERVAL_MINUTES

# Sensor configuration for HAEO compatibility
SENSOR_CONFIGS = {
    "price_import": {
        "name": "PowerSync Import Price Forecast",
        "unit": "$/kWh",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:currency-usd",
        "source_description": "Electricity import price forecast",
    },
    "price_export": {
        "name": "PowerSync Export Price Forecast",
        "unit": "$/kWh",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:currency-usd",
        "source_description": "Electricity export/feed-in price forecast",
    },
    "solar": {
        "name": "PowerSync Solar Forecast",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:solar-power",
        "source_description": "Solar PV generation forecast",
    },
    "load": {
        "name": "PowerSync Load Forecast",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:home-lightning-bolt",
        "source_description": "Home load/consumption forecast",
    },
}


class ForecastSensor(Entity):
    """
    Forecast sensor compatible with HAEO (Home Assistant Energy Optimizer).

    Creates sensors with the standard HAEO forecast attribute format,
    allowing seamless integration without additional configuration.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        sensor_type: str,
        name: str,
        unit: str,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = None,
        icon: str | None = None,
        source_entity: str | None = None,
    ) -> None:
        """Initialize the forecast sensor.

        Args:
            hass: Home Assistant instance
            entry_id: Config entry ID
            sensor_type: Type of sensor (price_import, price_export, solar, load)
            name: Friendly name for the sensor
            unit: Unit of measurement
            device_class: HA device class
            state_class: HA state class
            icon: MDI icon
            source_entity: Source entity this forecast is derived from
        """
        self.hass = hass
        self._entry_id = entry_id
        self._sensor_type = sensor_type
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon
        self._attr_unique_id = f"powersync_{entry_id}_{sensor_type}_forecast"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"powersync_{sensor_type}_forecast"

        # Forecast data
        self._forecast: list[dict[str, Any]] = []
        self._current_value: float = 0.0

        # Source tracking (HAFO-compatible)
        self._source_entity = source_entity
        self._last_forecast_update: datetime | None = None

    @property
    def entity_id(self) -> str:
        """Return the entity ID."""
        return f"sensor.powersync_{self._sensor_type}_forecast"

    @property
    def state(self) -> float:
        """Return the current value (first forecast point)."""
        return self._current_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast attributes in HAEO-compatible format.

        HAEO expects:
        - forecast: array of {"time": "ISO8601", "value": float}
        - Optionally: source_entity, last_forecast_update, etc.
        """
        attrs = {
            # Core forecast data (HAEO format)
            "forecast": self._forecast,

            # Metadata
            "forecast_horizon_hours": FORECAST_HORIZON_HOURS,
            "forecast_interval_minutes": FORECAST_INTERVAL_MINUTES,
            "forecast_points": len(self._forecast),

            # HAFO-compatible attributes
            "last_forecast_update": self._last_forecast_update.isoformat() if self._last_forecast_update else None,

            # PowerSync source info
            "source": "powersync",
            "haeo_compatible": True,
        }

        if self._source_entity:
            attrs["source_entity"] = self._source_entity

        return attrs

    def update_forecast(
        self,
        values: list[float],
        start_time: datetime | None = None,
        source_entity: str | None = None,
    ) -> None:
        """Update the forecast data.

        Args:
            values: List of forecast values (one per interval)
            start_time: Start time for forecast (defaults to now)
            source_entity: Optional source entity ID to track
        """
        if start_time is None:
            start_time = dt_util.now()

        # Update source if provided
        if source_entity:
            self._source_entity = source_entity

        # Truncate or pad to expected length
        if len(values) > FORECAST_POINTS:
            values = values[:FORECAST_POINTS]
        elif len(values) < FORECAST_POINTS:
            # Pad with last value or zero
            pad_value = values[-1] if values else 0.0
            values = values + [pad_value] * (FORECAST_POINTS - len(values))

        # Build forecast in HAEO format
        self._forecast = []
        current_time = start_time

        for value in values:
            self._forecast.append({
                "time": current_time.isoformat(),
                "value": round(value, 4),  # Reasonable precision
            })
            current_time += timedelta(minutes=FORECAST_INTERVAL_MINUTES)

        # Update current value (state = nearest forecast value)
        self._current_value = round(values[0], 4) if values else 0.0
        self._last_forecast_update = dt_util.now()

        # Trigger state update
        self.async_write_ha_state()

    def get_haeo_config(self) -> dict[str, str]:
        """Get HAEO configuration for this sensor.

        Returns dict that can be used in HAEO element configuration.
        """
        return {
            "entity_id": self.entity_id,
            "type": self._sensor_type,
            "unit": self._attr_native_unit_of_measurement,
        }


class ForecastBridge:
    """
    Bridge PowerSync data to HAEO-compatible forecast sensors.

    Creates and manages forecast sensors that HAEO can consume directly.
    Users can either:
    1. Manually configure HAEO to use these sensor entity IDs
    2. Let PowerSync auto-configure HAEO via OptimizerConfigurator
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        price_coordinator: Any | None = None,
        solar_forecaster: Any | None = None,
        load_estimator: Any | None = None,
        tariff_schedule: dict | None = None,
    ) -> None:
        """Initialize the forecast data bridge.

        Args:
            hass: Home Assistant instance
            entry_id: Config entry ID
            price_coordinator: Coordinator providing Amber/Octopus prices
            solar_forecaster: SolcastForecaster instance
            load_estimator: LoadEstimator instance
            tariff_schedule: Tariff schedule dict for TOU-based pricing
        """
        self.hass = hass
        self._entry_id = entry_id
        self._price_coordinator = price_coordinator
        self._solar_forecaster = solar_forecaster
        self._load_estimator = load_estimator
        self._tariff_schedule = tariff_schedule

        # Forecast sensors
        self._import_price_sensor: ForecastSensor | None = None
        self._export_price_sensor: ForecastSensor | None = None
        self._solar_sensor: ForecastSensor | None = None
        self._load_sensor: ForecastSensor | None = None

        # Callbacks for custom data sources
        self._get_prices_callback: Callable | None = None
        self._get_solar_callback: Callable | None = None
        self._get_load_callback: Callable | None = None

    def set_data_callbacks(
        self,
        get_prices: Callable | None = None,
        get_solar: Callable | None = None,
        get_load: Callable | None = None,
    ) -> None:
        """Set callbacks for getting forecast data.

        Args:
            get_prices: Callback returning (import_prices, export_prices) in $/kWh
            get_solar: Callback returning solar forecast in Watts
            get_load: Callback returning load forecast in Watts
        """
        self._get_prices_callback = get_prices
        self._get_solar_callback = get_solar
        self._get_load_callback = get_load

    async def setup_forecast_sensors(
        self,
        async_add_entities: AddEntitiesCallback | None = None,
    ) -> list[Entity]:
        """Create HAEO-compatible forecast sensors.

        Returns:
            List of sensor entities to be added to HA
        """
        _LOGGER.info("Setting up HAEO-compatible forecast sensors")

        # Create sensors with full HAEO-compatible configuration
        config = SENSOR_CONFIGS["price_import"]
        self._import_price_sensor = ForecastSensor(
            self.hass,
            self._entry_id,
            "price_import",
            config["name"],
            config["unit"],
            device_class=config.get("device_class"),
            state_class=config.get("state_class"),
            icon=config.get("icon"),
        )

        config = SENSOR_CONFIGS["price_export"]
        self._export_price_sensor = ForecastSensor(
            self.hass,
            self._entry_id,
            "price_export",
            config["name"],
            config["unit"],
            device_class=config.get("device_class"),
            state_class=config.get("state_class"),
            icon=config.get("icon"),
        )

        config = SENSOR_CONFIGS["solar"]
        self._solar_sensor = ForecastSensor(
            self.hass,
            self._entry_id,
            "solar",
            config["name"],
            config["unit"],
            device_class=config.get("device_class"),
            state_class=config.get("state_class"),
            icon=config.get("icon"),
        )

        config = SENSOR_CONFIGS["load"]
        self._load_sensor = ForecastSensor(
            self.hass,
            self._entry_id,
            "load",
            config["name"],
            config["unit"],
            device_class=config.get("device_class"),
            state_class=config.get("state_class"),
            icon=config.get("icon"),
        )

        sensors = [
            self._import_price_sensor,
            self._export_price_sensor,
            self._solar_sensor,
            self._load_sensor,
        ]

        # Add entities if callback provided
        if async_add_entities:
            async_add_entities(sensors, update_before_add=True)

        # Initial forecast update
        await self.update_forecasts()

        _LOGGER.info(
            "Created HAEO-compatible forecast sensors: %s",
            [s.entity_id for s in sensors]
        )

        return sensors

    def get_haeo_sensor_config(self) -> dict[str, str]:
        """Get sensor entity IDs for HAEO configuration.

        Returns dict that can be passed to HAEO network configuration:
        {
            "import_price_sensor": "sensor.powersync_price_import_forecast",
            "export_price_sensor": "sensor.powersync_price_export_forecast",
            "solar_forecast_sensor": "sensor.powersync_solar_forecast",
            "load_forecast_sensor": "sensor.powersync_load_forecast",
        }
        """
        return {
            "import_price_sensor": "sensor.powersync_price_import_forecast",
            "export_price_sensor": "sensor.powersync_price_export_forecast",
            "solar_forecast_sensor": "sensor.powersync_solar_forecast",
            "load_forecast_sensor": "sensor.powersync_load_forecast",
        }

    async def update_forecasts(self) -> None:
        """Update all forecast sensors from data sources."""
        now = dt_util.now()

        # Update price forecasts
        await self._update_price_forecasts(now)

        # Update solar forecast
        await self._update_solar_forecast(now)

        # Update load forecast
        await self._update_load_forecast(now)

    async def _update_price_forecasts(self, start_time: datetime) -> None:
        """Update price forecast sensors."""
        import_prices: list[float] = []
        export_prices: list[float] = []

        # Try callback first
        if self._get_prices_callback:
            try:
                result = await self._get_prices_callback()
                if result:
                    import_prices, export_prices = result
            except Exception as e:
                _LOGGER.warning(f"Error getting prices from callback: {e}")

        # Fallback: get from price coordinator
        if not import_prices and self._price_coordinator:
            import_prices, export_prices = self._extract_prices_from_coordinator()

        # Fallback: get from tariff schedule
        if not import_prices and self._tariff_schedule:
            import_prices, export_prices = self._generate_prices_from_tariff(start_time)

        # Update sensors
        if self._import_price_sensor and import_prices:
            self._import_price_sensor.update_forecast(import_prices, start_time)

        if self._export_price_sensor and export_prices:
            self._export_price_sensor.update_forecast(export_prices, start_time)

    def _extract_prices_from_coordinator(self) -> tuple[list[float], list[float]]:
        """Extract price forecasts from the price coordinator."""
        import_prices: list[float] = []
        export_prices: list[float] = []

        if not self._price_coordinator or not self._price_coordinator.data:
            return import_prices, export_prices

        data = self._price_coordinator.data

        # Amber format
        if "import_prices" in data and "export_prices" in data:
            import_raw = data.get("import_prices", [])
            export_raw = data.get("export_prices", [])

            # Amber prices are in c/kWh, convert to $/kWh
            import_prices = [p.get("perKwh", 0) / 100 for p in import_raw]
            export_prices = [p.get("perKwh", 0) / 100 for p in export_raw]

        # Octopus format
        elif "rates" in data:
            rates = data.get("rates", [])
            for rate in rates:
                # Octopus prices are in p/kWh, convert to $/kWh (assume GBP ~ 0.01 USD for simplicity)
                import_prices.append(rate.get("value_inc_vat", 0) / 100)

            # Octopus export rates
            export_rates = data.get("export_rates", [])
            for rate in export_rates:
                export_prices.append(rate.get("value_inc_vat", 0) / 100)

        return import_prices, export_prices

    def _generate_prices_from_tariff(
        self,
        start_time: datetime,
    ) -> tuple[list[float], list[float]]:
        """Generate price forecasts from static tariff schedule."""
        import_prices: list[float] = []
        export_prices: list[float] = []

        if not self._tariff_schedule:
            return import_prices, export_prices

        current_time = start_time
        energy_charges = self._tariff_schedule.get("energy_charges", {})
        sell_charges = self._tariff_schedule.get("sell_tariff", {}).get("energy_charges", {})

        # Get current season
        season_name = self._get_season_name(current_time)

        for _ in range(FORECAST_POINTS):
            period_name = self._get_period_name(current_time, season_name)

            # Get import price for this period
            import_price = energy_charges.get(season_name, {}).get(period_name, 0.30)
            import_prices.append(import_price)

            # Get export price for this period
            export_price = sell_charges.get(season_name, {}).get(period_name, 0.05)
            export_prices.append(export_price)

            current_time += timedelta(minutes=FORECAST_INTERVAL_MINUTES)

        return import_prices, export_prices

    def _get_season_name(self, dt: datetime) -> str:
        """Determine season name from datetime."""
        if not self._tariff_schedule:
            return "All Year"

        seasons = self._tariff_schedule.get("seasons", {})
        month = dt.month

        for name, season in seasons.items():
            from_month = season.get("fromMonth", 1)
            to_month = season.get("toMonth", 12)

            if from_month <= to_month:
                if from_month <= month <= to_month:
                    return name
            else:
                # Wraps around year (e.g., Nov-Feb)
                if month >= from_month or month <= to_month:
                    return name

        return list(seasons.keys())[0] if seasons else "All Year"

    def _get_period_name(self, dt: datetime, season_name: str) -> str:
        """Determine TOU period name from datetime."""
        if not self._tariff_schedule:
            return "ALL"

        seasons = self._tariff_schedule.get("seasons", {})
        season = seasons.get(season_name, {})
        tou_periods = season.get("tou_periods", {})

        hour = dt.hour
        dow = dt.weekday()
        # Tesla uses 0=Sunday, Python uses 0=Monday
        tesla_dow = (dow + 1) % 7

        for period_name, periods in tou_periods.items():
            for period in periods:
                from_dow = period.get("fromDayOfWeek", 0)
                to_dow = period.get("toDayOfWeek", 6)
                from_hour = period.get("fromHour", 0)
                to_hour = period.get("toHour", 24)

                # Check day of week
                if from_dow <= to_dow:
                    if not (from_dow <= tesla_dow <= to_dow):
                        continue
                else:
                    if not (tesla_dow >= from_dow or tesla_dow <= to_dow):
                        continue

                # Check hour
                if from_hour < to_hour:
                    if from_hour <= hour < to_hour:
                        return period_name
                else:
                    if hour >= from_hour or hour < to_hour:
                        return period_name

        return "OFF_PEAK"

    async def _update_solar_forecast(self, start_time: datetime) -> None:
        """Update solar forecast sensor."""
        solar_forecast: list[float] = []
        source_entity: str | None = None

        # Try callback first
        if self._get_solar_callback:
            try:
                solar_forecast = await self._get_solar_callback()
            except Exception as e:
                _LOGGER.warning(f"Error getting solar from callback: {e}")

        # Fallback: get from solar forecaster
        if not solar_forecast and self._solar_forecaster:
            try:
                solar_forecast = await self._solar_forecaster.get_forecast(
                    horizon_hours=FORECAST_HORIZON_HOURS,
                    start_time=start_time,
                )
                # Track source if available
                if hasattr(self._solar_forecaster, 'solcast_entity'):
                    source_entity = self._solar_forecaster.solcast_entity
            except Exception as e:
                _LOGGER.warning(f"Error getting solar forecast: {e}")

        # Update sensor
        if self._solar_sensor and solar_forecast:
            self._solar_sensor.update_forecast(solar_forecast, start_time, source_entity)

    async def _update_load_forecast(self, start_time: datetime) -> None:
        """Update load forecast sensor."""
        load_forecast: list[float] = []
        source_entity: str | None = None

        # Try callback first
        if self._get_load_callback:
            try:
                load_forecast = await self._get_load_callback()
            except Exception as e:
                _LOGGER.warning(f"Error getting load from callback: {e}")

        # Fallback: get from load estimator
        if not load_forecast and self._load_estimator:
            try:
                load_forecast = await self._load_estimator.get_forecast(
                    horizon_hours=FORECAST_HORIZON_HOURS,
                    start_time=start_time,
                )
                # Track source and HAFO status
                if hasattr(self._load_estimator, 'load_entity_id'):
                    source_entity = self._load_estimator.load_entity_id
            except Exception as e:
                _LOGGER.warning(f"Error getting load forecast: {e}")

        # Update sensor
        if self._load_sensor and load_forecast:
            self._load_sensor.update_forecast(load_forecast, start_time, source_entity)

    @property
    def sensors(self) -> list[ForecastSensor]:
        """Return list of all forecast sensors."""
        sensors = []
        if self._import_price_sensor:
            sensors.append(self._import_price_sensor)
        if self._export_price_sensor:
            sensors.append(self._export_price_sensor)
        if self._solar_sensor:
            sensors.append(self._solar_sensor)
        if self._load_sensor:
            sensors.append(self._load_sensor)
        return sensors
