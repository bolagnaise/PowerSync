"""
Optimization coordinator for PowerSync.

Coordinates data collection and provides schedule data from external
optimizer to the execution layer.

Simplified from previous CVXPY/ML implementation to use external optimizer exclusively.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ConfigEntryNotReady

from .forecast_bridge import ForecastBridge
from .optimizer_configurator import OptimizerConfigurator
from .schedule_reader import ScheduleReader, OptimizationSchedule
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProviderPriceConfig:
    """Configuration for price modifications from electricity provider settings."""
    export_boost_enabled: bool = False
    export_price_offset: float = 0.0
    export_min_price: float = 0.0
    export_boost_start: str = "17:00"
    export_boost_end: str = "21:00"
    export_boost_threshold: float = 0.0
    chip_mode_enabled: bool = False
    chip_mode_start: str = "22:00"
    chip_mode_end: str = "06:00"
    chip_mode_threshold: float = 30.0
    spike_protection_enabled: bool = False


@dataclass
class OptimizationConfig:
    """Configuration for optimization."""
    battery_capacity_wh: int = 13500
    max_charge_w: int = 5000
    max_discharge_w: int = 5000
    backup_reserve: float = 0.2
    interval_minutes: int = 5
    horizon_hours: int = 48
    cost_function: str = "cost"


# Update interval for the coordinator
UPDATE_INTERVAL = timedelta(minutes=5)


class CostFunction:
    """Cost function enumeration."""
    COST_MINIMIZATION = "cost"
    PROFIT_MAXIMIZATION = "profit"
    SELF_CONSUMPTION = "self_consumption"

    def __init__(self, value: str = "cost"):
        self.value = value


class OptimizationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Coordinator for external optimizer-based battery optimization.

    Manages:
    - External optimizer integration setup and configuration
    - Data bridging to optimizer (prices, solar, load forecasts)
    - Reading optimization schedules from optimizer
    - Schedule execution via the executor
    - Providing data for mobile app and HTTP API
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        battery_system: str,
        battery_controller: Any,
        price_coordinator: Any | None = None,
        energy_coordinator: Any | None = None,
        tariff_schedule: dict | None = None,
        force_state_getter: Callable[[], dict] | None = None,
        entry: Any | None = None,
        **kwargs,  # Ignore legacy feature flags
    ):
        """Initialize the optimization coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"power_sync_optimization_{entry_id}",
            update_interval=UPDATE_INTERVAL,
        )

        self.hass = hass
        self.entry_id = entry_id
        self._entry = entry
        self.battery_system = battery_system
        self.battery_controller = battery_controller
        self.price_coordinator = price_coordinator
        self.energy_coordinator = energy_coordinator
        self._tariff_schedule = tariff_schedule
        self._force_state_getter = force_state_getter

        # Configuration
        self._enabled = False
        self._config = OptimizationConfig()
        self._cost_function = CostFunction("cost")
        self._provider_config = ProviderPriceConfig()

        # Optimizer components
        self._forecast_bridge: ForecastBridge | None = None
        self._optimizer_configurator: OptimizerConfigurator | None = None
        self._schedule_reader: ScheduleReader | None = None

        # Data collection components (feed optimizer)
        self._load_estimator: LoadEstimator | None = None
        self._solar_forecaster: SolcastForecaster | None = None

        # Executor
        self._executor: ScheduleExecutor | None = None

        # EV Coordinator
        self._ev_coordinator: EVCoordinator | None = None
        self._ev_configs: list[EVConfig] = []

        # Cached schedule from optimizer
        self._current_schedule: OptimizationSchedule | None = None
        self._last_update_time: datetime | None = None

        # Price monitoring
        self._is_dynamic_pricing = False
        self._price_listener_unsub: Callable | None = None

    @property
    def enabled(self) -> bool:
        """Check if optimization is enabled."""
        return self._enabled

    @property
    def optimiser_available(self) -> bool:
        """Check if external optimizer is available."""
        if self._optimizer_configurator:
            return self.hass.async_add_executor_job(
                lambda: self._optimizer_configurator is not None
            )
        return False

    @property
    def current_schedule(self) -> OptimizationSchedule | None:
        """Get the current optimization schedule."""
        return self._current_schedule

    async def async_setup(self) -> bool:
        """Set up the optimization coordinator with external optimizer."""
        _LOGGER.info("Setting up optimization coordinator")

        # Initialize optimizer configurator
        self._optimizer_configurator = OptimizerConfigurator(self.hass, self._entry)

        # Check if optimizer is installed
        if not await self._optimizer_configurator.ensure_optimizer_installed():
            _LOGGER.warning(
                "External optimizer integration not installed. "
                "Install it from HACS to enable Smart Optimization."
            )
            # Don't fail setup - just disable optimization features
            return True

        # Initialize load estimator
        load_entity = self._get_load_entity_id()
        self._load_estimator = LoadEstimator(
            self.hass,
            load_entity_id=load_entity,
            interval_minutes=self._config.interval_minutes,
        )

        # Initialize solar forecaster
        self._solar_forecaster = SolcastForecaster(
            self.hass,
            interval_minutes=self._config.interval_minutes,
        )

        # Initialize forecast data bridge
        self._forecast_bridge = ForecastBridge(
            self.hass,
            self.entry_id,
            price_coordinator=self.price_coordinator,
            solar_forecaster=self._solar_forecaster,
            load_estimator=self._load_estimator,
            tariff_schedule=self._tariff_schedule,
        )

        # Set data callbacks for the bridge
        self._forecast_bridge.set_data_callbacks(
            get_prices=self._get_price_forecast,
            get_solar=self._get_solar_forecast,
            get_load=self._get_load_forecast,
        )

        # Initialize schedule reader
        self._schedule_reader = ScheduleReader(self.hass)

        # Initialize executor (for battery control)
        self._executor = ScheduleExecutor(
            self.hass,
            optimiser=None,  # No longer using local optimizer
            battery_controller=self.battery_controller,
            interval_minutes=self._config.interval_minutes,
        )

        # Set up data callbacks for executor
        self._executor.set_data_callbacks(
            get_prices=self._get_price_forecast,
            get_solar=self._get_solar_forecast,
            get_load=self._get_load_forecast,
            get_battery_state=self._get_battery_state,
        )

        # Create/verify optimizer network
        battery_config = {
            "capacity_wh": self._config.battery_capacity_wh,
            "max_charge_w": self._config.max_charge_w,
            "max_discharge_w": self._config.max_discharge_w,
            "efficiency": 0.92,
            "backup_reserve": self._config.backup_reserve,
        }

        network_id = await self._optimizer_configurator.create_optimizer_network(battery_config)
        if network_id:
            _LOGGER.info(f"Optimizer network configured: {network_id}")
        else:
            _LOGGER.warning("Failed to create optimizer network - optimization may not work")

        # Set up forecast sensors
        await self._forecast_bridge.setup_forecast_sensors()

        # Set up price-triggered updates for dynamic pricing
        await self._setup_price_listener()

        # Initialize EV coordinator
        await self._setup_ev_coordinator()

        _LOGGER.info(
            f"Optimization coordinator setup complete. "
            f"Optimizer available: {await self._optimizer_configurator.ensure_optimizer_installed()}, "
            f"Battery: {self._config.battery_capacity_wh/1000:.1f}kWh @ {self._config.max_charge_w/1000:.1f}kW"
        )
        return True

    async def _setup_ev_coordinator(self) -> None:
        """Set up EV charging coordination."""
        self._ev_coordinator = EVCoordinator(
            self.hass,
            ev_configs=self._ev_configs,
            price_getter=self._get_price_data_for_ev,
            battery_schedule_getter=self._get_battery_schedule_for_ev,
            solar_forecast_getter=self._get_solar_forecast,
        )
        _LOGGER.debug("EV coordinator initialized")

    async def _get_price_data_for_ev(self) -> list[dict]:
        """Get price data formatted for EV coordinator."""
        if not self.price_coordinator or not self.price_coordinator.data:
            return []

        data = self.price_coordinator.data
        prices = []

        # Amber format
        if "import_prices" in data:
            for p in data.get("import_prices", []):
                prices.append({
                    "time": p.get("startTime"),
                    "perKwh": p.get("perKwh", 0),
                })

        return prices

    async def _get_battery_schedule_for_ev(self) -> list[dict]:
        """Get battery schedule for EV coordinator."""
        if self._current_schedule:
            return self._current_schedule.to_executor_schedule()
        return []

    def _get_load_entity_id(self) -> str | None:
        """Get the load entity ID based on battery system."""
        from ..const import DOMAIN

        # Try to find the home load sensor
        fallbacks = [
            f"sensor.power_sync_home_load",
            f"sensor.power_sync_load",
        ]
        for entity_id in fallbacks:
            if self.hass.states.get(entity_id):
                return entity_id
        return None

    async def _setup_price_listener(self) -> None:
        """Set up price-triggered optimization for dynamic pricing providers."""
        if not self.price_coordinator:
            return

        coordinator_name = type(self.price_coordinator).__name__
        dynamic_providers = ["AmberPriceCoordinator", "AEMOPriceCoordinator"]

        if coordinator_name == "OctopusPriceCoordinator":
            product_code = getattr(self.price_coordinator, "product_code", "")
            if "AGILE" in product_code.upper() or "FLUX" in product_code.upper():
                dynamic_providers.append("OctopusPriceCoordinator")

        self._is_dynamic_pricing = coordinator_name in dynamic_providers

        if self._is_dynamic_pricing:
            self._price_listener_unsub = self.price_coordinator.async_add_listener(
                self._on_price_update
            )
            _LOGGER.info(f"Dynamic pricing detected ({coordinator_name}) - updating optimizer on price changes")

    def _on_price_update(self) -> None:
        """Callback when price coordinator updates."""
        if not self._enabled or not self._is_dynamic_pricing:
            return

        # Update optimizer forecast sensors with new prices
        self.hass.async_create_task(self._update_optimizer_forecasts())

    async def _update_optimizer_forecasts(self) -> None:
        """Update optimizer forecast sensors with latest data."""
        if self._forecast_bridge:
            await self._forecast_bridge.update_forecasts()
            _LOGGER.debug("Updated optimizer forecast sensors")

    async def enable(self) -> bool:
        """Enable optimization and start reading from optimizer."""
        if self._enabled:
            return True

        # Verify optimizer is available
        if not self._optimizer_configurator or not await self._optimizer_configurator.ensure_optimizer_installed():
            _LOGGER.error("Cannot enable optimization - external optimizer not installed")
            return False

        # Update optimizer forecasts
        await self._update_optimizer_forecasts()

        # Start executor (for battery control)
        if self._executor:
            self._executor.set_config(self._config)
            # Don't use periodic timer - we read from optimizer
            success = await self._executor.start(use_periodic_timer=False)
            if not success:
                return False

        self._enabled = True
        _LOGGER.info("Optimization enabled")

        # Start schedule polling
        self.hass.async_create_task(self._schedule_polling_loop())

        # Start EV coordination if enabled
        if self._ev_coordinator and self._ev_configs:
            await self._ev_coordinator.start()
            _LOGGER.info(f"EV coordination started with {len(self._ev_configs)} charger(s)")

        return True

    async def disable(self) -> None:
        """Disable optimization."""
        if not self._enabled:
            return

        if self._price_listener_unsub:
            self._price_listener_unsub()
            self._price_listener_unsub = None

        if self._executor:
            await self._executor.stop()

        if self._ev_coordinator:
            await self._ev_coordinator.stop()

        self._enabled = False
        _LOGGER.info("Optimization disabled")

    async def _schedule_polling_loop(self) -> None:
        """Poll optimizer for schedule updates and execute actions."""
        while self._enabled:
            try:
                # Read schedule from optimizer
                if self._schedule_reader:
                    schedule = await self._schedule_reader.get_schedule()
                    if schedule:
                        self._current_schedule = schedule
                        self._last_update_time = dt_util.now()

                        # Execute current action
                        current_action = await self._schedule_reader.get_current_action()
                        if current_action and self._executor:
                            await self._execute_optimizer_action(current_action)

                # Wait for next interval
                await asyncio.sleep(self._config.interval_minutes * 60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Error in schedule polling: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute

    async def _execute_optimizer_action(self, action: Any) -> None:
        """Execute an optimizer action on the battery."""
        if not self._executor or not self._executor.battery_controller:
            return

        battery = self._executor.battery_controller

        try:
            if action.action == "charge":
                if hasattr(battery, "force_charge"):
                    await battery.force_charge(
                        duration_minutes=self._config.interval_minutes + 5,
                        power_w=action.power_w,
                    )
                    _LOGGER.info(f"Optimizer: Charging at {action.power_w:.0f}W")
            elif action.action in ("discharge", "export"):
                if hasattr(battery, "force_discharge"):
                    await battery.force_discharge(
                        duration_minutes=self._config.interval_minutes + 5,
                        power_w=action.power_w,
                    )
                    _LOGGER.info(f"Optimizer: Discharging at {action.power_w:.0f}W")
            else:
                # Idle or consume - let battery operate normally
                if hasattr(battery, "set_self_consumption_mode"):
                    await battery.set_self_consumption_mode()
                elif hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
                _LOGGER.debug(f"Optimizer: Self-consumption mode (action={action.action})")

        except Exception as e:
            _LOGGER.error(f"Failed to execute optimizer action: {e}")

    async def _get_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts for optimizer."""
        if not self.price_coordinator or not self.price_coordinator.data:
            return None

        data = self.price_coordinator.data
        import_prices = []
        export_prices = []

        # Amber format
        if "import_prices" in data:
            for p in data.get("import_prices", []):
                import_prices.append(p.get("perKwh", 0) / 100)
            for p in data.get("export_prices", []):
                export_prices.append(p.get("perKwh", 0) / 100)

        return (import_prices, export_prices) if import_prices else None

    async def _get_solar_forecast(self) -> list[float] | None:
        """Get solar forecast for optimizer."""
        if self._solar_forecaster:
            return await self._solar_forecaster.get_forecast(
                horizon_hours=self._config.horizon_hours
            )
        return None

    async def _get_load_forecast(self) -> list[float] | None:
        """Get load forecast for optimizer."""
        if self._load_estimator:
            return await self._load_estimator.get_forecast(
                horizon_hours=self._config.horizon_hours
            )
        return None

    async def _get_battery_state(self) -> tuple[float, float]:
        """Get current battery state (SOC, capacity)."""
        soc = 0.5
        capacity = self._config.battery_capacity_wh

        if self.energy_coordinator and self.energy_coordinator.data:
            data = self.energy_coordinator.data
            soc_value = data.get("battery_level")
            if soc_value is not None:
                soc = soc_value / 100 if soc_value > 1 else soc_value

        return soc, capacity

    def _get_actual_battery_power_w(self) -> float:
        """Get actual battery power from energy coordinator."""
        if self.energy_coordinator and self.energy_coordinator.data:
            power = self.energy_coordinator.data.get("battery_power", 0)
            if power is not None:
                return abs(float(power) * 1000) if abs(power) < 100 else abs(power)
        return 0.0

    def set_cost_function(self, cost_function: str | CostFunction) -> None:
        """Set the optimization cost function."""
        if isinstance(cost_function, str):
            self._cost_function = CostFunction(cost_function)
        else:
            self._cost_function = cost_function

        self._config.cost_function = self._cost_function.value
        _LOGGER.info(f"Cost function set to: {self._cost_function.value}")

    def update_config(self, **kwargs) -> None:
        """Update optimization configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)

    async def force_reoptimize(self) -> Any:
        """Force optimizer to re-optimize by updating forecasts."""
        await self._update_optimizer_forecasts()

        # Read updated schedule from optimizer
        if self._schedule_reader:
            schedule = await self._schedule_reader.get_schedule()
            if schedule:
                self._current_schedule = schedule
                return schedule

        return None

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API and mobile app."""
        # Check if optimizer is available
        optimizer_available = self._schedule_reader.is_available() if self._schedule_reader else False

        # Determine status message
        if optimizer_available:
            if self._current_schedule:
                status_message = "Optimization active"
            else:
                status_message = "Optimizer ready - waiting for schedule"
        else:
            status_message = "External optimizer not installed"

        # Get current action info
        current_action = "idle"
        current_power_w = self._get_actual_battery_power_w()
        next_action = "idle"
        next_action_time = None

        if self._current_schedule and self._current_schedule.actions:
            if len(self._current_schedule.actions) > 0:
                current_action = self._current_schedule.actions[0].action
            if len(self._current_schedule.actions) > 1:
                next_action = self._current_schedule.actions[1].action
                next_action_time = self._current_schedule.actions[1].timestamp.isoformat()

        data = {
            "success": True,
            "enabled": self._enabled,
            "optimizer_available": optimizer_available,
            "engine_available": optimizer_available,
            "engine": "external",
            "status_message": status_message,
            "cost_function": self._cost_function.value,
            "status": "active" if self._enabled and optimizer_available else "disabled",
            "optimization_status": "active" if optimizer_available else "not_available",
            "current_action": current_action,
            "current_power_w": current_power_w,
            "next_action": next_action,
            "next_action_time": next_action_time,
            "last_optimization": self._last_update_time.isoformat() if self._last_update_time else None,
            "predicted_cost": self._current_schedule.predicted_cost if self._current_schedule else 0,
            "predicted_savings": self._current_schedule.predicted_savings if self._current_schedule else 0,
            "config": {
                "battery_capacity_wh": self._config.battery_capacity_wh,
                "max_charge_w": self._config.max_charge_w,
                "max_discharge_w": self._config.max_discharge_w,
                "backup_reserve": self._config.backup_reserve,
                "interval_minutes": self._config.interval_minutes,
                "horizon_hours": self._config.horizon_hours,
            },
            "features": {
                "ml_forecasting": False,  # Optimizer handles this
                "weather_integration": False,
                "ev_integration": len(self._ev_configs) > 0,
                "multi_battery": False,
                "vpp_enabled": False,
                "external_optimizer": True,
            },
        }

        # Add EV status if EV coordination is active
        if self._ev_coordinator:
            data["ev"] = self._ev_coordinator.get_status()

        # Add schedule data if available
        if self._current_schedule:
            data["schedule"] = self._current_schedule.to_api_response()
            data["summary"] = {
                "total_cost": self._current_schedule.predicted_cost,
                "total_import_kwh": 0,
                "total_export_kwh": 0,
                "total_charge_kwh": sum(self._current_schedule.charge_w) * (self._config.interval_minutes / 60) / 1000,
                "total_discharge_kwh": sum(self._current_schedule.discharge_w) * (self._config.interval_minutes / 60) / 1000,
                "baseline_cost": 0,
                "savings": self._current_schedule.predicted_savings,
            }
            data["next_actions"] = [a.to_dict() for a in self._current_schedule.actions[:5]]

        return data

    async def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update optimization settings from API."""
        response = {"success": True, "changes": []}

        # Handle enabled toggle
        if "enabled" in settings:
            enabled = settings["enabled"]
            if enabled and not self._enabled:
                success = await self.enable()
                response["changes"].append(f"enabled: {success}")
            elif not enabled and self._enabled:
                await self.disable()
                response["changes"].append("disabled")

            # Persist to config entry
            if self._entry:
                from ..const import CONF_OPTIMIZATION_ENABLED
                new_options = dict(self._entry.options)
                new_options[CONF_OPTIMIZATION_ENABLED] = enabled
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)

        # Handle cost function
        if "cost_function" in settings:
            try:
                self.set_cost_function(settings["cost_function"])
                response["changes"].append(f"cost_function: {settings['cost_function']}")

                if self._entry:
                    from ..const import CONF_OPTIMIZATION_COST_FUNCTION
                    new_data = dict(self._entry.data)
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = settings["cost_function"]
                    self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            except ValueError as e:
                response["success"] = False
                response["error"] = f"Invalid cost function: {e}"
                return response

        # Handle config updates
        config_keys = [
            "battery_capacity_wh", "max_charge_w", "max_discharge_w",
            "backup_reserve", "interval_minutes", "horizon_hours",
        ]
        config_updates = {k: v for k, v in settings.items() if k in config_keys}
        if config_updates:
            self.update_config(**config_updates)
            response["changes"].append(f"config: {list(config_updates.keys())}")

            # Update optimizer network with new battery config
            if self._optimizer_configurator:
                await self._optimizer_configurator.update_optimizer_network({
                    "capacity_wh": self._config.battery_capacity_wh,
                    "max_charge_w": self._config.max_charge_w,
                    "max_discharge_w": self._config.max_discharge_w,
                    "efficiency": 0.92,
                    "backup_reserve": self._config.backup_reserve,
                })

        return response

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from optimizer."""
        if self._schedule_reader and self._enabled:
            schedule = await self._schedule_reader.get_schedule()
            if schedule:
                self._current_schedule = schedule
                self._last_update_time = dt_util.now()

        return self.get_api_data()

    # ========================================
    # EV Charging Coordination Methods
    # ========================================

    def add_ev_charger(
        self,
        entity_id: str,
        name: str | None = None,
        max_power_w: int = 7400,
        target_soc: float = 0.8,
        departure_time: str | None = None,
        price_threshold: float | None = None,
    ) -> bool:
        """Add an EV charger to smart charging coordination.

        Args:
            entity_id: HA entity ID of the EV charger
            name: Friendly name for the charger
            max_power_w: Maximum charging power in watts
            target_soc: Target state of charge (0-1)
            departure_time: Time when car needs to be ready (HH:MM)
            price_threshold: Max $/kWh for smart charging

        Returns:
            True if added successfully
        """
        config = EVConfig(
            entity_id=entity_id,
            name=name or entity_id.split(".")[-1],
            max_charging_power_w=max_power_w,
            target_soc=target_soc,
            departure_time=departure_time,
            price_threshold=price_threshold,
        )

        self._ev_configs.append(config)

        if self._ev_coordinator:
            self._ev_coordinator.add_ev(config)

        _LOGGER.info(f"Added EV charger: {config.name} ({entity_id})")
        return True

    def remove_ev_charger(self, entity_id: str) -> bool:
        """Remove an EV charger from coordination.

        Args:
            entity_id: HA entity ID of the charger to remove

        Returns:
            True if removed successfully
        """
        self._ev_configs = [c for c in self._ev_configs if c.entity_id != entity_id]

        if self._ev_coordinator:
            self._ev_coordinator.remove_ev(entity_id)

        _LOGGER.info(f"Removed EV charger: {entity_id}")
        return True

    def set_ev_charging_mode(self, mode: str) -> bool:
        """Set the EV charging mode.

        Args:
            mode: One of "off", "smart", "solar_only", "immediate", "scheduled"

        Returns:
            True if mode set successfully
        """
        if self._ev_coordinator:
            try:
                self._ev_coordinator.set_mode(EVChargingMode(mode))
                return True
            except ValueError:
                _LOGGER.error(f"Invalid EV charging mode: {mode}")
                return False
        return False

    def get_ev_status(self) -> dict[str, Any]:
        """Get current EV charging status.

        Returns:
            Dict with EV coordination status
        """
        if self._ev_coordinator:
            return self._ev_coordinator.get_status()
        return {"enabled": False, "ev_count": 0, "evs": []}

    async def start_ev_coordination(self) -> bool:
        """Start EV charging coordination.

        Returns:
            True if started successfully
        """
        if self._ev_coordinator:
            return await self._ev_coordinator.start()
        return False

    async def stop_ev_coordination(self) -> None:
        """Stop EV charging coordination."""
        if self._ev_coordinator:
            await self._ev_coordinator.stop()
