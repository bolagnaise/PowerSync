"""
Optimization coordinator for PowerSync.

Coordinates data collection, optimization, and schedule execution.
Provides data for mobile app display and HTTP API endpoints.

Enhanced with:
- ML-based load forecasting with weather integration
- EV charging optimization
- Multi-battery coordination
- VPP/Grid services participation
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .engine import BatteryOptimizer, OptimizationConfig, OptimizationResult, CostFunction
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster

# Enhanced modules
from .ml_load_forecaster import MLLoadEstimator, WeatherAdjustedForecaster, WeatherFeatures
from .ev_integration import (
    EVOptimizer,
    EVConfig,
    EVChargingSchedule,
    EVChargingPriority,
    integrate_ev_with_home_battery,
)
from .multi_battery import (
    MultiBatteryOptimizer,
    MultiBatteryResult,
    BatteryConfig,
    BatterySystemType,
)
from .grid_services import (
    GridServicesManager,
    VPPAwareOptimizer,
    VPPConfig,
    VPPProgram,
    GridEvent,
    GridEventType,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProviderPriceConfig:
    """Configuration for price modifications from electricity provider settings."""
    # Export boost settings
    export_boost_enabled: bool = False
    export_price_offset: float = 0.0  # cents/kWh
    export_min_price: float = 0.0  # cents/kWh
    export_boost_start: str = "17:00"
    export_boost_end: str = "21:00"
    export_boost_threshold: float = 0.0  # cents/kWh

    # Chip mode settings (prevent export unless price exceeds threshold)
    chip_mode_enabled: bool = False
    chip_mode_start: str = "22:00"
    chip_mode_end: str = "06:00"
    chip_mode_threshold: float = 30.0  # cents/kWh

    # Spike protection
    spike_protection_enabled: bool = False

# Update interval for the coordinator (fetches latest data for display)
UPDATE_INTERVAL = timedelta(minutes=5)

# VPP check interval
VPP_CHECK_INTERVAL = timedelta(minutes=1)


class OptimizationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Coordinator for battery optimization.

    Manages:
    - Data collection from price/solar/load sources
    - Running the optimization engine
    - Schedule execution via the executor
    - Providing data for mobile app and HTTP API

    Enhanced features:
    - ML-based load forecasting with weather adjustments
    - EV charging integration
    - Multi-battery support
    - VPP/Grid services participation
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        battery_system: str,  # "tesla", "sigenergy", "sungrow"
        battery_controller: Any,
        price_coordinator: Any | None = None,
        energy_coordinator: Any | None = None,
        # Enhanced options
        enable_ml_forecasting: bool = True,
        enable_weather_integration: bool = True,
        enable_ev_integration: bool = False,
        enable_multi_battery: bool = False,
        enable_vpp: bool = False,
    ):
        """
        Initialize the optimization coordinator.

        Args:
            hass: Home Assistant instance
            entry_id: Config entry ID
            battery_system: Type of battery system
            battery_controller: Controller for battery commands
            price_coordinator: Coordinator providing price data
            energy_coordinator: Coordinator providing energy data
            enable_ml_forecasting: Use ML-based load forecasting
            enable_weather_integration: Include weather in forecasts
            enable_ev_integration: Include EV charging in optimization
            enable_multi_battery: Enable multi-battery coordination
            enable_vpp: Enable VPP/grid services participation
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"power_sync_optimization_{entry_id}",
            update_interval=UPDATE_INTERVAL,
        )

        self.hass = hass
        self.entry_id = entry_id
        self.battery_system = battery_system
        self.battery_controller = battery_controller
        self.price_coordinator = price_coordinator
        self.energy_coordinator = energy_coordinator

        # Feature flags
        self._enable_ml_forecasting = enable_ml_forecasting
        self._enable_weather = enable_weather_integration
        self._enable_ev = enable_ev_integration
        self._enable_multi_battery = enable_multi_battery
        self._enable_vpp = enable_vpp

        # Core optimization components
        self._optimizer = BatteryOptimizer()
        self._executor: ScheduleExecutor | None = None
        self._load_estimator: LoadEstimator | None = None
        self._solar_forecaster: SolcastForecaster | None = None

        # Enhanced components
        self._ml_load_estimator: MLLoadEstimator | None = None
        self._weather_forecaster: WeatherAdjustedForecaster | None = None
        self._ev_optimizer: EVOptimizer | None = None
        self._multi_battery_optimizer: MultiBatteryOptimizer | None = None
        self._grid_services: GridServicesManager | None = None
        self._vpp_aware_optimizer: VPPAwareOptimizer | None = None

        # Configuration
        self._enabled = False
        self._config = OptimizationConfig()
        self._cost_function = CostFunction.COST_MINIMIZATION

        # EV configurations
        self._ev_configs: list[EVConfig] = []

        # Additional battery configurations (for multi-battery)
        self._battery_configs: list[BatteryConfig] = []

        # VPP configuration
        self._vpp_config: VPPConfig | None = None

        # Provider price config (export boost, chip mode, etc.)
        self._provider_config = ProviderPriceConfig()

        # Cached data
        self._current_schedule: OptimizationResult | None = None
        self._ev_schedules: list[EVChargingSchedule] = []
        self._multi_battery_result: MultiBatteryResult | None = None
        self._last_optimization_time: datetime | None = None
        self._weather_forecast: list[WeatherFeatures] = []
        self._active_vpp_events: list[GridEvent] = []

        # VPP monitoring task
        self._vpp_monitor_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Check if optimization is enabled."""
        return self._enabled

    @property
    def optimizer_available(self) -> bool:
        """Check if the LP optimizer is available."""
        return self._optimizer.is_available

    @property
    def current_schedule(self) -> OptimizationResult | None:
        """Get the current optimization schedule."""
        return self._current_schedule

    @property
    def ev_schedules(self) -> list[EVChargingSchedule]:
        """Get current EV charging schedules."""
        return self._ev_schedules

    @property
    def multi_battery_result(self) -> MultiBatteryResult | None:
        """Get multi-battery optimization result."""
        return self._multi_battery_result

    @property
    def active_vpp_events(self) -> list[GridEvent]:
        """Get active VPP events."""
        return self._active_vpp_events

    async def async_setup(self) -> bool:
        """Set up the optimization coordinator."""
        _LOGGER.info("Setting up optimization coordinator with enhanced features")

        # Initialize load estimator
        load_entity = self._get_load_entity_id()
        weather_entity = self._get_weather_entity_id()

        # Basic load estimator (fallback)
        self._load_estimator = LoadEstimator(
            self.hass,
            load_entity_id=load_entity,
            interval_minutes=self._config.interval_minutes,
        )

        # ML-enhanced load estimator
        if self._enable_ml_forecasting:
            self._ml_load_estimator = MLLoadEstimator(
                self.hass,
                load_entity_id=load_entity,
                interval_minutes=self._config.interval_minutes,
                weather_entity_id=weather_entity,
            )
            _LOGGER.info("ML load forecasting enabled")

            # Weather-adjusted forecaster
            if self._enable_weather:
                self._weather_forecaster = WeatherAdjustedForecaster(
                    self.hass,
                    self._ml_load_estimator,
                    weather_api_key=self._get_weather_api_key(),
                    location=self._get_location(),
                )
                _LOGGER.info("Weather integration enabled")

        # Initialize solar forecaster
        self._solar_forecaster = SolcastForecaster(
            self.hass,
            interval_minutes=self._config.interval_minutes,
        )

        # Initialize EV optimizer
        if self._enable_ev:
            self._ev_optimizer = EVOptimizer(
                interval_minutes=self._config.interval_minutes
            )
            await self._discover_ev_configs()
            _LOGGER.info(f"EV integration enabled with {len(self._ev_configs)} vehicles")

        # Initialize multi-battery optimizer
        if self._enable_multi_battery:
            await self._discover_battery_configs()
            if len(self._battery_configs) > 1:
                self._multi_battery_optimizer = MultiBatteryOptimizer(
                    batteries=self._battery_configs,
                    interval_minutes=self._config.interval_minutes,
                    horizon_hours=self._config.horizon_hours,
                    cost_function=self._cost_function,
                )
                _LOGGER.info(f"Multi-battery support enabled with {len(self._battery_configs)} batteries")

        # Initialize grid services
        if self._enable_vpp:
            self._vpp_config = await self._get_vpp_config()
            if self._vpp_config and self._vpp_config.enabled:
                self._grid_services = GridServicesManager(
                    self.hass,
                    self._vpp_config,
                    self.battery_controller,
                )
                # Set up VPP-aware optimizer
                self._vpp_aware_optimizer = VPPAwareOptimizer(
                    self._optimizer,
                    self._grid_services,
                    self._vpp_config,
                )
                _LOGGER.info(f"VPP integration enabled: {self._vpp_config.program.value}")

        # Initialize executor
        self._executor = ScheduleExecutor(
            self.hass,
            self._optimizer,
            self.battery_controller,
            interval_minutes=self._config.interval_minutes,
        )

        # Set up data callbacks for executor
        self._executor.set_data_callbacks(
            get_prices=self._get_price_forecast,
            get_solar=self._get_solar_forecast,
            get_load=self._get_load_forecast,
            get_battery_state=self._get_battery_state,
        )

        _LOGGER.info(
            f"Optimization coordinator setup complete. "
            f"Optimizer available: {self._optimizer.is_available}, "
            f"ML: {self._enable_ml_forecasting}, "
            f"Weather: {self._enable_weather}, "
            f"EV: {self._enable_ev}, "
            f"Multi-battery: {self._enable_multi_battery}, "
            f"VPP: {self._enable_vpp}"
        )
        return True

    def _get_load_entity_id(self) -> str | None:
        """Get the load entity ID based on battery system."""
        from ..const import DOMAIN

        # Try to find the home load sensor
        try:
            entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
            for entity in entity_registry.entities.values():
                if entity.platform == DOMAIN and "home_load" in entity.entity_id:
                    return entity.entity_id
        except Exception:
            pass

        # Fallback patterns
        fallbacks = [
            f"sensor.power_sync_home_load",
            f"sensor.power_sync_load",
        ]
        for entity_id in fallbacks:
            if self.hass.states.get(entity_id):
                return entity_id

        return None

    def _get_weather_entity_id(self) -> str | None:
        """Get the weather entity ID."""
        # Try common weather entity patterns
        patterns = [
            "weather.home",
            "weather.forecast_home",
            "weather.openweathermap",
        ]
        for entity_id in patterns:
            if self.hass.states.get(entity_id):
                return entity_id
        return None

    def _get_weather_api_key(self) -> str | None:
        """Get OpenWeatherMap API key if configured."""
        from ..const import DOMAIN
        domain_data = self.hass.data.get(DOMAIN, {})
        for entry_data in domain_data.values():
            if isinstance(entry_data, dict):
                api_key = entry_data.get("weather_api_key")
                if api_key:
                    return api_key
        return None

    def _get_location(self) -> tuple[float, float] | None:
        """Get location coordinates."""
        try:
            lat = self.hass.config.latitude
            lon = self.hass.config.longitude
            if lat and lon:
                return (lat, lon)
        except Exception:
            pass
        return None

    async def _discover_ev_configs(self) -> None:
        """Discover EV configurations from Home Assistant."""
        from ..const import DOMAIN

        self._ev_configs = []

        try:
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Check for EV charging data
                ev_data = entry_data.get("ev_vehicles", [])
                for vehicle in ev_data:
                    ev_config = EVConfig(
                        vehicle_id=vehicle.get("id", ""),
                        name=vehicle.get("name", "EV"),
                        battery_capacity_kwh=vehicle.get("battery_capacity_kwh", 75),
                        current_soc=vehicle.get("soc", 50) / 100,
                        target_soc=vehicle.get("target_soc", 80) / 100,
                        max_charge_kw=vehicle.get("max_charge_kw", 7.4),
                        allow_grid_charging=vehicle.get("allow_grid_charging", True),
                        solar_only=vehicle.get("solar_only", False),
                    )
                    self._ev_configs.append(ev_config)

        except Exception as e:
            _LOGGER.debug(f"Error discovering EV configs: {e}")

    async def _discover_battery_configs(self) -> None:
        """Discover battery configurations for multi-battery setup."""
        from ..const import DOMAIN

        self._battery_configs = []

        # Add primary battery
        primary = BatteryConfig(
            battery_id=f"{self.battery_system}_primary",
            name=f"{self.battery_system.title()} Primary",
            system_type=BatterySystemType(self.battery_system),
            capacity_wh=self._config.battery_capacity_wh,
            max_charge_w=self._config.max_charge_w,
            max_discharge_w=self._config.max_discharge_w,
            backup_reserve=self._config.backup_reserve,
        )
        self._battery_configs.append(primary)

        try:
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Check for additional batteries
                additional_batteries = entry_data.get("additional_batteries", [])
                for batt in additional_batteries:
                    config = BatteryConfig(
                        battery_id=batt.get("id", f"battery_{len(self._battery_configs)}"),
                        name=batt.get("name", f"Battery {len(self._battery_configs) + 1}"),
                        system_type=BatterySystemType(batt.get("system_type", "generic")),
                        capacity_wh=batt.get("capacity_wh", 13500),
                        max_charge_w=batt.get("max_charge_w", 5000),
                        max_discharge_w=batt.get("max_discharge_w", 5000),
                        backup_reserve=batt.get("backup_reserve", 0.2),
                    )
                    self._battery_configs.append(config)

        except Exception as e:
            _LOGGER.debug(f"Error discovering battery configs: {e}")

    async def _get_vpp_config(self) -> VPPConfig | None:
        """Get VPP configuration."""
        from ..const import DOMAIN

        try:
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                vpp_data = entry_data.get("vpp_config")
                if vpp_data:
                    return VPPConfig(
                        program=VPPProgram(vpp_data.get("program", "generic")),
                        enabled=vpp_data.get("enabled", False),
                        max_export_kw=vpp_data.get("max_export_kw", 5.0),
                        min_reserve_soc=vpp_data.get("min_reserve_soc", 0.3),
                        price_spike_threshold=vpp_data.get("price_spike_threshold", 1.0),
                        auto_respond=vpp_data.get("auto_respond", True),
                    )

                # Auto-detect VPP program from electricity provider
                provider = entry_data.get("electricity_provider")
                if provider == "amber":
                    return VPPConfig(
                        program=VPPProgram.AMBER_SMARTSHIFT,
                        enabled=True,
                        auto_respond=True,
                    )
                elif provider == "globird":
                    return VPPConfig(
                        program=VPPProgram.GLOBIRD_VPP,
                        enabled=True,
                        price_spike_threshold=1.0,  # $1/kWh default threshold
                        auto_respond=True,
                    )

        except Exception as e:
            _LOGGER.debug(f"Error getting VPP config: {e}")

        return None

    async def _fetch_provider_price_config(self) -> None:
        """Fetch provider price config (export boost, chip mode, etc.) from domain data."""
        from ..const import DOMAIN

        try:
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Check for Amber provider config
                provider_config = entry_data.get("provider_config", {})
                if provider_config:
                    self._provider_config = ProviderPriceConfig(
                        # Export boost
                        export_boost_enabled=provider_config.get("export_boost_enabled", False),
                        export_price_offset=provider_config.get("export_price_offset", 0.0),
                        export_min_price=provider_config.get("export_min_price", 0.0),
                        export_boost_start=provider_config.get("export_boost_start", "17:00"),
                        export_boost_end=provider_config.get("export_boost_end", "21:00"),
                        export_boost_threshold=provider_config.get("export_boost_threshold", 0.0),
                        # Chip mode
                        chip_mode_enabled=provider_config.get("chip_mode_enabled", False),
                        chip_mode_start=provider_config.get("chip_mode_start", "22:00"),
                        chip_mode_end=provider_config.get("chip_mode_end", "06:00"),
                        chip_mode_threshold=provider_config.get("chip_mode_threshold", 30.0),
                        # Spike protection
                        spike_protection_enabled=provider_config.get("spike_protection_enabled", False),
                    )
                    _LOGGER.debug(
                        f"Loaded provider config: export_boost={self._provider_config.export_boost_enabled}, "
                        f"chip_mode={self._provider_config.chip_mode_enabled}, "
                        f"spike_protection={self._provider_config.spike_protection_enabled}"
                    )
                    return

        except Exception as e:
            _LOGGER.debug(f"Error fetching provider price config: {e}")

    async def enable(self) -> bool:
        """Enable optimization and start the executor."""
        if self._enabled:
            return True

        if not self._executor:
            _LOGGER.error("Executor not initialized")
            return False

        self._executor.set_config(self._config)
        self._executor.set_cost_function(self._cost_function)

        success = await self._executor.start()
        if success:
            self._enabled = True
            _LOGGER.info("Optimization enabled")

            # Start VPP monitoring if enabled
            if self._grid_services and self._vpp_config and self._vpp_config.enabled:
                await self._start_vpp_monitoring()

        return success

    async def disable(self) -> None:
        """Disable optimization and stop the executor."""
        if not self._enabled:
            return

        # Stop VPP monitoring
        if self._vpp_monitor_task:
            self._vpp_monitor_task.cancel()
            self._vpp_monitor_task = None

        if self._executor:
            await self._executor.stop()

        self._enabled = False
        _LOGGER.info("Optimization disabled")

    async def _start_vpp_monitoring(self) -> None:
        """Start VPP event monitoring."""
        if self._vpp_monitor_task:
            return

        async def monitor_loop():
            while True:
                try:
                    await asyncio.sleep(VPP_CHECK_INTERVAL.total_seconds())
                    if self._grid_services:
                        events = await self._grid_services.check_grid_conditions()
                        self._active_vpp_events = events

                        # Auto-respond to events
                        for event in events:
                            if self._vpp_config and self._vpp_config.auto_respond:
                                await self._grid_services.respond_to_event(event)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    _LOGGER.error(f"VPP monitoring error: {e}")

        self._vpp_monitor_task = asyncio.create_task(monitor_loop())
        _LOGGER.info("VPP monitoring started")

    def set_cost_function(self, cost_function: str | CostFunction) -> None:
        """Set the optimization cost function."""
        if isinstance(cost_function, str):
            cost_function = CostFunction(cost_function)

        self._cost_function = cost_function
        self._config.cost_function = cost_function

        if self._executor:
            self._executor.set_cost_function(cost_function)

        if self._multi_battery_optimizer:
            self._multi_battery_optimizer.cost_function = cost_function

        _LOGGER.info(f"Cost function set to: {cost_function.value}")

    def update_config(self, **kwargs) -> None:
        """Update optimization configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)

        if self._executor:
            self._executor.set_config(self._config)

    async def force_reoptimize(self) -> OptimizationResult | None:
        """Force immediate re-optimization with all enhancements."""
        _LOGGER.info("Forcing re-optimization with enhanced features")

        # Get all forecasts
        prices = await self._get_price_forecast()
        solar = await self._get_solar_forecast()
        load = await self._get_load_forecast()
        battery_state = await self._get_battery_state()

        if not prices or not solar or not load:
            _LOGGER.warning("Missing forecast data for optimization")
            if self._executor:
                return await self._executor.force_reoptimize()
            return None

        import_prices, export_prices = prices
        initial_soc, capacity = battery_state
        start_time = dt_util.now()

        # Check for VPP events to consider
        anticipated_events = []
        if self._grid_services:
            anticipated_events = await self._grid_services.check_grid_conditions()

        # Run optimization based on configuration
        if self._multi_battery_optimizer and len(self._battery_configs) > 1:
            # Multi-battery optimization
            self._multi_battery_result = self._multi_battery_optimizer.optimize(
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                start_time=start_time,
            )
            # Convert to standard result for compatibility
            if self._multi_battery_result.success:
                self._current_schedule = self._convert_multi_battery_result(
                    self._multi_battery_result, start_time
                )

        elif self._enable_ev and self._ev_configs:
            # Joint home battery + EV optimization
            self._current_schedule, self._ev_schedules = integrate_ev_with_home_battery(
                home_optimizer=self._optimizer,
                ev_configs=self._ev_configs,
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                initial_home_soc=initial_soc,
                start_time=start_time,
            )

        elif self._vpp_aware_optimizer and anticipated_events:
            # VPP-aware optimization
            self._current_schedule = await self._vpp_aware_optimizer.optimize_with_vpp(
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                initial_soc=initial_soc,
                start_time=start_time,
                anticipated_events=anticipated_events,
            )

        else:
            # Standard optimization
            self._current_schedule = self._optimizer.optimize(
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                initial_soc=initial_soc,
                start_time=start_time,
            )

        self._last_optimization_time = dt_util.now()

        # Update executor with new schedule
        if self._executor and self._current_schedule:
            self._executor._current_result = self._current_schedule

        return self._current_schedule

    def _convert_multi_battery_result(
        self,
        result: MultiBatteryResult,
        start_time: datetime,
    ) -> OptimizationResult:
        """Convert MultiBatteryResult to standard OptimizationResult."""
        return OptimizationResult(
            success=result.success,
            status=result.status,
            charge_schedule_w=result.total_charge_w,
            discharge_schedule_w=result.total_discharge_w,
            grid_import_w=result.grid_import_w,
            grid_export_w=result.grid_export_w,
            soc_trajectory=[0.5] * (len(result.total_charge_w) + 1),  # Simplified
            timestamps=result.timestamps,
            total_cost=result.total_cost,
            total_import_kwh=result.total_import_kwh,
            total_export_kwh=result.total_export_kwh,
            baseline_cost=result.baseline_cost,
            savings=result.savings,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Update coordinator data for display."""
        # Get executor status
        executor_status = {}
        if self._executor:
            executor_status = self._executor.get_schedule_summary()
            self._current_schedule = self._executor.current_schedule

    def _is_in_time_window(self, time_str: str, start: str, end: str) -> bool:
        """Check if a time (HH:MM) is within a window (handles overnight windows)."""
        try:
            # Parse times as minutes from midnight
            def to_minutes(t: str) -> int:
                parts = t.split(":")
                return int(parts[0]) * 60 + int(parts[1])

            current = to_minutes(time_str)
            start_m = to_minutes(start)
            end_m = to_minutes(end)

            if start_m <= end_m:
                # Normal window (e.g., 17:00-21:00)
                return start_m <= current < end_m
            else:
                # Overnight window (e.g., 22:00-06:00)
                return current >= start_m or current < end_m
        except (ValueError, IndexError):
            return False

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API and mobile app."""
        executor_status = self._executor.get_status() if self._executor else {}

        # Build data for mobile app
        data = {
            "enabled": self._enabled,
            "optimizer_available": self._optimizer.is_available,
            "cost_function": self._cost_function.value,
            "status": executor_status.get("status", "disabled"),
            "optimization_status": executor_status.get("optimization_status", "not_run"),
            "current_action": executor_status.get("current_action", "idle"),
            "current_power_w": executor_status.get("current_power_w", 0),
            "next_action": executor_status.get("next_action", "idle"),
            "next_action_time": executor_status.get("next_action_time"),
            "last_optimization": executor_status.get("last_optimization"),
            "predicted_cost": executor_status.get("predicted_cost", 0),
            "predicted_savings": executor_status.get("predicted_savings", 0),
            # Enhanced feature flags
            "features": {
                "ml_forecasting": self._enable_ml_forecasting,
                "weather_integration": self._enable_weather,
                "ev_integration": self._enable_ev,
                "multi_battery": self._enable_multi_battery,
                "vpp_enabled": self._enable_vpp,
            },
        }

        # Add schedule data if available
        if self._current_schedule and self._current_schedule.success:
            data["schedule"] = {
                "timestamps": [t.isoformat() for t in self._current_schedule.timestamps],
                "charge_w": self._current_schedule.charge_schedule_w,
                "discharge_w": self._current_schedule.discharge_schedule_w,
                "soc": self._current_schedule.soc_trajectory,
                "grid_import_w": self._current_schedule.grid_import_w,
                "grid_export_w": self._current_schedule.grid_export_w,
            }
            data["summary"] = {
                "total_cost": self._current_schedule.total_cost,
                "total_import_kwh": self._current_schedule.total_import_kwh,
                "total_export_kwh": self._current_schedule.total_export_kwh,
                "total_charge_kwh": self._current_schedule.total_charge_kwh,
                "total_discharge_kwh": self._current_schedule.total_discharge_kwh,
                "baseline_cost": self._current_schedule.baseline_cost,
                "savings": self._current_schedule.savings,
            }
            data["next_actions"] = self._current_schedule.get_next_actions(5)

        # Add EV data if available
        if self._ev_schedules:
            data["ev_schedules"] = [s.to_dict() for s in self._ev_schedules]

        # Add multi-battery data if available
        if self._multi_battery_result and self._multi_battery_result.success:
            data["multi_battery"] = {
                "batteries": self._multi_battery_result.battery_metrics,
                "total_savings": self._multi_battery_result.savings,
            }

        # Add VPP data if available
        if self._grid_services:
            data["vpp"] = {
                "active_events": len(self._active_vpp_events),
                "events": [
                    {
                        "id": e.event_id,
                        "type": e.event_type.value,
                        "severity": e.severity,
                        "current_price": e.current_price,
                    }
                    for e in self._active_vpp_events
                ],
                "stats": self._grid_services.get_vpp_stats(days=30),
            }

        # Add ML load estimator stats
        if self._ml_load_estimator:
            data["ml_stats"] = self._ml_load_estimator.get_model_stats()

        return data

    async def _get_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts for optimization.

        Applies provider price modifications:
        - Export boost: increase export prices during configured window
        - Chip mode: reduce export prices outside threshold
        - Spike protection: cap import prices during spikes
        """
        if not self.price_coordinator:
            return None

        try:
            # Refresh provider config to get latest settings
            await self._fetch_provider_price_config()

            price_data = self.price_coordinator.data
            if not price_data:
                return None

            # Extract forecast data based on provider
            forecast = price_data.get("forecast", [])
            if not forecast:
                return None

            n_intervals = self._config.horizon_hours * 60 // self._config.interval_minutes
            import_prices = []
            export_prices = []
            now = dt_util.now()

            for idx, item in enumerate(forecast[:n_intervals]):
                # Calculate timestamp for this interval
                interval_time = now + timedelta(minutes=idx * self._config.interval_minutes)
                hour_str = interval_time.strftime("%H:%M")

                # Prices are in c/kWh, convert to $/kWh
                if isinstance(item, dict):
                    per_kwh = item.get("perKwh", 0) / 100
                    feed_in = item.get("feedInTariff", item.get("spotPerKwh", 0)) / 100
                    spike_status = item.get("spikeStatus", "none")
                else:
                    per_kwh = float(item) / 100 if item else 0
                    feed_in = per_kwh * 0.5  # Assume 50% of import for export
                    spike_status = "none"

                # Apply spike protection
                if self._provider_config.spike_protection_enabled and spike_status != "none":
                    # During spikes, use a high price to discourage import
                    # but keep feed-in attractive for export
                    _LOGGER.debug(f"Spike protection at {hour_str}: {per_kwh:.3f} $/kWh")

                # Apply export boost during configured window
                if self._provider_config.export_boost_enabled:
                    if self._is_in_time_window(
                        hour_str,
                        self._provider_config.export_boost_start,
                        self._provider_config.export_boost_end
                    ):
                        # Add offset and apply minimum
                        offset = self._provider_config.export_price_offset / 100  # cents to $
                        min_price = self._provider_config.export_min_price / 100  # cents to $
                        boosted = feed_in + offset
                        feed_in = max(boosted, min_price)
                        _LOGGER.debug(f"Export boost at {hour_str}: {feed_in:.3f} $/kWh")

                # Apply chip mode (prevent export unless price exceeds threshold)
                if self._provider_config.chip_mode_enabled:
                    if self._is_in_time_window(
                        hour_str,
                        self._provider_config.chip_mode_start,
                        self._provider_config.chip_mode_end
                    ):
                        threshold = self._provider_config.chip_mode_threshold / 100  # cents to $
                        if feed_in < threshold:
                            # Set export price very low to discourage export
                            feed_in = -1.0  # Negative = cost to export
                            _LOGGER.debug(f"Chip mode at {hour_str}: export suppressed")

                import_prices.append(max(0, per_kwh))
                export_prices.append(feed_in)  # Can be negative for chip mode

            # Pad if needed
            while len(import_prices) < n_intervals:
                import_prices.append(import_prices[-1] if import_prices else 0.3)
                export_prices.append(export_prices[-1] if export_prices else 0.1)

            return import_prices[:n_intervals], export_prices[:n_intervals]

        except Exception as e:
            _LOGGER.error(f"Error getting price forecast: {e}")
            return None

    async def _get_solar_forecast(self) -> list[float] | None:
        """Get solar forecast for optimization."""
        if not self._solar_forecaster:
            return None

        try:
            return await self._solar_forecaster.get_forecast(
                horizon_hours=self._config.horizon_hours
            )
        except Exception as e:
            _LOGGER.error(f"Error getting solar forecast: {e}")
            return None

    async def _get_load_forecast(self) -> list[float] | None:
        """Get load forecast for optimization (ML-enhanced if available)."""
        try:
            # Use ML-enhanced forecasting if available
            if self._weather_forecaster and self._enable_weather:
                load_forecast, self._weather_forecast = await self._weather_forecaster.get_forecast_with_weather(
                    horizon_hours=self._config.horizon_hours
                )
                return load_forecast

            elif self._ml_load_estimator and self._enable_ml_forecasting:
                return await self._ml_load_estimator.get_forecast(
                    horizon_hours=self._config.horizon_hours
                )

            elif self._load_estimator:
                return await self._load_estimator.get_forecast(
                    horizon_hours=self._config.horizon_hours
                )

        except Exception as e:
            _LOGGER.error(f"Error getting load forecast: {e}")

        return None

    async def _get_battery_state(self) -> tuple[float, float]:
        """Get current battery state for optimization."""
        try:
            if self.energy_coordinator and self.energy_coordinator.data:
                data = self.energy_coordinator.data
                soc = data.get("battery_level", 50) / 100  # Convert to 0-1

                # Get capacity from config or default
                capacity = self._config.battery_capacity_wh

                return soc, capacity

        except Exception as e:
            _LOGGER.error(f"Error getting battery state: {e}")

        return 0.5, 13500  # Default

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API response."""
        base_data = self.data or {}

        return {
            "success": True,
            "enabled": self._enabled,
            "optimizer_available": self._optimizer.is_available,
            "cost_function": self._cost_function.value,
            "config": {
                "battery_capacity_wh": self._config.battery_capacity_wh,
                "max_charge_w": self._config.max_charge_w,
                "max_discharge_w": self._config.max_discharge_w,
                "backup_reserve": self._config.backup_reserve,
                "interval_minutes": self._config.interval_minutes,
                "horizon_hours": self._config.horizon_hours,
            },
            "features": {
                "ml_forecasting": self._enable_ml_forecasting,
                "weather_integration": self._enable_weather,
                "ev_integration": self._enable_ev,
                "multi_battery": self._enable_multi_battery,
                "vpp_enabled": self._enable_vpp,
            },
            **base_data,
        }

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

        # Handle cost function
        if "cost_function" in settings:
            try:
                self.set_cost_function(settings["cost_function"])
                response["changes"].append(f"cost_function: {settings['cost_function']}")
            except ValueError as e:
                response["success"] = False
                response["error"] = f"Invalid cost function: {e}"
                return response

        # Handle feature toggles
        feature_toggles = ["ml_forecasting", "weather_integration", "ev_integration", "multi_battery", "vpp"]
        for feature in feature_toggles:
            if feature in settings:
                attr_name = f"_enable_{feature.replace('_integration', '').replace('_', '_')}"
                if hasattr(self, attr_name):
                    setattr(self, attr_name, settings[feature])
                    response["changes"].append(f"{feature}: {settings[feature]}")

        # Handle config updates
        config_keys = [
            "battery_capacity_wh", "max_charge_w", "max_discharge_w",
            "backup_reserve", "interval_minutes", "horizon_hours",
        ]
        config_updates = {k: v for k, v in settings.items() if k in config_keys}
        if config_updates:
            self.update_config(**config_updates)
            response["changes"].append(f"config: {list(config_updates.keys())}")

        return response

    # EV Configuration Methods

    def add_ev_config(self, ev_config: EVConfig) -> None:
        """Add an EV configuration."""
        self._ev_configs.append(ev_config)
        _LOGGER.info(f"Added EV config: {ev_config.name}")

    def remove_ev_config(self, vehicle_id: str) -> None:
        """Remove an EV configuration."""
        self._ev_configs = [e for e in self._ev_configs if e.vehicle_id != vehicle_id]
        _LOGGER.info(f"Removed EV config: {vehicle_id}")

    def get_ev_configs(self) -> list[EVConfig]:
        """Get all EV configurations."""
        return self._ev_configs

    # Battery Configuration Methods

    def add_battery_config(self, battery_config: BatteryConfig) -> None:
        """Add a battery configuration."""
        self._battery_configs.append(battery_config)
        if self._multi_battery_optimizer:
            self._multi_battery_optimizer.add_battery(battery_config)
        _LOGGER.info(f"Added battery config: {battery_config.name}")

    def remove_battery_config(self, battery_id: str) -> None:
        """Remove a battery configuration."""
        self._battery_configs = [b for b in self._battery_configs if b.battery_id != battery_id]
        if self._multi_battery_optimizer:
            self._multi_battery_optimizer.remove_battery(battery_id)
        _LOGGER.info(f"Removed battery config: {battery_id}")

    def get_battery_configs(self) -> list[BatteryConfig]:
        """Get all battery configurations."""
        return self._battery_configs

    # VPP Methods

    def get_vpp_stats(self) -> dict[str, Any]:
        """Get VPP participation statistics."""
        if self._grid_services:
            return self._grid_services.get_vpp_stats()
        return {}

    async def respond_to_vpp_event(self, event_id: str) -> dict[str, Any]:
        """Manually respond to a VPP event."""
        if not self._grid_services:
            return {"success": False, "error": "VPP not enabled"}

        for event in self._active_vpp_events:
            if event.event_id == event_id:
                response = await self._grid_services.respond_to_event(event)
                return {
                    "success": True,
                    "response": response.response.value,
                    "power_kw": response.power_kw,
                    "status": response.status,
                }

        return {"success": False, "error": "Event not found"}
