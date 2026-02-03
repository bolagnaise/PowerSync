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
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .engine import BatteryOptimiser, OptimizationConfig, OptimizationResult, CostFunction
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster

# Enhanced modules
from .ml_load_forecaster import MLLoadEstimator, WeatherAdjustedForecaster, WeatherFeatures
from .ev_integration import (
    EVOptimiser,
    EVConfig,
    EVChargingSchedule,
    EVChargingPriority,
    EVChargerType,
    integrate_ev_with_home_battery,
)
from .multi_battery import (
    MultiBatteryOptimiser,
    MultiBatteryResult,
    BatteryConfig,
    BatterySystemType,
)
from .grid_services import (
    GridServicesManager,
    VPPAwareOptimiser,
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

# Add-on API configuration - try multiple possible hostnames and ports
# Port 5001 used to avoid conflict with EMHASS (which uses 5000)
ADDON_PORTS = [5001, 5000, 5002]  # Try these ports in order
ADDON_HOSTNAMES = [
    "powersync_optimiser",       # Standard slug
    "powersync-optimiser",       # Hyphenated slug
    "local_powersync_optimiser", # Local add-on slug
    "local-powersync-optimiser", # Local add-on hyphenated
    "addon_powersync_optimiser", # Alternative prefix
]
# Build full URL list from hostnames and ports
ADDON_URLS = [f"http://{host}:{port}" for host in ADDON_HOSTNAMES for port in ADDON_PORTS]
# Supervisor API for add-on discovery
SUPERVISOR_API = "http://supervisor/addons"


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
        tariff_schedule: dict | None = None,  # For Globird/TOU-based pricing
        force_state_getter: Callable[[], dict] | None = None,  # Get force charge/discharge state
        entry: Any | None = None,  # Config entry for persisting settings
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
            price_coordinator: Coordinator providing price data (Amber/Octopus)
            energy_coordinator: Coordinator providing energy data
            tariff_schedule: Tariff schedule dict for TOU-based pricing (Globird)
            force_state_getter: Callback to get force charge/discharge state
            entry: ConfigEntry for persisting settings (used by set_settings)
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
        self._entry = entry  # ConfigEntry for persisting settings
        self.battery_system = battery_system
        self.battery_controller = battery_controller
        self.price_coordinator = price_coordinator
        self.energy_coordinator = energy_coordinator
        self._tariff_schedule = tariff_schedule  # For Globird/TOU-based pricing
        self._force_state_getter = force_state_getter  # For checking if force charge/discharge is active

        # Feature flags
        self._enable_ml_forecasting = enable_ml_forecasting
        self._enable_weather = enable_weather_integration
        self._enable_ev = enable_ev_integration
        self._enable_multi_battery = enable_multi_battery
        self._enable_vpp = enable_vpp

        # Core optimization components
        self._optimiser = BatteryOptimiser()
        self._executor: ScheduleExecutor | None = None
        self._load_estimator: LoadEstimator | None = None
        self._solar_forecaster: SolcastForecaster | None = None

        # Enhanced components
        self._ml_load_estimator: MLLoadEstimator | None = None
        self._weather_forecaster: WeatherAdjustedForecaster | None = None
        self._ev_optimiser: EVOptimiser | None = None
        self._multi_battery_optimiser: MultiBatteryOptimiser | None = None
        self._grid_services: GridServicesManager | None = None
        self._vpp_aware_optimiser: VPPAwareOptimiser | None = None

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

        # Add-on availability cache
        self._addon_available: bool = False
        self._addon_url: str | None = None

        # Dynamic pricing / price-triggered optimization
        self._is_dynamic_pricing = False  # True for Amber, AEMO, Octopus Agile
        self._price_listener_unsub: Callable | None = None
        self._last_command_time: datetime | None = None
        self._command_throttle_seconds = 300  # 5 minutes between battery commands
        self._last_price_hash: str | None = None  # Detect actual price changes

        # VPP spike-triggered optimization tracking
        self._last_vpp_optimization_time: datetime | None = None
        self._last_vpp_event_id: str | None = None

        # Export boost window tracking (triggers optimization on window entry/exit)
        self._in_export_boost_window: bool | None = None  # None = not yet checked
        self._in_chip_mode_window: bool | None = None  # Chip mode window tracking

    @property
    def enabled(self) -> bool:
        """Check if optimization is enabled."""
        return self._enabled

    @property
    def optimiser_available(self) -> bool:
        """Check if the LP optimiser is available (local or add-on)."""
        # Local optimiser has priority info, but add-on is preferred
        return self._optimiser.is_available or self._addon_available

    def _check_addon_sync(self) -> bool:
        """Synchronous check if add-on was available on last check."""
        return getattr(self, "_addon_available", False)

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

    def _is_force_mode_active(self) -> bool:
        """Check if force charge or discharge is currently active.

        When force mode is active, the tariff contains fake rates that shouldn't
        be used for optimization calculations.
        """
        if not self._force_state_getter:
            return False
        try:
            state = self._force_state_getter()
            return state.get("active", False)
        except Exception:
            return False

    def _get_force_mode_type(self) -> str | None:
        """Get the type of force mode if active (charge/discharge)."""
        if not self._force_state_getter:
            return None
        try:
            state = self._force_state_getter()
            if state.get("active"):
                return state.get("type")
            return None
        except Exception:
            return None

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

        # Initialize EV optimiser
        if self._enable_ev:
            self._ev_optimiser = EVOptimiser(
                interval_minutes=self._config.interval_minutes
            )
            await self._discover_ev_configs()
            _LOGGER.info(f"EV integration enabled with {len(self._ev_configs)} vehicles")

            # Sync EV integration to auto_schedule_executor
            # This enables ML-based scheduling for EV charging
            self._sync_ev_integration_to_auto_scheduler(True)

        # Initialize multi-battery optimiser
        if self._enable_multi_battery:
            await self._discover_battery_configs()
            if len(self._battery_configs) > 1:
                self._multi_battery_optimiser = MultiBatteryOptimiser(
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
                # Set up VPP-aware optimiser
                self._vpp_aware_optimiser = VPPAwareOptimiser(
                    self._optimiser,
                    self._grid_services,
                    self._vpp_config,
                )
                _LOGGER.info(f"VPP integration enabled: {self._vpp_config.program.value}")

        # Initialize executor
        self._executor = ScheduleExecutor(
            self.hass,
            self._optimiser,
            self.battery_controller,
            interval_minutes=self._config.interval_minutes,
        )

        # Set up data callbacks for executor
        # The optimize callback allows the executor to use the add-on when available
        self._executor.set_data_callbacks(
            get_prices=self._get_price_forecast,
            get_solar=self._get_solar_forecast,
            get_load=self._get_load_forecast,
            get_battery_state=self._get_battery_state,
            optimize=self.force_reoptimize,
        )

        # Check add-on availability
        self._addon_available = await self._is_addon_available()

        # Auto-detect battery capacity and power limits from Tesla API
        if self.battery_system == "tesla":
            await self._detect_battery_power_limits()

        # Set up price-triggered optimization for dynamic pricing providers
        await self._setup_price_listener()

        _LOGGER.info(
            f"Optimization coordinator setup complete. "
            f"Local optimiser: {self._optimiser.is_available}, "
            f"Add-on optimiser: {self._addon_available}, "
            f"ML: {self._enable_ml_forecasting}, "
            f"Weather: {self._enable_weather}, "
            f"EV: {self._enable_ev}, "
            f"Multi-battery: {self._enable_multi_battery}, "
            f"VPP: {self._enable_vpp}, "
            f"Dynamic pricing: {self._is_dynamic_pricing}, "
            f"Battery: {self._config.battery_capacity_wh/1000:.1f}kWh @ {self._config.max_charge_w/1000:.1f}kW"
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

    async def _setup_price_listener(self) -> None:
        """
        Set up price-triggered optimization for dynamic pricing providers.

        For dynamic pricing (Amber, AEMO, Octopus Agile):
        - Optimize on every price update (every 5 minutes for Amber)
        - Only re-optimize if prices actually changed

        For static/TOU pricing (GloBird, standard tariffs):
        - Keep the standard 5-minute optimization interval
        """
        if not self.price_coordinator:
            _LOGGER.debug("No price coordinator - using standard optimization interval")
            return

        # Detect dynamic pricing providers by coordinator type
        coordinator_name = type(self.price_coordinator).__name__
        dynamic_providers = ["AmberPriceCoordinator", "AEMOPriceCoordinator"]

        # Octopus is dynamic only for Agile/Flux tariffs
        if coordinator_name == "OctopusPriceCoordinator":
            # Check if it's an Agile or Flux tariff (dynamic pricing)
            product_code = getattr(self.price_coordinator, "product_code", "")
            if "AGILE" in product_code.upper() or "FLUX" in product_code.upper():
                dynamic_providers.append("OctopusPriceCoordinator")

        self._is_dynamic_pricing = coordinator_name in dynamic_providers

        if self._is_dynamic_pricing:
            # Subscribe to price coordinator updates
            self._price_listener_unsub = self.price_coordinator.async_add_listener(
                self._on_price_update
            )
            _LOGGER.info(
                f"⚡ Dynamic pricing detected ({coordinator_name}) - "
                f"will optimize on every price update"
            )
        else:
            _LOGGER.info(
                f"📊 Static/TOU pricing detected ({coordinator_name}) - "
                f"using standard 5-minute optimization interval"
            )

    def _on_price_update(self) -> None:
        """
        Callback when price coordinator updates.

        For dynamic pricing, this triggers re-optimization if:
        - Prices have actually changed (not just a refresh)
        - Enough time has passed since last command (throttle)
        """
        if not self._enabled or not self._is_dynamic_pricing:
            return

        # Create hash of current prices to detect actual changes
        try:
            if self.price_coordinator and self.price_coordinator.data:
                current_data = self.price_coordinator.data
                # Hash the current and next few price periods
                current = current_data.get("current", {})
                price_hash = f"{current.get('per_kwh', 0):.4f}"

                if price_hash == self._last_price_hash:
                    _LOGGER.debug(f"Price unchanged ({price_hash}), skipping re-optimization")
                    return

                _LOGGER.info(f"⚡ Price changed: {self._last_price_hash} → {price_hash}")
                self._last_price_hash = price_hash
        except Exception as e:
            _LOGGER.debug(f"Could not hash prices: {e}")

        # Schedule the async optimization
        self.hass.async_create_task(self._price_triggered_optimization())

    async def _price_triggered_optimization(self) -> None:
        """Run optimization triggered by price update with command throttling."""
        now = dt_util.now()

        # Check command throttle (minimum 5 minutes between battery commands)
        if self._last_command_time:
            elapsed = (now - self._last_command_time).total_seconds()
            if elapsed < self._command_throttle_seconds:
                _LOGGER.debug(
                    f"Command throttle active - {self._command_throttle_seconds - elapsed:.0f}s remaining"
                )
                return

        _LOGGER.info("⚡ Price update detected - running optimization")

        try:
            # Run optimization
            result = await self.force_reoptimize()

            if result and result.success:
                self._last_command_time = now

                # Execute the immediate action (first interval) on the battery
                if self._executor:
                    await self._executor._execute_action(result, 0)

                action = result.get_action_at_index(0).get('action', 'idle')
                _LOGGER.info(
                    f"⚡ Price-triggered optimization complete: "
                    f"action={action}, cost=${result.total_cost:.2f}"
                )
            else:
                _LOGGER.warning("Price-triggered optimization failed or returned no result")

        except Exception as e:
            _LOGGER.error(f"Error in price-triggered optimization: {e}")

    async def _discover_ev_configs(self) -> None:
        """
        Discover EV configurations from Home Assistant Tesla integrations.

        Integrates with the existing EV charging planner to get:
        - Vehicle VINs and names from device registry
        - SOC from Tesla integration entities
        - Charging settings from auto-schedule executor
        """
        from ..const import DOMAIN
        from ..automations.ev_charging_planner import (
            discover_all_tesla_vehicles,
            get_ev_battery_level,
            is_ev_plugged_in,
            get_auto_schedule_executor,
        )

        self._ev_configs = []

        try:
            # Get config entry for this integration
            config_entry = None
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.entry_id == self.entry_id:
                    config_entry = entry
                    break

            if not config_entry:
                _LOGGER.debug("No config entry found for EV discovery")
                return

            # Discover Tesla vehicles from device registry
            vehicles = await discover_all_tesla_vehicles(self.hass, config_entry)
            _LOGGER.debug(f"Discovered {len(vehicles)} Tesla vehicles for ML optimization")

            # Get auto-schedule executor for charging settings
            auto_executor = await get_auto_schedule_executor(self.hass, config_entry)

            for vehicle in vehicles:
                vin = vehicle.get("vin", "")
                name = vehicle.get("name", "Tesla")

                # Get current SOC from Tesla integration
                try:
                    soc = await get_ev_battery_level(self.hass, config_entry, vehicle_vin=vin)
                    if soc is None:
                        soc = 50  # Default if can't read
                except Exception:
                    soc = 50

                # Get charging settings from auto-schedule executor if available
                target_soc = 80
                max_charge_kw = 11.5  # Default for Tesla Wall Connector
                allow_grid_charging = True
                solar_only = False

                if auto_executor:
                    try:
                        settings = auto_executor.get_settings(vin)
                        target_soc = settings.target_soc
                    except Exception:
                        pass

                # Check if vehicle is plugged in
                try:
                    plugged_in = await is_ev_plugged_in(self.hass, config_entry, vehicle_vin=vin)
                except Exception:
                    plugged_in = False

                # Only include plugged-in vehicles in optimization
                if plugged_in:
                    ev_config = EVConfig(
                        vehicle_id=vin,
                        name=name,
                        battery_capacity_kwh=self._get_tesla_battery_capacity(name),
                        current_soc=soc / 100,  # Convert to 0-1
                        target_soc=target_soc / 100,  # Convert to 0-1
                        max_charge_kw=max_charge_kw,
                        allow_grid_charging=allow_grid_charging,
                        solar_only=solar_only,
                        charger_type=EVChargerType.TESLA_WALL_CONNECTOR,
                    )
                    self._ev_configs.append(ev_config)
                    _LOGGER.info(f"🚗 Added EV to ML optimization: {name} (SOC: {soc}%, target: {target_soc}%)")
                else:
                    _LOGGER.debug(f"Vehicle {name} not plugged in, skipping ML optimization")

        except ImportError as e:
            _LOGGER.debug(f"EV charging planner not available: {e}")
        except Exception as e:
            _LOGGER.warning(f"Error discovering EV configs: {e}")

    def _get_tesla_battery_capacity(self, vehicle_name: str) -> float:
        """
        Estimate Tesla battery capacity from vehicle name.

        Returns capacity in kWh.
        """
        name_lower = vehicle_name.lower()

        # Model S/X Long Range
        if "model s" in name_lower or "model x" in name_lower:
            if "plaid" in name_lower:
                return 100.0
            return 100.0  # Long Range default

        # Model 3
        if "model 3" in name_lower:
            if "long range" in name_lower or "lr" in name_lower:
                return 82.0
            if "performance" in name_lower:
                return 82.0
            return 60.0  # Standard Range

        # Model Y
        if "model y" in name_lower:
            if "long range" in name_lower or "lr" in name_lower:
                return 82.0
            if "performance" in name_lower:
                return 82.0
            return 60.0  # Standard Range

        # Cybertruck
        if "cybertruck" in name_lower:
            return 123.0  # Cyberbeast

        # Default for unknown
        return 75.0

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

    async def _call_addon_optimiser(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        battery_state: tuple[float, float],
    ) -> OptimizationResult | None:
        """Call the PowerSync Optimiser add-on for optimization."""
        import aiohttp

        initial_soc, capacity = battery_state

        # Build request payload
        payload = {
            "prices_import": prices_import,
            "prices_export": prices_export,
            "solar_forecast": solar_forecast,
            "load_forecast": load_forecast,
            "battery": {
                "current_soc": initial_soc,
                "capacity_wh": capacity,
                "max_charge_w": self._config.max_charge_w,
                "max_discharge_w": self._config.max_discharge_w,
                "efficiency": self._config.charge_efficiency,
                "backup_reserve": self._config.backup_reserve,
            },
            "cost_function": self._cost_function.value,
            "interval_minutes": self._config.interval_minutes,
            "provider_config": {
                "export_boost_enabled": self._provider_config.export_boost_enabled,
                "export_price_offset": self._provider_config.export_price_offset,
                "export_min_price": self._provider_config.export_min_price,
                "export_boost_start": self._provider_config.export_boost_start,
                "export_boost_end": self._provider_config.export_boost_end,
                "chip_mode_enabled": self._provider_config.chip_mode_enabled,
                "chip_mode_start": self._provider_config.chip_mode_start,
                "chip_mode_end": self._provider_config.chip_mode_end,
                "chip_mode_threshold": self._provider_config.chip_mode_threshold,
            },
        }

        # Use discovered add-on URL or try all known URLs
        addon_url = self._addon_url or ADDON_URLS[0]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{addon_url}/optimize",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    if response.status != 200:
                        _LOGGER.warning(f"Add-on returned status {response.status}")
                        return None

                    data = await response.json()
                    _LOGGER.debug(f"Add-on response keys: {list(data.keys())}")
                    if not data.get("success"):
                        _LOGGER.warning(f"Add-on optimization failed: {data.get('error')}")
                        return None

                    # Convert response to OptimizationResult
                    schedule = data.get("schedule", {})
                    summary = data.get("summary", {})
                    _LOGGER.debug(f"Add-on schedule keys: {list(schedule.keys())}, summary keys: {list(summary.keys())}")

                    # Parse timestamps
                    timestamps = []
                    for ts in schedule.get("timestamps", []):
                        try:
                            timestamps.append(datetime.fromisoformat(ts))
                        except (ValueError, TypeError):
                            timestamps.append(dt_util.now())

                    # Get costs and calculate savings if not provided
                    total_cost = summary.get("total_cost", 0)
                    baseline_cost = summary.get("baseline_cost", 0)
                    savings = summary.get("savings", 0)

                    # If add-on didn't calculate savings, estimate from baseline
                    if savings == 0 and baseline_cost > 0:
                        savings = baseline_cost - total_cost

                    # If no baseline provided, estimate it (cost without optimization)
                    # Baseline = importing all load at average price
                    if baseline_cost == 0 and prices_import:
                        avg_price = sum(prices_import) / len(prices_import)
                        total_load = sum(load_forecast) * (self._config.interval_minutes / 60) / 1000  # kWh
                        baseline_cost = avg_price * total_load
                        savings = baseline_cost - total_cost

                    charge_w = schedule.get("charge_w", [])
                    discharge_w = schedule.get("discharge_w", [])
                    battery_consume_w = schedule.get("battery_consume_w", [])
                    battery_export_w = schedule.get("battery_export_w", [])

                    # Log first few intervals and find first non-idle action
                    first_charge_idx = None
                    first_consume_idx = None
                    first_export_idx = None
                    for i in range(len(charge_w)):
                        if charge_w[i] > 10 and first_charge_idx is None:
                            first_charge_idx = i
                        consume = battery_consume_w[i] if i < len(battery_consume_w) else 0
                        export = battery_export_w[i] if i < len(battery_export_w) else 0
                        if consume > 10 and first_consume_idx is None:
                            first_consume_idx = i
                        if export > 10 and first_export_idx is None:
                            first_export_idx = i
                        if first_charge_idx and first_consume_idx and first_export_idx:
                            break

                    for i in range(min(4, len(charge_w))):
                        ts = timestamps[i].strftime("%H:%M") if i < len(timestamps) else "?"
                        c = charge_w[i] if i < len(charge_w) else 0
                        d = discharge_w[i] if i < len(discharge_w) else 0
                        consume = battery_consume_w[i] if i < len(battery_consume_w) else 0
                        export = battery_export_w[i] if i < len(battery_export_w) else 0
                        if c > 10:
                            action = "charge"
                        elif consume > export and consume > 10:
                            action = "consume"  # Battery powering home
                        elif export > 10:
                            action = "export"   # Battery exporting to grid
                        elif d > 10:
                            action = "discharge"  # Legacy fallback
                        else:
                            action = "idle"
                        _LOGGER.info(f"📊 Schedule[{i}] {ts}: {action} (charge={c:.0f}W, consume={consume:.0f}W, export={export:.0f}W)")

                    # Log when first charge/consume/export happens
                    if first_charge_idx is not None:
                        ts = timestamps[first_charge_idx].strftime("%H:%M %a") if first_charge_idx < len(timestamps) else "?"
                        _LOGGER.info(f"📊 First CHARGE at interval {first_charge_idx} ({ts}): {charge_w[first_charge_idx]:.0f}W")
                    if first_consume_idx is not None:
                        ts = timestamps[first_consume_idx].strftime("%H:%M %a") if first_consume_idx < len(timestamps) else "?"
                        _LOGGER.info(f"📊 First CONSUME (battery→home) at interval {first_consume_idx} ({ts}): {battery_consume_w[first_consume_idx]:.0f}W")
                    if first_export_idx is not None:
                        ts = timestamps[first_export_idx].strftime("%H:%M %a") if first_export_idx < len(timestamps) else "?"
                        _LOGGER.info(f"📊 First EXPORT (battery→grid) at interval {first_export_idx} ({ts}): {battery_export_w[first_export_idx]:.0f}W")

                    return OptimizationResult(
                        success=True,
                        status=data.get("status", "optimal"),
                        # Legacy/aggregate fields
                        charge_schedule_w=charge_w,
                        discharge_schedule_w=discharge_w,
                        grid_import_w=schedule.get("grid_import_w", []),
                        grid_export_w=schedule.get("grid_export_w", []),
                        soc_trajectory=schedule.get("soc_trajectory", []),
                        # New detailed power flow breakdown
                        battery_consume_w=schedule.get("battery_consume_w", []),
                        battery_export_w=schedule.get("battery_export_w", []),
                        solar_to_load_w=schedule.get("solar_to_load_w", []),
                        solar_to_battery_w=schedule.get("solar_to_battery_w", []),
                        solar_to_grid_w=schedule.get("solar_to_grid_w", []),
                        grid_to_load_w=schedule.get("grid_to_load_w", []),
                        grid_to_battery_w=schedule.get("grid_to_battery_w", []),
                        timestamps=timestamps,
                        total_cost=total_cost,
                        total_import_kwh=summary.get("total_import_kwh", 0),
                        total_export_kwh=summary.get("total_export_kwh", 0),
                        total_charge_kwh=summary.get("total_charge_kwh", 0),
                        total_discharge_kwh=summary.get("total_discharge_kwh", 0),
                        average_import_price=summary.get("average_import_price", 0),
                        average_export_price=summary.get("average_export_price", 0),
                        # New detailed metrics
                        total_battery_consume_kwh=summary.get("total_battery_consume_kwh", 0),
                        total_battery_export_kwh=summary.get("total_battery_export_kwh", 0),
                        total_solar_consumed_kwh=summary.get("total_solar_consumed_kwh", 0),
                        total_solar_exported_kwh=summary.get("total_solar_exported_kwh", 0),
                        baseline_cost=baseline_cost,
                        savings=savings,
                    )

        except aiohttp.ClientError as e:
            _LOGGER.debug(f"Add-on not available: {e}")
            return None
        except Exception as e:
            _LOGGER.error(f"Error calling add-on optimiser: {e}")
            return None

    async def _is_addon_available(self) -> bool:
        """Check if the optimiser add-on is available."""
        import aiohttp
        import os

        # First, try to discover via Supervisor API (HAOS only)
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            _LOGGER.debug("Found SUPERVISOR_TOKEN, querying Supervisor API for add-ons")
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {supervisor_token}"}
                    async with session.get(
                        "http://supervisor/addons",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            addons = data.get("data", {}).get("addons", [])
                            _LOGGER.debug(f"Found {len(addons)} add-ons in Supervisor")
                            for addon in addons:
                                # Look for PowerSync Optimiser add-on
                                name = addon.get("name", "").lower()
                                slug = addon.get("slug", "")
                                state = addon.get("state", "unknown")
                                _LOGGER.debug(f"Checking add-on: {name} (slug={slug}, state={state})")
                                if "powersync" in name and "optimi" in name:
                                    # Found it! Try to connect on different ports
                                    _LOGGER.info(f"🔋 Discovered PowerSync Optimiser add-on: {slug} (state={state})")
                                    if state != "started":
                                        _LOGGER.warning(f"Add-on {slug} is not started (state={state})")
                                        continue

                                    # Try to get the add-on's IP from its info endpoint
                                    addon_ip = None
                                    try:
                                        async with session.get(
                                            f"http://supervisor/addons/{slug}/info",
                                            headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=3),
                                        ) as info_response:
                                            if info_response.status == 200:
                                                info_data = await info_response.json()
                                                addon_ip = info_data.get("data", {}).get("ip_address")
                                                _LOGGER.debug(f"Add-on {slug} has IP: {addon_ip}")
                                    except Exception as e:
                                        _LOGGER.debug(f"Failed to get add-on info: {e}")

                                    # Build list of URLs to try (slug hostname, IP, etc.)
                                    urls_to_try = []
                                    for port in ADDON_PORTS:
                                        urls_to_try.append(f"http://{slug}:{port}")
                                        if addon_ip:
                                            urls_to_try.append(f"http://{addon_ip}:{port}")

                                    for addon_url in urls_to_try:
                                        if await self._check_addon_health(addon_url):
                                            self._addon_url = addon_url
                                            _LOGGER.info(f"✓ PowerSync Optimiser add-on connected at {addon_url}")
                                            return True
                                    _LOGGER.warning(f"Add-on {slug} found but health check failed on URLs: {urls_to_try[:4]}...")
                        else:
                            _LOGGER.debug(f"Supervisor API returned status {response.status}")
            except Exception as e:
                _LOGGER.debug(f"Supervisor API discovery failed: {e}")

        # Fallback: try each possible URL
        _LOGGER.debug(f"Trying fallback URLs: {ADDON_URLS[:3]}...")
        for url in ADDON_URLS:
            if await self._check_addon_health(url):
                self._addon_url = url
                _LOGGER.info(f"Found PowerSync Optimiser add-on at {url}")
                return True

        _LOGGER.warning(
            "PowerSync Optimiser add-on not found. "
            "Install it from the add-on store to enable ML optimization, "
            "or install cvxpy locally (pip install cvxpy highspy)"
        )
        return False

    async def _check_addon_health(self, url: str) -> bool:
        """Check if add-on is responding at the given URL."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                # Try the /health endpoint first
                async with session.get(
                    f"{url}/health",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                            # Accept if optimiser_available is True, or if it's a valid response
                            if data.get("optimiser_available", False):
                                return True
                            # Also accept if status is "ok" or "healthy"
                            if data.get("status") in ("ok", "healthy"):
                                _LOGGER.debug(f"Add-on at {url} responded with status={data.get('status')}")
                                return True
                        except Exception:
                            # If we got 200 but can't parse JSON, still consider it available
                            _LOGGER.debug(f"Add-on at {url} returned 200 but non-JSON response")
                            return True
        except aiohttp.ClientError as e:
            _LOGGER.debug(f"Health check failed for {url}: {e}")
        except Exception as e:
            _LOGGER.debug(f"Unexpected error checking {url}: {e}")
        return False

    async def enable(self) -> bool:
        """Enable optimization and start the executor."""
        if self._enabled:
            return True

        if not self._executor:
            _LOGGER.error("Executor not initialized")
            return False

        self._executor.set_config(self._config)
        self._executor.set_cost_function(self._cost_function)

        # For dynamic pricing (Amber/AEMO), don't use periodic timer - optimize on price updates
        # For static/TOU pricing, use periodic 5-minute timer
        use_periodic_timer = not self._is_dynamic_pricing
        success = await self._executor.start(use_periodic_timer=use_periodic_timer)
        if success:
            self._enabled = True
            _LOGGER.info(f"Optimization enabled (dynamic_pricing={self._is_dynamic_pricing})")

            # Start monitoring for:
            # 1. VPP events (price spikes, negative prices) - if VPP enabled
            # 2. Price window transitions (export boost, chip mode) - for static/TOU pricing
            should_monitor = (
                (self._grid_services and self._vpp_config and self._vpp_config.enabled) or
                (not self._is_dynamic_pricing and (
                    self._provider_config.export_boost_enabled or
                    self._provider_config.chip_mode_enabled
                ))
            )
            if should_monitor:
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

        # Unsubscribe from price updates
        if self._price_listener_unsub:
            self._price_listener_unsub()
            self._price_listener_unsub = None

        if self._executor:
            await self._executor.stop()

        self._enabled = False
        _LOGGER.info("Optimization disabled")

    async def _start_vpp_monitoring(self) -> None:
        """Start VPP event and price window monitoring."""
        if self._vpp_monitor_task:
            return

        async def monitor_loop():
            while True:
                try:
                    await asyncio.sleep(VPP_CHECK_INTERVAL.total_seconds())

                    # Check for VPP events (price spikes, negative prices)
                    if self._grid_services:
                        events = await self._grid_services.check_grid_conditions()
                        self._active_vpp_events = events

                        # Handle events based on type
                        for event in events:
                            if self._vpp_config and self._vpp_config.auto_respond:
                                await self._handle_vpp_event(event)

                    # Check for price window transitions (for static/TOU pricing)
                    if not self._is_dynamic_pricing:
                        await self._check_price_window_transitions()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    _LOGGER.error(f"VPP monitoring error: {e}")

        self._vpp_monitor_task = asyncio.create_task(monitor_loop())
        _LOGGER.info("VPP/price window monitoring started")

    async def _check_price_window_transitions(self) -> None:
        """
        Check if we've entered or exited price-affecting time windows.

        Monitors:
        - Export boost window (higher FIT rates)
        - Chip mode window (suppress export unless price exceeds threshold)

        Triggers re-optimization on transition to ensure the optimizer
        immediately sees the new prices.
        """
        now = dt_util.now()
        hour_str = now.strftime("%H:%M")
        should_reoptimize = False
        transition_reason = ""

        # Check export boost window
        if self._provider_config.export_boost_enabled:
            currently_in_boost = self._is_in_time_window(
                hour_str,
                self._provider_config.export_boost_start,
                self._provider_config.export_boost_end
            )

            # First check - just record state
            if self._in_export_boost_window is None:
                self._in_export_boost_window = currently_in_boost
                _LOGGER.debug(f"Export boost window initial state: {currently_in_boost}")
            elif currently_in_boost != self._in_export_boost_window:
                self._in_export_boost_window = currently_in_boost
                should_reoptimize = True
                if currently_in_boost:
                    transition_reason = f"📈 Entering export boost window ({self._provider_config.export_boost_start}-{self._provider_config.export_boost_end})"
                else:
                    transition_reason = "📉 Exiting export boost window"

        # Check chip mode window
        if self._provider_config.chip_mode_enabled:
            currently_in_chip = self._is_in_time_window(
                hour_str,
                self._provider_config.chip_mode_start,
                self._provider_config.chip_mode_end
            )

            # First check - just record state
            if self._in_chip_mode_window is None:
                self._in_chip_mode_window = currently_in_chip
                _LOGGER.debug(f"Chip mode window initial state: {currently_in_chip}")
            elif currently_in_chip != self._in_chip_mode_window:
                self._in_chip_mode_window = currently_in_chip
                should_reoptimize = True
                if currently_in_chip:
                    transition_reason = f"🔒 Entering chip mode window ({self._provider_config.chip_mode_start}-{self._provider_config.chip_mode_end})"
                else:
                    transition_reason = "🔓 Exiting chip mode window"

        # Trigger re-optimization if any transition occurred
        if should_reoptimize:
            _LOGGER.info(f"{transition_reason} - triggering re-optimization")

            result = await self.force_reoptimize()
            if result and result.success:
                # Execute the immediate action
                if self._executor:
                    await self._executor._execute_action(result, 0)

                action = result.get_action_at_index(0).get('action', 'idle')
                _LOGGER.info(
                    f"Price window optimization complete: action={action}, "
                    f"cost=${result.total_cost:.2f}"
                )

    async def _handle_vpp_event(self, event: GridEvent) -> None:
        """
        Handle a VPP event intelligently.

        For price events (spikes, negative prices):
        - Trigger re-optimization so the optimizer can determine optimal response
        - The optimizer will see the current high/low prices and adjust accordingly

        For other events (demand response, FCAS):
        - Use direct response (these require immediate action)
        """
        # Price events should trigger re-optimization
        price_event_types = {GridEventType.PRICE_SPIKE, GridEventType.NEGATIVE_PRICE}

        if event.event_type in price_event_types:
            # Check if we already optimized for this event (avoid spam during sustained spikes)
            if self._last_vpp_event_id == event.event_id:
                _LOGGER.debug(f"Already optimized for VPP event {event.event_id}, skipping")
                return

            # Check throttle (minimum 5 minutes between VPP-triggered optimizations)
            now = dt_util.now()
            if self._last_vpp_optimization_time:
                elapsed = (now - self._last_vpp_optimization_time).total_seconds()
                if elapsed < self._command_throttle_seconds:
                    _LOGGER.debug(
                        f"VPP optimization throttle active - {self._command_throttle_seconds - elapsed:.0f}s remaining"
                    )
                    return

            # Trigger re-optimization with current prices
            price_str = f"${event.current_price:.2f}/kWh" if event.current_price else "unknown"
            _LOGGER.info(
                f"⚡ VPP {event.event_type.value} detected (price: {price_str}) - "
                f"triggering re-optimization"
            )

            result = await self.force_reoptimize()

            if result and result.success:
                self._last_vpp_optimization_time = now
                self._last_vpp_event_id = event.event_id

                # Execute the immediate action on the battery
                if self._executor:
                    await self._executor._execute_action(result, 0)

                action = result.get_action_at_index(0).get('action', 'idle')
                power_w = result.get_action_at_index(0).get('power_w', 0)
                _LOGGER.info(
                    f"⚡ VPP-triggered optimization complete: "
                    f"action={action} @ {power_w:.0f}W, cost=${result.total_cost:.2f}"
                )
            else:
                _LOGGER.warning("VPP-triggered optimization failed")

        else:
            # Non-price events (demand response, FCAS) - use direct response
            if self._grid_services:
                await self._grid_services.respond_to_event(event)

    def set_cost_function(self, cost_function: str | CostFunction) -> None:
        """Set the optimization cost function."""
        if isinstance(cost_function, str):
            cost_function = CostFunction(cost_function)

        self._cost_function = cost_function
        self._config.cost_function = cost_function

        if self._executor:
            self._executor.set_cost_function(cost_function)

        if self._multi_battery_optimiser:
            self._multi_battery_optimiser.cost_function = cost_function

        _LOGGER.info(f"Cost function set to: {cost_function.value}")

    def update_config(self, **kwargs) -> None:
        """Update optimization configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)

        if self._executor:
            self._executor.set_config(self._config)

    async def force_reoptimize(self) -> OptimizationResult | None:
        """Force immediate re-optimization with all enhancements.

        Tries the PowerSync Optimiser add-on first (has cvxpy/numpy),
        falls back to local optimization if add-on unavailable.
        """
        # Skip optimization if force charge/discharge is active
        # The current tariff contains fake rates that would give incorrect results
        force_mode = self._get_force_mode_type()
        if force_mode:
            _LOGGER.info(f"Skipping re-optimization - force {force_mode} is active (fake tariff in use)")
            # Return current schedule unchanged
            return self._current_schedule

        _LOGGER.info("Forcing re-optimization with enhanced features")

        # Get all forecasts
        prices = await self._get_price_forecast()
        solar = await self._get_solar_forecast()
        load = await self._get_load_forecast()
        battery_state = await self._get_battery_state()

        # Fetch actual backup reserve from battery system (Tesla, Sigenergy, Sungrow)
        await self._get_backup_reserve()
        _LOGGER.debug(f"Using backup reserve: {self._config.backup_reserve:.0%}")

        if not prices:
            _LOGGER.warning(f"No price data - price_coordinator exists: {bool(self.price_coordinator)}")
        if not solar:
            _LOGGER.warning(f"No solar data - solar_forecaster exists: {bool(self._solar_forecaster)}")
        if not load:
            _LOGGER.warning(f"No load data - load_estimator exists: {bool(self._load_estimator)}")

        if not prices or not solar or not load:
            _LOGGER.warning("Missing forecast data for optimization")
            if self._executor:
                return await self._executor.force_reoptimize()
            return None

        import_prices, export_prices = prices
        initial_soc, capacity = battery_state
        start_time = dt_util.now()

        # Refresh EV configs before optimization (vehicles may have been plugged/unplugged)
        ev_plugged_in = False
        if self._enable_ev:
            await self._discover_ev_configs()
            if self._ev_configs:
                ev_plugged_in = True
                _LOGGER.info(f"🚗 ML optimization includes {len(self._ev_configs)} plugged-in EVs")

        # Use add-on for home-battery-only optimization
        # Use local optimization when EVs are plugged in (add-on doesn't support EV yet)
        if not ev_plugged_in:
            addon_result = await self._call_addon_optimiser(
                import_prices, export_prices, solar, load, battery_state
            )
            if addon_result:
                _LOGGER.info(f"Optimization completed via add-on: success={addon_result.success}, "
                             f"total_cost=${addon_result.total_cost:.2f}, savings=${addon_result.savings:.2f}, "
                             f"schedule_len={len(addon_result.charge_schedule_w)}")
                self._current_schedule = addon_result
                self._last_optimization_time = dt_util.now()

                # Update executor with new schedule
                if self._executor:
                    self._executor._current_schedule = addon_result
                    _LOGGER.debug(f"Updated executor._current_schedule: {addon_result is not None}")

                return addon_result

            _LOGGER.debug("Add-on not available, trying local optimization")
        else:
            _LOGGER.info("🚗 Using local optimization for joint home battery + EV scheduling")

        # Check for VPP events to consider
        anticipated_events = []
        if self._grid_services:
            anticipated_events = await self._grid_services.check_grid_conditions()

        # Run optimization based on configuration (local fallback)
        if self._multi_battery_optimiser and len(self._battery_configs) > 1:
            # Multi-battery optimization
            self._multi_battery_result = self._multi_battery_optimiser.optimize(
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
                home_optimiser=self._optimiser,
                ev_configs=self._ev_configs,
                prices_import=import_prices,
                prices_export=export_prices,
                solar_forecast=solar,
                load_forecast=load,
                initial_home_soc=initial_soc,
                start_time=start_time,
            )

        elif self._vpp_aware_optimiser and anticipated_events:
            # VPP-aware optimization
            self._current_schedule = await self._vpp_aware_optimiser.optimize_with_vpp(
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
            self._current_schedule = self._optimiser.optimize(
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
            self._executor._current_schedule = self._current_schedule

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
        # Sync current_schedule from executor if available
        if self._executor:
            self._current_schedule = self._executor.current_schedule

        # Continuously learn battery power limits from observed rates
        await self._observe_battery_power()

        # Return the API data for coordinator.data
        return self.get_api_data()

    async def _observe_battery_power(self) -> None:
        """Observe battery power to learn actual charge/discharge limits."""
        try:
            if not self.energy_coordinator or not self.energy_coordinator.data:
                return

            battery_power_kw = self.energy_coordinator.data.get("battery_power", 0)
            battery_power_w = abs(battery_power_kw * 1000)  # Convert to W

            # Initialize tracking if needed
            if not hasattr(self, '_observed_max_charge_w'):
                self._observed_max_charge_w = 0.0
            if not hasattr(self, '_observed_max_discharge_w'):
                self._observed_max_discharge_w = 0.0

            # Track max observed power
            if battery_power_kw < 0:  # Charging (negative = power into battery)
                if battery_power_w > self._observed_max_charge_w:
                    old_max = self._observed_max_charge_w
                    self._observed_max_charge_w = battery_power_w
                    # Update config if significantly higher (>500W more)
                    if battery_power_w > self._config.max_charge_w and (battery_power_w - old_max) > 500:
                        self._config.max_charge_w = battery_power_w * 1.05  # 5% headroom
                        _LOGGER.info(f"🔋 Learned new max charge rate: {self._config.max_charge_w/1000:.1f} kW (observed {battery_power_w/1000:.1f} kW)")

            elif battery_power_kw > 0:  # Discharging
                if battery_power_w > self._observed_max_discharge_w:
                    old_max = self._observed_max_discharge_w
                    self._observed_max_discharge_w = battery_power_w
                    # Update config if significantly higher
                    if battery_power_w > self._config.max_discharge_w and (battery_power_w - old_max) > 500:
                        self._config.max_discharge_w = battery_power_w * 1.05
                        _LOGGER.info(f"🔋 Learned new max discharge rate: {self._config.max_discharge_w/1000:.1f} kW (observed {battery_power_w/1000:.1f} kW)")

        except Exception as e:
            _LOGGER.debug(f"Error observing battery power: {e}")

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

    async def _get_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts for optimization.

        Applies provider price modifications:
        - Export boost: increase export prices during configured window
        - Chip mode: reduce export prices outside threshold
        - Spike protection: cap import prices during spikes

        Falls back to tariff schedule (TOU periods) if no price coordinator.
        """
        # Try tariff schedule first if no price coordinator (Globird, etc.)
        if not self.price_coordinator and self._tariff_schedule:
            _LOGGER.info("📊 Using tariff_schedule for price forecast (no price_coordinator)")
            return self._get_prices_from_tariff_schedule()

        if not self.price_coordinator:
            _LOGGER.debug("No price coordinator and no tariff schedule available")
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
                    pass  # Price adjustments logged at summary level

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

    def _get_prices_from_tariff_schedule(self) -> tuple[list[float], list[float]] | None:
        """Generate price forecast from TOU tariff schedule (for Globird, etc.).

        Uses the Tesla tariff schedule's TOU periods to generate a 48-hour
        price forecast based on time-of-use rates.
        """
        if not self._tariff_schedule:
            return None

        try:
            tou_periods = self._tariff_schedule.get("tou_periods", {})
            buy_rates = self._tariff_schedule.get("buy_rates", {})
            sell_rates = self._tariff_schedule.get("sell_rates", {})

            # Debug log the raw tariff data
            _LOGGER.info(f"📊 TOU tariff data: tou_periods={list(tou_periods.keys())}, buy_rates={buy_rates}, sell_rates={sell_rates}")

            # If no TOU data, use flat rates
            if not tou_periods or not buy_rates:
                flat_buy = self._tariff_schedule.get("buy_price", 25.0) / 100  # cents to $/kWh
                flat_sell = self._tariff_schedule.get("sell_price", 8.0) / 100
                n_intervals = self._config.horizon_hours * 60 // self._config.interval_minutes
                _LOGGER.debug(f"Using flat tariff rates: buy=${flat_buy:.3f}, sell=${flat_sell:.3f}")
                return [flat_buy] * n_intervals, [flat_sell] * n_intervals

            n_intervals = self._config.horizon_hours * 60 // self._config.interval_minutes
            import_prices = []
            export_prices = []
            now = dt_util.now()

            # Define period priority order - more specific/valuable periods first
            # SUPER_OFF_PEAK should be checked before OFF_PEAK to avoid overlap issues
            period_priority = ["SUPER_OFF_PEAK", "ON_PEAK", "PEAK", "PARTIAL_PEAK", "SHOULDER", "OFF_PEAK"]

            for idx in range(n_intervals):
                interval_time = now + timedelta(minutes=idx * self._config.interval_minutes)
                current_hour = interval_time.hour
                current_dow = interval_time.weekday()  # Python: 0=Monday

                # Find the TOU period for this interval
                # Check periods in priority order to handle overlaps correctly
                current_period = None
                for period_name in period_priority:
                    if period_name not in tou_periods:
                        continue
                    period_data = tou_periods[period_name]
                    periods_list = period_data if isinstance(period_data, list) else []
                    for period in periods_list:
                        from_dow = period.get("fromDayOfWeek", 0)
                        to_dow = period.get("toDayOfWeek", 6)
                        from_hour = period.get("fromHour", 0)
                        to_hour = period.get("toHour", 24)

                        # Tesla format: 0=Sunday, Python: 0=Monday
                        tesla_dow = (current_dow + 1) % 7
                        if from_dow <= tesla_dow <= to_dow:
                            # Handle overnight periods
                            if from_hour <= to_hour:
                                if from_hour <= current_hour < to_hour:
                                    current_period = period_name
                                    break
                            else:
                                if current_hour >= from_hour or current_hour < to_hour:
                                    current_period = period_name
                                    break
                    if current_period:
                        break

                if not current_period:
                    # No period matched - use PARTIAL_PEAK as default (shoulder rate)
                    # since this is typically the "everything else" rate
                    current_period = "PARTIAL_PEAK" if "PARTIAL_PEAK" in buy_rates else "OFF_PEAK"

                # Get rates for this period (rates may be in $/kWh or cents)
                # Try current_period first, then common fallbacks
                buy_rate = buy_rates.get(current_period)
                if buy_rate is None:
                    buy_rate = buy_rates.get("PARTIAL_PEAK", buy_rates.get("OFF_PEAK", buy_rates.get("ALL", 0.25)))
                sell_rate = sell_rates.get(current_period, sell_rates.get("ALL", 0.08))

                # Convert to $/kWh if rates appear to be in cents (> 1.0)
                if buy_rate > 1.0:
                    buy_rate = buy_rate / 100
                if sell_rate > 1.0:
                    sell_rate = sell_rate / 100

                import_prices.append(buy_rate)
                export_prices.append(sell_rate)

                # Debug log first 4 intervals to see period detection
                if idx < 4:
                    _LOGGER.info(f"📊 Interval {idx}: {interval_time.strftime('%H:%M %a')} -> period={current_period}, buy=${buy_rate:.3f}/kWh")

            return import_prices, export_prices

        except Exception as e:
            _LOGGER.error(f"Error getting prices from tariff schedule: {e}")
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

                # Try to get actual battery capacity from Tesla site_info
                capacity = await self._get_battery_capacity()

                return soc, capacity

        except Exception as e:
            _LOGGER.error(f"Error getting battery state: {e}")

        return 0.5, 13500  # Default

    async def _get_battery_capacity(self) -> float:
        """
        Get actual battery capacity from Tesla site_info or calculate from battery count.

        Priority:
        1. total_pack_energy from site_info (most accurate)
        2. nameplate_energy from site_info components
        3. Calculate from battery_count × 13.5 kWh per Powerwall
        4. Fall back to stored battery health data
        5. Default to config value
        """
        # Use cached value if available (capacity doesn't change often)
        if hasattr(self, '_detected_capacity_wh') and self._detected_capacity_wh:
            return self._detected_capacity_wh

        DEFAULT_POWERWALL_CAPACITY_WH = 13500  # 13.5 kWh per Powerwall 2

        try:
            # Try to get site_info from energy coordinator
            if hasattr(self.energy_coordinator, 'async_get_site_info'):
                site_info = await self.energy_coordinator.async_get_site_info()
                if site_info:
                    # Check for total_pack_energy (Wh) - most accurate
                    if site_info.get("total_pack_energy"):
                        capacity = float(site_info.get("total_pack_energy"))
                        _LOGGER.info(f"🔋 Auto-detected battery capacity from total_pack_energy: {capacity/1000:.1f} kWh")
                        self._detected_capacity_wh = capacity
                        self._config.battery_capacity_wh = capacity
                        return capacity

                    # Check components for nameplate_energy (sometimes in kWh)
                    components = site_info.get("components", {})
                    if components.get("nameplate_energy"):
                        # nameplate_energy is usually in kWh
                        capacity_kwh = float(components.get("nameplate_energy"))
                        capacity = capacity_kwh * 1000  # Convert to Wh
                        _LOGGER.info(f"🔋 Auto-detected battery capacity from nameplate_energy: {capacity/1000:.1f} kWh")
                        self._detected_capacity_wh = capacity
                        self._config.battery_capacity_wh = capacity
                        return capacity

                    # Check for battery_count and calculate
                    battery_count = components.get("battery_count") or site_info.get("battery_count")
                    if battery_count and int(battery_count) > 0:
                        capacity = int(battery_count) * DEFAULT_POWERWALL_CAPACITY_WH
                        _LOGGER.info(f"🔋 Calculated battery capacity from {battery_count} Powerwalls: {capacity/1000:.1f} kWh")
                        self._detected_capacity_wh = capacity
                        self._config.battery_capacity_wh = capacity
                        return capacity

            # Try to get from stored battery health data (from TEDAPI scan)
            domain_data = self.hass.data.get("power_sync", {}).get(self.entry_id, {})
            battery_health = domain_data.get("battery_health")
            if battery_health:
                original_capacity = battery_health.get("original_capacity_wh")
                battery_count = battery_health.get("battery_count", 1)
                if original_capacity and original_capacity > 0:
                    _LOGGER.info(f"🔋 Using battery capacity from health data: {original_capacity/1000:.1f} kWh ({battery_count} units)")
                    self._detected_capacity_wh = original_capacity
                    self._config.battery_capacity_wh = original_capacity
                    return original_capacity

        except Exception as e:
            _LOGGER.warning(f"Error detecting battery capacity: {e}")

        # Fall back to config default
        _LOGGER.debug(f"Using default battery capacity: {self._config.battery_capacity_wh/1000:.1f} kWh")
        return self._config.battery_capacity_wh

    async def _get_backup_reserve(self) -> float:
        """
        Get actual backup reserve percentage from the battery system.

        Priority:
        1. Tesla site_info backup_reserve_percent
        2. Sigenergy controller get_backup_soc()
        3. Sungrow coordinator backup_reserve
        4. Fall back to config value
        """
        from ..const import DOMAIN, CONF_SIGENERGY_STATION_ID, CONF_SUNGROW_HOST

        try:
            domain_data = self.hass.data.get(DOMAIN, {})
            for entry_id, entry_data in domain_data.items():
                if not isinstance(entry_data, dict):
                    continue

                # Tesla: Get from site_info
                if hasattr(self.energy_coordinator, 'async_get_site_info'):
                    site_info = await self.energy_coordinator.async_get_site_info()
                    if site_info and "backup_reserve_percent" in site_info:
                        reserve_percent = site_info.get("backup_reserve_percent", 20)
                        reserve = reserve_percent / 100.0  # Convert to 0-1
                        _LOGGER.debug(f"🔋 Fetched Tesla backup reserve: {reserve_percent}%")
                        self._config.backup_reserve = reserve
                        return reserve

                # Sigenergy: Get from coordinator data
                sigenergy_coordinator = entry_data.get("sigenergy_coordinator")
                if sigenergy_coordinator and sigenergy_coordinator.data:
                    coord_data = sigenergy_coordinator.data
                    if "backup_reserve" in coord_data:
                        reserve_percent = coord_data.get("backup_reserve", 20)
                        reserve = reserve_percent / 100.0
                        _LOGGER.debug(f"🔋 Fetched Sigenergy backup reserve: {reserve_percent}%")
                        self._config.backup_reserve = reserve
                        return reserve

                # Sungrow: Get from coordinator data
                sungrow_coordinator = entry_data.get("sungrow_coordinator")
                if sungrow_coordinator and sungrow_coordinator.data:
                    coord_data = sungrow_coordinator.data
                    if "backup_reserve" in coord_data:
                        reserve_percent = coord_data.get("backup_reserve", 20)
                        reserve = reserve_percent / 100.0
                        _LOGGER.debug(f"🔋 Fetched Sungrow backup reserve: {reserve_percent}%")
                        self._config.backup_reserve = reserve
                        return reserve

        except Exception as e:
            _LOGGER.warning(f"Error fetching backup reserve: {e}")

        # Fall back to config default
        _LOGGER.debug(f"Using default backup reserve: {self._config.backup_reserve:.0%}")
        return self._config.backup_reserve

    async def _detect_battery_power_limits(self) -> None:
        """
        Auto-detect max charge/discharge power from observed battery power rates.

        This method:
        1. First tries to use observed max power from energy coordinator
        2. Falls back to calculating from battery capacity and system defaults
        3. Works for all battery types (Tesla, Sigenergy, Sungrow)

        Battery power convention:
        - Negative = charging (power into battery)
        - Positive = discharging (power out of battery)
        """
        # Initialize observed power tracking if not exists
        if not hasattr(self, '_observed_max_charge_w'):
            self._observed_max_charge_w = 0.0
        if not hasattr(self, '_observed_max_discharge_w'):
            self._observed_max_discharge_w = 0.0

        # Try to get current battery power and update observations
        try:
            if self.energy_coordinator and self.energy_coordinator.data:
                battery_power_kw = self.energy_coordinator.data.get("battery_power", 0)
                battery_power_w = abs(battery_power_kw * 1000)  # Convert to W

                if battery_power_kw < 0:  # Charging (negative = power into battery)
                    if battery_power_w > self._observed_max_charge_w:
                        self._observed_max_charge_w = battery_power_w
                        _LOGGER.debug(f"🔋 New max observed charge rate: {battery_power_w/1000:.2f} kW")
                elif battery_power_kw > 0:  # Discharging
                    if battery_power_w > self._observed_max_discharge_w:
                        self._observed_max_discharge_w = battery_power_w
                        _LOGGER.debug(f"🔋 New max observed discharge rate: {battery_power_w/1000:.2f} kW")
        except Exception as e:
            _LOGGER.debug(f"Could not read battery power for limit detection: {e}")

        # Use observed values if we have meaningful data (>1kW suggests real operation)
        MIN_MEANINGFUL_POWER = 1000  # 1 kW - ignore very small values

        if self._observed_max_charge_w > MIN_MEANINGFUL_POWER:
            # Add 10% headroom to observed max (battery may not have been at full rate)
            observed_charge = self._observed_max_charge_w * 1.1
            if observed_charge > self._config.max_charge_w:
                _LOGGER.info(f"🔋 Updated max charge rate from observed: {observed_charge/1000:.1f} kW")
                self._config.max_charge_w = observed_charge

        if self._observed_max_discharge_w > MIN_MEANINGFUL_POWER:
            observed_discharge = self._observed_max_discharge_w * 1.1
            if observed_discharge > self._config.max_discharge_w:
                _LOGGER.info(f"🔋 Updated max discharge rate from observed: {observed_discharge/1000:.1f} kW")
                self._config.max_discharge_w = observed_discharge

        # If no observed data yet, fall back to capacity-based estimation
        if self._config.max_charge_w == 5000 and self._observed_max_charge_w < MIN_MEANINGFUL_POWER:
            await self._estimate_power_from_capacity()

    async def _estimate_power_from_capacity(self) -> None:
        """Estimate power limits from battery capacity when no observed data available."""
        # Default power per unit by manufacturer
        POWER_DEFAULTS = {
            "tesla": 5000,      # 5 kW per Powerwall 2
            "sigenergy": 5000,  # Varies, typically 5 kW
            "sungrow": 5000,    # Varies, typically 5 kW
        }
        CAPACITY_DEFAULTS = {
            "tesla": 13500,     # 13.5 kWh per Powerwall 2
            "sigenergy": 10000, # Varies
            "sungrow": 10000,   # Varies
        }

        default_power = POWER_DEFAULTS.get(self.battery_system, 5000)
        default_capacity = CAPACITY_DEFAULTS.get(self.battery_system, 13500)

        # Get actual capacity
        capacity = await self._get_battery_capacity()

        # Estimate number of units
        estimated_units = max(1, round(capacity / default_capacity))

        # Calculate power limits
        max_power = estimated_units * default_power

        if max_power != self._config.max_charge_w:
            _LOGGER.info(f"🔋 Estimated battery power from capacity: {max_power/1000:.1f} kW ({estimated_units} units)")
            self._config.max_charge_w = max_power
            self._config.max_discharge_w = max_power

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API and mobile app."""
        executor_status = self._executor.get_schedule_summary() if self._executor else {}

        # Sync schedule from executor if we don't have one locally
        if self._executor and self._executor.current_schedule:
            self._current_schedule = self._executor.current_schedule

        # Check if optimization engine is available (local cvxpy or add-on)
        engine_available = self._optimiser.is_available or self._addon_available

        # Determine status message based on engine availability
        if engine_available:
            if self._current_schedule and self._current_schedule.success:
                status_message = "Schedule optimized"
            else:
                status_message = "Ready to optimize"
        else:
            status_message = "Optimizer engine not installed. Install the PowerSync Optimiser add-on or cvxpy package."

        _LOGGER.debug(f"get_api_data: _current_schedule={self._current_schedule is not None}, "
                      f"success={self._current_schedule.success if self._current_schedule else 'N/A'}, "
                      f"executor_status keys={list(executor_status.keys())}")

        # Build data for mobile app
        data = {
            "success": True,
            "enabled": self._enabled,
            "optimizer_available": self._optimiser.is_available or self._addon_available,
            "engine_available": engine_available,
            "status_message": status_message,
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
        }

        # Add schedule data if available
        if self._current_schedule and self._current_schedule.success:
            _LOGGER.debug(f"Adding schedule to API response: {len(self._current_schedule.timestamps)} intervals, "
                          f"cost=${self._current_schedule.total_cost:.2f}, savings=${self._current_schedule.savings:.2f}")
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
            next_actions = self._current_schedule.get_next_actions(5)
            data["next_actions"] = next_actions
            # Override next_action from schedule to ensure consistency with next_actions list
            if len(next_actions) > 1:
                data["next_action"] = next_actions[1].get("action", "idle")
                data["next_action_time"] = next_actions[1].get("timestamp")
            elif len(next_actions) > 0:
                data["next_action"] = next_actions[0].get("action", "idle")
                data["next_action_time"] = next_actions[0].get("timestamp")
            # Also set predicted cost/savings from schedule directly
            data["predicted_cost"] = self._current_schedule.total_cost
            data["predicted_savings"] = self._current_schedule.savings

        # Add EV data if available
        if self._ev_configs:
            data["ev_vehicles"] = [
                {
                    "vehicle_id": ev.vehicle_id,
                    "name": ev.name,
                    "current_soc": round(ev.current_soc * 100, 1),
                    "target_soc": round(ev.target_soc * 100, 1),
                    "battery_capacity_kwh": ev.battery_capacity_kwh,
                    "max_charge_kw": ev.max_charge_kw,
                }
                for ev in self._ev_configs
            ]
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
                # Persist cost function to config entry
                if self._entry:
                    from ..const import CONF_OPTIMIZATION_COST_FUNCTION
                    new_data = dict(self._entry.data)
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = settings["cost_function"]
                    self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                    _LOGGER.info(f"🔧 Persisted cost_function: {settings['cost_function']}")
            except ValueError as e:
                response["success"] = False
                response["error"] = f"Invalid cost function: {e}"
                return response

        # Handle feature toggles
        # Map API keys to internal attribute names and config entry keys
        feature_map = {
            # api_key: (attr_name, config_key)
            "ml_forecasting": ("_enable_ml_forecasting", "CONF_OPTIMIZATION_ML_FORECASTING"),
            "weather_integration": ("_enable_weather", "CONF_OPTIMIZATION_WEATHER_INTEGRATION"),
            "ev_integration": ("_enable_ev", "CONF_OPTIMIZATION_EV_INTEGRATION"),
            "multi_battery": ("_enable_multi_battery", "CONF_OPTIMIZATION_MULTI_BATTERY"),
            "vpp": ("_enable_vpp", "CONF_OPTIMIZATION_VPP_ENABLED"),
            "vpp_enabled": ("_enable_vpp", "CONF_OPTIMIZATION_VPP_ENABLED"),  # Alias
        }

        # Build config entry updates
        config_entry_updates = {}

        for api_key, (attr_name, config_const) in feature_map.items():
            if api_key in settings:
                value = settings[api_key]
                if hasattr(self, attr_name):
                    setattr(self, attr_name, value)
                    response["changes"].append(f"{api_key}: {value}")

                    # Store in config entry updates
                    from ..const import (
                        CONF_OPTIMIZATION_EV_INTEGRATION,
                        CONF_OPTIMIZATION_VPP_ENABLED,
                        CONF_OPTIMIZATION_MULTI_BATTERY,
                        CONF_OPTIMIZATION_ML_FORECASTING,
                        CONF_OPTIMIZATION_WEATHER_INTEGRATION,
                    )
                    config_key_map = {
                        "CONF_OPTIMIZATION_ML_FORECASTING": CONF_OPTIMIZATION_ML_FORECASTING,
                        "CONF_OPTIMIZATION_WEATHER_INTEGRATION": CONF_OPTIMIZATION_WEATHER_INTEGRATION,
                        "CONF_OPTIMIZATION_EV_INTEGRATION": CONF_OPTIMIZATION_EV_INTEGRATION,
                        "CONF_OPTIMIZATION_MULTI_BATTERY": CONF_OPTIMIZATION_MULTI_BATTERY,
                        "CONF_OPTIMIZATION_VPP_ENABLED": CONF_OPTIMIZATION_VPP_ENABLED,
                    }
                    config_entry_updates[config_key_map[config_const]] = value

                    # When EV integration is toggled, sync to auto_schedule_executor
                    if api_key == "ev_integration":
                        self._sync_ev_integration_to_auto_scheduler(value)

        # Persist feature toggles to config entry so they survive restart
        if config_entry_updates and self._entry:
            try:
                new_data = dict(self._entry.data)
                new_data.update(config_entry_updates)
                self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                _LOGGER.info(f"🔧 Persisted ML optimization feature toggles: {list(config_entry_updates.keys())}")
            except Exception as e:
                _LOGGER.warning(f"Failed to persist feature toggles: {e}")

        # Handle config updates
        config_keys = [
            "battery_capacity_wh", "max_charge_w", "max_discharge_w",
            "backup_reserve", "interval_minutes", "horizon_hours",
        ]
        config_updates = {k: v for k, v in settings.items() if k in config_keys}
        if config_updates:
            self.update_config(**config_updates)
            response["changes"].append(f"config: {list(config_updates.keys())}")
            # Persist interval_minutes to config entry
            if "interval_minutes" in config_updates and self._entry:
                from ..const import CONF_OPTIMIZATION_INTERVAL
                new_data = dict(self._entry.data)
                new_data[CONF_OPTIMIZATION_INTERVAL] = config_updates["interval_minutes"]
                self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                _LOGGER.info(f"🔧 Persisted interval_minutes: {config_updates['interval_minutes']}")

        return response

    def _sync_ev_integration_to_auto_scheduler(self, enabled: bool) -> None:
        """
        Sync EV integration setting to the auto_schedule_executor.

        When EV integration is enabled in ML optimization, the auto_schedule_executor
        should use ML-generated schedules instead of its own planning logic.
        """
        try:
            from ..const import DOMAIN
            from ..automations.ev_charging_planner import get_auto_schedule_executor

            # Get the auto_schedule_executor
            auto_executor = get_auto_schedule_executor()
            if auto_executor:
                auto_executor.set_use_ml_optimization(enabled)
                _LOGGER.info(
                    f"🔗 Synced EV integration to auto_schedule_executor: "
                    f"ML optimization {'enabled' if enabled else 'disabled'}"
                )
            else:
                _LOGGER.debug("Auto-schedule executor not available for EV integration sync")

        except Exception as e:
            _LOGGER.warning(f"Failed to sync EV integration to auto_scheduler: {e}")

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
        if self._multi_battery_optimiser:
            self._multi_battery_optimiser.add_battery(battery_config)
        _LOGGER.info(f"Added battery config: {battery_config.name}")

    def remove_battery_config(self, battery_id: str) -> None:
        """Remove a battery configuration."""
        self._battery_configs = [b for b in self._battery_configs if b.battery_id != battery_id]
        if self._multi_battery_optimiser:
            self._multi_battery_optimiser.remove_battery(battery_id)
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
