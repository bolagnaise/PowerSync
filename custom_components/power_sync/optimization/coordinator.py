"""
Optimization coordinator for PowerSync.

Coordinates data collection and runs the built-in LP battery optimizer
to produce a schedule, which the execution layer then applies.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ConfigEntryNotReady

from .battery_optimizer import BatteryOptimizer, OptimizerResult
from .schedule_reader import OptimizationSchedule
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode

_LOGGER = logging.getLogger(__name__)

COST_STORE_VERSION = 1
COST_STORE_SAVE_DELAY = 300  # Coalesce writes — flush at most every 5 minutes


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

    def __init__(self, value: str = "cost"):
        # Always use cost minimization (self-consumption is the battery's native mode)
        self.value = "cost"


class OptimizationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Coordinator for built-in LP battery optimization.

    Manages:
    - Built-in LP optimizer (BatteryOptimizer)
    - Data collection (prices, solar, load forecasts)
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

        # Built-in optimizer
        self._optimizer: BatteryOptimizer | None = None
        self._last_optimizer_result: OptimizerResult | None = None

        # Data collection components
        self._load_estimator: LoadEstimator | None = None
        self._solar_forecaster: SolcastForecaster | None = None

        # Executor
        self._executor: ScheduleExecutor | None = None

        # EV Coordinator
        self._ev_coordinator: EVCoordinator | None = None
        self._ev_configs: list[EVConfig] = []

        # EV integration persisted flag (loaded from config entry)
        self._ev_integration_enabled = False
        if self._entry:
            from ..const import CONF_OPTIMIZATION_EV_INTEGRATION
            self._ev_integration_enabled = self._entry.options.get(
                CONF_OPTIMIZATION_EV_INTEGRATION, False
            )

        # Cached schedule from optimizer
        self._current_schedule: OptimizationSchedule | None = None
        self._last_update_time: datetime | None = None

        # Cached forecast data (populated each optimization run)
        self._last_solar_forecast: list[float] | None = None    # kW values
        self._last_load_forecast: list[float] | None = None     # kW values
        self._last_import_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_export_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_display_import_prices: list[float] | None = None  # $/kWh actual tariff
        self._last_display_export_prices: list[float] | None = None  # $/kWh actual tariff

        # Battery specs source tracking
        self._battery_specs_source = "default"  # "default", "auto", or "manual"

        # Daily cost tracking (midnight-to-midnight), persisted via Store
        self._actual_cost_today = 0.0        # Accumulated actual cost since midnight ($)
        self._actual_baseline_today = 0.0    # Accumulated baseline cost since midnight ($)
        self._last_cost_date: str | None = None  # Date string for midnight reset
        self._last_cost_tracking_time: datetime | None = None  # For actual elapsed time
        self._actual_import_kwh_today = 0.0
        self._actual_export_kwh_today = 0.0
        self._actual_charge_kwh_today = 0.0
        self._actual_discharge_kwh_today = 0.0
        self._actual_import_cost_today = 0.0    # Gross import cost ($)
        self._actual_export_earnings_today = 0.0  # Gross export earnings ($)
        self._cost_store = Store(
            hass,
            COST_STORE_VERSION,
            f"power_sync.costs.{entry_id}",
        )

        # Price monitoring
        self._is_dynamic_pricing = False
        self._price_listener_unsub: Callable | None = None

        # Track last executed action for IDLE→non-IDLE transition
        self._last_executed_action: str | None = None

    @property
    def enabled(self) -> bool:
        """Check if optimization is enabled."""
        return self._enabled

    @property
    def optimiser_available(self) -> bool:
        """Check if optimizer is available (always True with built-in)."""
        return self._optimizer is not None

    @property
    def current_schedule(self) -> OptimizationSchedule | None:
        """Get the current optimization schedule."""
        return self._current_schedule

    async def async_setup(self) -> bool:
        """Set up the optimization coordinator with built-in LP optimizer."""
        _LOGGER.info("Setting up optimization coordinator (built-in LP)")

        # Auto-detect battery specs from Tesla site_info if available
        await self._auto_detect_battery_specs()

        # Initialize built-in optimizer
        self._optimizer = BatteryOptimizer(
            capacity_wh=self._config.battery_capacity_wh,
            max_charge_w=self._config.max_charge_w,
            max_discharge_w=self._config.max_discharge_w,
            efficiency=0.92,
            backup_reserve=self._config.backup_reserve,
            interval_minutes=self._config.interval_minutes,
            horizon_hours=self._config.horizon_hours,
        )

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

        # Initialize executor (for battery control)
        self._executor = ScheduleExecutor(
            self.hass,
            optimiser=None,
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

        # Set up price-triggered updates for dynamic pricing
        await self._setup_price_listener()

        # Initialize EV coordinator
        await self._setup_ev_coordinator()

        # Restore persisted daily cost data (survives HA restarts)
        await self._restore_cost_data()

        _LOGGER.info(
            "Optimization coordinator setup complete (built-in LP). "
            "Battery: %.1fkWh @ %.1fkW",
            self._config.battery_capacity_wh / 1000,
            self._config.max_charge_w / 1000,
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
        # Try known sensor names first (most specific → least specific)
        fallbacks = [
            "sensor.power_sync_home_load",
            "sensor.power_sync_load",
            "sensor.home_load",
            "sensor.home_load_power",
            "sensor.house_consumption",
            "sensor.load_power",
        ]
        for entity_id in fallbacks:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                _LOGGER.info("Using load sensor: %s", entity_id)
                return entity_id

        # Broader search: find any sensor with "load" or "consumption" in the name
        # that has a power unit (W or kW)
        for state in self.hass.states.async_all("sensor"):
            eid = state.entity_id
            name_lower = eid.lower()
            if state.state in ("unknown", "unavailable"):
                continue
            unit = (state.attributes.get("unit_of_measurement") or "").lower()
            if unit not in ("w", "kw"):
                continue
            if "home_load" in name_lower or "house_load" in name_lower or (
                "load" in name_lower and "power" in name_lower
            ):
                _LOGGER.info("Auto-discovered load sensor: %s", eid)
                return eid

        _LOGGER.warning("No home load sensor found — load forecast will use defaults")
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
            _LOGGER.info(
                "Dynamic pricing detected (%s) - re-optimizing on price changes",
                coordinator_name,
            )

    def _on_price_update(self) -> None:
        """Callback when price coordinator updates."""
        if not self._enabled or not self._is_dynamic_pricing:
            return

        # Re-optimize with new prices and update dashboard sensors
        self.hass.async_create_background_task(
            self._run_optimization(), "powersync_price_reoptimize"
        )

    async def enable(self) -> bool:
        """Enable optimization and start the built-in optimizer."""
        if self._enabled:
            return True

        if not self._optimizer:
            _LOGGER.error("Cannot enable optimization - optimizer not initialized")
            return False

        # Start executor (for battery control)
        if self._executor:
            self._executor.set_config(self._config)
            success = await self._executor.start(use_periodic_timer=False)
            if not success:
                return False

        self._enabled = True
        _LOGGER.info("Optimization enabled (built-in LP)")

        # Safety: restore backup_reserve to configured value on enable.
        # Handles HA restart during IDLE where backup_reserve was set to
        # current SOC% but _last_executed_action was lost (in-memory only).
        if self.battery_controller and hasattr(self.battery_controller, "set_backup_reserve"):
            configured_reserve_pct = int(self._config.backup_reserve * 100)
            try:
                success = await self.battery_controller.set_backup_reserve(configured_reserve_pct)
                if success:
                    _LOGGER.info(
                        "Optimizer startup: ensured backup reserve is %d%%",
                        configured_reserve_pct,
                    )
                else:
                    _LOGGER.warning("Optimizer startup: set_backup_reserve returned failure")
            except Exception as e:
                _LOGGER.warning("Failed to restore backup reserve on enable: %s", e)
        self._last_executed_action = None

        # Run initial optimization and start polling loop as background tasks
        # so they don't block HA bootstrap (LP solve can take several seconds)
        self.hass.async_create_background_task(
            self._run_optimization(), "powersync_initial_optimization"
        )
        self.hass.async_create_background_task(
            self._schedule_polling_loop(), "powersync_schedule_polling"
        )

        # Start EV coordination if enabled
        if self._ev_coordinator and self._ev_configs:
            await self._ev_coordinator.start()
            _LOGGER.info(
                "EV coordination started with %d charger(s)", len(self._ev_configs)
            )

        return True

    async def disable(self) -> None:
        """Disable optimization."""
        if not self._enabled:
            return

        # Safety: if IDLE was the last action, restore backup_reserve to
        # configured value before shutting down. Otherwise the battery
        # stays locked at the IDLE-elevated backup_reserve.
        if (
            self._last_executed_action == "idle"
            and self.battery_controller
            and hasattr(self.battery_controller, "set_backup_reserve")
        ):
            configured_reserve_pct = int(self._config.backup_reserve * 100)
            try:
                await self.battery_controller.set_backup_reserve(configured_reserve_pct)
                _LOGGER.info(
                    "Optimizer disable: restored backup reserve from IDLE to %d%%",
                    configured_reserve_pct,
                )
            except Exception as e:
                _LOGGER.warning("Failed to restore backup reserve on disable: %s", e)
        self._last_executed_action = None

        if self._price_listener_unsub:
            self._price_listener_unsub()
            self._price_listener_unsub = None

        if self._executor:
            await self._executor.stop()

        if self._ev_coordinator:
            await self._ev_coordinator.stop()

        # Flush cost data to disk before shutdown
        await self._cost_store.async_save(self._cost_data_to_save())

        self._enabled = False
        _LOGGER.info("Optimization disabled")

    async def _run_optimization(self) -> None:
        """Run the built-in LP optimizer with current forecast data."""
        if not self._optimizer:
            return

        try:
            # Collect forecast data
            prices = await self._get_price_forecast()
            solar = await self._get_solar_forecast()
            load = await self._get_load_forecast()
            soc, capacity = await self._get_battery_state()

            # Overlay EV charging plan onto load forecast
            ev_peak_kw = 0.0
            if load and self._ev_integration_enabled:
                ev_load_w = self._get_ev_planned_load(len(load))
                if ev_load_w:
                    load = [l + ev for l, ev in zip(load, ev_load_w)]
                    ev_peak_kw = max(ev_load_w) / 1000

            import_prices = prices[0] if prices else []
            export_prices = prices[1] if prices else []

            # Convert forecasts from Watts (forecaster output) to kW (LP input)
            solar_forecast = [v / 1000.0 for v in solar] if solar else []
            load_forecast = [v / 1000.0 for v in load] if load else []

            if solar_forecast and load_forecast:
                ev_msg = f" (ev={ev_peak_kw:.1f}kW peak)" if ev_peak_kw > 0 else ""
                _LOGGER.debug(
                    "LP inputs: solar=%.1f-%.1fkW (avg %.1fkW), "
                    "load=%.1f-%.1fkW (avg %.1fkW)%s, soc=%.1f%%",
                    min(solar_forecast), max(solar_forecast),
                    sum(solar_forecast) / len(solar_forecast),
                    min(load_forecast), max(load_forecast),
                    sum(load_forecast) / len(load_forecast),
                    ev_msg,
                    soc * 100,
                )

            # Run LP in executor thread to avoid blocking event loop
            result: OptimizerResult = await self.hass.async_add_executor_job(
                self._optimizer.optimize,
                import_prices,
                export_prices,
                solar_forecast,
                load_forecast,
                soc,
                self._cost_function.value,
            )

            self._last_optimizer_result = result
            self._current_schedule = result.schedule
            self._last_update_time = dt_util.now()

            # Store forecast data for LP forecast sensors
            self._last_solar_forecast = solar_forecast
            self._last_load_forecast = load_forecast
            self._last_import_prices = import_prices
            self._last_export_prices = export_prices

            # Track actual cost for this interval (midnight-to-midnight daily cost)
            self._track_actual_cost()

            # Log action distribution summary
            action_counts: dict[str, int] = {}
            for a in result.schedule.actions:
                action_counts[a.action] = action_counts.get(a.action, 0) + 1
            action_summary = ", ".join(
                f"{k}={v}" for k, v in sorted(action_counts.items())
            )

            _LOGGER.info(
                "Optimization complete (%s, %.2fs): "
                "daily_cost=$%.2f (actual=$%.2f + remaining=$%.2f), "
                "daily_savings=$%.2f, %d steps [%s]",
                result.solver_used,
                result.solve_time_s,
                self._get_daily_cost(),
                self._actual_cost_today,
                self._get_predicted_cost_to_midnight()[0],
                self._get_daily_savings(),
                len(result.schedule.actions),
                action_summary,
            )

        except Exception as e:
            _LOGGER.error("Optimization failed: %s", e, exc_info=True)

    async def _schedule_polling_loop(self) -> None:
        """Periodically re-optimize and execute current action."""
        while self._enabled:
            try:
                # Execute current action from schedule
                if self._current_schedule and self._current_schedule.actions:
                    current_action = self._get_current_action()
                    if current_action and self._executor:
                        _LOGGER.info(
                            "Polling: current action=%s power=%.0fW soc=%.1f%%",
                            current_action.action,
                            current_action.power_w,
                            current_action.soc * 100,
                        )
                        await self._execute_optimizer_action(current_action)
                    elif not current_action:
                        _LOGGER.debug("Polling: no current action found in schedule")

                # Wait for next interval
                await asyncio.sleep(self._config.interval_minutes * 60)

                # Re-optimize on each interval
                await self._run_optimization()

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error("Error in schedule polling: %s", e)
                await asyncio.sleep(60)

    def _get_current_action(self) -> Any | None:
        """Get the current scheduled action based on time."""
        if not self._current_schedule or not self._current_schedule.actions:
            return None

        now = dt_util.now()

        for i, action in enumerate(self._current_schedule.actions):
            if action.timestamp <= now:
                if i + 1 < len(self._current_schedule.actions):
                    if now < self._current_schedule.actions[i + 1].timestamp:
                        return action
                else:
                    return action

        return self._current_schedule.actions[0] if self._current_schedule.actions else None

    async def _execute_optimizer_action(self, action: Any) -> None:
        """Execute an optimizer action on the battery."""
        if not self._executor or not self._executor.battery_controller:
            return

        battery = self._executor.battery_controller

        # Check if force charge/discharge is active — skip optimizer execution.
        # Force mode is manually triggered by the user and owns the battery state
        # (backup_reserve, TOU tariff, operation mode). The optimizer must not
        # override it regardless of what the LP schedule wants.
        if self._force_state_getter:
            force_state = self._force_state_getter()
            if force_state and force_state.get("active"):
                force_type = force_state.get("type", "unknown")
                _LOGGER.debug(
                    "Optimizer: force %s active — skipping action execution (LP wants %s)",
                    force_type, action.action,
                )
                return

        try:
            # When transitioning from IDLE to any other action, restore backup
            # reserve to the user's configured value. IDLE sets backup_reserve
            # to current SOC% to prevent discharge; we must undo that.
            if (
                self._last_executed_action == "idle"
                and action.action != "idle"
                and hasattr(battery, "set_backup_reserve")
            ):
                configured_reserve_pct = int(self._config.backup_reserve * 100)
                await battery.set_backup_reserve(configured_reserve_pct)
                _LOGGER.info(
                    "Optimizer: Exiting IDLE — restored backup reserve to %d%%",
                    configured_reserve_pct,
                )

            if action.action == "charge":
                if hasattr(battery, "force_charge"):
                    await battery.force_charge(
                        duration_minutes=self._config.interval_minutes + 5,
                        power_w=action.power_w,
                    )
                    _LOGGER.info("Optimizer: Charging at %.0fW", action.power_w)
            elif action.action in ("discharge", "export"):
                if hasattr(battery, "force_discharge"):
                    await battery.force_discharge(
                        duration_minutes=self._config.interval_minutes + 5,
                        power_w=action.power_w,
                    )
                    _LOGGER.info("Optimizer: Discharging/exporting at %.0fW", action.power_w)
            elif action.action == "idle":
                # IDLE: Hold battery at current SOC by setting backup reserve
                # to current percentage. This prevents discharge while grid
                # serves the home load. Useful for Amber when prices are cheap
                # and the battery should hold charge for an upcoming spike.
                #
                # Tesla constraint (July 2025): backup_reserve only accepts
                # 0-80% or 100%. Values 81-99% are rejected and clamped to 80%.
                # When SOC > 80%, we CANNOT set backup_reserve to the actual SOC.
                # Setting 100% causes the Powerwall to charge FROM GRID to reach
                # 100% (especially when a TOU tariff shows cheap buy / expensive
                # sell). Instead, use self_consumption mode with backup_reserve=80%
                # — the Powerwall won't charge from grid in self_consumption mode,
                # and the 80% floor limits discharge. Minor SOC decline from home
                # load is acceptable as the LP re-evaluates every 5 minutes.
                soc, _ = await self._get_battery_state()
                soc_pct = int(soc * 100)
                if soc_pct > 80:
                    # Self_consumption mode prevents TOU-driven grid charging
                    if hasattr(battery, "set_self_consumption_mode"):
                        await battery.set_self_consumption_mode()
                    if hasattr(battery, "set_backup_reserve"):
                        await battery.set_backup_reserve(80)
                    _LOGGER.info(
                        "Optimizer: IDLE — holding SOC at %d%% via self_consumption "
                        "(backup reserve=80%%, Tesla 81-99%% constraint)",
                        soc_pct,
                    )
                elif hasattr(battery, "set_backup_reserve"):
                    # Tesla requires autonomous (TOU) mode for backup_reserve
                    # to act as a hard floor. In self_consumption mode, the
                    # Powerwall may still discharge below the reserve.
                    if hasattr(battery, "set_autonomous_mode"):
                        await battery.set_autonomous_mode()
                    await battery.set_backup_reserve(soc_pct)
                    _LOGGER.info(
                        "Optimizer: IDLE — holding SOC at %d%% (autonomous + backup reserve=%d%%)",
                        soc_pct, soc_pct,
                    )
                elif hasattr(battery, "set_self_consumption_mode"):
                    await battery.set_self_consumption_mode()
                    _LOGGER.info("Optimizer: IDLE — self-consumption (no set_backup_reserve)")
                elif hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
            else:
                # self_consumption or consume — let battery operate naturally
                if hasattr(battery, "set_self_consumption_mode"):
                    await battery.set_self_consumption_mode()
                elif hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
                _LOGGER.debug("Optimizer: Self-consumption mode (action=%s)", action.action)

            self._last_executed_action = action.action

        except Exception as e:
            _LOGGER.error("Failed to execute optimizer action: %s", e)

    def _apply_export_boost(
        self,
        export_prices: list[float],
    ) -> list[float]:
        """Apply export boost to LP export prices during configured window.

        Increases export prices by offset and applies a minimum floor so the LP
        is more willing to discharge during the boost window. Mirrors the Tesla
        tariff pipeline logic but operates on flat 5-min price arrays.
        """
        if not self._entry:
            return export_prices

        from ..const import (
            CONF_EXPORT_BOOST_ENABLED,
            CONF_EXPORT_PRICE_OFFSET,
            CONF_EXPORT_MIN_PRICE,
            CONF_EXPORT_BOOST_START,
            CONF_EXPORT_BOOST_END,
            CONF_EXPORT_BOOST_THRESHOLD,
            DEFAULT_EXPORT_BOOST_START,
            DEFAULT_EXPORT_BOOST_END,
            DEFAULT_EXPORT_BOOST_THRESHOLD,
        )

        opts = self._entry.options
        data = self._entry.data
        if not opts.get(CONF_EXPORT_BOOST_ENABLED, data.get(CONF_EXPORT_BOOST_ENABLED, False)):
            return export_prices

        offset = (opts.get(CONF_EXPORT_PRICE_OFFSET, 0) or 0) / 100  # cents → $/kWh
        min_price = (opts.get(CONF_EXPORT_MIN_PRICE, 0) or 0) / 100
        threshold = (opts.get(CONF_EXPORT_BOOST_THRESHOLD,
                              DEFAULT_EXPORT_BOOST_THRESHOLD) or 0) / 100
        boost_start = opts.get(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START)
        boost_end = opts.get(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END)

        try:
            sh, sm = map(int, boost_start.split(":"))
            eh, em = map(int, boost_end.split(":"))
        except (ValueError, IndexError):
            return export_prices

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        interval = self._config.interval_minutes
        now = dt_util.now()
        boosted = 0

        result = list(export_prices)
        for t in range(len(result)):
            ts = now + timedelta(minutes=t * interval)
            minutes_of_day = ts.hour * 60 + ts.minute

            # Check if in boost window (handles overnight wrap)
            if end_min <= start_min:
                in_window = minutes_of_day >= start_min or minutes_of_day < end_min
            else:
                in_window = start_min <= minutes_of_day < end_min

            if in_window and result[t] >= threshold:
                boosted_price = result[t] + offset
                result[t] = max(boosted_price, min_price)
                boosted += 1

        if boosted:
            _LOGGER.debug(
                "Export boost: boosted %d intervals (offset=%.1fc, min=%.1fc, window=%s-%s)",
                boosted, offset * 100, min_price * 100, boost_start, boost_end,
            )

        return result

    def _apply_chip_mode(
        self,
        export_prices: list[float],
    ) -> list[float]:
        """Apply chip mode to LP export prices — suppress exports unless price exceeds threshold.

        During the configured window, sets export prices to 0 so the LP won't plan
        exports. Preserves original price for spikes above threshold. Mirrors the
        Tesla tariff pipeline logic but operates on flat 5-min price arrays.
        """
        if not self._entry:
            return export_prices

        from ..const import (
            CONF_CHIP_MODE_ENABLED,
            CONF_CHIP_MODE_START,
            CONF_CHIP_MODE_END,
            CONF_CHIP_MODE_THRESHOLD,
            DEFAULT_CHIP_MODE_START,
            DEFAULT_CHIP_MODE_END,
            DEFAULT_CHIP_MODE_THRESHOLD,
        )

        opts = self._entry.options
        data = self._entry.data
        if not opts.get(CONF_CHIP_MODE_ENABLED, data.get(CONF_CHIP_MODE_ENABLED, False)):
            return export_prices

        chip_start = opts.get(CONF_CHIP_MODE_START, DEFAULT_CHIP_MODE_START)
        chip_end = opts.get(CONF_CHIP_MODE_END, DEFAULT_CHIP_MODE_END)
        threshold = (opts.get(CONF_CHIP_MODE_THRESHOLD,
                              DEFAULT_CHIP_MODE_THRESHOLD) or 0) / 100  # cents → $/kWh

        try:
            sh, sm = map(int, chip_start.split(":"))
            eh, em = map(int, chip_end.split(":"))
        except (ValueError, IndexError):
            return export_prices

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        interval = self._config.interval_minutes
        now = dt_util.now()
        suppressed = 0
        allowed_spikes = 0

        result = list(export_prices)
        for t in range(len(result)):
            ts = now + timedelta(minutes=t * interval)
            minutes_of_day = ts.hour * 60 + ts.minute

            # Check if in chip window (handles overnight wrap)
            if end_min <= start_min:
                in_window = minutes_of_day >= start_min or minutes_of_day < end_min
            else:
                in_window = start_min <= minutes_of_day < end_min

            if in_window:
                if result[t] >= threshold:
                    allowed_spikes += 1  # Keep original price for spike
                else:
                    result[t] = 0.0  # Suppress export
                    suppressed += 1

        if suppressed or allowed_spikes:
            _LOGGER.debug(
                "Chip mode: suppressed %d intervals, allowed %d spikes "
                "(threshold=%.1fc, window=%s-%s)",
                suppressed, allowed_spikes, threshold * 100, chip_start, chip_end,
            )

        return result

    def _apply_demand_charge_penalty(
        self, import_prices: list[float]
    ) -> list[float]:
        """Add import price penalty during demand charge windows.

        During configured demand charge peak periods, adds a penalty to
        import prices that strongly discourages grid imports. The LP will
        prefer battery discharge or self-consumption during these windows.
        """
        if not self._entry or not import_prices:
            return import_prices

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_RATE,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return import_prices

        rate = self._entry.options.get(
            CONF_DEMAND_CHARGE_RATE,
            self._entry.data.get(CONF_DEMAND_CHARGE_RATE, 0.0),
        )
        if rate <= 0:
            return import_prices

        start_str = self._entry.options.get(
            CONF_DEMAND_CHARGE_START_TIME,
            self._entry.data.get(CONF_DEMAND_CHARGE_START_TIME, "14:00"),
        )
        end_str = self._entry.options.get(
            CONF_DEMAND_CHARGE_END_TIME,
            self._entry.data.get(CONF_DEMAND_CHARGE_END_TIME, "20:00"),
        )
        days = self._entry.options.get(
            CONF_DEMAND_CHARGE_DAYS,
            self._entry.data.get(CONF_DEMAND_CHARGE_DAYS, "All Days"),
        )

        # Parse start/end times
        try:
            s_parts = start_str.split(":")
            start_min = int(s_parts[0]) * 60 + int(s_parts[1])
            e_parts = end_str.split(":")
            end_min = int(e_parts[0]) * 60 + int(e_parts[1])
        except (ValueError, IndexError):
            return import_prices

        # Penalty: rate/10 converts $/kW/month to aggressive $/kWh penalty
        penalty = rate / 10.0

        now = dt_util.now()
        interval = self._config.interval_minutes
        adjusted = list(import_prices)
        penalised = 0

        for t in range(len(adjusted)):
            ts = now + timedelta(minutes=t * interval)
            weekday = ts.weekday()

            # Day filter
            if days == "Weekdays Only" and weekday >= 5:
                continue
            if days == "Weekends Only" and weekday < 5:
                continue

            current_min = ts.hour * 60 + ts.minute

            # Time window check (handles overnight wrap)
            in_window = False
            if end_min <= start_min:
                in_window = current_min >= start_min or current_min < end_min
            else:
                in_window = start_min <= current_min < end_min

            if in_window:
                adjusted[t] += penalty
                penalised += 1

        if penalised:
            _LOGGER.info(
                "Demand charge penalty: +$%.2f/kWh on %d intervals (%s-%s, %s)",
                penalty, penalised, start_str, end_str, days,
            )

        return adjusted

    def _apply_confidence_decay(
        self,
        import_prices: list[float],
        export_prices: list[float],
        confidence_horizon_hours: float = 4.0,
        decay_rate: float = 0.15,
    ) -> tuple[list[float], list[float]]:
        """Pull far-future prices toward median to reflect forecast uncertainty.

        Prices within confidence_horizon_hours are unchanged. Beyond that,
        each price decays toward the median at exp(-decay_rate * excess_hours).

        Asymmetric decay: only prices ABOVE median are decayed. Below-median
        prices are preserved so the LP can see that cheap future periods
        (e.g. midday solar + low grid) are genuinely cheaper than overnight,
        and won't pre-charge overnight for a spike 18h away when cheaper
        daytime charging is available. Above-median export prices (spikes)
        are still decayed to prevent over-valuing speculative opportunities.
        """
        import math

        if not import_prices:
            return (import_prices, export_prices)

        import_median = sorted(import_prices)[len(import_prices) // 2]
        export_median = sorted(export_prices)[len(export_prices) // 2] if export_prices else 0.05
        interval = self._config.interval_minutes

        decayed_import = []
        for t, price in enumerate(import_prices):
            hours_ahead = (t * interval) / 60.0
            excess = max(0.0, hours_ahead - confidence_horizon_hours)
            if excess > 0 and price > import_median:
                confidence = math.exp(-decay_rate * excess)
                decayed_import.append(import_median + (price - import_median) * confidence)
            else:
                decayed_import.append(price)

        decayed_export = []
        for t, price in enumerate(export_prices):
            hours_ahead = (t * interval) / 60.0
            excess = max(0.0, hours_ahead - confidence_horizon_hours)
            if excess > 0 and price > export_median:
                confidence = math.exp(-decay_rate * excess)
                decayed_export.append(export_median + (price - export_median) * confidence)
            else:
                decayed_export.append(price)

        return (decayed_import, decayed_export)

    async def _get_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts for optimizer.

        For dynamic providers (Amber, Flow Power): reads from price_coordinator.
        For static TOU providers (GloBird, etc.): generates from tariff_schedule.
        """
        # Dynamic pricing (Amber, Flow Power, etc.)
        if self.price_coordinator and self.price_coordinator.data:
            data = self.price_coordinator.data

            # Amber format: {"current": [...], "forecast": [...]}
            # Each entry has perKwh (cents), channelType ("general"/"feedIn")
            # forecast is 30-min resolution; expand to 5-min intervals for LP
            if "current" in data or "forecast" in data:
                all_entries = list(data.get("current", []) or []) + list(data.get("forecast", []) or [])
                if all_entries:
                    # Separate by channel type
                    general = [e for e in all_entries if e.get("channelType") == "general"]
                    feed_in = [e for e in all_entries if e.get("channelType") == "feedIn"]

                    # Sort by startTime if available
                    for lst in (general, feed_in):
                        lst.sort(key=lambda e: e.get("startTime", ""))

                    # Filter out past entries — Amber API returns
                    # ActualInterval entries from midnight, but the LP
                    # needs prices starting from the current interval.
                    now = dt_util.now()
                    current_window = now.replace(
                        minute=(now.minute // 30) * 30,
                        second=0, microsecond=0,
                    )
                    for lst in (general, feed_in):
                        original_len = len(lst)
                        filtered = []
                        for e in lst:
                            st = e.get("startTime")
                            if st:
                                try:
                                    entry_time = datetime.fromisoformat(
                                        st.replace("Z", "+00:00")
                                    )
                                    if entry_time < current_window:
                                        continue
                                except (ValueError, TypeError):
                                    pass
                            filtered.append(e)
                        lst[:] = filtered
                        if len(lst) < original_len:
                            _LOGGER.debug(
                                "Filtered %d past Amber entries (before %s), "
                                "%d remaining",
                                original_len - len(lst),
                                current_window.isoformat(),
                                len(lst),
                            )

                    # Build 5-min price arrays: each 30-min entry → 6 intervals
                    interval = self._config.interval_minutes  # 5
                    expand = 30 // interval  # 6
                    n_steps = int(self._config.horizon_hours * 60) // interval  # 576

                    import_prices = []
                    for e in general:
                        price_dollar = e.get("perKwh", 0) / 100  # cents → $/kWh
                        import_prices.extend([price_dollar] * expand)

                    export_prices = []
                    for e in feed_in:
                        # feedIn perKwh is negative (you get paid), abs for optimizer
                        price_dollar = abs(e.get("perKwh", 0)) / 100
                        export_prices.extend([price_dollar] * expand)

                    # Pad or trim to n_steps
                    if import_prices:
                        if len(import_prices) < n_steps:
                            last = import_prices[-1] if import_prices else 0.25
                            import_prices.extend([last] * (n_steps - len(import_prices)))
                        import_prices = import_prices[:n_steps]

                    if export_prices:
                        if len(export_prices) < n_steps:
                            last = export_prices[-1] if export_prices else 0.08
                            export_prices.extend([last] * (n_steps - len(export_prices)))
                        export_prices = export_prices[:n_steps]

                    # Spike protection: cap buy prices during Amber spike periods
                    # so the LP optimizer won't choose to charge at extreme prices
                    if import_prices and general:
                        spike_protection_on = False
                        if self._entry:
                            from ..const import CONF_SPIKE_PROTECTION_ENABLED
                            spike_protection_on = self._entry.options.get(
                                CONF_SPIKE_PROTECTION_ENABLED,
                                self._entry.data.get(CONF_SPIKE_PROTECTION_ENABLED, False),
                            )

                        if spike_protection_on:
                            median_price = sorted(import_prices)[len(import_prices) // 2]
                            cap_price = max(median_price * 2, 0.50)  # At least 50c/kWh cap
                            for idx, e in enumerate(general):
                                spike_status = e.get("spikeStatus", "none")
                                if spike_status in ("spike", "potential"):
                                    base_idx = idx * expand
                                    original_price = e.get("perKwh", 0)
                                    capped_count = 0
                                    for j in range(expand):
                                        pos = base_idx + j
                                        if pos < len(import_prices) and import_prices[pos] > cap_price:
                                            import_prices[pos] = cap_price
                                            capped_count += 1
                                    if capped_count:
                                        _LOGGER.info(
                                            "Spike protection: capped %d intervals at %.1fc/kWh "
                                            "(was %.1fc, status=%s)",
                                            capped_count, cap_price * 100,
                                            original_price, spike_status,
                                        )

                    if import_prices:
                        # Store actual Amber prices for UI display BEFORE LP adjustments
                        self._last_display_import_prices = list(import_prices)
                        self._last_display_export_prices = list(export_prices)

                        # Apply export boost and chip mode to LP export prices
                        export_prices = self._apply_export_boost(export_prices)
                        export_prices = self._apply_chip_mode(export_prices)

                        # Apply demand charge penalty to LP import prices
                        import_prices = self._apply_demand_charge_penalty(import_prices)

                        # Apply confidence decay for LP input
                        import_prices, export_prices = self._apply_confidence_decay(
                            import_prices, export_prices
                        )

                        _LOGGER.debug(
                            "Amber prices: %d steps, display %.1fc-%.1fc, "
                            "LP (decayed) %.1fc-%.1fc",
                            len(import_prices),
                            min(self._last_display_import_prices) * 100,
                            max(self._last_display_import_prices) * 100,
                            min(import_prices) * 100,
                            max(import_prices) * 100,
                        )
                        return (import_prices, export_prices)

        # Static TOU pricing (GloBird, custom tariff, etc.)
        # Generate 576-point price forecast from tariff schedule
        tariff = self._tariff_schedule
        if not tariff:
            # Try reading from hass.data (updated by fetch_tesla_tariff_schedule)
            from ..const import DOMAIN
            tariff = (
                self.hass.data.get(DOMAIN, {})
                .get(self.entry_id, {})
                .get("tariff_schedule")
            )
            if tariff:
                _LOGGER.info("Using tariff_schedule from hass.data (not constructor)")
                self._tariff_schedule = tariff  # Cache for next time

        if tariff and tariff.get("tou_periods"):
            periods = tariff["tou_periods"]
            _LOGGER.info(
                "TOU tariff available: %s, periods=%s, buy_rates=%s, sell_rates=%s",
                tariff.get("plan_name", "unknown"),
                list(periods.keys()),
                {k: f"{v*100:.0f}c" for k, v in tariff.get("buy_rates", {}).items()},
                {k: f"{v*100:.0f}c" for k, v in tariff.get("sell_rates", {}).items()},
            )
            return self._generate_tou_price_forecast(tariff)

        _LOGGER.warning(
            "No price data available! price_coordinator=%s, tariff=%s. "
            "Optimizer will use default flat rates.",
            self.price_coordinator is not None,
            tariff is not None,
        )
        return None

    def _generate_tou_price_forecast(
        self, tariff: dict
    ) -> tuple[list[float], list[float]]:
        """Generate a 576-point price forecast from a TOU tariff schedule.

        Uses the tariff's TOU periods and buy/sell rates to produce
        per-interval prices for the LP optimizer's 48-hour horizon.

        Also stores unadjusted display prices for the mobile app chart
        (the LP needs tiny positive values to avoid degeneracy, but users
        should see the actual tariff rates).
        """
        # Snap to previous interval boundary so price steps align with
        # hour/TOU boundaries and match the schedule timestamps.
        raw_now = dt_util.now()
        interval = self._config.interval_minutes
        now = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0, microsecond=0,
        )
        tou_periods = tariff.get("tou_periods", {})
        buy_rates = tariff.get("buy_rates", {})
        sell_rates = tariff.get("sell_rates", {})
        horizon_minutes = int(self._config.horizon_hours * 60)
        n_steps = horizon_minutes // interval

        import_prices: list[float] = []
        export_prices: list[float] = []
        display_import: list[float] = []
        display_export: list[float] = []

        for t in range(n_steps):
            ts = now + timedelta(minutes=t * interval)
            hour = ts.hour
            dow = ts.weekday()
            # Tesla format: 0=Sunday, Python: 0=Monday
            tesla_dow = (dow + 1) % 7

            matched_period = None
            # Check in priority order to handle overlaps
            priority = [
                "SUPER_OFF_PEAK", "ON_PEAK", "PEAK",
                "PARTIAL_PEAK", "SHOULDER", "OFF_PEAK",
            ]
            for period_name in priority:
                if period_name not in tou_periods:
                    continue
                periods_list = tou_periods[period_name]
                if not isinstance(periods_list, list):
                    continue
                for period in periods_list:
                    from_dow = period.get("fromDayOfWeek", 0)
                    to_dow = period.get("toDayOfWeek", 6)
                    from_hour = period.get("fromHour", 0)
                    to_hour = period.get("toHour", 24)

                    # Day-of-week check
                    if from_dow <= to_dow:
                        if not (from_dow <= tesla_dow <= to_dow):
                            continue
                    else:
                        if not (tesla_dow >= from_dow or tesla_dow <= to_dow):
                            continue

                    # Hour check (handles overnight periods)
                    if from_hour <= to_hour:
                        if from_hour <= hour < to_hour:
                            matched_period = period_name
                            break
                    else:
                        if hour >= from_hour or hour < to_hour:
                            matched_period = period_name
                            break
                if matched_period:
                    break

            if not matched_period:
                matched_period = "OFF_PEAK"

            # buy_rates values are in $/kWh (e.g. 0.48 for 48c)
            # When the matched period isn't in buy_rates (e.g. GloBird gaps at 14-17, 21-24),
            # try common fallback period names, then use the median of available rates.
            buy = buy_rates.get(matched_period)
            if buy is None:
                for fallback in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                    if fallback in buy_rates:
                        buy = buy_rates[fallback]
                        break
                if buy is None:
                    # Use median of defined rates (better than arbitrary hardcoded default)
                    defined = sorted(v for v in buy_rates.values() if isinstance(v, (int, float)))
                    buy = defined[len(defined) // 2] if defined else 0.30

            sell = sell_rates.get(matched_period)
            if sell is None:
                for fallback in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                    if fallback in sell_rates:
                        sell = sell_rates[fallback]
                        break
                if sell is None:
                    defined = sorted(v for v in sell_rates.values() if isinstance(v, (int, float)))
                    sell = defined[len(defined) // 2] if defined else 0.05

            # Store actual tariff rates for display before LP adjustment
            display_import.append(buy)
            display_export.append(sell)

            # When price is zero the LP has zero marginal cost, so HiGHS
            # may assign imports/exports arbitrarily (LP degeneracy).
            # Use tiny positive values to break degeneracy while keeping
            # the LP bounded (negative import price → unbounded!).
            if buy < 0.001:
                buy = 0.001    # Near-free import: incentivizes charging
            if sell < 0.001:
                sell = 0.001   # Near-zero export: discourages wasteful export

            import_prices.append(buy)
            export_prices.append(sell)

        if import_prices:
            # Log price profile summary: unique (buy, sell) combos with hour ranges
            price_profile: dict[tuple[float, float], list[int]] = {}
            for t_idx in range(len(import_prices)):
                ts = now + timedelta(minutes=t_idx * interval)
                key = (round(import_prices[t_idx] * 100, 1), round(export_prices[t_idx] * 100, 1))
                if key not in price_profile:
                    price_profile[key] = []
                if not price_profile[key] or price_profile[key][-1] != ts.hour:
                    price_profile[key].append(ts.hour)
            profile_parts = []
            for (buy_c, sell_c), hours in sorted(price_profile.items()):
                unique_hours = sorted(set(hours))
                profile_parts.append(f"buy={buy_c}c sell={sell_c}c hrs={unique_hours}")
            _LOGGER.info(
                "Generated TOU price forecast: %d steps, %d unique profiles. %s",
                len(import_prices),
                len(price_profile),
                " | ".join(profile_parts),
            )

        # Store actual tariff prices for mobile app display
        self._last_display_import_prices = display_import
        self._last_display_export_prices = display_export

        # Apply demand charge penalty to LP import prices
        import_prices = self._apply_demand_charge_penalty(import_prices)

        return (import_prices, export_prices)

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

    def _get_ev_planned_load(self, n_intervals: int) -> list[float] | None:
        """Get EV planned charging load from AutoScheduleExecutor.

        Reads the selected charging windows from each vehicle's current plan
        and returns a per-interval power array in Watts matching the load
        forecast resolution.

        Args:
            n_intervals: Number of intervals in the load forecast.

        Returns:
            List of EV load in Watts per interval, or None if no EV plan.
        """
        from ..automations.ev_charging_planner import get_auto_schedule_executor

        executor = get_auto_schedule_executor()
        if not executor:
            return None

        # Access vehicle states directly for typed AutoScheduleState objects
        states = getattr(executor, "_state", {})
        if not states:
            return None

        now = dt_util.now()
        interval_minutes = self._config.interval_minutes
        ev_load = [0.0] * n_intervals
        has_any_windows = False

        for vehicle_id, state in states.items():
            plan = state.current_plan
            if not plan or not plan.windows:
                continue

            for window in plan.windows:
                try:
                    w_start = datetime.fromisoformat(window.start_time)
                    w_end = datetime.fromisoformat(window.end_time)
                except (ValueError, TypeError):
                    continue

                # Ensure timezone-aware comparison
                if w_start.tzinfo is None:
                    w_start = w_start.replace(tzinfo=now.tzinfo)
                if w_end.tzinfo is None:
                    w_end = w_end.replace(tzinfo=now.tzinfo)

                # Skip windows entirely in the past
                if w_end <= now:
                    continue

                power_w = window.estimated_power_kw * 1000

                # Map window to forecast indices
                start_offset_min = (w_start - now).total_seconds() / 60
                end_offset_min = (w_end - now).total_seconds() / 60

                idx_start = int(start_offset_min / interval_minutes)
                idx_end = int(end_offset_min / interval_minutes)

                # Clamp to valid range
                idx_start = max(0, idx_start)
                idx_end = min(n_intervals, idx_end)

                for i in range(idx_start, idx_end):
                    ev_load[i] += power_w
                    has_any_windows = True

        if not has_any_windows:
            return None

        # Log summary
        peak_kw = max(ev_load) / 1000
        dt_h = interval_minutes / 60
        total_kwh = sum(ev_load) / 1000 * dt_h
        active_intervals = sum(1 for v in ev_load if v > 0)
        _LOGGER.debug(
            "EV load overlay: %d intervals, peak %.1f kW, total %.1f kWh",
            active_intervals, peak_kw, total_kwh,
        )

        return ev_load

    async def _auto_detect_battery_specs(self) -> None:
        """Auto-detect battery capacity and power from Tesla site_info.

        User overrides saved in config entry take priority over auto-detection.
        """
        # Check for user overrides in config entry first
        if self._entry:
            from ..const import (
                CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
                CONF_OPTIMIZATION_MAX_CHARGE_W,
                CONF_OPTIMIZATION_MAX_DISCHARGE_W,
            )
            opts = self._entry.options
            saved_capacity = opts.get(CONF_OPTIMIZATION_BATTERY_CAPACITY_WH)
            saved_charge = opts.get(CONF_OPTIMIZATION_MAX_CHARGE_W)
            saved_discharge = opts.get(CONF_OPTIMIZATION_MAX_DISCHARGE_W)

            if saved_capacity or saved_charge or saved_discharge:
                if saved_capacity:
                    self._config.battery_capacity_wh = int(saved_capacity)
                if saved_charge:
                    self._config.max_charge_w = int(saved_charge)
                if saved_discharge:
                    self._config.max_discharge_w = int(saved_discharge)
                self._battery_specs_source = "manual"
                _LOGGER.info(
                    "Using saved battery specs (manual): %.1f kWh, charge %.1f kW, discharge %.1f kW",
                    self._config.battery_capacity_wh / 1000,
                    self._config.max_charge_w / 1000,
                    self._config.max_discharge_w / 1000,
                )
                return

        if not self.energy_coordinator:
            return

        # FoxESS auto-detection: read max charge/discharge current from Modbus data
        # FoxESS coordinators don't have site_info, but provide current limits via Modbus
        if hasattr(self.energy_coordinator, '_controller') and self.energy_coordinator.data:
            data = self.energy_coordinator.data
            max_charge_a = data.get("max_charge_current_a")
            max_discharge_a = data.get("max_discharge_current_a")

            if max_charge_a and max_charge_a > 0:
                # FoxESS HV batteries typically run at ~300-400V nominal
                # Use a conservative 300V to estimate power from current
                # Users can override via app settings if this is inaccurate
                battery_voltage = 300
                charge_w = int(max_charge_a * battery_voltage)
                discharge_w = int((max_discharge_a or max_charge_a) * battery_voltage)

                self._config.max_charge_w = charge_w
                self._config.max_discharge_w = discharge_w
                self._battery_specs_source = "auto"

                _LOGGER.info(
                    "Auto-detected FoxESS battery power from Modbus: "
                    "charge %.1fA × %dV = %.1f kW, discharge %.1fA × %dV = %.1f kW",
                    max_charge_a, battery_voltage, charge_w / 1000,
                    max_discharge_a or max_charge_a, battery_voltage, discharge_w / 1000,
                )
                return

        site_info = getattr(self.energy_coordinator, "_site_info_cache", None)
        if not site_info:
            # Try fetching it
            if hasattr(self.energy_coordinator, "async_get_site_info"):
                site_info = await self.energy_coordinator.async_get_site_info()

        if not site_info:
            _LOGGER.debug("No site_info available for battery auto-detection")
            return

        battery_count = site_info.get("battery_count", 0)
        nameplate_power = site_info.get("nameplate_power", 0)

        if battery_count > 0 and nameplate_power > 0:
            # nameplate_power is total site discharge power in watts
            discharge_w = int(nameplate_power)
            # Grid charge rate is limited by the gateway/inverter (~5kW per unit),
            # which is typically lower than the discharge rate.
            charge_w = min(discharge_w, 5000)
            # Estimate capacity: battery_count * 13.5 kWh per unit
            capacity_wh = int(battery_count * 13500)

            self._config.battery_capacity_wh = capacity_wh
            self._config.max_charge_w = charge_w
            self._config.max_discharge_w = discharge_w
            self._battery_specs_source = "auto"

            _LOGGER.info(
                "Auto-detected battery specs from site_info: "
                "%d units, %.1f kWh, charge %.1f kW, discharge %.1f kW",
                battery_count,
                capacity_wh / 1000,
                charge_w / 1000,
                discharge_w / 1000,
            )
        elif battery_count > 0:
            # Have count but no nameplate — estimate power per unit
            capacity_wh = int(battery_count * 13500)
            charge_w = 5000  # Conservative single gateway limit
            discharge_w = int(battery_count * 5000)

            self._config.battery_capacity_wh = capacity_wh
            self._config.max_charge_w = charge_w
            self._config.max_discharge_w = discharge_w
            self._battery_specs_source = "auto"

            _LOGGER.info(
                "Estimated battery specs from count: "
                "%d units, %.1f kWh, charge %.1f kW, discharge %.1f kW",
                battery_count,
                capacity_wh / 1000,
                charge_w / 1000,
                discharge_w / 1000,
            )

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

    async def _restore_cost_data(self) -> None:
        """Restore daily cost accumulators from persistent storage."""
        try:
            data = await self._cost_store.async_load()
        except Exception as e:
            _LOGGER.warning("Failed to load persisted cost data: %s", e)
            return

        if not data:
            _LOGGER.debug("No persisted cost data found (first run)")
            return

        stored_date = data.get("date")
        today = dt_util.now().strftime("%Y-%m-%d")

        if stored_date == today:
            self._actual_cost_today = float(data.get("actual_cost", 0.0))
            self._actual_baseline_today = float(data.get("baseline_cost", 0.0))
            self._actual_import_kwh_today = float(data.get("import_kwh", 0.0))
            self._actual_export_kwh_today = float(data.get("export_kwh", 0.0))
            self._actual_charge_kwh_today = float(data.get("charge_kwh", 0.0))
            self._actual_discharge_kwh_today = float(data.get("discharge_kwh", 0.0))
            self._actual_import_cost_today = float(data.get("import_cost", 0.0))
            self._actual_export_earnings_today = float(data.get("export_earnings", 0.0))
            self._last_cost_date = stored_date
            _LOGGER.info(
                "Restored daily costs: actual=$%.2f, baseline=$%.2f, "
                "import=%.2fkWh, export=%.2fkWh (date=%s)",
                self._actual_cost_today,
                self._actual_baseline_today,
                self._actual_import_kwh_today,
                self._actual_export_kwh_today,
                stored_date,
            )
        else:
            _LOGGER.info(
                "Persisted cost data is from %s (today=%s), starting fresh",
                stored_date, today,
            )

    def _schedule_cost_save(self) -> None:
        """Schedule a coalesced write of daily cost data to persistent storage."""
        self._cost_store.async_delay_save(
            self._cost_data_to_save,
            COST_STORE_SAVE_DELAY,
        )

    def _cost_data_to_save(self) -> dict:
        """Return cost data dict for Store serialization."""
        return {
            "date": self._last_cost_date,
            "actual_cost": round(self._actual_cost_today, 4),
            "baseline_cost": round(self._actual_baseline_today, 4),
            "import_kwh": round(self._actual_import_kwh_today, 4),
            "export_kwh": round(self._actual_export_kwh_today, 4),
            "charge_kwh": round(self._actual_charge_kwh_today, 4),
            "discharge_kwh": round(self._actual_discharge_kwh_today, 4),
            "import_cost": round(self._actual_import_cost_today, 4),
            "export_earnings": round(self._actual_export_earnings_today, 4),
        }

    def _get_forecast_offset(self) -> int:
        """Get number of steps elapsed since last LP run.

        The cached price/grid arrays start from the LP run time, not 'now'.
        This offset allows correct indexing when reading them later.
        """
        if not self._last_update_time:
            return 0
        elapsed = (dt_util.now() - self._last_update_time).total_seconds()
        return max(0, int(elapsed / (self._config.interval_minutes * 60)))

    def _track_actual_cost(self) -> None:
        """Track actual electricity cost using real elapsed time.

        Accumulates actual grid import/export costs since midnight.
        Also tracks baseline cost (what cost would be without battery).
        Uses actual elapsed time between calls to prevent multi-counting
        when called from multiple triggers (DataUpdateCoordinator, polling
        loop, price updates).
        Resets automatically at midnight.
        """
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")

        # Reset at midnight
        if self._last_cost_date != today:
            if self._last_cost_date is not None:
                _LOGGER.info(
                    "Daily cost reset (new day). Yesterday actual=$%.2f, baseline=$%.2f, savings=$%.2f",
                    self._actual_cost_today,
                    self._actual_baseline_today,
                    self._actual_baseline_today - self._actual_cost_today,
                )
            self._actual_cost_today = 0.0
            self._actual_baseline_today = 0.0
            self._actual_import_kwh_today = 0.0
            self._actual_export_kwh_today = 0.0
            self._actual_charge_kwh_today = 0.0
            self._actual_discharge_kwh_today = 0.0
            self._actual_import_cost_today = 0.0
            self._actual_export_earnings_today = 0.0
            self._last_cost_tracking_time = None
            self._last_cost_date = today

        # Use actual elapsed time to prevent multi-counting
        if self._last_cost_tracking_time is None:
            self._last_cost_tracking_time = now
            return  # First call — no interval to accumulate yet

        elapsed_seconds = (now - self._last_cost_tracking_time).total_seconds()

        # Skip if called too frequently (< 30s) — eliminates multi-counting
        if elapsed_seconds < 30:
            return

        self._last_cost_tracking_time = now

        # Cap at 10 minutes to avoid inflated accumulation after long gaps
        dt_hours = min(elapsed_seconds / 3600, 10.0 / 60)

        # Need energy coordinator data and cached prices
        if not self.energy_coordinator or not self.energy_coordinator.data:
            _LOGGER.debug("Cost tracking skipped: no energy coordinator data")
            return
        if not self._last_import_prices or not self._last_export_prices:
            _LOGGER.debug("Cost tracking skipped: no cached prices yet")
            return

        data = self.energy_coordinator.data
        # Energy coordinator stores values in kW
        grid_power_kw = float(data.get("grid_power", 0) or 0)
        solar_power_kw = float(data.get("solar_power", 0) or 0)
        battery_power_kw = float(data.get("battery_power", 0) or 0)

        # Current prices — use actual tariff prices, not LP-adjusted
        disp_import = self._last_display_import_prices or self._last_import_prices
        disp_export = self._last_display_export_prices or self._last_export_prices
        if not disp_import or not disp_export:
            _LOGGER.warning("Cost tracking skipped: empty price arrays")
            return
        import_price = disp_import[0]  # $/kWh — safe: arrays verified non-empty
        export_price = disp_export[0]   # $/kWh

        # Actual cost: grid_import costs money, grid_export earns money
        grid_import_kw = max(0.0, grid_power_kw)
        grid_export_kw = max(0.0, -grid_power_kw)
        actual_cost = (
            grid_import_kw * import_price * dt_hours
            - grid_export_kw * export_price * dt_hours
        )
        self._actual_cost_today += actual_cost

        # Accumulate actual energy measurements
        self._actual_import_kwh_today += grid_import_kw * dt_hours
        self._actual_export_kwh_today += grid_export_kw * dt_hours
        self._actual_import_cost_today += grid_import_kw * import_price * dt_hours
        self._actual_export_earnings_today += grid_export_kw * export_price * dt_hours

        # Track battery charge/discharge energy
        battery_charge_kw = max(0.0, -battery_power_kw)   # negative = charging
        battery_discharge_kw = max(0.0, battery_power_kw)  # positive = discharging
        self._actual_charge_kwh_today += battery_charge_kw * dt_hours
        self._actual_discharge_kwh_today += battery_discharge_kw * dt_hours

        # Baseline cost: what would happen without a battery
        # Power balance: load = solar + grid + battery (Tesla sign convention)
        # Without battery, net_grid = load - solar = grid_power + battery_power
        baseline_grid_kw = grid_power_kw + battery_power_kw
        baseline_import_kw = max(0.0, baseline_grid_kw)
        baseline_export_kw = max(0.0, -baseline_grid_kw)
        baseline_cost = (
            baseline_import_kw * import_price * dt_hours
            - baseline_export_kw * export_price * dt_hours
        )
        self._actual_baseline_today += baseline_cost

        _LOGGER.debug(
            "Cost tracking: grid=%.2fkW, dt=%.4fh, actual_interval=$%.4f, "
            "actual_today=$%.2f, baseline_today=$%.2f, "
            "import=%.2fkWh, export=%.2fkWh",
            grid_power_kw, dt_hours, actual_cost,
            self._actual_cost_today, self._actual_baseline_today,
            self._actual_import_kwh_today, self._actual_export_kwh_today,
        )

        # Persist cost data (coalesced — writes at most every 5 minutes)
        self._schedule_cost_save()

    def _get_predicted_cost_to_midnight(self) -> tuple[float, float]:
        """Calculate predicted cost and baseline from now until midnight.

        Uses the LP optimizer's solution (grid_import/export arrays) and
        cached forecasts to project cost for the remainder of today.

        Arrays are indexed from the LP run time, so we apply a time offset
        to align them with 'now'.

        Returns:
            Tuple of (predicted_cost_remaining, baseline_cost_remaining)
        """
        if not self._last_optimizer_result or not self._last_import_prices:
            return (0.0, 0.0)

        grid_import_w = self._last_optimizer_result.grid_import_w
        grid_export_w = self._last_optimizer_result.grid_export_w
        if not grid_import_w or not grid_export_w:
            _LOGGER.warning(
                "Predicted cost: LP returned empty grid arrays, skipping prediction"
            )
            return (0.0, 0.0)

        now = dt_util.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        minutes_to_midnight = (midnight - now).total_seconds() / 60
        steps_to_midnight = int(minutes_to_midnight / self._config.interval_minutes)

        # Use actual tariff prices for cost projections, not LP-adjusted
        prices_import = self._last_display_import_prices or self._last_import_prices
        prices_export = self._last_display_export_prices or self._last_export_prices

        # Arrays start from LP run time — offset to align with 'now'
        offset = self._get_forecast_offset()

        dt_hours = self._config.interval_minutes / 60

        predicted_cost = 0.0
        baseline_cost = 0.0
        for step in range(1, steps_to_midnight + 1):
            # Index into arrays: offset (LP run → now) + step (now → future)
            idx = offset + step

            # Bounds-check all arrays consistently
            if idx >= len(grid_import_w) or idx >= len(prices_import):
                break

            import_p = prices_import[idx]
            export_p = (
                prices_export[idx]
                if idx < len(prices_export)
                else 0.05
            )

            # Predicted cost with battery optimization
            predicted_cost += import_p * (grid_import_w[idx] / 1000) * dt_hours
            predicted_cost -= export_p * (
                grid_export_w[idx] / 1000
                if idx < len(grid_export_w)
                else 0.0
            ) * dt_hours

            # Baseline cost without battery
            solar_kw = (
                self._last_solar_forecast[idx]
                if self._last_solar_forecast and idx < len(self._last_solar_forecast)
                else 0.0
            )
            load_kw = (
                self._last_load_forecast[idx]
                if self._last_load_forecast and idx < len(self._last_load_forecast)
                else 0.0
            )
            net_load = load_kw - solar_kw
            baseline_import = max(0.0, net_load)
            baseline_export = max(0.0, -net_load)
            baseline_cost += import_p * baseline_import * dt_hours
            baseline_cost -= export_p * baseline_export * dt_hours

        return (predicted_cost, baseline_cost)

    def _get_daily_cost(self) -> float:
        """Get today's total cost: actual (midnight→now) + predicted (now→midnight)."""
        predicted_remaining, _ = self._get_predicted_cost_to_midnight()
        return round(self._actual_cost_today + predicted_remaining, 2)

    def _get_daily_savings(self) -> float:
        """Get today's total savings vs baseline without battery."""
        predicted_remaining, baseline_remaining = self._get_predicted_cost_to_midnight()
        total_cost = self._actual_cost_today + predicted_remaining
        total_baseline = self._actual_baseline_today + baseline_remaining
        return round(total_baseline - total_cost, 2)

    def set_cost_function(self, cost_function: str | CostFunction) -> None:
        """Set the optimization cost function."""
        if isinstance(cost_function, str):
            self._cost_function = CostFunction(cost_function)
        else:
            self._cost_function = cost_function

        self._config.cost_function = self._cost_function.value
        _LOGGER.info("Cost function set to: %s", self._cost_function.value)

    def update_config(self, **kwargs) -> None:
        """Update optimization configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)

        # Sync config to optimizer
        if self._optimizer:
            self._optimizer.update_config(
                capacity_wh=self._config.battery_capacity_wh,
                max_charge_w=self._config.max_charge_w,
                max_discharge_w=self._config.max_discharge_w,
                backup_reserve=self._config.backup_reserve,
            )

    async def force_reoptimize(self) -> Any:
        """Force immediate re-optimization."""
        await self._run_optimization()
        return self._current_schedule

    def get_forecast_data(self) -> dict[str, Any]:
        """Get forecast data for LP forecast sensors.

        Returns summary values (for sensor state) and full arrays (for attributes).
        """
        data: dict[str, Any] = {
            "available": self._last_solar_forecast is not None,
        }
        dt_h = self._config.interval_minutes / 60

        if self._last_solar_forecast:
            data["solar_forecast_kwh"] = sum(self._last_solar_forecast) * dt_h
            data["solar_peak_kw"] = max(self._last_solar_forecast)
            data["solar_forecast"] = self._last_solar_forecast

        if self._last_load_forecast:
            data["load_forecast_kwh"] = sum(self._last_load_forecast) * dt_h
            data["load_peak_kw"] = max(self._last_load_forecast)
            data["load_forecast"] = self._last_load_forecast

        # Use actual tariff prices for display (not LP-adjusted values)
        disp_import = self._last_display_import_prices or self._last_import_prices
        disp_export = self._last_display_export_prices or self._last_export_prices

        if disp_import:
            data["import_price_avg"] = sum(disp_import) / len(disp_import)
            data["import_price_min"] = min(disp_import)
            data["import_price_max"] = max(disp_import)
            data["import_prices"] = disp_import

        if disp_export:
            data["export_price_avg"] = sum(disp_export) / len(disp_export)
            data["export_price_min"] = min(disp_export)
            data["export_price_max"] = max(disp_export)
            data["export_prices"] = disp_export

        return data

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API and mobile app."""
        optimizer_available = self._optimizer is not None

        # Determine status message
        if optimizer_available:
            if self._current_schedule and self._current_schedule.actions:
                status_message = "Optimization active"
            else:
                status_message = "Optimizer ready — waiting for data"
        else:
            status_message = "Optimizer not initialized"

        # Get current action info
        current_action = "idle"
        current_power_w = self._get_actual_battery_power_w()
        next_action = "idle"
        next_action_time = None
        next_action_power_w = 0

        if self._current_schedule and self._current_schedule.actions:
            ca = self._get_current_action()
            if ca:
                current_action = ca.action

            # Find next different action
            now = dt_util.now()
            for a in self._current_schedule.actions:
                if a.timestamp > now and a.action != current_action:
                    next_action = a.action
                    next_action_time = a.timestamp.isoformat()
                    next_action_power_w = a.power_w
                    break

        # LP-specific stats
        lp_stats = {}
        if self._last_optimizer_result:
            lp_stats = {
                "solve_time_s": round(self._last_optimizer_result.solve_time_s, 3),
                "objective_value": round(self._last_optimizer_result.objective_value, 4),
                "solver_used": self._last_optimizer_result.solver_used,
                "feasible": self._last_optimizer_result.feasible,
            }

        data = {
            "success": True,
            "enabled": self._enabled,
            "optimizer_available": optimizer_available,
            "engine_available": optimizer_available,
            "engine": "built-in",
            "status_message": status_message,
            "cost_function": self._cost_function.value,
            "status": "active" if self._enabled and optimizer_available else "disabled",
            "optimization_status": "active" if optimizer_available else "not_available",
            "current_action": current_action,
            "current_power_w": current_power_w,
            "next_action": next_action,
            "next_action_time": next_action_time,
            "next_action_power_w": next_action_power_w,
            "last_optimization": self._last_update_time.isoformat() if self._last_update_time else None,
            "predicted_cost": self._get_daily_cost(),
            "predicted_savings": self._get_daily_savings(),
            "lp_stats": lp_stats,
            "config": {
                "battery_capacity_wh": self._config.battery_capacity_wh,
                "max_charge_w": self._config.max_charge_w,
                "max_discharge_w": self._config.max_discharge_w,
                "battery_specs_source": self._battery_specs_source,
                "backup_reserve": self._config.backup_reserve,
                "interval_minutes": self._config.interval_minutes,
                "horizon_hours": self._config.horizon_hours,
            },
            "features": {
                "ev_integration": self._ev_integration_enabled or len(self._ev_configs) > 0,
                "vpp_enabled": False,
                "built_in_optimizer": True,
            },
        }

        # Add daily cost breakdown (actual + predicted remaining)
        pred_remaining, baseline_remaining = self._get_predicted_cost_to_midnight()
        data["daily_cost_breakdown"] = {
            "actual_cost": round(self._actual_cost_today, 2),
            "actual_baseline": round(self._actual_baseline_today, 2),
            "actual_savings": round(self._actual_baseline_today - self._actual_cost_today, 2),
            "predicted_remaining": round(pred_remaining, 2),
            "predicted_baseline_remaining": round(baseline_remaining, 2),
            "actual_import_cost": round(self._actual_import_cost_today, 2),
            "actual_export_earnings": round(self._actual_export_earnings_today, 2),
        }

        # Add EV status if EV coordination is active
        if self._ev_coordinator:
            data["ev"] = self._ev_coordinator.get_status()

            # Also include auto-schedule plan data if available
            from ..automations.ev_charging_planner import get_auto_schedule_executor
            executor = get_auto_schedule_executor()
            if executor:
                data["ev"]["auto_schedule"] = executor.get_all_states()

        # Add schedule data if available
        if self._current_schedule:
            api_response = self._current_schedule.to_api_response()
            # Add grid import/export from LP result
            if self._last_optimizer_result:
                api_response["grid_import_w"] = self._last_optimizer_result.grid_import_w
                api_response["grid_export_w"] = self._last_optimizer_result.grid_export_w
            # Add price arrays for pricing overlay (use actual tariff rates, not LP-adjusted)
            n_sched = len(api_response["timestamps"])
            display_import = self._last_display_import_prices or self._last_import_prices
            display_export = self._last_display_export_prices or self._last_export_prices
            if display_import:
                api_response["import_price"] = display_import[:n_sched]
            if display_export:
                api_response["export_price"] = display_export[:n_sched]
            data["schedule"] = api_response

            # Add EV charging power overlay if EV coordination is active
            if self._ev_coordinator and data.get("ev"):
                ev_power = [0.0] * len(api_response["timestamps"])
                charging_plan = data["ev"].get("charging_plan", [])
                if charging_plan:
                    from datetime import datetime as _dt
                    for window in charging_plan:
                        w_start = _dt.fromisoformat(window["start"])
                        w_end = _dt.fromisoformat(window["end"])
                        w_power = window.get("power_available_w", 0)
                        for idx, ts_str in enumerate(api_response["timestamps"]):
                            ts = _dt.fromisoformat(ts_str)
                            if w_start <= ts < w_end:
                                ev_power[idx] = w_power
                api_response["ev_charging_w"] = ev_power

            daily_cost = self._get_daily_cost()
            daily_savings = self._get_daily_savings()
            data["summary"] = {
                "total_cost": daily_cost,
                "total_import_kwh": round(self._actual_import_kwh_today, 2),
                "total_export_kwh": round(self._actual_export_kwh_today, 2),
                "total_charge_kwh": round(self._actual_charge_kwh_today, 2),
                "total_discharge_kwh": round(self._actual_discharge_kwh_today, 2),
                "baseline_cost": daily_cost + daily_savings,
                "savings": daily_savings,
            }
            # Consolidate schedule into action ranges for the next 24h
            # e.g. [self_consumption 16:00-17:00, export 17:00-21:00, ...]
            intervals_24h = min(
                int(24 * 60 / self._config.interval_minutes),
                len(self._current_schedule.actions),
            )
            action_ranges: list[dict[str, Any]] = []
            for a in self._current_schedule.actions[:intervals_24h]:
                ad = a.to_dict()
                if (
                    action_ranges
                    and action_ranges[-1]["action"] == ad["action"]
                ):
                    # Extend the current range
                    action_ranges[-1]["end_time"] = ad["timestamp"]
                    action_ranges[-1]["soc"] = ad["soc"]
                    if ad["power_w"]:
                        power_vals = action_ranges[-1].setdefault("_powers", [])
                        power_vals.append(ad["power_w"])
                        action_ranges[-1]["power_w"] = max(power_vals)
                else:
                    # Start a new range
                    action_ranges.append({
                        "action": ad["action"],
                        "timestamp": ad["timestamp"],
                        "end_time": ad["timestamp"],
                        "power_w": ad["power_w"],
                        "soc": ad["soc"],
                        "_powers": [ad["power_w"]] if ad["power_w"] else [],
                    })
            # Clean up internal _powers list before sending
            for ar in action_ranges:
                ar.pop("_powers", None)
            data["next_actions"] = action_ranges

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
            # Convert backup_reserve from percentage (0-100) to decimal (0-1)
            if "backup_reserve" in config_updates:
                reserve = config_updates["backup_reserve"]
                if reserve > 1:
                    config_updates["backup_reserve"] = reserve / 100

            self.update_config(**config_updates)
            response["changes"].append(f"config: {list(config_updates.keys())}")

            # Persist settings to config entry
            if self._entry:
                from ..const import (
                    CONF_OPTIMIZATION_BACKUP_RESERVE,
                    CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
                    CONF_OPTIMIZATION_MAX_CHARGE_W,
                    CONF_OPTIMIZATION_MAX_DISCHARGE_W,
                )
                new_options = dict(self._entry.options)
                if "backup_reserve" in settings:
                    reserve_pct = settings["backup_reserve"]
                    if reserve_pct <= 1:
                        reserve_pct = int(reserve_pct * 100)
                    new_options[CONF_OPTIMIZATION_BACKUP_RESERVE] = reserve_pct
                if "battery_capacity_wh" in settings:
                    new_options[CONF_OPTIMIZATION_BATTERY_CAPACITY_WH] = int(settings["battery_capacity_wh"])
                if "max_charge_w" in settings:
                    new_options[CONF_OPTIMIZATION_MAX_CHARGE_W] = int(settings["max_charge_w"])
                if "max_discharge_w" in settings:
                    new_options[CONF_OPTIMIZATION_MAX_DISCHARGE_W] = int(settings["max_discharge_w"])
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)

            # Mark as manual when user explicitly sets battery specs
            if any(k in settings for k in ("battery_capacity_wh", "max_charge_w", "max_discharge_w")):
                self._battery_specs_source = "manual"

        # Handle EV integration toggle
        if "ev_integration" in settings:
            ev_enabled = settings["ev_integration"]
            self._ev_integration_enabled = ev_enabled
            if self._entry:
                from ..const import CONF_OPTIMIZATION_EV_INTEGRATION
                new_options = dict(self._entry.options)
                new_options[CONF_OPTIMIZATION_EV_INTEGRATION] = ev_enabled
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)
                response["changes"].append(f"ev_integration: {ev_enabled}")

        return response

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic data update — re-optimize and return API data."""
        if self._enabled:
            await self._run_optimization()

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

        _LOGGER.info("Added EV charger: %s (%s)", config.name, entity_id)
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

        _LOGGER.info("Removed EV charger: %s", entity_id)
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
                _LOGGER.error("Invalid EV charging mode: %s", mode)
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
