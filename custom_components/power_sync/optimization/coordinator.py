"""
Optimization coordinator for PowerSync.

Coordinates data collection and runs the built-in LP battery optimizer
to produce a schedule, which the execution layer then applies.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ConfigEntryNotReady

from .battery_controller import TRUSTED_FOR_PERSIST
from .battery_optimizer import BatteryOptimizer, OptimizerResult
from .schedule_reader import OptimizationSchedule, ScheduleAction
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode
from ..const import (
    CONF_GENERIC_CHARGER_POWER_ENTITY,
    DEFAULT_OPTIMIZATION_INTERVAL,
    supports_no_idle_mode_provider,
)
from ..flow_power_pricing import (
    FlowPowerPricingContext,
    calculate_flow_power_pea,
    resolve_flow_power_pricing_context,
)
from ..tariff_time import find_matching_tou_period, period_entries
from ..zerohero import (
    ZeroHeroConfig,
    settle_zerocharge_imports,
    settle_zerohero_series,
    zerohero_config_from_entry,
    zerohero_credit_status,
    zerohero_is_in_window,
    zerohero_window_end_for,
    zerocharge_is_in_window,
)

_LOGGER = logging.getLogger(__name__)

# Optimiser decision summary logger.
#
# The per-cycle decision line (solver result + planned schedule) is the single
# most useful signal for support/triage: it answers "is it planning to charge,
# export, or hold?" at a glance. We want it visible in standard logs WITHOUT
# asking users to raise the whole integration to INFO/DEBUG.
#
# So this dedicated child logger is pinned to INFO. Because the record still
# propagates to the root handlers, an INFO record here is emitted even when the
# parent ``custom_components.power_sync`` logger sits at the default WARNING.
# We only ever *raise* visibility (NOTSET / stricter-than-INFO -> INFO); a user
# who deliberately enables DEBUG keeps DEBUG. This is intentionally scoped to one
# logger, unlike the old blanket force-DEBUG-on-import that PR #f8192959 removed.
_DECISION_LOGGER = logging.getLogger(f"{__name__}.decisions")
if _DECISION_LOGGER.level == logging.NOTSET or _DECISION_LOGGER.level > logging.INFO:
    _DECISION_LOGGER.setLevel(logging.INFO)

CUSTOM_BATTERY_SYSTEM = "custom"
CUSTOM_BATTERY_LEVEL_ENTITY = "custom_battery_level_entity"
CUSTOM_BATTERY_POWER_ENTITY = "custom_battery_power_entity"
CUSTOM_GRID_POWER_ENTITY = "custom_grid_power_entity"
CUSTOM_SOLAR_POWER_ENTITY = "custom_solar_power_entity"
CUSTOM_LOAD_POWER_ENTITY = "custom_load_power_entity"

COST_STORE_VERSION = 1
COST_STORE_SAVE_DELAY = 300  # Coalesce writes — flush at most every 5 minutes
INITIAL_OPTIMIZATION_DELAY_SECONDS = 90.0
FIXED_OPTIMIZATION_INTERVAL_MINUTES = DEFAULT_OPTIMIZATION_INTERVAL
FLOW_POWER_NEM_TZ = timezone(timedelta(hours=10))
EXPORT_ACTIONS = {"discharge", "export"}
SELF_USE_ACTIONS = {"consume", "self_consumption"}
CHARGE_ACTIONS = {"charge"}
OPTIMIZER_FORCE_CHARGE_MIN_COMMITMENT = timedelta(minutes=20)
OPTIMIZER_FORCE_DISCHARGE_MIN_COMMITMENT = timedelta(minutes=20)
SUNGROW_INFERRED_RESTORE_COOLDOWN = timedelta(minutes=5)
BELOW_RESERVE_RECOVERY_HOLD_MARGIN_SOC = 0.02


def _flow_power_network_tariff_rate(
    when: datetime,
    network: str,
    tariff_code: str,
) -> float | None:
    """Return the Flow Power v2 network tariff rate for an interval."""
    from ..tariff_utils import get_network_tariff_rate

    return get_network_tariff_rate(when, network, tariff_code)


def _hhmm_to_minutes(value: Any, default: str = "17:15") -> int:
    """Return minutes after midnight for a HH:MM or HHMM value."""
    candidate = value if isinstance(value, str) else default
    compact = candidate.strip()
    if compact.isdigit() and len(compact) in (3, 4):
        hour = int(compact[:-2])
        minute = int(compact[-2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute

    try:
        hour_raw, minute_raw = compact.split(":", 1)
        hour = int(hour_raw)
        minute = int(minute_raw)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (AttributeError, TypeError, ValueError):
        pass

    if candidate != default:
        return _hhmm_to_minutes(default, default)
    return 17 * 60 + 15


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
    max_grid_import_w: int | None = None
    max_grid_export_w: int | None = None
    allow_grid_charge: bool = True
    max_grid_charge_price: float | None = None
    grid_charge_soc_cap: float = 1.0
    backup_reserve: float = 0.2
    interval_minutes: int = FIXED_OPTIMIZATION_INTERVAL_MINUTES
    horizon_hours: int = 48
    cost_function: str = "cost"
    profit_max_enabled: bool = False
    charge_by_time_enabled: bool = False
    charge_by_time_target_time: str = "17:15"
    charge_by_time_target_soc: float = 1.0
    spread_export_enabled: bool = False
    spread_import_enabled: bool = False
    disable_idle_enabled: bool = False
    auto_apply_reserve_enabled: bool = False
    manual_backup_reserve: float | None = None


# Update interval for the coordinator
UPDATE_INTERVAL = timedelta(minutes=FIXED_OPTIMIZATION_INTERVAL_MINUTES)


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
        force_state_clearer: Callable[[], None] | None = None,
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
        self._force_state_clearer = force_state_clearer

        # Configuration
        self._enabled = False
        self._config = OptimizationConfig()
        self._cost_function = CostFunction("cost")
        self._provider_config = ProviderPriceConfig()
        self._last_custom_energy_warning: str | None = None
        self._auto_apply_reserve_enabled = False
        self._manual_backup_reserve: float | None = None
        self._active_export_reserve_floor_slots: list[float] | None = None
        self._active_export_reserve_floor_timestamps: list[datetime] | None = None
        if self._entry:
            from ..const import (
                CONF_OPTIMIZATION_AUTO_APPLY_RESERVE,
                CONF_OPTIMIZATION_BACKUP_RESERVE,
                CONF_OPTIMIZATION_MANUAL_RESERVE,
            )

            self._auto_apply_reserve_enabled = bool(
                self._entry.options.get(
                    CONF_OPTIMIZATION_AUTO_APPLY_RESERVE,
                    self._entry.data.get(CONF_OPTIMIZATION_AUTO_APPLY_RESERVE, False),
                )
            )
            self._manual_backup_reserve = self._reserve_ratio(
                self._entry.options.get(
                    CONF_OPTIMIZATION_MANUAL_RESERVE,
                    self._entry.data.get(CONF_OPTIMIZATION_MANUAL_RESERVE),
                )
            )
            if self._manual_backup_reserve is None:
                self._manual_backup_reserve = self._reserve_ratio(
                    self._entry.data.get(
                        CONF_OPTIMIZATION_BACKUP_RESERVE,
                        self._entry.options.get(CONF_OPTIMIZATION_BACKUP_RESERVE),
                    )
                )
            self._config.auto_apply_reserve_enabled = self._auto_apply_reserve_enabled
            self._config.manual_backup_reserve = self._manual_backup_reserve

        # Lock to prevent concurrent LP solves. Three independent triggers
        # (DataUpdateCoordinator's _async_update_data, _schedule_polling_loop,
        # and _on_price_update) can fire at the same 5-min boundary, causing
        # 2-3 duplicate Modbus writes per cycle. The lock serialises them so
        # only one LP solve runs at a time.
        try:
            self._optimization_lock = asyncio.Lock()
        except RuntimeError:
            # Python 3.9 requires an event loop at construction time; some
            # tests instantiate the coordinator synchronously.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._optimization_lock = asyncio.Lock()

        # Reentrancy guard around _execute_optimizer_action. The polling
        # loop's cached-action path (_execute_cached_current_action_if_changed)
        # and the DataUpdateCoordinator's refresh cycle can both cross the
        # same wall-clock boundary and try to apply an action transition at
        # once. _last_executed_action is only written at the END of
        # _execute_optimizer_action (after awaited hardware I/O), so both
        # callers can pass the dedup check before either has updated the
        # marker, producing a double hardware command (double force-timer
        # extension, double Tesla TOU upload). This lock is independent of
        # _optimization_lock: _run_optimization acquires _optimization_lock
        # first and then this lock around its call to
        # _execute_optimizer_action (consistent nesting order), while
        # _execute_cached_current_action_if_changed only ever acquires this
        # lock on its own — so the two locks can never deadlock each other.
        try:
            self._execute_lock = asyncio.Lock()
        except RuntimeError:
            self._execute_lock = asyncio.Lock()

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
        self._configured_load_entity_id: str | None = None
        self._planned_ev_load_entity_id: str | None = None
        if self._entry:
            from ..const import (
                CONF_OPTIMIZATION_EV_INTEGRATION,
                CONF_OPTIMIZATION_LOAD_ENTITY,
                CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY,
            )
            self._configured_load_entity_id = self._entry.options.get(
                CONF_OPTIMIZATION_LOAD_ENTITY,
                self._entry.data.get(CONF_OPTIMIZATION_LOAD_ENTITY),
            ) or None
            self._ev_integration_enabled = self._entry.options.get(
                CONF_OPTIMIZATION_EV_INTEGRATION,
                self._entry.data.get(CONF_OPTIMIZATION_EV_INTEGRATION, False),
            )
            self._planned_ev_load_entity_id = self._entry.options.get(
                CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY,
                self._entry.data.get(CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY),
            ) or None

        # Cached schedule from optimizer
        self._current_schedule: OptimizationSchedule | None = None
        self._last_update_time: datetime | None = None
        self._initial_optimization_not_before: datetime | None = None

        # Cached forecast data (populated each optimization run)
        self._last_solar_forecast: list[float] | None = None    # kW values
        self._has_solar_forecast: bool | None = None  # None until the first forecast attempt
        self._last_load_forecast: list[float] | None = None     # kW values
        self._last_import_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_export_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_display_import_prices: list[float] | None = None  # $/kWh actual tariff
        self._last_display_export_prices: list[float] | None = None  # $/kWh actual tariff
        self._last_grid_charge_cap_import_prices: list[float] | None = None  # $/kWh hard cap reference
        self._last_export_boost_allowed_slots: list[bool] = []
        self._last_price_timestamps: list[datetime] | None = None
        self._last_planned_ev_load_forecast_w: list[float] | None = None
        self._last_zerohero_bonus_prices: list[float] | None = None
        self._last_zerohero_bonus_cap_kwh: float | None = None
        self._last_zerocharge_bonus_prices: list[float] | None = None
        self._last_zerocharge_bonus_cap_kwh: float | None = None
        self._solar_nowcast_derate: float = 1.0
        self._last_solar_nowcast_ratio: float | None = None
        self._last_logged_solar_nowcast_derate: float | None = None

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
        # Grid-sourced battery charging only (excludes solar charging and
        # house-load import). Their ratio is the true $/kWh acquisition cost of
        # stored grid energy used by the export-profitability gate.
        self._actual_grid_charge_kwh_today = 0.0
        self._actual_grid_charge_cost_today = 0.0
        self._actual_zerohero_import_kwh_today = 0.0
        self._actual_zerohero_export_kwh_today = 0.0
        self._actual_zerohero_bonus_export_kwh_today = 0.0
        self._actual_zerohero_base_export_earnings_today = 0.0
        self._actual_zerohero_bonus_export_earnings_today = 0.0
        self._actual_zerohero_credit_value_today = 0.0
        self._actual_zerocharge_import_kwh_today = 0.0
        self._actual_zerocharge_credit_value_today = 0.0
        self._baseline_zerohero_import_kwh_today = 0.0
        self._baseline_zerohero_bonus_export_kwh_today = 0.0
        self._baseline_zerohero_credit_value_today = 0.0
        self._baseline_zerocharge_import_kwh_today = 0.0
        self._baseline_zerocharge_credit_value_today = 0.0
        self._cost_store = Store(
            hass,
            COST_STORE_VERSION,
            f"power_sync.costs.{entry_id}",
        )

        # Saving sessions coordinator (set from __init__.py when configured)
        self._saving_session_coordinator = None

        # Price monitoring
        self._is_dynamic_pricing = False
        self._price_listener_unsub: Callable | None = None
        # Secondary listener used only for Octopus on a non-dynamic tariff:
        # re-checks the live tariff_code on each refresh and promotes to
        # dynamic pricing if the user moves onto AGILE/FLUX/COSY.
        self._octopus_gate_listener_unsub: Callable | None = None
        # Deduplication key for AEMO price-update trigger — LP only fires on new dispatch files
        self._last_aemo_dispatch_file: str | None = None
        # Rate-limit for non-AEMO price-triggered LP runs. Amber/Octopus can
        # send both usage and spot-price updates in one billing window; running
        # the LP twice in quick succession can churn force mode commands.
        self._last_price_triggered_optimization: datetime | None = None

        # Track last executed action for mode transitions and status reporting.
        self._last_executed_action: str | None = None
        self._last_executed_planned_action: str | None = None
        # Optimizer-issued force commands use a hardware-only service path for
        # non-Tesla systems so automated actions do not appear as manual force
        # countdowns in the UI. Track that private state here so a later LP
        # solve can distinguish "no force active" from "optimizer force active".
        self._optimizer_force_state: dict[str, Any] = {
            "active": False,
            "type": None,
            "expires_at": None,
            "hardware_expires_at": None,
            "power_w": 0,
            "started_at": None,
            "source": "optimizer",
            "scope": "optimizer",
        }
        # Physical battery backup reserve saved before IDLE raises it.
        # Restored when exiting IDLE so we don't overwrite the user's
        # hardware reserve with the optimizer's LP floor.
        self._pre_idle_backup_reserve: int | None = None
        self._idle_hold_reserve: int | None = None
        self._scheduled_ev_no_discharge_active = False
        # User's real backup reserve captured ONCE on startup, before any
        # IDLE modifies it. Used as the authoritative restore value.
        self._startup_backup_reserve: int | None = None
        self._idle_reserve_adjustment: bool = False  # True while IDLE is setting backup_reserve (suppresses persistence)

        # Background task handles (for cancellation on disable)
        self._polling_task: asyncio.Task | None = None
        self._initial_opt_task: asyncio.Task | None = None
        self._deferred_restore_task: asyncio.Task | None = None
        self._settings_reoptimize_task: asyncio.Task | None = None
        self._settings_reoptimize_requested = False
        # Price-triggered re-optimization spawned by _on_price_update. Must be
        # tracked and cancelled on disable() like the other background tasks —
        # otherwise a price-triggered LP solve in flight during disable() can
        # complete and re-command the battery after disable() already
        # restored normal operation.
        self._price_reoptimize_task: asyncio.Task | None = None

    def _monitoring_mode_active(self) -> bool:
        """Return True when monitoring mode should block hardware writes."""
        if self.battery_system == CUSTOM_BATTERY_SYSTEM:
            return True
        if not self._entry:
            return False
        from ..const import CONF_MONITORING_MODE

        return bool(
            self._entry.options.get(
                CONF_MONITORING_MODE,
                self._entry.data.get(CONF_MONITORING_MODE, False),
            )
        )

    async def _restore_pre_idle_backup_reserve(self, battery, context: str = "") -> bool:
        """Restore pre-IDLE backup reserve with retry. Only clears on success."""
        if self._pre_idle_backup_reserve is None:
            self._idle_hold_reserve = None
            return True
        if not hasattr(battery, "set_backup_reserve"):
            self._pre_idle_backup_reserve = None
            self._idle_hold_reserve = None
            return True
        if self._monitoring_mode_active():
            _LOGGER.info(
                "[MONITORING] Optimizer would restore pre-IDLE backup reserve to %d%%%s — blocked by monitoring mode",
                self._pre_idle_backup_reserve,
                f" ({context})" if context else "",
            )
            return False
        try:
            result = await battery.set_backup_reserve(self._pre_idle_backup_reserve)
            if result is False:
                _LOGGER.warning(
                    "Failed to restore backup reserve to %d%%: command returned False "
                    "(will retry next cycle)",
                    self._pre_idle_backup_reserve,
                )
                return False
            _LOGGER.info(
                "Optimizer: Restored backup reserve to %d%%%s",
                self._pre_idle_backup_reserve,
                f" ({context})" if context else "",
            )
            self._pre_idle_backup_reserve = None
            self._idle_hold_reserve = None
            return True
        except Exception as e:
            _LOGGER.warning(
                "Failed to restore backup reserve to %d%%: %s (will retry next cycle)",
                self._pre_idle_backup_reserve, e,
            )
            return False

    def _should_restore_pre_idle_backup_reserve_from_polling(self) -> bool:
        """Return True when the polling loop should retry a pending reserve restore."""
        return (
            self._pre_idle_backup_reserve is not None
            and self._last_executed_action != "idle"
            and not self._scheduled_ev_no_discharge_active
        )

    def _scheduled_ev_preserve_active(self) -> bool:
        """Return True when scheduled EV charging requested no-discharge mode."""
        state = (
            self.hass.data.get("power_sync", {})
            .get(self.entry_id, {})
            .get("scheduled_ev_preserve_state", {})
        )
        return bool(state.get("active"))

    async def _set_scheduled_ev_no_discharge_mode(self, battery, reason: str) -> bool:
        """Prevent home-battery discharge while still allowing battery charging."""
        if self._scheduled_ev_no_discharge_active:
            return True

        try:
            if (
                self.energy_coordinator
                and hasattr(self.energy_coordinator, "set_no_discharge_mode")
            ):
                ok = await self.energy_coordinator.set_no_discharge_mode()
            else:
                ok = await self._set_idle_hold_mode(battery, preserve_charge=True)
        except Exception as err:
            _LOGGER.warning(
                "Scheduled EV preserve: failed to enter no-discharge mode: %s",
                err,
            )
            return False

        if ok is False:
            _LOGGER.warning("Scheduled EV preserve: no-discharge mode returned False")
            return False

        self._scheduled_ev_no_discharge_active = True
        _LOGGER.info(
            "Scheduled EV preserve: battery discharge blocked, charging still allowed (%s)",
            reason,
        )
        return True

    async def _release_scheduled_ev_no_discharge_mode(self, reason: str = "") -> bool:
        """Release scheduled EV no-discharge mode when preserve is no longer active."""
        if not self._scheduled_ev_no_discharge_active:
            return True
        if self._monitoring_mode_active():
            _LOGGER.info(
                "[MONITORING] Optimizer would release scheduled EV no-discharge mode%s — blocked by monitoring mode",
                f" ({reason})" if reason else "",
            )
            return False

        # Keep the active flag set until the hardware restore is confirmed:
        # clearing it first made a failed release unretryable (the early-return
        # above short-circuits every later attempt) and left discharge capped.
        try:
            if (
                self.energy_coordinator
                and hasattr(self.energy_coordinator, "restore_no_discharge_mode")
            ):
                ok = await self.energy_coordinator.restore_no_discharge_mode()
            elif (
                self.energy_coordinator
                and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
            ):
                ok = await self.energy_coordinator.restore_work_mode_from_idle()
            elif (
                self._executor
                and hasattr(self._executor.battery_controller, "restore_normal")
            ):
                ok = await self._executor.battery_controller.restore_normal()
            else:
                ok = True
        except Exception as err:
            _LOGGER.warning(
                "Scheduled EV preserve: failed to release no-discharge mode (will retry): %s",
                err,
            )
            return False

        if ok is False:
            _LOGGER.warning(
                "Scheduled EV preserve: no-discharge release returned False (will retry)"
            )
            return False

        self._scheduled_ev_no_discharge_active = False
        _LOGGER.info(
            "Scheduled EV preserve: battery no-discharge mode released%s",
            f" ({reason})" if reason else "",
        )
        return True

    async def _set_idle_hold_mode(self, battery, preserve_charge: bool = False) -> bool:
        """Apply the existing optimiser hold semantics.

        ``preserve_charge`` means prefer no-discharge paths that still allow
        solar/grid charge. Backends without such a primitive fall back to their
        existing IDLE hold behavior.
        """
        soc, _ = await self._get_battery_state()
        soc_pct = int(soc * 100)
        configured_idle_floor = int(self._config.backup_reserve * 100)

        if self.battery_system == "goodwe" and not preserve_charge:
            if hasattr(battery, "set_self_consumption_mode"):
                await battery.set_self_consumption_mode()
            elif hasattr(battery, "restore_normal"):
                await battery.restore_normal()
            _LOGGER.info(
                "Optimizer: IDLE — GoodWe self-consumption without DOD hold "
                "(current_soc=%d%%, optimizer_floor=%d%%)",
                soc_pct,
                configured_idle_floor,
            )
            self._idle_hold_reserve = None
            return True

        if self._pre_idle_backup_reserve is None:
            if self._startup_backup_reserve is not None:
                self._pre_idle_backup_reserve = self._startup_backup_reserve
                _LOGGER.debug(
                    "Optimizer: Using startup backup reserve for IDLE restore: %d%%",
                    self._startup_backup_reserve,
                )
            else:
                saved = None
                if hasattr(battery, "read_backup_reserve"):
                    try:
                        reading = await battery.read_backup_reserve()
                        if reading.trust in TRUSTED_FOR_PERSIST:
                            saved = reading.percent
                    except Exception:
                        pass
                elif hasattr(battery, "get_backup_reserve"):
                    try:
                        saved = await battery.get_backup_reserve()
                    except Exception:
                        pass
                if (
                    saved is None
                    and self.energy_coordinator
                    and hasattr(self.energy_coordinator, "data")
                ):
                    coord_data = self.energy_coordinator.data or {}
                    saved = coord_data.get("backup_reserve") or coord_data.get("min_soc")
                    if saved is not None:
                        saved = int(saved)
                if saved is not None:
                    self._pre_idle_backup_reserve = saved
                    _LOGGER.debug(
                        "Optimizer: Saved pre-IDLE backup reserve (fallback): %d%%",
                        saved,
                    )
                else:
                    configured_reserve, reserve_source = (
                        self._configured_startup_backup_reserve()
                    )
                    if configured_reserve is None:
                        configured_reserve = configured_idle_floor
                        reserve_source = "optimizer floor"
                    self._pre_idle_backup_reserve = configured_reserve
                    _LOGGER.info(
                        "Optimizer: Pre-IDLE reserve read unavailable; using %s: %d%%",
                        reserve_source,
                        configured_reserve,
                    )

        non_tesla_hold_pct = max(soc_pct, configured_idle_floor)

        if (
            self.energy_coordinator
            and hasattr(self.energy_coordinator, "set_backup_mode")
        ):
            await self.energy_coordinator.set_backup_mode()
            if hasattr(battery, "set_backup_reserve") and self.battery_system != "sigenergy":
                self._idle_reserve_adjustment = True
                try:
                    await battery.set_backup_reserve(non_tesla_hold_pct)
                finally:
                    self._idle_reserve_adjustment = False
            _LOGGER.info(
                "Optimizer: IDLE — holding SOC at %d%% (hold mode)",
                non_tesla_hold_pct,
            )
            self._idle_hold_reserve = non_tesla_hold_pct
            return True

        if hasattr(battery, "set_backup_reserve"):
            if hasattr(battery, "set_self_consumption_mode"):
                reserve = min(max(soc_pct, 0), 80)
                if await battery.set_self_consumption_mode() is False:
                    return False
            elif hasattr(battery, "restore_normal"):
                reserve = non_tesla_hold_pct
                if await battery.restore_normal() is False:
                    return False
            else:
                reserve = non_tesla_hold_pct
            self._idle_reserve_adjustment = True
            try:
                reserve_result = await battery.set_backup_reserve(reserve)
            finally:
                self._idle_reserve_adjustment = False
            if reserve_result is False:
                _LOGGER.warning(
                    "Optimizer: IDLE backup reserve command returned False; "
                    "keeping the previous action marker so the next cycle retries"
                )
                return False
            _LOGGER.info(
                "Optimizer: IDLE — holding SOC at %d%% via self_consumption "
                "(backup reserve=%d%%)",
                soc_pct, reserve,
            )
            self._idle_hold_reserve = reserve
            return True

        if hasattr(battery, "set_self_consumption_mode"):
            await battery.set_self_consumption_mode()
            _LOGGER.info("Optimizer: IDLE — self-consumption (no set_backup_reserve)")
            self._idle_hold_reserve = None
            return True
        if hasattr(battery, "restore_normal"):
            await battery.restore_normal()
            self._idle_hold_reserve = None
            return True
        return False

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

    @property
    def away_mode(self) -> bool:
        """Return whether away mode is active (user is currently away)."""
        return self._load_estimator.away_mode if self._load_estimator else False

    def set_away_mode(self, enabled: bool) -> None:
        """Enable or disable away mode.

        Turning ON records departure timestamp (enables vacation-low LP bias).
        Turning OFF records return timestamp and starts the 7-day recovery window
        during which vacation data is excluded from the load history.
        Short toggles under 1 hour are treated as no-ops to avoid polluting history.
        """
        if not self._load_estimator:
            return

        from ..const import CONF_AWAY_ENABLED_AT, CONF_AWAY_DISABLED_AT

        now = dt_util.utcnow()

        if enabled:
            self._load_estimator.away_enabled_at = now
            self._load_estimator.away_disabled_at = None
            self._load_estimator.invalidate_cache()
            _LOGGER.info("Away mode ENABLED — departure recorded at %s", now.isoformat())
        else:
            enabled_at = self._load_estimator.away_enabled_at
            if enabled_at and (now - enabled_at) < timedelta(hours=1):
                # Short toggle — treat as no-op, clear both timestamps
                _LOGGER.info("Away mode toggle ignored (under 1 hour) — no recovery window set")
                self._load_estimator.away_enabled_at = None
                self._load_estimator.away_disabled_at = None
            else:
                self._load_estimator.away_disabled_at = now
                _LOGGER.info(
                    "Away mode DISABLED — return recorded at %s, recovery window active for 7 days",
                    now.isoformat(),
                )
            self._load_estimator.invalidate_cache()

        # Persist timestamps to config entry so they survive HA restarts
        if self._entry:
            new_options = dict(self._entry.options)
            en = self._load_estimator.away_enabled_at
            dis = self._load_estimator.away_disabled_at
            new_options[CONF_AWAY_ENABLED_AT] = en.isoformat() if en else None
            new_options[CONF_AWAY_DISABLED_AT] = dis.isoformat() if dis else None
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    @property
    def profit_max_mode(self) -> bool:
        """Return whether profit maximisation mode is active."""
        return self._config.profit_max_enabled

    @property
    def charge_by_time_enabled(self) -> bool:
        """Return whether charge-by-time prefill is active."""
        return self._config.charge_by_time_enabled

    @property
    def spread_export_enabled(self) -> bool:
        """Return whether export spreading is active."""
        return self._config.spread_export_enabled

    @property
    def spread_import_enabled(self) -> bool:
        """Return whether import spreading is active."""
        return self._config.spread_import_enabled

    @property
    def disable_idle_enabled(self) -> bool:
        """Return whether optimizer IDLE actions are disabled."""
        return self._should_disable_idle_schedule()

    @property
    def auto_apply_reserve_enabled(self) -> bool:
        """Return whether forecast reserve recommendations update the LP floor."""
        return bool(getattr(self, "_auto_apply_reserve_enabled", False))

    @property
    def manual_backup_reserve(self) -> float | None:
        """Return the saved manual optimizer reserve restore point."""
        return getattr(self, "_manual_backup_reserve", None)

    def _supports_target_export_power(self) -> bool:
        """Return True when the selected battery can honor a target export power."""
        try:
            from ..const import TARGET_EXPORT_POWER_BATTERY_SYSTEMS
            return self.battery_system in TARGET_EXPORT_POWER_BATTERY_SYSTEMS
        except Exception:
            return False

    def _supports_target_charge_power(self) -> bool:
        """Return True when the selected battery can honor a target charge power."""
        try:
            from ..const import TARGET_CHARGE_POWER_BATTERY_SYSTEMS
            return self.battery_system in TARGET_CHARGE_POWER_BATTERY_SYSTEMS
        except Exception:
            return False

    def set_spread_export_enabled(self, enabled: bool) -> bool:
        """Enable or disable spread-export mode."""
        # No-op when unchanged: a redundant settings push (e.g. the periodic
        # settings sync from the companion app) must not invalidate the
        # load-estimator cache, which forces an expensive temperature-sensitivity
        # refit over the full load history on the event loop.
        if self._config.spread_export_enabled == bool(enabled):
            return False
        self._config.spread_export_enabled = bool(enabled)
        load_estimator = getattr(self, "_load_estimator", None)
        if load_estimator:
            load_estimator.invalidate_cache()
        _LOGGER.info(
            "Spread Export Across Window %s",
            "ENABLED" if enabled else "DISABLED",
        )
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_spread_export",
                bool(enabled),
            )
        if self._entry:
            from ..const import CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED, DOMAIN
            new_options = dict(self._entry.options)
            new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = bool(enabled)
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        return True

    def set_spread_import_enabled(self, enabled: bool) -> bool:
        """Enable or disable spread-import mode."""
        # No-op when unchanged (see set_spread_export_enabled).
        if self._config.spread_import_enabled == bool(enabled):
            return False
        self._config.spread_import_enabled = bool(enabled)
        if self._load_estimator:
            self._load_estimator.invalidate_cache()
        _LOGGER.info(
            "Spread Import Across Window %s",
            "ENABLED" if enabled else "DISABLED",
        )
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_spread_import",
                bool(enabled),
            )
        if self._entry:
            from ..const import CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED, DOMAIN
            new_options = dict(self._entry.options)
            new_options[CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED] = bool(enabled)
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        return True

    def set_profit_max_mode(self, enabled: bool) -> bool:
        """Enable or disable profit maximisation mode."""
        # No-op when unchanged (see set_spread_export_enabled) — avoids a
        # redundant cache invalidation + load-estimator refit on every sync.
        if self._config.profit_max_enabled == bool(enabled):
            return False
        self._config.profit_max_enabled = enabled
        if self._optimizer:
            self._optimizer.terminal_weight = self._profit_max_terminal_weight()
        if self._load_estimator:
            self._load_estimator.invalidate_cache()
        _LOGGER.info("Profit Maximisation mode %s", "ENABLED" if enabled else "DISABLED")
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_profit_max_mode",
                enabled,
            )
        if self._entry:
            from ..const import CONF_PROFIT_MAX_ENABLED, DOMAIN
            new_options = dict(self._entry.options)
            new_options[CONF_PROFIT_MAX_ENABLED] = enabled
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        return True

    def set_charge_by_time_enabled(self, enabled: bool) -> bool:
        """Enable or disable charge-by-time prefill mode."""
        enabled = bool(enabled)
        if self._config.charge_by_time_enabled == enabled:
            return False
        self._config.charge_by_time_enabled = enabled
        load_estimator = getattr(self, "_load_estimator", None)
        if load_estimator:
            load_estimator.invalidate_cache()
        _LOGGER.info("Charge By Time %s", "ENABLED" if enabled else "DISABLED")
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_charge_by_time",
                enabled,
            )
        if self._entry:
            from ..const import CONF_CHARGE_BY_TIME_ENABLED, DOMAIN
            new_options = dict(self._entry.options)
            new_options[CONF_CHARGE_BY_TIME_ENABLED] = enabled
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        return True

    def set_disable_idle_enabled(self, enabled: bool) -> bool:
        """Enable or disable no-idle mode."""
        enabled = bool(enabled) and self._supports_disable_idle_mode()
        if self._config.disable_idle_enabled == enabled:
            return False
        self._config.disable_idle_enabled = enabled
        if self._load_estimator:
            self._load_estimator.invalidate_cache()
        _LOGGER.info(
            "No Idle mode %s",
            "ENABLED" if enabled else "DISABLED",
        )
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_disable_idle",
                enabled,
            )
        if self._entry:
            from ..const import CONF_OPTIMIZATION_DISABLE_IDLE, DOMAIN

            new_data = dict(self._entry.data)
            new_options = dict(self._entry.options)
            new_data[CONF_OPTIMIZATION_DISABLE_IDLE] = enabled
            new_options[CONF_OPTIMIZATION_DISABLE_IDLE] = enabled
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(
                self._entry,
                data=new_data,
                options=new_options,
            )
        return True

    async def set_auto_apply_reserve_enabled(
        self,
        enabled: bool,
        *,
        rerun: bool = True,
    ) -> bool:
        """Enable or disable forecast-driven optimizer reserve tracking."""
        enabled = bool(enabled)
        was_enabled = bool(getattr(self, "_auto_apply_reserve_enabled", False))
        current_manual = getattr(self, "_manual_backup_reserve", None)
        changed = enabled != was_enabled
        if enabled:
            if not was_enabled or current_manual is None:
                current_manual = self._config.backup_reserve
                changed = True
            self._manual_backup_reserve = current_manual
            self._auto_apply_reserve_enabled = True
            self._config.auto_apply_reserve_enabled = True
            self._config.manual_backup_reserve = current_manual
            self._persist_optimizer_reserve_settings(
                auto_apply=True,
                manual_reserve=current_manual,
            )
        else:
            restore_reserve = current_manual
            if restore_reserve is None:
                restore_reserve = self._config.backup_reserve
                self._manual_backup_reserve = restore_reserve
                changed = True
            self._auto_apply_reserve_enabled = False
            self._config.auto_apply_reserve_enabled = False
            if restore_reserve is not None and (
                changed
                or not math.isclose(
                    self._config.backup_reserve,
                    restore_reserve,
                    abs_tol=0.0001,
                )
            ):
                self.update_config(backup_reserve=restore_reserve)
                changed = True
            self._config.manual_backup_reserve = restore_reserve
            self._persist_optimizer_reserve_settings(
                auto_apply=False,
                manual_reserve=restore_reserve,
                backup_reserve=restore_reserve,
            )

        self._dispatch_auto_apply_reserve_state()
        _LOGGER.info(
            "Auto-Apply Optimizer Reserve %s%s",
            "ENABLED" if enabled else "DISABLED",
            (
                f" (manual restore {current_manual * 100:.0f}%)"
                if current_manual is not None
                else ""
            ),
        )
        if rerun and changed and getattr(self, "_enabled", False):
            await self._run_optimization()
        return changed

    async def _run_settings_reoptimization(self) -> None:
        """Run settings-triggered optimizer refreshes after the API response."""
        try:
            while self._settings_reoptimize_requested and getattr(
                self, "_enabled", False
            ):
                self._settings_reoptimize_requested = False
                await self._run_optimization(force=True)
        finally:
            self._settings_reoptimize_task = None

    def _schedule_settings_reoptimization(self) -> None:
        """Coalesce settings-triggered optimizer refreshes into one background task."""
        if not getattr(self, "_enabled", False):
            return
        self._settings_reoptimize_requested = True
        settings_task = getattr(self, "_settings_reoptimize_task", None)
        if settings_task and not settings_task.done():
            return
        self._settings_reoptimize_task = self.hass.async_create_background_task(
            self._run_settings_reoptimization(),
            "powersync_settings_reoptimize",
        )

    def _dispatch_auto_apply_reserve_state(self) -> None:
        """Notify HA switches after config-flow/API/mobile changes."""
        if not (getattr(self, "hass", None) and getattr(self, "entry_id", None)):
            return
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        from ..const import DOMAIN

        async_dispatcher_send(
            self.hass,
            f"{DOMAIN}_{self.entry_id}_auto_apply_reserve",
            bool(getattr(self, "_auto_apply_reserve_enabled", False)),
        )

    @staticmethod
    def _reserve_ratio(value: Any, default: float | None = None) -> float | None:
        """Normalize reserve values stored as either 0-1 decimals or 0-100 percents."""
        if value is None:
            return default
        try:
            reserve = float(value)
        except (TypeError, ValueError):
            return default
        if reserve > 1:
            reserve = reserve / 100.0
        return max(0.0, min(1.0, reserve))

    def _persist_optimizer_reserve_settings(
        self,
        *,
        auto_apply: bool | None = None,
        manual_reserve: float | None = None,
        backup_reserve: float | None = None,
    ) -> None:
        """Persist optimizer reserve settings without touching hardware reserve state."""
        if not getattr(self, "_entry", None):
            return
        from ..const import (
            CONF_OPTIMIZATION_AUTO_APPLY_RESERVE,
            CONF_OPTIMIZATION_BACKUP_RESERVE,
            CONF_OPTIMIZATION_MANUAL_RESERVE,
            DOMAIN,
        )

        new_data = dict(self._entry.data)
        new_options = dict(self._entry.options)
        if auto_apply is not None:
            new_data[CONF_OPTIMIZATION_AUTO_APPLY_RESERVE] = bool(auto_apply)
            new_options[CONF_OPTIMIZATION_AUTO_APPLY_RESERVE] = bool(auto_apply)
        if manual_reserve is not None:
            manual = self._reserve_ratio(manual_reserve, self._config.backup_reserve)
            new_data[CONF_OPTIMIZATION_MANUAL_RESERVE] = manual
            new_options[CONF_OPTIMIZATION_MANUAL_RESERVE] = manual
        if backup_reserve is not None:
            reserve = self._reserve_ratio(backup_reserve, self._config.backup_reserve)
            new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = reserve
            new_options[CONF_OPTIMIZATION_BACKUP_RESERVE] = reserve

        self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})[
            "_skip_reload"
        ] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            data=new_data,
            options=new_options,
        )

    def _recommended_auto_reserve_ratio(
        self,
        reserve_recommendation: dict[str, Any],
    ) -> float | None:
        """Return clamped forecast optimizer reserve target as a ratio."""
        candidate = reserve_recommendation.get("suggested_optimizer_reserve_percent")
        if candidate is None:
            return None
        try:
            suggested_percent = float(candidate)
        except (TypeError, ValueError):
            return None
        hardware_percent = (
            getattr(self, "_startup_backup_reserve", None)
            if getattr(self, "_startup_backup_reserve", None) is not None
            else 0
        )
        target_percent = max(float(hardware_percent), min(100.0, suggested_percent))
        return max(0.0, min(1.0, target_percent / 100.0))

    def _force_discharge_reserve_floor(self, action: Any | None = None) -> float:
        """Return the software floor used before force discharge/export commands."""
        floor = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
        action_timestamp = getattr(action, "timestamp", None)
        matched_per_slot = False
        if self.auto_apply_reserve_enabled and action_timestamp is not None:
            timestamps = getattr(self, "_active_export_reserve_floor_timestamps", None) or []
            floors = getattr(self, "_active_export_reserve_floor_slots", None) or []
            for idx, timestamp in enumerate(timestamps):
                if timestamp == action_timestamp and idx < len(floors):
                    floor = max(floor, floors[idx])
                    matched_per_slot = True
                    break
        if not matched_per_slot and self.auto_apply_reserve_enabled:
            recommendation = (
                getattr(getattr(self, "_last_optimizer_result", None), "reserve_recommendation", {})
                or {}
            )
            export_floor = self._reserve_ratio(
                recommendation.get("home_load_export_floor_percent"),
                None,
            )
            if export_floor is not None:
                bridge_export_start = recommendation.get(
                    "home_load_bridge_after_export_start"
                )
                if bridge_export_start:
                    try:
                        bridge_start = datetime.fromisoformat(
                            str(bridge_export_start)
                        )
                        now = dt_util.now()
                        if bridge_start.tzinfo is not None:
                            now = now.astimezone(bridge_start.tzinfo)
                        if bridge_start.date() != now.date():
                            export_floor = None
                    except (TypeError, ValueError):
                        pass
            if export_floor is not None:
                floor = max(floor, export_floor)
        return max(0.0, min(1.0, floor))

    def _auto_export_reserve_floor(
        self,
        reserve_recommendation: dict[str, Any],
    ) -> float | None:
        """Return the transient export-only floor from the reserve recommendation."""
        if not self.auto_apply_reserve_enabled:
            return None
        export_floor = self._reserve_ratio(
            reserve_recommendation.get("home_load_export_floor_percent"),
            None,
        )
        if export_floor is None:
            return None
        optimizer_floor = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
        if export_floor <= optimizer_floor + 0.0001:
            return None
        bridge_export_start = reserve_recommendation.get(
            "home_load_bridge_after_export_start"
        )
        if bridge_export_start:
            try:
                bridge_start = datetime.fromisoformat(str(bridge_export_start))
                now = dt_util.now()
                if bridge_start.tzinfo is not None:
                    now = now.astimezone(bridge_start.tzinfo)
                if bridge_start.date() != now.date():
                    return None
            except (TypeError, ValueError):
                pass
        return export_floor

    def _auto_export_reserve_floor_slots(
        self,
        reserve_recommendation: dict[str, Any],
        slot_count: int,
    ) -> list[float] | None:
        """Return future-scoped export reserve floors for the optimizer horizon."""
        if not self.auto_apply_reserve_enabled or slot_count <= 0:
            return None
        export_floor = self._reserve_ratio(
            reserve_recommendation.get("home_load_export_floor_percent"),
            None,
        )
        if export_floor is None:
            return None
        optimizer_floor = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
        if export_floor <= optimizer_floor + 0.0001:
            return None
        bridge_export_start = reserve_recommendation.get(
            "home_load_bridge_after_export_start"
        )
        if not bridge_export_start:
            return None
        try:
            bridge_start = datetime.fromisoformat(str(bridge_export_start))
            now = dt_util.now()
            if bridge_start.tzinfo is not None:
                now = now.astimezone(bridge_start.tzinfo)
            if bridge_start.date() == now.date():
                return None
            seconds_until_start = (bridge_start - now).total_seconds()
        except (TypeError, ValueError):
            return None
        if seconds_until_start <= 0:
            return None
        interval_seconds = max(1, int(self._config.interval_minutes or 5)) * 60
        start_slot = max(0, math.floor(seconds_until_start / interval_seconds))
        if start_slot >= slot_count:
            return None
        floors = [0.0] * slot_count
        for idx in range(start_slot, slot_count):
            floors[idx] = export_floor
        return floors

    def _hardware_reserve_ratio(self) -> float:
        """Return the configured hardware backup reserve as a ratio."""
        startup_reserve = getattr(self, "_startup_backup_reserve", None)
        if startup_reserve is not None:
            try:
                return max(0.0, min(1.0, float(startup_reserve) / 100.0))
            except (TypeError, ValueError):
                pass
        optimizer = getattr(self, "_optimizer", None)
        if getattr(optimizer, "hardware_reserve_known", False):
            try:
                return max(
                    0.0,
                    min(
                        1.0,
                        float(getattr(optimizer, "hardware_reserve", 0.0) or 0.0),
                    ),
                )
            except (TypeError, ValueError):
                pass
        return 0.0

    def _post_processed_export_reserve_floor_slots(
        self,
        schedule: OptimizationSchedule | None,
        solar_forecast: list[float] | None,
        load_forecast: list[float] | None,
    ) -> tuple[list[float] | None, dict[str, Any]]:
        """Build export-only reserve floors from the final candidate schedule."""
        actions = list(getattr(schedule, "actions", None) or [])
        if not actions:
            return None, {}

        capacity_kwh = max(
            0.0,
            float(getattr(self._config, "battery_capacity_wh", 0) or 0) / 1000.0,
        )
        if capacity_kwh <= 0:
            return None, {}

        interval_hours = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        ) / 60.0
        efficiency = max(
            0.001,
            float(getattr(getattr(self, "_optimizer", None), "efficiency", 0.95) or 0.95),
        )
        hardware_reserve = self._hardware_reserve_ratio()
        active_floor = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
        threshold_kw = 0.1
        floors = [0.0] * len(actions)
        best_floor = 0.0
        best_meta: dict[str, Any] = {}

        def _forecast_kw(values: list[float] | None, index: int) -> float:
            if not values or index >= len(values):
                return 0.0
            try:
                return max(0.0, float(values[index]))
            except (TypeError, ValueError):
                return 0.0

        def _charge_opportunity(index: int) -> tuple[bool, str | None]:
            action = actions[index]
            if float(getattr(action, "battery_charge_w", 0.0) or 0.0) > 100.0:
                return (
                    True,
                    "scheduled_grid_charge"
                    if getattr(action, "action", None) == "charge"
                    else "forecast_solar_surplus",
                )
            if _forecast_kw(solar_forecast, index) - _forecast_kw(load_forecast, index) > threshold_kw:
                return True, "forecast_solar_surplus"
            return False, None

        idx = 0
        while idx < len(actions):
            action = actions[idx]
            if getattr(action, "action", None) not in EXPORT_ACTIONS:
                idx += 1
                continue
            discharge_w = float(
                getattr(action, "battery_discharge_w", None)
                or getattr(action, "power_w", 0.0)
                or 0.0
            )
            if discharge_w <= 100.0:
                idx += 1
                continue

            run_start = idx
            while idx < len(actions):
                run_action = actions[idx]
                if getattr(run_action, "action", None) not in EXPORT_ACTIONS:
                    break
                run_discharge_w = float(
                    getattr(run_action, "battery_discharge_w", None)
                    or getattr(run_action, "power_w", 0.0)
                    or 0.0
                )
                if run_discharge_w <= 100.0:
                    break
                idx += 1
            run_end = idx

            bridge_kwh = 0.0
            next_charge_idx: int | None = None
            next_charge_reason: str | None = None
            for scan_idx in range(run_end, len(actions)):
                is_charge, reason = _charge_opportunity(scan_idx)
                if is_charge:
                    next_charge_idx = scan_idx
                    next_charge_reason = reason
                    break
                bridge_kwh += max(
                    0.0,
                    _forecast_kw(load_forecast, scan_idx)
                    - _forecast_kw(solar_forecast, scan_idx),
                ) * interval_hours

            bridge_soc = bridge_kwh / max(capacity_kwh * efficiency, 0.001)
            floor = max(hardware_reserve, min(1.0, hardware_reserve + bridge_soc))
            if floor <= active_floor + 0.0001:
                continue

            for floor_idx in range(run_start, run_end):
                floors[floor_idx] = floor
            if floor > best_floor:
                best_floor = floor
                protects_until_idx = (
                    next_charge_idx if next_charge_idx is not None else len(actions) - 1
                )
                bridge_start_idx = min(run_end, len(actions) - 1)
                best_meta = {
                    "home_load_export_floor_percent": max(
                        0,
                        min(100, int(round(floor * 100))),
                    ),
                    "home_load_bridge_kwh": round(bridge_kwh, 3),
                    "home_load_bridge_start": actions[
                        bridge_start_idx
                    ].timestamp.isoformat(),
                    "home_load_bridge_until": actions[
                        protects_until_idx
                    ].timestamp.isoformat(),
                    "home_load_bridge_next_charge_reason": (
                        next_charge_reason or "no_charge_in_horizon"
                    ),
                    "home_load_bridge_after_export_start": actions[
                        run_start
                    ].timestamp.isoformat(),
                }

        if best_floor <= 0.0:
            return None, {}
        return floors, best_meta

    def _set_active_export_reserve_floor_slots(
        self,
        floors: list[float] | None,
        schedule: OptimizationSchedule | None,
    ) -> None:
        """Store transient export floors for runtime export guards."""
        if not floors:
            self._active_export_reserve_floor_slots = None
            self._active_export_reserve_floor_timestamps = None
            return
        actions = list(getattr(schedule, "actions", None) or [])
        normalized = [
            max(0.0, min(1.0, float(value or 0.0)))
            for value in floors[: len(actions)]
        ]
        self._active_export_reserve_floor_slots = normalized
        self._active_export_reserve_floor_timestamps = [
            getattr(action, "timestamp", None)
            for action in actions[: len(normalized)]
        ]

    def _force_discharge_reaches_reserve(
        self,
        action: Any,
        soc_now: float | None,
        reserve: float,
    ) -> tuple[bool, float | None]:
        """Return whether a forced discharge/export command would hit reserve."""
        projected_soc = self._reserve_ratio(getattr(action, "soc", None))
        if soc_now is not None and soc_now <= reserve + 0.0001:
            return True, projected_soc
        if projected_soc is not None and projected_soc <= reserve + 0.0001:
            return True, projected_soc
        return False, projected_soc

    def _apply_auto_reserve_recommendation(
        self,
        result: OptimizerResult,
    ) -> bool:
        """Apply one forecast optimizer reserve update after a solve."""
        if not bool(getattr(self, "_auto_apply_reserve_enabled", False)):
            return False
        # Never act on a relaxed/greedy fallback solve. Those run with an
        # artificially lowered reserve floor (5%), so their reserve
        # recommendation is not a real forecast — applying it would ratchet the
        # optimiser reserve down to the hardware floor and leave it there.
        if not bool(getattr(result, "feasible", True)):
            return False
        recommendation = getattr(result, "reserve_recommendation", {}) or {}
        target_ratio = self._recommended_auto_reserve_ratio(recommendation)
        if target_ratio is None:
            return False
        current_ratio = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
        recommendation["auto_apply_enabled"] = True
        manual_reserve = getattr(self, "_manual_backup_reserve", None)
        if manual_reserve is not None:
            manual_reserve = self._reserve_ratio(manual_reserve, None)
        if manual_reserve is not None:
            recommendation["manual_optimizer_reserve_percent"] = int(
                round(manual_reserve * 100)
            )
        recommendation["applied_optimizer_reserve_percent"] = int(
            round(current_ratio * 100)
        )
        if math.isclose(target_ratio, current_ratio, abs_tol=0.0001):
            return False

        # Apply the forecast floor to the running optimiser ONLY. This value is
        # recomputed every solve, so it must not be written to the config entry:
        # persisting it each cycle fired HA's config-entry-updated event every
        # ~5 minutes, refreshing the dashboard (and risking reload churn) for a
        # purely transient value. The live reserve is still surfaced to sensors
        # and the mobile app via get_api_data (self._config.backup_reserve), and
        # it is recomputed from the manual baseline within one solve of a restart.
        self.update_config(backup_reserve=target_ratio)
        recommendation["applied_optimizer_reserve_percent"] = int(
            round(target_ratio * 100)
        )
        _LOGGER.info(
            "Auto-Apply Optimizer Reserve: applied forecast floor %.0f%% "
            "(was %.0f%%)",
            target_ratio * 100,
            current_ratio * 100,
        )
        return True

    @staticmethod
    def _reserve_percent(value: Any) -> int | None:
        """Normalize reserve values stored as either 0-1 decimals or 0-100 percents."""
        if value is None:
            return None
        try:
            reserve = float(value)
        except (TypeError, ValueError):
            return None
        if reserve <= 1:
            reserve *= 100
        return max(0, min(100, int(reserve)))

    @staticmethod
    def _soc_ratio(value: Any, default: float = 1.0) -> float:
        """Normalize SOC values stored as either 0-1 decimals or 0-100 percents."""
        try:
            soc = float(value)
        except (TypeError, ValueError):
            soc = default
        if soc > 1:
            soc = soc / 100.0
        return max(0.0, min(1.0, soc))

    @staticmethod
    def _kw_to_w(value: Any) -> int | None:
        """Normalize a kW-like value to watts."""
        if value is None:
            return None
        try:
            kw = float(value)
        except (TypeError, ValueError):
            return None
        if kw < 0:
            return None
        return int(round(kw * 1000))

    def _get_custom_entity_id(self, key: str) -> str:
        """Return one configured custom telemetry entity ID."""
        if not self._entry:
            return ""

        return str(
            self._entry.options.get(key, self._entry.data.get(key, ""))
            or ""
        ).strip()

    @staticmethod
    def _power_to_kw(value: float | None, unit: str = "") -> float | None:
        """Normalize a power value to kW using unit metadata or a W/kW heuristic."""
        if value is None:
            return None
        unit = unit.lower()
        if unit in ("w", "watt", "watts"):
            return value / 1000.0
        if unit in ("kw", "kilowatt", "kilowatts"):
            return value
        return value / 1000.0 if abs(value) > 100 else value

    def _read_numeric_state(self, entity_id: str) -> tuple[float | None, str]:
        """Read a numeric HA state and return its value plus unit."""
        if not entity_id:
            return None, ""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable", "None", None):
            if self._last_custom_energy_warning != entity_id:
                _LOGGER.warning(
                    "Custom battery telemetry entity %s is unavailable",
                    entity_id,
                )
                self._last_custom_energy_warning = entity_id
            return None, ""
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            if self._last_custom_energy_warning != entity_id:
                _LOGGER.warning(
                    "Custom battery telemetry entity %s is not numeric",
                    entity_id,
                )
                self._last_custom_energy_warning = entity_id
            return None, ""
        return value, str((state.attributes or {}).get("unit_of_measurement") or "")

    def _read_custom_energy_data(self) -> dict[str, Any] | None:
        """Read custom battery/site telemetry from user-selected entities."""
        if getattr(self, "battery_system", "") != CUSTOM_BATTERY_SYSTEM:
            return None

        entity_keys = {
            "battery_level": CUSTOM_BATTERY_LEVEL_ENTITY,
            "battery_power": CUSTOM_BATTERY_POWER_ENTITY,
            "grid_power": CUSTOM_GRID_POWER_ENTITY,
            "solar_power": CUSTOM_SOLAR_POWER_ENTITY,
            "load_power": CUSTOM_LOAD_POWER_ENTITY,
        }
        source_entities = {
            name: self._get_custom_entity_id(key)
            for name, key in entity_keys.items()
        }
        if not any(source_entities.values()):
            return None

        data: dict[str, Any] = {"source_entities": source_entities}
        battery_level, _battery_level_unit = self._read_numeric_state(
            source_entities["battery_level"]
        )
        if battery_level is not None:
            data["battery_level"] = max(0.0, min(100.0, battery_level))

        for target in ("battery_power", "grid_power", "solar_power", "load_power"):
            raw, unit = self._read_numeric_state(source_entities[target])
            kw = self._power_to_kw(raw, unit)
            if kw is not None:
                data[target] = kw

        if len(data) == 1:
            return None

        self._last_custom_energy_warning = None
        return data

    def _get_energy_data(self) -> dict[str, Any] | None:
        """Return custom aggregate telemetry when configured, else coordinator data."""
        custom_data = self._read_custom_energy_data()
        if custom_data:
            return custom_data
        data = getattr(self.energy_coordinator, "data", None)
        return data if isinstance(data, dict) else None

    def _resolve_max_grid_export_w(self) -> int | None:
        """Return the configured or reported grid export cap for optimizer planning."""
        if self._entry:
            from ..const import (
                CONF_ALPHAESS_EXPORT_LIMIT_KW,
                CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
                CONF_SIGENERGY_EXPORT_LIMIT_KW,
            )

            if (
                CONF_OPTIMIZATION_MAX_GRID_EXPORT_W in self._entry.options
                or CONF_OPTIMIZATION_MAX_GRID_EXPORT_W in self._entry.data
            ):
                value = self._entry.options.get(
                    CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
                    self._entry.data.get(CONF_OPTIMIZATION_MAX_GRID_EXPORT_W),
                )
                return self._normalize_optional_export_power_w(value)

            for key in (CONF_SIGENERGY_EXPORT_LIMIT_KW, CONF_ALPHAESS_EXPORT_LIMIT_KW):
                value = self._entry.options.get(key, self._entry.data.get(key))
                watts = self._kw_to_w(value)
                if watts is not None:
                    return int(round(watts))

        data = self._get_energy_data()
        if isinstance(data, dict):
            export_limit = data.get("export_limit_kw")
            if data.get("is_curtailed") and self._kw_to_w(export_limit) == 0:
                return None
            if export_limit != "unlimited":
                watts = self._kw_to_w(export_limit)
                return int(round(watts)) if watts is not None else None

        return None

    def _sync_grid_export_cap_to_optimizer(self) -> None:
        """Keep the LP grid export cap aligned with current site settings."""
        self._config.max_grid_export_w = self._resolve_max_grid_export_w()
        if self._optimizer:
            self._optimizer.max_grid_export_w = self._config.max_grid_export_w

    def _resolve_physical_max_discharge_w(self) -> int | None:
        """Return the battery/inverter physical discharge limit when available."""
        data = self._get_energy_data()
        if not isinstance(data, dict):
            return None

        for key in (
            "battery_max_discharge_power_w",
            "rated_power_w",
            "max_discharge_power_w",
        ):
            try:
                value = int(round(float(data.get(key))))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value

        for key in (
            "battery_max_discharge_power",
            "discharge_rate_limit_kw",
            "max_discharge_power_kw",
        ):
            try:
                value = float(data.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return int(round(value * 1000))

        return None

    def _sync_optimizer_discharge_limits(self) -> None:
        """Sync physical discharge and target-export caps into the LP model."""
        if not self._optimizer:
            return

        physical_discharge_w = self._config.max_discharge_w
        export_command_cap_w: int | None = None

        if self._supports_target_export_power():
            export_command_cap_w = (
                self._config.max_grid_export_w
                if self._config.max_grid_export_w is not None
                else self._config.max_discharge_w
            )
            detected_physical_w = self._resolve_physical_max_discharge_w()
            if detected_physical_w and detected_physical_w > physical_discharge_w:
                physical_discharge_w = detected_physical_w

        self._optimizer.update_config(
            max_discharge_w=physical_discharge_w,
            max_battery_export_w=export_command_cap_w,
        )

    def _configured_startup_backup_reserve(self) -> tuple[int | None, str]:
        """Return the persisted user reserve target used after temporary IDLE holds."""
        if not self._entry:
            return self._reserve_percent(self._config.backup_reserve), "optimizer floor"

        from ..const import CONF_HARDWARE_BACKUP_RESERVE, CONF_OPTIMIZATION_BACKUP_RESERVE

        hw_reserve = self._reserve_percent(
            self._entry.data.get(
                CONF_HARDWARE_BACKUP_RESERVE,
                self._entry.options.get(CONF_HARDWARE_BACKUP_RESERVE),
            )
        )
        if hw_reserve is not None:
            return hw_reserve, "hardware backup reserve config"

        persisted_user_reserve = self._reserve_percent(
            self._entry.options.get("_user_backup_reserve")
        )
        if persisted_user_reserve is not None and (
            persisted_user_reserve > 0 or self.battery_system != "tesla"
        ):
            return persisted_user_reserve, "persisted user backup reserve"

        optimizer_reserve = self._reserve_percent(
            self._entry.options.get(
                CONF_OPTIMIZATION_BACKUP_RESERVE,
                self._entry.data.get(CONF_OPTIMIZATION_BACKUP_RESERVE),
            )
        )
        if optimizer_reserve is not None:
            return optimizer_reserve, "optimizer floor config"

        return self._reserve_percent(self._config.backup_reserve), "optimizer floor"

    async def _resolve_startup_backup_reserve(
        self,
        battery: Any,
        startup_reserve: int | None,
        reserve_source: str,
    ) -> tuple[int | None, str]:
        """Self-heal stale legacy Tesla user reserves using the lower live reserve."""
        if (
            startup_reserve is None
            or reserve_source != "persisted user backup reserve"
            or self.battery_system != "tesla"
            or not (
                hasattr(battery, "read_backup_reserve")
                or hasattr(battery, "get_backup_reserve")
            )
        ):
            return startup_reserve, reserve_source

        try:
            if hasattr(battery, "read_backup_reserve"):
                reading = await battery.read_backup_reserve()
                if reading.trust not in TRUSTED_FOR_PERSIST:
                    return startup_reserve, reserve_source
                live_reserve = self._reserve_percent(reading.percent)
            else:
                live_reserve = self._reserve_percent(await battery.get_backup_reserve())
        except Exception as exc:
            _LOGGER.debug("Could not verify live Tesla backup reserve: %s", exc)
            return startup_reserve, reserve_source

        if live_reserve is None or live_reserve >= startup_reserve:
            return startup_reserve, reserve_source

        if live_reserve == 0 and startup_reserve > 0:
            _LOGGER.info(
                "Optimizer startup: ignoring live Tesla backup reserve 0%% while "
                "persisted user backup reserve is %d%%",
                startup_reserve,
            )
            return startup_reserve, reserve_source

        _LOGGER.info(
            "Optimizer startup: replacing stale persisted user backup reserve "
            "%d%% with live Tesla reserve %d%%",
            startup_reserve,
            live_reserve,
        )
        if self._entry:
            try:
                from ..const import DOMAIN as _DOMAIN

                new_options = {
                    **self._entry.options,
                    "_user_backup_reserve": live_reserve,
                }
                if new_options != dict(self._entry.options):
                    entry_data = self.hass.data.get(_DOMAIN, {}).get(self.entry_id, {})
                    entry_data["_skip_reload"] = True
                    self.hass.config_entries.async_update_entry(
                        self._entry,
                        options=new_options,
                    )
            except Exception as exc:
                _LOGGER.debug("Could not update persisted backup reserve: %s", exc)

        return live_reserve, "live Tesla backup reserve"

    def _provider_key(self) -> str:
        """Return the configured electricity provider key."""
        if not self._entry:
            return ""
        from ..const import CONF_ELECTRICITY_PROVIDER

        return self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )

    def _zerohero_config(self) -> ZeroHeroConfig | None:
        """Return resolved GloBird ZeroHero settings for this entry."""
        if self._provider_key() != "globird":
            return None
        return zerohero_config_from_entry(self._entry)

    def _price_timestamps(self, n: int) -> list[datetime]:
        """Return local timestamps aligned with the current optimizer interval."""
        if self._last_price_timestamps and len(self._last_price_timestamps) >= n:
            return self._last_price_timestamps[:n]

        raw_now = dt_util.now()
        interval = self._config.interval_minutes
        start = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        return [start + timedelta(minutes=idx * interval) for idx in range(n)]

    def _zerohero_window_slots(self, n: int) -> list[bool]:
        """Return optimizer slots inside the configured ZeroHero window."""
        config = self._zerohero_config()
        if config is None or n <= 0:
            return [False] * max(0, n)
        return [
            zerohero_is_in_window(ts, config)
            for ts in self._price_timestamps(n)
        ]

    def _zerocharge_window_slots(self, n: int) -> list[bool]:
        """Return optimizer slots inside the configured ZeroCharge window."""
        config = self._zerohero_config()
        if config is None or n <= 0 or not config.zerocharge_enabled:
            return [False] * max(0, n)
        return [
            zerocharge_is_in_window(ts, config)
            for ts in self._price_timestamps(n)
        ]

    def _zerohero_credit_status(self, now: datetime | None = None) -> str:
        """Return current ZeroHero import-threshold status."""
        config = self._zerohero_config()
        if config is None:
            return "disabled"
        return zerohero_credit_status(
            config,
            now or dt_util.now(),
            self._actual_zerohero_import_kwh_today,
            self._actual_zerohero_credit_value_today > 0,
        )

    def _zerohero_credit_lost(self) -> bool:
        """Return True once the ZeroHero import threshold has been exceeded."""
        return self._zerohero_credit_status() == "lost"

    def _apply_zerohero_optimizer_inputs(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Prepare capped ZeroHero bonus inputs for the LP optimizer."""
        n = min(len(import_prices), len(export_prices))
        self._last_zerohero_bonus_prices = [0.0] * n
        self._last_zerohero_bonus_cap_kwh = None
        self._last_zerocharge_bonus_prices = [0.0] * n
        self._last_zerocharge_bonus_cap_kwh = None

        config = self._zerohero_config()
        if config is None or n <= 0:
            return

        timestamps = self._price_timestamps(n)
        if config.zerocharge_enabled:
            remaining_import_cap = max(
                0.0,
                config.zerocharge_import_cap_kwh
                - self._actual_zerocharge_import_kwh_today,
            )
            for idx, ts in enumerate(timestamps):
                if zerocharge_is_in_window(ts, config):
                    self._last_zerocharge_bonus_prices[idx] = max(
                        0.0,
                        import_prices[idx] if idx < len(import_prices) else 0.0,
                    )
            self._last_zerocharge_bonus_cap_kwh = remaining_import_cap
            if remaining_import_cap > 0 and any(self._last_zerocharge_bonus_prices):
                _LOGGER.info(
                    "ZeroCharge optimizer: %.2fkWh free-import cap remaining, %s-%s",
                    remaining_import_cap,
                    config.zerocharge_start,
                    config.zerocharge_end,
                )

        if self._zerohero_credit_lost():
            _LOGGER.info(
                "ZeroHero no-import credit lost for today: import %.3fkWh exceeded allowance %.3fkWh",
                self._actual_zerohero_import_kwh_today,
                config.import_allowance_kwh,
            )

        remaining_cap = max(
            0.0,
            config.export_cap_kwh - self._actual_zerohero_bonus_export_kwh_today,
        )
        for idx, ts in enumerate(timestamps):
            if not zerohero_is_in_window(ts, config):
                continue
            base_fit = max(0.0, export_prices[idx] if idx < len(export_prices) else 0.0)
            self._last_zerohero_bonus_prices[idx] = max(
                0.0,
                config.super_export_rate - base_fit,
            )
            # Keep planned grid import out of the no-import window without
            # making the LP infeasible when household load must still be served.
            import_prices[idx] += 5.0

        self._last_zerohero_bonus_cap_kwh = remaining_cap
        if remaining_cap > 0 and any(self._last_zerohero_bonus_prices):
            _LOGGER.info(
                "ZeroHero optimizer: %.2fkWh bonus cap remaining, %.1fc/kWh Super Export target",
                remaining_cap,
                config.super_export_rate * 100,
            )

    def _zerohero_cost_breakdown(self) -> dict[str, Any]:
        """Return API-visible ZeroHero daily settlement status."""
        config = self._zerohero_config()
        if config is None:
            return {"status": "disabled", "credit_status": "disabled"}

        status = self._zerohero_credit_status()
        remaining_bonus = max(
            0.0,
            config.export_cap_kwh - self._actual_zerohero_bonus_export_kwh_today,
        )
        remaining_import = max(
            0.0,
            config.import_allowance_kwh - self._actual_zerohero_import_kwh_today,
        )
        remaining_zerocharge = (
            max(
                0.0,
                config.zerocharge_import_cap_kwh
                - self._actual_zerocharge_import_kwh_today,
            )
            if config.zerocharge_enabled
            else 0.0
        )
        return {
            "status": "enabled",
            "plan": config.plan,
            "window_start": config.start,
            "window_end": config.end,
            "super_export_rate": round(config.super_export_rate, 4),
            "bonus_export_cap_kwh": round(config.export_cap_kwh, 4),
            "zerocharge_enabled": config.zerocharge_enabled,
            "zerocharge_window_start": config.zerocharge_start,
            "zerocharge_window_end": config.zerocharge_end,
            "zerocharge_import_cap_kwh": round(config.zerocharge_import_cap_kwh, 4),
            "zerocharge_import_kwh_used": round(
                self._actual_zerocharge_import_kwh_today,
                4,
            ),
            "zerocharge_import_kwh_remaining": round(remaining_zerocharge, 4),
            "zerocharge_credit_value": round(
                self._actual_zerocharge_credit_value_today,
                4,
            ),
            "bonus_export_kwh_used": round(
                self._actual_zerohero_bonus_export_kwh_today,
                4,
            ),
            "bonus_export_kwh_remaining": round(remaining_bonus, 4),
            "import_window_kwh": round(
                self._actual_zerohero_import_kwh_today,
                4,
            ),
            "export_window_kwh": round(
                self._actual_zerohero_export_kwh_today,
                4,
            ),
            "import_allowance_kwh_remaining": round(remaining_import, 4),
            "credit_status": status,
            "base_export_earnings": round(
                self._actual_zerohero_base_export_earnings_today,
                4,
            ),
            "bonus_export_earnings": round(
                self._actual_zerohero_bonus_export_earnings_today,
                4,
            ),
            "credit_value": round(self._actual_zerohero_credit_value_today, 4),
        }

    def _profit_max_terminal_weight(self) -> float:
        """Return the terminal SOC weight for the current profit mode."""
        if self._config.profit_max_enabled:
            return 0.3
        return 1.0

    def _summarise_load_forecast(self) -> dict | None:
        """Slice the cached load forecast into today-remaining and tomorrow kWh totals."""
        if not self._last_load_forecast:
            return None

        now = dt_util.now()
        dt_h = self._config.interval_minutes / 60
        interval_minutes = self._config.interval_minutes

        # Build per-slot timestamps starting from the most recent optimizer run
        # The forecast was generated at _last_update_time (or now if not set)
        forecast_start = self._last_update_time or now
        # Align to interval boundary
        elapsed_intervals = int(
            (now - forecast_start).total_seconds() / 60 / interval_minutes
        )

        today_remaining_kw = []
        tomorrow_kw = []
        slot_time = forecast_start + elapsed_intervals * timedelta(minutes=interval_minutes)
        local_midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        local_midnight_tomorrow = local_midnight_today + timedelta(days=1)

        hourly_remaining: list[dict] = []
        hourly_tomorrow: list[dict] = []
        current_hour_vals: list[float] = []
        current_hour_ts: datetime | None = None

        def _flush_hour(vals: list[float], ts: datetime | None, target: list) -> None:
            if vals and ts is not None:
                # vals are in kW; average kW * 1h = kWh for a 1-hour bucket
                avg_kw = sum(vals) / len(vals)
                target.append({"period_start": ts.isoformat(), "load_kwh": round(avg_kw, 3)})

        for i, load_kw in enumerate(self._last_load_forecast[elapsed_intervals:], start=elapsed_intervals):
            if i >= len(self._last_load_forecast):
                break
            load_kw = self._last_load_forecast[i]
            local_slot = dt_util.as_local(slot_time)

            slot_hour_ts = local_slot.replace(minute=0, second=0, microsecond=0)
            if current_hour_ts is None:
                current_hour_ts = slot_hour_ts
            if slot_hour_ts != current_hour_ts:
                if local_midnight_today > now and slot_time <= local_midnight_today:
                    _flush_hour(current_hour_vals, current_hour_ts, hourly_remaining)
                else:
                    _flush_hour(current_hour_vals, current_hour_ts, hourly_tomorrow)
                current_hour_vals = []
                current_hour_ts = slot_hour_ts

            current_hour_vals.append(load_kw)
            if slot_time <= local_midnight_today:
                today_remaining_kw.append(load_kw)
            elif slot_time <= local_midnight_tomorrow:
                tomorrow_kw.append(load_kw)
            else:
                break

            slot_time += timedelta(minutes=interval_minutes)

        # _last_load_forecast is in kW; multiply by interval hours to get kWh
        today_remaining_kwh = sum(today_remaining_kw) * dt_h if today_remaining_kw else 0
        tomorrow_kwh = sum(tomorrow_kw) * dt_h if tomorrow_kw else 0

        return {
            "today_remaining_kwh": round(today_remaining_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
            "peak_kw": round(max(self._last_load_forecast) if self._last_load_forecast else 0, 2),
            "hourly_today_remaining": hourly_remaining,
            "hourly_tomorrow": hourly_tomorrow,
            "temperature_adjusted": (
                self._load_estimator._temp_alpha is not None
                if self._load_estimator else False
            ),
            "away_mode": self.away_mode,
            "away_in_recovery": self._load_estimator._in_recovery if self._load_estimator else False,
            "away_enabled_at": (
                self._load_estimator.away_enabled_at.isoformat()
                if self._load_estimator and self._load_estimator.away_enabled_at else None
            ),
            "away_disabled_at": (
                self._load_estimator.away_disabled_at.isoformat()
                if self._load_estimator and self._load_estimator.away_disabled_at else None
            ),
            "away_recovery_remaining_hours": (
                round(
                    (timedelta(days=7) - (dt_util.utcnow() - self._load_estimator.away_disabled_at))
                    .total_seconds() / 3600, 1
                )
                if self._load_estimator and self._load_estimator._in_recovery else None
            ),
            "profit_max_mode": self.profit_max_mode,
            "charge_by_time_enabled": self.charge_by_time_enabled,
        }

    async def async_setup(self) -> bool:
        """Set up the optimization coordinator with built-in LP optimizer."""
        _LOGGER.info("Setting up optimization coordinator (built-in LP)")

        # Auto-detect battery specs from Tesla site_info if available
        await self._auto_detect_battery_specs()
        self._config.max_grid_export_w = self._resolve_max_grid_export_w()

        # Initialize built-in optimizer
        # Hardware reserve: captured at startup from the battery's actual setting.
        # Starts unknown when not yet captured and is updated on first poll.
        hw_reserve_pct = (
            self._startup_backup_reserve / 100
            if self._startup_backup_reserve is not None
            else None
        )
        self._optimizer = BatteryOptimizer(
            capacity_wh=self._config.battery_capacity_wh,
            max_charge_w=self._config.max_charge_w,
            max_discharge_w=self._config.max_discharge_w,
            max_grid_import_w=self._config.max_grid_import_w,
            max_grid_export_w=self._config.max_grid_export_w,
            efficiency=0.92,
            backup_reserve=self._config.backup_reserve,
            hardware_reserve=hw_reserve_pct,
            grid_charge_soc_cap=self._config.grid_charge_soc_cap,
            interval_minutes=self._config.interval_minutes,
            horizon_hours=self._config.horizon_hours,
        )

        # Initialize load estimator
        load_entity = self._get_load_entity_id()
        from ..const import CONF_WEATHER_ENTITY
        weather_entity = None
        if self._entry:
            weather_entity = self._entry.options.get(
                CONF_WEATHER_ENTITY,
                self._entry.data.get(CONF_WEATHER_ENTITY),
            ) or None
        self._load_estimator = LoadEstimator(
            self.hass,
            load_entity_id=load_entity,
            interval_minutes=self._config.interval_minutes,
            weather_entity_id=weather_entity,
        )

        # Restore away mode timestamps from config entry (persisted across HA restarts)
        if self._entry:
            from ..const import CONF_AWAY_ENABLED_AT, CONF_AWAY_DISABLED_AT
            raw_en = self._entry.options.get(CONF_AWAY_ENABLED_AT) or self._entry.data.get(CONF_AWAY_ENABLED_AT)
            raw_dis = self._entry.options.get(CONF_AWAY_DISABLED_AT) or self._entry.data.get(CONF_AWAY_DISABLED_AT)
            try:
                self._load_estimator.away_enabled_at = (
                    datetime.fromisoformat(raw_en) if raw_en else None
                )
                self._load_estimator.away_disabled_at = (
                    datetime.fromisoformat(raw_dis) if raw_dis else None
                )
                if raw_en or raw_dis:
                    _LOGGER.info(
                        "Restored away mode state: enabled_at=%s, disabled_at=%s",
                        raw_en, raw_dis,
                    )
            except (ValueError, TypeError) as exc:
                _LOGGER.warning("Could not restore away mode timestamps: %s", exc)

        if self._entry:
            from ..const import (
                CONF_OPTIMIZATION_ALLOW_GRID_CHARGE,
                CONF_OPTIMIZATION_DISABLE_IDLE,
                CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED,
                CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED,
                CONF_PROFIT_MAX_ENABLED,
                CONF_CHARGE_BY_TIME_ENABLED,
                CONF_CHARGE_BY_TIME_TARGET_TIME,
                CONF_CHARGE_BY_TIME_TARGET_SOC,
                CONF_PROFIT_MAX_TARGET_TIME,
                CONF_PROFIT_MAX_TARGET_SOC,
                DEFAULT_CHARGE_BY_TIME_TARGET_TIME,
                DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
            )
            allow_grid_charge = self._entry.options.get(
                CONF_OPTIMIZATION_ALLOW_GRID_CHARGE,
                self._entry.data.get(CONF_OPTIMIZATION_ALLOW_GRID_CHARGE, True),
            )
            self._config.allow_grid_charge = bool(allow_grid_charge)
            if not self._config.allow_grid_charge:
                _LOGGER.info("Smart Optimization grid charging: DISABLED")
            self._config.spread_export_enabled = bool(
                self._entry.options.get(
                    CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED,
                    self._entry.data.get(CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED, False),
                )
            )
            if self._config.spread_export_enabled:
                _LOGGER.info("Spread Export Across Window: ENABLED")
            self._config.spread_import_enabled = bool(
                self._entry.options.get(
                    CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED,
                    self._entry.data.get(CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED, False),
                )
            )
            if self._config.spread_import_enabled:
                _LOGGER.info("Spread Import Across Window: ENABLED")
            raw_disable_idle = bool(
                self._entry.options.get(
                    CONF_OPTIMIZATION_DISABLE_IDLE,
                    self._entry.data.get(CONF_OPTIMIZATION_DISABLE_IDLE, False),
                )
            )
            self._config.disable_idle_enabled = (
                raw_disable_idle and self._supports_disable_idle_mode()
            )
            if self._should_disable_idle_schedule():
                _LOGGER.info("No Idle mode: ENABLED")

            profit_max = self._entry.options.get(
                CONF_PROFIT_MAX_ENABLED,
                self._entry.data.get(CONF_PROFIT_MAX_ENABLED, False),
            )
            self._config.profit_max_enabled = bool(profit_max)
            charge_by_time = self._entry.options.get(
                CONF_CHARGE_BY_TIME_ENABLED,
                self._entry.data.get(
                    CONF_CHARGE_BY_TIME_ENABLED,
                    bool(profit_max),
                ),
            )
            self._config.charge_by_time_enabled = bool(charge_by_time)
            self._config.charge_by_time_target_time = str(
                self._entry.options.get(
                    CONF_CHARGE_BY_TIME_TARGET_TIME,
                    self._entry.data.get(
                        CONF_CHARGE_BY_TIME_TARGET_TIME,
                        self._entry.options.get(
                            CONF_PROFIT_MAX_TARGET_TIME,
                            self._entry.data.get(
                                CONF_PROFIT_MAX_TARGET_TIME,
                                DEFAULT_CHARGE_BY_TIME_TARGET_TIME,
                            ),
                        ),
                    ),
                )
            )
            self._config.charge_by_time_target_soc = self._soc_ratio(
                self._entry.options.get(
                    CONF_CHARGE_BY_TIME_TARGET_SOC,
                    self._entry.data.get(
                        CONF_CHARGE_BY_TIME_TARGET_SOC,
                        self._entry.options.get(
                            CONF_PROFIT_MAX_TARGET_SOC,
                            self._entry.data.get(
                                CONF_PROFIT_MAX_TARGET_SOC,
                                DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
                            ),
                        ),
                    ),
                ),
                DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
            )
            if self._optimizer:
                self._optimizer.terminal_weight = self._profit_max_terminal_weight()
            if profit_max:
                _LOGGER.info("Restored profit maximisation mode: ENABLED")
            if charge_by_time:
                _LOGGER.info("Restored Charge By Time: ENABLED")

        # Initialize solar forecaster
        from ..const import (
            CONF_SOLAR_FORECAST_PROVIDER,
            CONF_SOLCAST_ESTIMATE_TYPE,
            DEFAULT_SOLAR_FORECAST_PROVIDER,
            DEFAULT_SOLCAST_ESTIMATE_TYPE,
            SOLAR_FORECAST_PROVIDERS,
        )
        solar_forecast_provider = DEFAULT_SOLAR_FORECAST_PROVIDER
        solcast_estimate_type = DEFAULT_SOLCAST_ESTIMATE_TYPE
        if self._entry:
            solar_forecast_provider = self._entry.options.get(
                CONF_SOLAR_FORECAST_PROVIDER,
                self._entry.data.get(
                    CONF_SOLAR_FORECAST_PROVIDER, DEFAULT_SOLAR_FORECAST_PROVIDER
                ),
            )
            if solar_forecast_provider not in SOLAR_FORECAST_PROVIDERS:
                solar_forecast_provider = DEFAULT_SOLAR_FORECAST_PROVIDER
            solcast_estimate_type = self._entry.options.get(
                CONF_SOLCAST_ESTIMATE_TYPE,
                self._entry.data.get(
                    CONF_SOLCAST_ESTIMATE_TYPE, DEFAULT_SOLCAST_ESTIMATE_TYPE
                ),
            )
        self._solar_forecaster = SolcastForecaster(
            self.hass,
            interval_minutes=self._config.interval_minutes,
            estimate_type=solcast_estimate_type,
            provider_preference=solar_forecast_provider,
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
            config_entry=self._entry,
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
        if self._configured_load_entity_id:
            configured_state = self.hass.states.get(self._configured_load_entity_id)
            if self._is_usable_load_sensor_state(
                configured_state
            ) and not self._is_generated_load_forecast_sensor(configured_state):
                _LOGGER.info(
                    "Using configured load sensor: %s",
                    self._configured_load_entity_id,
                )
                return self._configured_load_entity_id
            _LOGGER.warning(
                "Configured load sensor %s is unavailable or not a live load sensor; "
                "falling back to auto-discovery",
                self._configured_load_entity_id,
            )

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
            if self._is_usable_load_sensor_state(state):
                _LOGGER.info("Using load sensor: %s", entity_id)
                return entity_id

        # Broader search: find any sensor with "load" or "consumption" in the name
        # that has a power unit (W or kW)
        for state in self.hass.states.async_all("sensor"):
            eid = state.entity_id
            name_lower = eid.lower()
            if not self._is_usable_load_sensor_state(state):
                continue
            if self._is_generated_load_forecast_sensor(state):
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

    @staticmethod
    def _is_usable_load_sensor_state(state) -> bool:
        """Return True when a state can be used as a live load source."""
        if not state or state.state in ("unknown", "unavailable", "None", None):
            return False
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return False
        return 0 <= value < 100_000

    @staticmethod
    def _is_generated_load_forecast_sensor(state) -> bool:
        """Return True for generated forecast sensors, not live load."""
        friendly_name = str(state.attributes.get("friendly_name") or "")
        label = f"{state.entity_id} {friendly_name}".lower()
        return any(
            marker in label
            for marker in (
                "forecast",
                "prediction",
                "predicted",
                "estimated",
            )
        )

    def _is_octopus_dynamic_tariff(self) -> bool:
        """Return True when the active Octopus tariff is genuinely half-hourly.

        Checks both product_code and the live tariff_code. The tariff_code is
        authoritative when data is sourced from BottlecapDave (the configured
        product_code may not match what the user is actually billed on).
        """
        if not self.price_coordinator:
            return False
        product = (getattr(self.price_coordinator, "product_code", "") or "").upper()
        tariff = (getattr(self.price_coordinator, "tariff_code", "") or "").upper()
        for token in ("AGILE", "FLUX", "COSY"):
            if token in product or token in tariff:
                return True
        return False

    async def _setup_price_listener(self) -> None:
        """Set up price-triggered optimization for dynamic pricing providers."""
        if not self.price_coordinator:
            return

        if self._prefers_static_tou_pricing():
            if self._price_listener_unsub:
                self._price_listener_unsub()
                self._price_listener_unsub = None
            if self._octopus_gate_listener_unsub:
                self._octopus_gate_listener_unsub()
                self._octopus_gate_listener_unsub = None
            self._is_dynamic_pricing = False
            return

        coordinator_name = type(self.price_coordinator).__name__
        dynamic_providers = [
            "AmberPriceCoordinator",
            "AEMOPriceCoordinator",
            "FlowPowerKWatchPriceCoordinator",
        ]

        if coordinator_name == "OctopusPriceCoordinator" and self._is_octopus_dynamic_tariff():
            dynamic_providers.append("OctopusPriceCoordinator")

        self._is_dynamic_pricing = coordinator_name in dynamic_providers

        if self._is_dynamic_pricing:
            # Unsubscribe existing listener before re-registering (idempotent)
            if self._price_listener_unsub:
                self._price_listener_unsub()
            self._price_listener_unsub = self.price_coordinator.async_add_listener(
                self._on_price_update
            )
            _LOGGER.info(
                "Dynamic pricing detected (%s) - re-optimizing on price changes",
                coordinator_name,
            )
        elif coordinator_name == "OctopusPriceCoordinator":
            # Octopus on a non-dynamic tariff today might roll onto an AGILE
            # variant tomorrow (BottlecapDave reports the live agreement).
            # Listen once so we can re-evaluate when fresh data arrives.
            if not self._octopus_gate_listener_unsub:
                self._octopus_gate_listener_unsub = (
                    self.price_coordinator.async_add_listener(
                        self._reevaluate_octopus_gate
                    )
                )

    def _reevaluate_octopus_gate(self) -> None:
        """Promote Octopus to dynamic pricing if the live tariff turns out to be AGILE/FLUX."""
        if self._is_dynamic_pricing or not self.price_coordinator:
            return
        if type(self.price_coordinator).__name__ != "OctopusPriceCoordinator":
            return
        if not self._is_octopus_dynamic_tariff():
            return
        # Promote: drop the gate listener, attach the real one.
        if self._octopus_gate_listener_unsub:
            self._octopus_gate_listener_unsub()
            self._octopus_gate_listener_unsub = None
        self._is_dynamic_pricing = True
        if self._price_listener_unsub:
            self._price_listener_unsub()
        self._price_listener_unsub = self.price_coordinator.async_add_listener(
            self._on_price_update
        )
        _LOGGER.info(
            "Octopus tariff %s detected as dynamic — enabling price-triggered LP",
            getattr(self.price_coordinator, "tariff_code", "?"),
        )

    def _electricity_provider(self) -> str:
        """Return the configured electricity provider for this entry."""
        if not self._entry:
            return ""
        from ..const import CONF_ELECTRICITY_PROVIDER

        return self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )

    def _prefers_static_tou_pricing(self) -> bool:
        """Return True for providers whose LP source is a tariff schedule.

        Values match CONF_ELECTRICITY_PROVIDER. New Zealand retailers (Octopus
        NZ, Electric Kiwi, Contact, etc.) all set the provider to "nz"; the
        retailer choice itself lives in CONF_NZ_RETAILER and is not checked
        here. aemo_vpp is a VPP spike-detection mode; its normal import/export
        rates still come from the user's tariff schedule, not the AEMO spot
        feed. tou_only is set internally by __init__.py:14540 for Tesla-only
        TOU users without a retailer integration.
        """
        return self._electricity_provider() in (
            "globird",
            "aemo_vpp",
            "other",
            "tou_only",
            "nz",
        )

    def _get_tou_tariff_schedule(self) -> dict | None:
        """Get the current TOU tariff schedule.

        The HTTP tariff endpoint and sensors refresh hass.data after the
        coordinator is constructed. If we keep returning the constructor copy,
        the LP can continue planning from a stale tariff until HA reloads.
        Prefer the shared schedule whenever it has full TOU periods.
        """
        from ..const import DOMAIN

        live_tariff = (
            self.hass.data.get(DOMAIN, {})
            .get(self.entry_id, {})
            .get("tariff_schedule")
        )
        if live_tariff and live_tariff.get("tou_periods"):
            if live_tariff is not self._tariff_schedule:
                cached_name = (
                    self._tariff_schedule or {}
                ).get("plan_name", "none")
                _LOGGER.info(
                    "Refreshing optimizer tariff_schedule from hass.data: "
                    "%s -> %s (%d TOU periods, last_sync=%s)",
                    cached_name,
                    live_tariff.get("plan_name", "unknown"),
                    len(live_tariff.get("tou_periods", {})),
                    live_tariff.get("last_sync"),
                )
            self._tariff_schedule = live_tariff
            return live_tariff

        if self._tariff_schedule:
            return self._tariff_schedule

        if live_tariff:
            _LOGGER.info("Using tariff_schedule from hass.data (not constructor)")
            self._tariff_schedule = live_tariff
        return live_tariff

    def _get_tou_price_forecast_if_available(
        self,
    ) -> tuple[list[float], list[float]] | None:
        """Generate a TOU price forecast when a tariff schedule is available."""
        tariff = self._get_tou_tariff_schedule()
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
        return None

    def _on_price_update(self) -> None:
        """Callback when price coordinator updates."""
        if not self._enabled or not self._is_dynamic_pricing:
            return

        startup_delay = self._seconds_until_initial_optimization_allowed()
        if startup_delay > 0:
            _LOGGER.debug(
                "Price update: skipping LP re-optimization for %.0fs during startup",
                startup_delay,
            )
            self._last_price_triggered_optimization = dt_util.utcnow()
            return

        # AEMO coordinator polls at 1-second intervals while searching for a new
        # dispatch file (ACTIVE mode). HA fires all listeners on every successful
        # poll, even when the file hasn't changed. Guard against that: only
        # re-optimize when the dispatch_file key in the coordinator's data
        # actually changes. Non-AEMO coordinators don't set dispatch_file so
        # this check is skipped for Amber/Octopus.
        if self.price_coordinator and hasattr(self.price_coordinator, "_polling_mode"):
            current_file = (self.price_coordinator.data or {}).get("dispatch_file")
            if current_file is not None and current_file == self._last_aemo_dispatch_file:
                return
            self._last_aemo_dispatch_file = current_file

        force_state = self._get_active_force_state()
        if force_state.get("active") and force_state.get("source") == "optimizer":
            _LOGGER.info(
                "Price update: skipping LP re-optimization while optimizer force %s is active",
                force_state.get("type", "mode"),
            )
            return

        # Rate-limit: Amber/Octopus can fire two coordinator updates per
        # billing window (usage price + spot price). Avoid duplicate LP runs
        # and repeated force mode commands inside the same interval.
        now = dt_util.utcnow()
        min_interval_seconds = (self._config.interval_minutes if self._config else 5) * 60
        if self._last_price_triggered_optimization is not None:
            elapsed = (now - self._last_price_triggered_optimization).total_seconds()
            if elapsed < min_interval_seconds:
                _LOGGER.debug(
                    "Price update: skipping LP (last ran %.0fs ago, interval %ds)",
                    elapsed, min_interval_seconds,
                )
                return
        self._last_price_triggered_optimization = now

        # Re-optimize with new prices and update dashboard sensors. Track the
        # task handle so disable() can cancel it — otherwise a price-solve
        # already in flight when disable() runs would complete afterwards
        # and re-command the battery (see OB-10).
        self._price_reoptimize_task = self.hass.async_create_background_task(
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
        initial_delay = max(0.0, float(INITIAL_OPTIMIZATION_DELAY_SECONDS))
        self._initial_optimization_not_before = (
            dt_util.utcnow() + timedelta(seconds=initial_delay)
        )

        # Restore dynamic price listener (may have been lost on disable/enable cycle)
        await self._setup_price_listener()

        # Defer Modbus-heavy startup operations to a background task so they
        # don't block async_setup_entry.  HA's bootstrap stage 2 has a global
        # timeout — if Modbus is slow (retries / no response) the entire
        # config entry setup gets CancelledError, leaving all views unregistered.
        self._deferred_restore_task = self.hass.async_create_background_task(
            self._deferred_enable_restore(), "powersync_enable_restore"
        )

        # Run initial optimization and start polling loop as background tasks
        # so they don't block HA bootstrap (LP solve can take several seconds)
        self._initial_opt_task = self.hass.async_create_background_task(
            self._run_initial_optimization_after_startup_delay(),
            "powersync_initial_optimization",
        )
        self._polling_task = self.hass.async_create_background_task(
            self._schedule_polling_loop(), "powersync_schedule_polling"
        )

        # Start EV coordination if enabled
        if self._ev_coordinator and self._ev_configs:
            await self._ev_coordinator.start()
            _LOGGER.info(
                "EV coordination started with %d charger(s)", len(self._ev_configs)
            )

        return True

    async def _run_initial_optimization_after_startup_delay(self) -> None:
        """Run the first optimizer pass once HA has finished starting.

        Gates on HA's real startup-complete signal rather than a fixed window,
        so the first solve lands as soon as startup settles instead of after an
        arbitrary delay. The heavy forecast data processing runs in an executor
        now, so a long hold is no longer needed to keep the event loop
        responsive during startup.
        """
        try:
            if not self.hass.is_running:
                from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

                _LOGGER.info("Deferring initial optimization until HA finishes starting")
                started = asyncio.Event()
                unsub = self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, lambda _event: started.set()
                )
                # Bounded by the legacy startup window so a missed start event can
                # never hold the first solve forever.
                cap = max(0.0, float(INITIAL_OPTIMIZATION_DELAY_SECONDS))
                try:
                    await asyncio.wait_for(started.wait(), timeout=cap or None)
                except asyncio.TimeoutError:
                    pass
                finally:
                    # async_listen_once removes its own listener once it fires;
                    # calling unsub() again raises "unknown job listener". Only
                    # remove it on the timeout path where it never fired, and guard
                    # the boundary race where it fires just as we time out.
                    if not started.is_set():
                        try:
                            unsub()
                        except ValueError:
                            pass

            if not self._enabled:
                return

            await self._run_optimization()
        finally:
            if self._initial_opt_task is asyncio.current_task():
                self._initial_opt_task = None

    def _seconds_until_initial_optimization_allowed(self) -> float:
        """Return remaining startup hold before the first LP solve may run.

        Returns 0 once HA has finished starting: startup pressure is gone, so
        price-triggered and polling re-optimizations may proceed normally.
        """
        if self.hass.is_running:
            return 0.0
        if self._initial_optimization_not_before is None:
            return 0.0
        return max(
            0.0,
            (self._initial_optimization_not_before - dt_util.utcnow()).total_seconds(),
        )

    async def _deferred_enable_restore(self) -> None:
        """Restore backup reserve and work mode in the background.

        Runs as a background task so Modbus operations (which may retry /
        time-out) don't block async_setup_entry and risk HA bootstrap
        stage 2 cancellation.
        """
        if not self._enabled:
            return
        # Start in self-consumption mode so the battery serves home load
        # immediately. Without this, the first LP action might be IDLE
        # (especially at night with no solar), forcing grid import until
        # the optimizer completes its first run.
        battery = self._executor.battery_controller if self._executor else None
        if battery:
            # Restore the user's reserve target without trusting the live
            # inverter value. GoodWe/Tesla IDLE temporarily raises the hardware
            # reserve to hold SOC; after an HA restart or update that live value
            # can still be elevated and must not become the restore target.
            startup_reserve, reserve_source = self._configured_startup_backup_reserve()
            startup_reserve, reserve_source = await self._resolve_startup_backup_reserve(
                battery,
                startup_reserve,
                reserve_source,
            )
            if startup_reserve is not None:
                self._startup_backup_reserve = startup_reserve
                if self._optimizer:
                    self._optimizer.update_hardware_reserve(startup_reserve / 100)
                _LOGGER.info(
                    "Optimizer startup: using %s: %d%%",
                    reserve_source,
                    startup_reserve,
                )
            else:
                try:
                    if hasattr(battery, "read_backup_reserve"):
                        reading = await battery.read_backup_reserve()
                        if (
                            reading.percent is not None
                            and reading.trust in TRUSTED_FOR_PERSIST
                        ):
                            startup_reserve = reading.percent
                            self._startup_backup_reserve = startup_reserve
                            _LOGGER.info(
                                "Optimizer startup: captured live backup reserve: %d%%",
                                startup_reserve,
                            )
                            if self._optimizer:
                                self._optimizer.update_hardware_reserve(startup_reserve / 100)
                    elif hasattr(battery, "get_backup_reserve"):
                        startup_reserve = await battery.get_backup_reserve()
                        if startup_reserve is not None:
                            self._startup_backup_reserve = startup_reserve
                            _LOGGER.info(
                                "Optimizer startup: captured live backup reserve: %d%%",
                                startup_reserve,
                            )
                            if self._optimizer:
                                self._optimizer.update_hardware_reserve(startup_reserve / 100)
                except Exception as e:
                    _LOGGER.debug("Could not read startup backup reserve: %s", e)

            # Skip startup mode change if monitoring mode or force mode is active
            from ..const import CONF_MONITORING_MODE, DOMAIN as _STARTUP_DOMAIN
            _monitoring = (
                self._entry and self._entry.options.get(
                    CONF_MONITORING_MODE, self._entry.data.get(CONF_MONITORING_MODE, False)
                )
            )
            # Check if force charge/discharge is active (persisted across restart)
            _entry_data = self.hass.data.get(_STARTUP_DOMAIN, {}).get(self.entry_id, {})
            _restart_restore_pending = bool(
                _entry_data.get("optimizer_force_restart_restore_pending", False)
            )
            _force_active = (
                not _restart_restore_pending
                and (
                    _entry_data.get("force_charge_state", {}).get("active", False)
                    or _entry_data.get("force_discharge_state", {}).get("active", False)
                )
            )
            if _monitoring:
                _LOGGER.info("Optimizer startup: monitoring mode active — skipping self-consumption mode set")
            elif _restart_restore_pending:
                _LOGGER.info("Optimizer startup: stale force restore pending — setting self-consumption mode")
                try:
                    if hasattr(battery, "set_self_consumption_mode"):
                        await battery.set_self_consumption_mode()
                        _LOGGER.info("Optimizer startup: set self-consumption mode (stale force restore)")
                except Exception as e:
                    _LOGGER.warning("Failed to set self-consumption during stale force restore: %s", e)
            elif _force_active:
                _LOGGER.info("Optimizer startup: force mode active — skipping self-consumption mode set")
            else:
                try:
                    if hasattr(battery, "set_self_consumption_mode"):
                        await battery.set_self_consumption_mode()
                        _LOGGER.info("Optimizer startup: set self-consumption mode (battery serves load)")
                except Exception as e:
                    _LOGGER.warning("Failed to set self-consumption on startup: %s", e)

        # FoxESS/Sungrow/Sigenergy: also ensure normal work mode (exit any
        # leftover IDLE hold mode from a previous HA restart)
        if (
            self.energy_coordinator
            and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
            and not _monitoring
            and not _force_active
        ):
            try:
                await self.energy_coordinator.restore_work_mode_from_idle()
                _LOGGER.info("Optimizer startup: ensured normal operation mode")
            except Exception as e:
                _LOGGER.warning("Failed to restore work mode on enable: %s", e)

        # Safety: if the Powerwall was left off-grid from a prior session
        # (e.g. HA crashed while off-grid curtailment was active), reconnect
        # so the optimizer starts from a clean on-grid state.
        if self._should_apply_offgrid_overlay() and not _monitoring and not _force_active:
            try:
                from ..powerwall_local.curtailment_fallback import get_fallback
                fallback = get_fallback(self.hass, self._entry)
                if not fallback._active:
                    # No active curtailment session — check actual grid state
                    from ..const import DOMAIN as _STARTUP_OG_DOMAIN
                    _og_data = self.hass.data.get(_STARTUP_OG_DOMAIN, {}).get(self.entry_id, {})
                    _pw_local = _og_data.get("powerwall_local", {})
                    _coord = _pw_local.get("coordinator")
                    if _coord and _coord.data and hasattr(_coord.data, "grid_status"):
                        gs = _coord.data.grid_status or ""
                        if "island" in gs.lower():
                            _LOGGER.warning(
                                "Optimizer startup: Powerwall is off-grid "
                                "(grid_status=%s) without active curtailment "
                                "session — reconnecting",
                                gs,
                            )
                            await fallback.release(
                                trigger_reason="startup_orphan_cleanup", force=True
                            )
            except Exception as e:
                _LOGGER.debug("Optimizer startup: off-grid orphan check failed: %s", e)

    async def disable(self) -> None:
        """Disable optimization."""
        if not self._enabled:
            return

        monitoring_mode = self._monitoring_mode_active()

        # Safety: if IDLE was the last action, restore backup_reserve and
        # work mode before shutting down. Otherwise the battery stays locked
        # at the IDLE-elevated backup_reserve (and Backup mode for FoxESS).
        if not monitoring_mode and self._last_executed_action == "idle":
            if self.battery_controller:
                await self._restore_pre_idle_backup_reserve(
                    self.battery_controller,
                    "optimizer disable",
                )
            # FoxESS/Sungrow: restore from IDLE hold mode to normal operation
            if (
                self.energy_coordinator
                and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
            ):
                try:
                    await self.energy_coordinator.restore_work_mode_from_idle()
                    _LOGGER.info("Optimizer disable: restored work mode from IDLE")
                except Exception as e:
                    _LOGGER.warning("Failed to restore work mode on disable: %s", e)
        elif monitoring_mode and self._last_executed_action == "idle":
            _LOGGER.info(
                "Optimizer shutdown: monitoring mode active — skipping IDLE cleanup writes"
            )
            self._idle_hold_reserve = None
        if self._scheduled_ev_no_discharge_active:
            if monitoring_mode:
                _LOGGER.info(
                    "Optimizer shutdown: monitoring mode active — skipping scheduled EV no-discharge release"
                )
            else:
                await self._release_scheduled_ev_no_discharge_mode("optimizer disabled")
        self._last_executed_action = None

        # Cancel background tasks first so they can't run optimization
        # after _enabled is set to False (e.g. polling loop waking from sleep)
        self._enabled = False

        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            self._polling_task = None
        if self._initial_opt_task and not self._initial_opt_task.done():
            self._initial_opt_task.cancel()
            self._initial_opt_task = None
        if self._deferred_restore_task and not self._deferred_restore_task.done():
            self._deferred_restore_task.cancel()
            self._deferred_restore_task = None
        if self._settings_reoptimize_task and not self._settings_reoptimize_task.done():
            self._settings_reoptimize_task.cancel()
            self._settings_reoptimize_task = None
        price_reoptimize_task = getattr(self, "_price_reoptimize_task", None)
        if price_reoptimize_task and not price_reoptimize_task.done():
            price_reoptimize_task.cancel()
            self._price_reoptimize_task = None

        if self._price_listener_unsub:
            self._price_listener_unsub()
            self._price_listener_unsub = None

        if self._octopus_gate_listener_unsub:
            self._octopus_gate_listener_unsub()
            self._octopus_gate_listener_unsub = None

        if self._executor:
            if monitoring_mode:
                _LOGGER.info(
                    "Optimizer shutdown: monitoring mode active — restoring optimizer-owned "
                    "battery mode before handing off to monitoring mode"
                )
            await self._executor.stop(restore_normal=True)

        if self._ev_coordinator:
            await self._ev_coordinator.stop()

        # Flush cost data to disk before shutdown
        await self._cost_store.async_save(self._cost_data_to_save())

        _LOGGER.info("Optimization disabled")

    async def _run_optimization(self, force: bool = False) -> None:
        """Run the built-in LP optimizer with current forecast data.

        When ``force`` is True (user-initiated re-optimization), queue behind
        any in-flight solve instead of skipping, so the request is never
        silently dropped.
        """
        if not self._optimizer or not self._enabled:
            return

        if await self._wait_for_restart_force_restore():
            return

        # Skip if another LP solve is already in progress. Three independent
        # triggers (DataUpdateCoordinator, polling loop, price update) can
        # fire at the same 5-min boundary; serialise them so only one runs.
        # The locked() check + acquire() are safe without await between them
        # because asyncio is single-threaded on the event loop.
        #
        # A forced (user-initiated) re-optimization must NOT be dropped when a
        # periodic solve is mid-flight — the in-flight run may have baked in
        # now-stale config (e.g. a just-saved reserve). Queue behind it and run
        # a fresh solve once the lock frees, rather than returning a stale one.
        if self._optimization_lock.locked():
            if not force:
                _LOGGER.debug("Optimization already in progress — skipping concurrent request")
                return
            _LOGGER.debug("Optimization in progress — queuing forced re-optimization")
        await self._optimization_lock.acquire()
        try:
            # Retry battery auto-detection if still on defaults
            # (site_info may not have been available during initial setup)
            if self._battery_specs_source == "default":
                await self._auto_detect_battery_specs()
                # If detection just succeeded, push the corrected specs into
                # the optimizer. Nothing else syncs capacity/charge after
                # construction unless the user saves a setting, so without this
                # the LP would keep modelling the default 13.5 kWh / 5 kW
                # indefinitely while the rest of the run uses the real specs.
                if self._battery_specs_source != "default" and self._optimizer:
                    self._optimizer.update_config(
                        capacity_wh=self._config.battery_capacity_wh,
                        max_charge_w=self._config.max_charge_w,
                        max_discharge_w=self._config.max_discharge_w,
                    )
                    _LOGGER.info(
                        "Optimizer: synced auto-detected battery specs "
                        "(%.1f kWh, %.1f kW charge, %.1f kW discharge)",
                        self._config.battery_capacity_wh / 1000,
                        self._config.max_charge_w / 1000,
                        self._config.max_discharge_w / 1000,
                    )

            # Warn if battery specs haven't been configured — optimization
            # will still run but may produce suboptimal results with defaults.
            # Don't block: existing users who had working auto-detect may
            # temporarily hit "default" if Tesla API is slow on startup.
            if self._battery_specs_source == "default" and not self._current_schedule:
                _LOGGER.warning(
                    "Optimizer: battery specs not configured (using defaults: %.1f kWh, "
                    "%.1f kW charge, %.1f kW discharge). Configure battery specs in the "
                    "PowerSync app under Optimizer Settings for accurate optimization.",
                    self._config.battery_capacity_wh / 1000,
                    self._config.max_charge_w / 1000,
                    self._config.max_discharge_w / 1000,
                )

            if self._ev_integration_enabled:
                await self._refresh_ev_forecast_inputs()

            # Collect forecast data
            self._last_export_boost_allowed_slots = []
            prices = await self._get_price_forecast()
            solar = await self._get_solar_forecast()
            load = await self._get_load_forecast()
            soc, capacity = await self._get_battery_state()

            # Overlay EV charging plan onto load forecast
            ev_peak_kw = 0.0
            self._last_planned_ev_load_forecast_w = None
            if load:
                planned_ev_load_w = self._get_planned_ev_load_forecast(len(load))
                if planned_ev_load_w:
                    load = [l + ev for l, ev in zip(load, planned_ev_load_w)]
                    self._last_planned_ev_load_forecast_w = planned_ev_load_w
                    ev_peak_kw = max(ev_peak_kw, max(planned_ev_load_w) / 1000)

            if load and self._ev_integration_enabled:
                ev_load_w = self._get_ev_planned_load(len(load))
                if ev_load_w:
                    load = [l + ev for l, ev in zip(load, ev_load_w)]
                    ev_peak_kw = max(ev_peak_kw, max(ev_load_w) / 1000)

            import_prices = prices[0] if prices else []
            export_prices = prices[1] if prices else []

            # Convert forecasts from Watts (forecaster output) to kW (LP input)
            solar_forecast = [v / 1000.0 for v in solar] if solar else []
            load_forecast = [v / 1000.0 for v in load] if load else []

            if solar_forecast:
                solar_forecast = self._apply_solar_nowcast_derate(solar_forecast, soc)

            # Curtailment-aware solar: cap forecast during predicted curtailment periods
            if solar_forecast and load_forecast and export_prices and self._entry:
                from ..const import (
                    CONF_AC_INVERTER_CURTAILMENT_ENABLED,
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
                )
                curtailment_enabled = (
                    self._entry.options.get(CONF_AC_INVERTER_CURTAILMENT_ENABLED, False)
                    or self._entry.options.get(CONF_BATTERY_CURTAILMENT_ENABLED, False)
                    or self._entry.options.get(CONF_SIGENERGY_DC_CURTAILMENT_ENABLED, False)
                )
                if curtailment_enabled:
                    # Curtailment activates when export < 1c/kWh AND battery
                    # is full — matching runtime logic in should_curtail_ac/dc.
                    # While battery has room, solar charges it (no curtailment).
                    # Use forward SOC projection to estimate when battery fills.
                    curtail_threshold = 0.01  # $/kWh
                    max_charge_kw = self._config.max_charge_w / 1000.0
                    capacity_kwh = self._config.battery_capacity_wh / 1000.0
                    dt_hours = self._config.interval_minutes / 60.0
                    projected_soc = soc  # 0-1 range
                    capped = 0
                    min_len = min(len(solar_forecast), len(load_forecast), len(export_prices))
                    for t in range(min_len):
                        surplus_kw = solar_forecast[t] - load_forecast[t]
                        low_price = export_prices[t] < curtail_threshold
                        battery_full = projected_soc >= 0.99

                        if low_price and battery_full and solar_forecast[t] > 0:
                            # Battery full + low price → inverter curtails to load only
                            cap = load_forecast[t]
                            if solar_forecast[t] > cap:
                                solar_forecast[t] = cap
                                capped += 1

                        # Forward-project SOC for next interval
                        if surplus_kw > 0 and capacity_kwh > 0:
                            charge_kw = min(surplus_kw, max_charge_kw)
                            projected_soc = min(1.0, projected_soc + charge_kw * dt_hours / capacity_kwh)
                        elif surplus_kw < 0 and capacity_kwh > 0:
                            projected_soc = max(0.0, projected_soc + surplus_kw * dt_hours / capacity_kwh)

                    if capped:
                        _LOGGER.info(
                            "Curtailment-aware solar: capped %d intervals where "
                            "export < %.0fc/kWh and battery full (solar limited to load)",
                            capped, curtail_threshold * 100,
                        )

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

            # Compute acquisition cost: actual cost per kWh of GRID-charged
            # energy. Use the grid-charge-specific accumulators (cost of grid
            # energy that went into the battery / kWh of grid charging), not
            # total household import cost over total charge (which includes
            # house-load import in the numerator and solar charging in the
            # denominator, inflating the value and wrongly blocking exports).
            if self._actual_grid_charge_kwh_today > 0.1:
                acq_cost = (
                    self._actual_grid_charge_cost_today
                    / self._actual_grid_charge_kwh_today
                )
            else:
                # No meaningful grid-charge data yet — use median import price
                # as proxy.
                acq_cost = (
                    sorted(import_prices)[len(import_prices) // 2]
                    if import_prices
                    else 0.0
                )

            # Suppress the below-reserve WARNING when a user-triggered force
            # discharge is active — draining past the LP reserve is intentional
            # in that case, so the adjustment should log at INFO not WARNING.
            if self._force_state_getter:
                _fs = self._force_state_getter()
                self._optimizer.suppress_reserve_warning = bool(
                    _fs
                    and _fs.get("active")
                    and _fs.get("type") == "discharge"
                    and _fs.get("source") != "optimizer"
                )
            else:
                self._optimizer.suppress_reserve_warning = False

            # Pre-window SOC floor: in Charge By Time mode, force the battery
            # to reach the configured SOC by the configured target time.
            _target_slot = (
                self._next_charge_by_time_target_slot()
                if self._config.allow_grid_charge
                else None
            )
            self._optimizer.pre_window_slot = _target_slot
            self._optimizer.pre_window_soc_target = (
                self._charge_by_time_target_soc()
                if self._optimizer.pre_window_slot is not None
                else 0.0
            )
            self._apply_zerohero_optimizer_inputs(import_prices, export_prices)
            battery_export_allowed = self._battery_export_allowed_slots(
                len(import_prices),
                export_prices,
            )
            battery_charge_blocked = self._battery_charge_blocked_slots(
                len(import_prices),
            )
            grid_charge_cap_import_prices = self._grid_charge_cap_import_prices(
                import_prices
            )
            grid_charge_allowed = self._grid_charge_allowed_slots(
                grid_charge_cap_import_prices,
                solar_forecast,
                load_forecast,
                soc,
            )
            spread_import_blocked = [
                bool(blocked) or not bool(allowed)
                for blocked, allowed in zip(
                    battery_charge_blocked,
                    grid_charge_allowed,
                    strict=False,
                )
            ]
            self._sync_grid_export_cap_to_optimizer()
            self._sync_optimizer_discharge_limits()
            schedule_timestamps = self._price_timestamps(len(import_prices))
            priority_export_slots = self._priority_export_slots_for_run(
                len(import_prices),
                export_prices,
            )

            def _auto_reserve_baseline_floor() -> float | None:
                if not self.auto_apply_reserve_enabled:
                    return None
                manual_reserve = self._reserve_ratio(
                    getattr(self, "_manual_backup_reserve", None),
                    self._config.backup_reserve,
                )
                if manual_reserve is None:
                    return None
                if math.isclose(
                    manual_reserve,
                    self._config.backup_reserve,
                    abs_tol=0.0001,
                ):
                    return None
                return manual_reserve

            async def _run_optimizer_once(
                reserve_floor: float | None = None,
                export_reserve_floor: float | list[float] | None = None,
            ) -> OptimizerResult:
                if reserve_floor is not None:
                    self._optimizer.update_config(backup_reserve=reserve_floor)
                try:
                    return await self.hass.async_add_executor_job(
                        self._optimizer.optimize,
                        import_prices,
                        export_prices,
                        solar_forecast,
                        load_forecast,
                        soc,
                        self._cost_function.value,
                        acq_cost,
                        battery_export_allowed,
                        battery_charge_blocked,
                        self._config.allow_grid_charge,
                        grid_charge_allowed,
                        self._last_zerohero_bonus_prices,
                        self._last_zerohero_bonus_cap_kwh,
                        self._last_zerocharge_bonus_prices,
                        self._last_zerocharge_bonus_cap_kwh,
                        export_reserve_floor,
                        schedule_timestamps,
                        priority_export_slots,
                        any(priority_export_slots),
                    )
                finally:
                    if reserve_floor is not None:
                        self._optimizer.update_config(
                            backup_reserve=self._config.backup_reserve
                        )

            # Run LP in executor thread to avoid blocking event loop
            recommendation_floor = _auto_reserve_baseline_floor()
            result: OptimizerResult = await _run_optimizer_once(
                recommendation_floor
            )
            used_recommendation_floor = recommendation_floor is not None

            self._last_optimizer_result = result
            self._current_schedule = result.schedule
            if self._should_spread_import_schedule():
                self._current_schedule = self._spread_import_schedule(
                    self._current_schedule,
                    import_prices,
                    spread_import_blocked,
                    soc,
                    solar_forecast=solar_forecast,
                    load_forecast=load_forecast,
                )
                result.schedule = self._current_schedule
            if self._should_spread_export_schedule():
                self._current_schedule = self._spread_export_schedule(
                    self._current_schedule,
                    battery_export_allowed,
                )
                result.schedule = self._current_schedule
            self._current_schedule = self._bridge_short_export_gaps(
                self._current_schedule,
                export_prices,
            )
            result.schedule = self._current_schedule
            if self._should_disable_idle_schedule():
                self._current_schedule = self._disable_idle_schedule(
                    self._current_schedule,
                    solar_forecast=solar_forecast,
                    load_forecast=load_forecast,
                    initial_soc=soc,
                )
                result.schedule = self._current_schedule
            self._last_update_time = dt_util.now()

            # Apply off-grid curtailment overlay if enabled — converts
            # eligible SELF_CONSUMPTION/IDLE slots to OFF_GRID during
            # negative export price periods.
            if self._should_apply_offgrid_overlay():
                self._current_schedule = self._apply_offgrid_overlay(
                    self._current_schedule, export_prices,
                )
                result.schedule = self._current_schedule

            reserve_recommendation = dict(
                getattr(result, "reserve_recommendation", {}) or {}
            )
            reserve_changed = self._apply_auto_reserve_recommendation(result)
            if getattr(result, "reserve_recommendation", {}) or {}:
                reserve_recommendation = dict(
                    getattr(result, "reserve_recommendation", {}) or {}
                )
            export_reserve_floor = None
            export_reserve_metadata: dict[str, Any] = {}
            if self.auto_apply_reserve_enabled:
                export_reserve_floor, export_reserve_metadata = (
                    self._post_processed_export_reserve_floor_slots(
                        self._current_schedule,
                        solar_forecast,
                        load_forecast,
                    )
                )
            if export_reserve_metadata:
                reserve_recommendation.update(export_reserve_metadata)
            if export_reserve_floor is None:
                export_reserve_floor = self._auto_export_reserve_floor(
                    reserve_recommendation
                )
                if export_reserve_floor is None:
                    export_reserve_floor = self._auto_export_reserve_floor_slots(
                        reserve_recommendation,
                        len(import_prices),
                    )
            if (
                reserve_changed
                or used_recommendation_floor
                or export_reserve_floor is not None
            ):
                result = await _run_optimizer_once(
                    export_reserve_floor=export_reserve_floor
                )
                self._last_optimizer_result = result
                self._current_schedule = result.schedule
                if self._should_spread_import_schedule():
                    self._current_schedule = self._spread_import_schedule(
                        self._current_schedule,
                        import_prices,
                        spread_import_blocked,
                        soc,
                        solar_forecast=solar_forecast,
                        load_forecast=load_forecast,
                    )
                    result.schedule = self._current_schedule
                if self._should_spread_export_schedule():
                    self._current_schedule = self._spread_export_schedule(
                        self._current_schedule,
                        battery_export_allowed,
                        export_reserve_floor=export_reserve_floor,
                    )
                    result.schedule = self._current_schedule
                self._current_schedule = self._bridge_short_export_gaps(
                    self._current_schedule,
                    export_prices,
                    export_reserve_floor=export_reserve_floor,
                )
                result.schedule = self._current_schedule
                if self._should_disable_idle_schedule():
                    self._current_schedule = self._disable_idle_schedule(
                        self._current_schedule,
                        solar_forecast=solar_forecast,
                        load_forecast=load_forecast,
                        initial_soc=soc,
                    )
                    result.schedule = self._current_schedule
                if self._should_apply_offgrid_overlay():
                    self._current_schedule = self._apply_offgrid_overlay(
                        self._current_schedule, export_prices,
                    )
                    result.schedule = self._current_schedule
                if reserve_recommendation and result.reserve_recommendation:
                    for recommendation_key in (
                        "configured_optimizer_reserve_percent",
                        "manual_optimizer_reserve_percent",
                        "home_load_export_floor_percent",
                    ):
                        if recommendation_key in reserve_recommendation:
                            result.reserve_recommendation.setdefault(
                                recommendation_key,
                                reserve_recommendation[recommendation_key],
                            )
                    if export_reserve_floor is not None:
                        result.reserve_recommendation.setdefault(
                            "applied_export_reserve_floor_percent",
                            int(
                                round(
                                    (
                                        max(export_reserve_floor)
                                        if isinstance(export_reserve_floor, list)
                                        else export_reserve_floor
                                    )
                                    * 100
                                )
                            ),
                        )
                self._last_update_time = dt_util.now()

            final_export_reserve_floor = None
            final_export_reserve_metadata: dict[str, Any] = {}
            if self.auto_apply_reserve_enabled:
                final_export_reserve_floor, final_export_reserve_metadata = (
                    self._post_processed_export_reserve_floor_slots(
                        self._current_schedule,
                        solar_forecast,
                        load_forecast,
                    )
                )
            if final_export_reserve_floor is not None:
                self._set_active_export_reserve_floor_slots(
                    final_export_reserve_floor,
                    self._current_schedule,
                )
                result.reserve_recommendation = dict(
                    getattr(result, "reserve_recommendation", {}) or {}
                )
                result.reserve_recommendation.update(final_export_reserve_metadata)
                result.reserve_recommendation.setdefault(
                    "applied_export_reserve_floor_percent",
                    int(round(max(final_export_reserve_floor) * 100)),
                )
            elif export_reserve_floor is not None:
                self._set_active_export_reserve_floor_slots(
                    export_reserve_floor if isinstance(export_reserve_floor, list) else None,
                    self._current_schedule,
                )
            else:
                self._set_active_export_reserve_floor_slots(None, None)

            # Store forecast data for LP forecast sensors
            self._has_solar_forecast = solar_forecast is not None and any(v > 0 for v in (solar_forecast or []))
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

            _DECISION_LOGGER.info(
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

            # Push fresh data to HA sensors immediately after LP solve.
            # Without this, sensors only update on the 5-minute DataUpdateCoordinator
            # interval and can show stale "idle" while the API returns the real action.
            self.async_set_updated_data(self.get_api_data())

            # Execute the current action immediately so the battery responds
            # right after the LP solve — don't wait for the next polling tick
            # (up to 5 minutes away).  The polling loop still re-applies the
            # action as a heartbeat, but this removes the initial delay.
            current_action = self._get_current_action()
            # Defensive re-check: disable() may have flipped _enabled to False
            # while this solve was awaiting forecast/battery-state I/O above
            # (see OB-10). _execute_optimizer_action also guards on _enabled
            # internally, but skip the call — and the lock acquisition below —
            # entirely once disabled rather than relying solely on that.
            if current_action and self._executor and self._enabled:
                # Serialise against _execute_cached_current_action_if_changed
                # (OB-11): both this in-cycle execution and the cached-action
                # path issue hardware commands, and at an action-transition
                # boundary they can otherwise interleave and double-command
                # the battery. _execute_lock is independent of
                # _optimization_lock (held for this whole solve) so nesting
                # it here cannot deadlock — _execute_cached_current_action_if_changed
                # never acquires _optimization_lock.
                async with self._execute_lock:
                    await self._execute_optimizer_action(current_action)

        except Exception as e:
            _LOGGER.error("Optimization failed: %s", e, exc_info=True)
        finally:
            self._optimization_lock.release()

    async def _wait_for_restart_force_restore(self) -> bool:
        """Wait for stale optimizer force cleanup before dispatching hardware."""
        from ..const import DOMAIN as _STARTUP_DOMAIN

        for attempt in range(30):
            entry_data = self.hass.data.get(_STARTUP_DOMAIN, {}).get(self.entry_id, {})
            if not entry_data.get("optimizer_force_restart_restore_pending", False):
                if attempt:
                    _LOGGER.info(
                        "Optimizer startup: stale force cleanup completed; running optimization"
                    )
                return False

            if attempt == 0:
                _LOGGER.info(
                    "Optimizer startup: waiting for stale force cleanup before optimization"
                )
            await asyncio.sleep(1)

        _LOGGER.warning(
            "Optimizer startup: stale force cleanup still pending after 30s; "
            "skipping this optimization run"
        )
        return True

    async def _schedule_polling_loop(self) -> None:
        """Periodically re-optimize and execute current action.

        Sleep-first structure: wait until the next wall-clock interval boundary
        before re-optimizing. This keeps execution aligned with tariff changes
        instead of drifting by however long the previous LP solve took.
        """
        while self._enabled:
            try:
                # Safety: if a pre-IDLE backup reserve restore is pending,
                # keep trying until it succeeds. This catches API failures
                # during previous restore attempts.
                if self._should_restore_pre_idle_backup_reserve_from_polling():
                    battery = self._executor.battery_controller if self._executor else None
                    if battery:
                        await self._restore_pre_idle_backup_reserve(battery, "polling safety check")

                # Wait for next wall-clock interval boundary. A fixed sleep
                # from the previous solve can miss tariff flips by nearly a
                # full interval when the solve finishes just before a boundary.
                await asyncio.sleep(self._seconds_until_next_interval())

                # Check again after sleep — disable() may have been called
                if not self._enabled:
                    break

                startup_delay = self._seconds_until_initial_optimization_allowed()
                if startup_delay > 0:
                    _LOGGER.debug(
                        "Schedule polling waiting %.0fs for startup optimization delay",
                        startup_delay,
                    )
                    await asyncio.sleep(startup_delay)
                    if not self._enabled:
                        break
                    # The dedicated initial optimization task owns the first
                    # post-startup solve. Resume polling at the next boundary.
                    initial_task = self._initial_opt_task
                    if initial_task is not None and not initial_task.done():
                        continue
                    self._initial_opt_task = None

                # Apply the already-computed slot at the wall-clock boundary
                # before any forecast/API work in the next LP solve can delay
                # hardware control.
                await self._execute_cached_current_action_if_changed()

                # Re-optimize on each interval (executes the resulting action internally)
                await self._run_optimization()

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error("Error in schedule polling: %s", e)
                await asyncio.sleep(60)

    async def _execute_cached_current_action_if_changed(self) -> None:
        """Apply the cached schedule action when coordinator refresh crosses a boundary."""
        if not getattr(self, "_enabled", False):
            return
        if not getattr(self, "_executor", None):
            return

        optimization_lock = getattr(self, "_optimization_lock", None)
        if optimization_lock is not None and optimization_lock.locked():
            return

        current_action = self._get_current_action()
        action_name = getattr(current_action, "action", None)
        if not current_action or not action_name:
            return
        if action_name == getattr(self, "_last_executed_action", None):
            return
        if action_name in ("charge", "discharge", "export"):
            _LOGGER.debug(
                "Optimizer: skipping cached forced action %s before fresh LP solve",
                action_name,
            )
            return

        # Reentrancy guard (OB-11): the polling loop and the
        # DataUpdateCoordinator refresh cycle can both cross the same
        # wall-clock boundary and reach this point concurrently at an action
        # transition. _last_executed_action is only written at the end of
        # _execute_optimizer_action after awaited hardware I/O, so both
        # callers can pass the dedup check above before either has updated
        # the marker. Serialise on _execute_lock and re-check the dedup
        # condition once inside — if the other caller already applied this
        # action while we were waiting for the lock, skip instead of issuing
        # a second (duplicate) hardware command.
        execute_lock = getattr(self, "_execute_lock", None)
        if execute_lock is None:
            execute_lock = asyncio.Lock()
            self._execute_lock = execute_lock

        async with execute_lock:
            if action_name == getattr(self, "_last_executed_action", None):
                return

            _LOGGER.info(
                "Optimizer: applying cached schedule action %s on coordinator refresh",
                action_name,
            )
            await self._execute_optimizer_action(current_action)

    def _seconds_until_next_interval(self) -> float:
        """Return seconds until the next optimizer interval boundary."""
        interval = max(1, int(getattr(self._config, "interval_minutes", 5) or 5))
        now = dt_util.now()
        current_minute = now.replace(second=0, microsecond=0)
        minutes_past_boundary = current_minute.minute % interval
        if (
            minutes_past_boundary == 0
            and now.second == 0
            and now.microsecond == 0
        ):
            next_boundary = current_minute + timedelta(minutes=interval)
        else:
            next_boundary = current_minute + timedelta(
                minutes=interval - minutes_past_boundary
            )
        return max(1.0, (next_boundary - now).total_seconds())

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

    def _force_duration_for_action_window(
        self,
        action: Any,
        matching_actions: set[str],
        *,
        allow_boundary_overrun: bool = True,
        minimum_minutes: int | None = None,
    ) -> int:
        """Return a force duration for the contiguous LP action block.

        By default this preserves the legacy behavior: choose a supported
        duration that covers the block, even if that rounds slightly beyond the
        final matching slot. For hard action boundaries (for example charge
        immediately before Flow Power Happy Hour export), callers can disable
        boundary overrun so the force command cannot cross into the next LP
        action.
        """
        interval = max(1, int(getattr(self._config, "interval_minutes", 5) or 5))
        minimum = minimum_minutes if minimum_minutes is not None else interval + 5
        actions = getattr(getattr(self, "_current_schedule", None), "actions", None) or []

        start_idx = None
        action_ts = getattr(action, "timestamp", None)
        for idx, scheduled in enumerate(actions):
            if scheduled is action or (
                action_ts is not None and getattr(scheduled, "timestamp", None) == action_ts
            ):
                start_idx = idx
                break

        if start_idx is None:
            requested = minimum
        else:
            slots = 0
            for scheduled in actions[start_idx:]:
                if getattr(scheduled, "action", None) not in matching_actions:
                    break
                slots += 1
            block_minutes = max(interval, slots * interval)
            if allow_boundary_overrun:
                requested = max(minimum, block_minutes)
            else:
                requested = block_minutes

        try:
            from ..const import DISCHARGE_DURATIONS
        except Exception:
            DISCHARGE_DURATIONS = [5, 10, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180, 195, 210, 225, 240]

        supported = sorted(int(duration) for duration in DISCHARGE_DURATIONS)
        if allow_boundary_overrun:
            for duration in supported:
                if duration >= requested:
                    return int(duration)
            return int(max(supported))

        return int(max(1, requested))

    def _supports_disable_idle_mode(self) -> bool:
        """Return True when this provider can disable optimizer IDLE."""
        return supports_no_idle_mode_provider(self._provider_key())

    def _should_disable_idle_schedule(self) -> bool:
        """Return True when no-idle mode should replace optimizer IDLE."""
        return self._supports_disable_idle_mode() and bool(
            self._config.disable_idle_enabled
        )

    def _effective_runtime_action(
        self,
        action_name: str | None,
        timestamp: datetime | None = None,
    ) -> str | None:
        """Return the action that runtime execution will apply."""
        if action_name != "idle":
            return action_name
        if self._should_disable_idle_schedule():
            return "self_consumption"
        if timestamp is not None and self._is_in_demand_window_at(timestamp):
            return "self_consumption"
        return action_name

    def _disable_idle_schedule(
        self,
        schedule: OptimizationSchedule,
        *,
        solar_forecast: list[float] | None = None,
        load_forecast: list[float] | None = None,
        initial_soc: float | None = None,
    ) -> OptimizationSchedule:
        """Replace optimizer IDLE slots with self-consumption."""
        actions = getattr(schedule, "actions", None) or []
        if not actions:
            return schedule

        changed = False
        new_actions = []
        interval_hours = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        ) / 60.0
        capacity_wh = max(
            0.0,
            float(getattr(self._config, "battery_capacity_wh", 0) or 0),
        )
        max_discharge_w = max(
            0.0,
            float(getattr(self._config, "max_discharge_w", 0) or 0),
        )
        efficiency = max(
            0.001,
            float(
                getattr(getattr(self, "_optimizer", None), "efficiency", 0.95)
                or 0.95
            ),
        )
        optimizer_reserve = max(
            0.0,
            min(1.0, float(getattr(self._config, "backup_reserve", 0) or 0)),
        )
        soc_cursor = (
            max(0.0, min(1.0, float(initial_soc)))
            if initial_soc is not None
            else None
        )
        hardware_reserve_known = False
        hardware_reserve = optimizer_reserve
        startup_reserve = getattr(self, "_startup_backup_reserve", None)
        if startup_reserve is not None:
            hardware_reserve_known = True
            hardware_reserve = float(startup_reserve) / 100.0
        else:
            optimizer = getattr(self, "_optimizer", None)
            if getattr(optimizer, "hardware_reserve_known", False):
                hardware_reserve_known = True
                hardware_reserve = float(
                    getattr(optimizer, "hardware_reserve", 0.0) or 0.0
                )
        self_consumption_floor = (
            max(0.0, min(1.0, hardware_reserve))
            if hardware_reserve_known
            else optimizer_reserve
        )
        if soc_cursor is not None:
            self_consumption_floor = min(soc_cursor, self_consumption_floor)
        charge_by_time_target_slot = self._next_charge_by_time_target_slot()
        charge_by_time_target_soc = (
            self._charge_by_time_target_soc()
            if charge_by_time_target_slot is not None
            else 0.0
        )
        future_grid_charge_planned = [False] * len(actions)
        has_future_grid_charge = False
        for index in range(len(actions) - 1, -1, -1):
            future_grid_charge_planned[index] = has_future_grid_charge
            future_action = actions[index]
            if (
                getattr(future_action, "action", None) == "charge"
                and float(getattr(future_action, "battery_charge_w", 0.0) or 0.0) > 0
            ):
                has_future_grid_charge = True

        def _forecast_w(values: list[float] | None, index: int) -> float:
            if not values or index >= len(values):
                return 0.0
            try:
                return max(0.0, float(values[index]) * 1000.0)
            except (TypeError, ValueError):
                return 0.0

        def _natural_discharge_w(index: int, soc: float | None) -> float:
            net_load_w = _forecast_w(load_forecast, index) - _forecast_w(
                solar_forecast,
                index,
            )
            if net_load_w <= 0 or max_discharge_w <= 0:
                return 0.0
            if soc is None or capacity_wh <= 0:
                return min(max_discharge_w, net_load_w)
            available_wh = max(0.0, soc - self_consumption_floor) * capacity_wh
            available_w = available_wh * efficiency / interval_hours
            return min(max_discharge_w, net_load_w, max(0.0, available_w))

        def _advance_soc(
            soc: float | None,
            charge_w: float,
            discharge_w: float,
        ) -> float | None:
            if soc is None or capacity_wh <= 0:
                return soc
            stored_wh = max(0.0, charge_w) * interval_hours * efficiency
            removed_wh = max(0.0, discharge_w) * interval_hours / efficiency
            return max(
                self_consumption_floor,
                min(1.0, soc + (stored_wh - removed_wh) / capacity_wh),
            )

        for index, action in enumerate(actions):
            action_name = getattr(action, "action", None)
            action_charge_w = float(getattr(action, "battery_charge_w", 0.0) or 0.0)
            action_discharge_w = float(
                getattr(action, "battery_discharge_w", 0.0) or 0.0
            )
            should_simulate_self_use = (
                action_name in SELF_USE_ACTIONS
                and action_charge_w <= 0
                and action_discharge_w <= 0
            )
            should_preserve_charge_by_time_hold = (
                charge_by_time_target_slot is not None
                and index < charge_by_time_target_slot
                and soc_cursor is not None
                and soc_cursor < charge_by_time_target_soc - 0.0001
                and (action_name == "idle" or should_simulate_self_use)
            )
            should_preserve_below_reserve_recovery_hold = (
                action_name == "idle"
                and soc_cursor is not None
                and soc_cursor <= optimizer_reserve
                and soc_cursor
                <= self_consumption_floor
                + BELOW_RESERVE_RECOVERY_HOLD_MARGIN_SOC
                and future_grid_charge_planned[index]
            )
            if (
                should_preserve_charge_by_time_hold
                or should_preserve_below_reserve_recovery_hold
            ):
                new_actions.append(
                    ScheduleAction(
                        timestamp=action.timestamp,
                        action=action.action,
                        power_w=0.0,
                        soc=round(soc_cursor, 4),
                        battery_charge_w=0.0,
                        battery_discharge_w=0.0,
                    )
                )
                continue
            if action_name != "idle" and not should_simulate_self_use:
                next_soc = _advance_soc(
                    soc_cursor,
                    action_charge_w,
                    action_discharge_w,
                )
                if soc_cursor is None:
                    new_actions.append(action)
                else:
                    new_actions.append(
                        ScheduleAction(
                            timestamp=action.timestamp,
                            action=action.action,
                            power_w=action.power_w,
                            soc=(
                                round(next_soc, 4)
                                if next_soc is not None
                                else getattr(action, "soc", None)
                            ),
                            battery_charge_w=action.battery_charge_w,
                            battery_discharge_w=action.battery_discharge_w,
                        )
                    )
                soc_cursor = next_soc
                continue
            changed = True
            discharge_w = round(_natural_discharge_w(index, soc_cursor), 1)
            soc_cursor = _advance_soc(soc_cursor, 0.0, discharge_w)
            new_actions.append(
                ScheduleAction(
                    timestamp=action.timestamp,
                    action="self_consumption",
                    power_w=discharge_w,
                    soc=(
                        round(soc_cursor, 4)
                        if soc_cursor is not None
                        else getattr(action, "soc", None)
                    ),
                    battery_charge_w=0.0,
                    battery_discharge_w=discharge_w,
                )
            )

        if not changed:
            return schedule

        _LOGGER.info("No Idle mode: converted optimizer IDLE slots to self-consumption")
        return OptimizationSchedule(
            actions=new_actions,
            predicted_cost=schedule.predicted_cost,
            predicted_savings=schedule.predicted_savings,
            last_updated=schedule.last_updated,
        )

    def _bridge_short_export_gaps(
        self,
        schedule: OptimizationSchedule,
        export_prices: list[float] | None = None,
        export_reserve_floor: float | list[float] | None = None,
    ) -> OptimizationSchedule:
        """Keep export mode through one-slot self-use islands between exports."""
        actions = getattr(schedule, "actions", None) or []
        if len(actions) < 3:
            return schedule
        if self._dynamic_export_prices_can_have_real_one_slot_gaps():
            return schedule

        interval = max(1, int(getattr(self._config, "interval_minutes", 5) or 5))
        max_gap_slots = 1
        bridged = 0
        idx = 1
        while idx < len(actions) - 1:
            action_name = getattr(actions[idx], "action", None)
            if action_name not in SELF_USE_ACTIONS:
                idx += 1
                continue

            gap_start = idx
            while idx < len(actions) - 1 and getattr(actions[idx], "action", None) in SELF_USE_ACTIONS:
                idx += 1
            gap_end = idx
            gap_slots = gap_end - gap_start

            previous_action = actions[gap_start - 1]
            next_action = actions[gap_end] if gap_end < len(actions) else None
            if (
                gap_slots > max_gap_slots
                or getattr(previous_action, "action", None) not in EXPORT_ACTIONS
                or getattr(next_action, "action", None) not in EXPORT_ACTIONS
                or not self._short_export_gap_prices_match(
                    gap_start,
                    gap_end,
                    export_prices,
                )
            ):
                continue

            export_action = (
                "export"
                if "export" in {
                    getattr(previous_action, "action", None),
                    getattr(next_action, "action", None),
                }
                else "discharge"
            )
            bridge_power_w = self._bridged_export_power_w(
                previous_action,
                next_action,
            )
            reserve_floor = self._bridge_export_reserve_floor(
                export_reserve_floor,
                gap_start,
                gap_end,
            )
            if not self._can_bridge_export_gap_above_reserve(
                previous_action,
                actions[gap_start:gap_end],
                bridge_power_w,
                reserve_floor,
            ):
                continue

            for gap_action in actions[gap_start:gap_end]:
                gap_action.action = export_action
                gap_action.power_w = bridge_power_w
                gap_action.battery_charge_w = 0.0
                gap_action.battery_discharge_w = max(
                    getattr(gap_action, "battery_discharge_w", 0.0) or 0.0,
                    bridge_power_w,
                )
                bridged_soc = self._bridged_gap_soc(previous_action, bridge_power_w)
                if bridged_soc is not None:
                    gap_action.soc = bridged_soc
                bridged += 1

        if bridged:
            _LOGGER.info(
                "Optimizer: bridged %dmin self-consumption gap inside export window",
                bridged * interval,
            )
        return schedule

    def _can_bridge_export_gap_above_reserve(
        self,
        previous_action: Any,
        gap_actions: list[Any],
        bridge_power_w: float,
        reserve_floor: float | None = None,
    ) -> bool:
        """Return False when bridging would export below the configured floor."""
        reserve_floor = (
            self._force_discharge_reserve_floor()
            if reserve_floor is None
            else max(0.0, min(1.0, reserve_floor))
        )
        previous_soc = self._reserve_ratio(getattr(previous_action, "soc", None), None)
        gap_socs = [
            soc
            for soc in (
                self._reserve_ratio(getattr(action, "soc", None), None)
                for action in gap_actions
            )
            if soc is not None
        ]
        if previous_soc is None and not gap_socs:
            return True
        if previous_soc is not None and previous_soc <= reserve_floor + 1e-6:
            return False
        if any(soc <= reserve_floor + 1e-6 for soc in gap_socs):
            return False
        bridged_soc = self._bridged_gap_soc(previous_action, bridge_power_w)
        if bridged_soc is None:
            return True
        return bridged_soc >= reserve_floor - 1e-6

    def _bridge_export_reserve_floor(
        self,
        export_reserve_floor: float | list[float] | None,
        gap_start: int,
        gap_end: int,
    ) -> float:
        """Return the reserve floor that applies while filling an export gap."""
        floor = self._force_discharge_reserve_floor()
        if isinstance(export_reserve_floor, list):
            scoped_floors = [
                self._reserve_ratio(value, None)
                for value in export_reserve_floor[gap_start:gap_end]
            ]
            scoped_floors = [value for value in scoped_floors if value is not None]
            if scoped_floors:
                floor = max(floor, max(scoped_floors))
        else:
            explicit_floor = self._reserve_ratio(export_reserve_floor, None)
            if explicit_floor is not None:
                floor = max(floor, explicit_floor)
        return max(0.0, min(1.0, floor))

    def _bridged_gap_soc(
        self,
        previous_action: Any,
        bridge_power_w: float,
    ) -> float | None:
        """Estimate SOC after one bridged export slot."""
        previous_soc = self._reserve_ratio(getattr(previous_action, "soc", None), None)
        if previous_soc is None:
            return None
        capacity_wh = float(getattr(self._config, "battery_capacity_wh", 0.0) or 0.0)
        if capacity_wh <= 0:
            return previous_soc
        interval_hours = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        ) / 60.0
        efficiency = float(
            getattr(getattr(self, "_optimizer", None), "efficiency", 0.92) or 0.92
        )
        removed_wh = max(0.0, float(bridge_power_w or 0.0)) * interval_hours / max(
            efficiency,
            0.001,
        )
        return max(0.0, min(1.0, round(previous_soc - removed_wh / capacity_wh, 4)))

    def _dynamic_export_prices_can_have_real_one_slot_gaps(self) -> bool:
        """Return True when a one-slot export gap may be a real price signal."""
        if getattr(self, "_is_dynamic_pricing", False):
            return True
        coordinator_name = type(getattr(self, "price_coordinator", None)).__name__
        return coordinator_name in {
            "AmberPriceCoordinator",
            "AEMOPriceCoordinator",
            "FlowPowerKWatchPriceCoordinator",
        }

    @staticmethod
    def _short_export_gap_prices_match(
        gap_start: int,
        gap_end: int,
        export_prices: list[float] | None,
        *,
        tolerance: float = 1e-6,
    ) -> bool:
        """Return True when a one-slot gap has the same export price as its neighbours."""
        if not export_prices:
            return False
        if gap_end - gap_start != 1:
            return False
        prev_idx = gap_start - 1
        next_idx = gap_end
        if prev_idx < 0 or next_idx >= len(export_prices):
            return False
        try:
            previous_price = float(export_prices[prev_idx])
            gap_price = float(export_prices[gap_start])
            next_price = float(export_prices[next_idx])
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(previous_price)
            and math.isfinite(gap_price)
            and math.isfinite(next_price)
            and abs(previous_price - gap_price) <= tolerance
            and abs(next_price - gap_price) <= tolerance
        )

    @staticmethod
    def _bridged_export_power_w(previous_action: Any, next_action: Any) -> float:
        """Return a conservative export power for a bridged gap."""
        powers: list[float] = []
        for action in (previous_action, next_action):
            try:
                power = float(getattr(action, "power_w", 0.0) or 0.0)
            except (TypeError, ValueError):
                power = 0.0
            if power > 0:
                powers.append(power)
        return min(powers) if powers else 0.0

    def _tesla_tariff_duration_for_force_window(
        self,
        force_duration_minutes: int,
    ) -> int | None:
        """Return a longer Tesla tariff duration near 30-min TOU boundaries."""
        if self.battery_system != "tesla":
            return None

        try:
            force_duration = int(force_duration_minutes)
        except (TypeError, ValueError):
            return None
        if force_duration <= 0:
            return None

        interval = max(1, int(getattr(self._config, "interval_minutes", 5) or 5))
        now = dt_util.now()
        minute = 30 if now.minute < 30 else 60
        next_boundary = now.replace(
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=minute)
        force_expiry = now + timedelta(minutes=force_duration)

        seconds_from_boundary = abs((force_expiry - next_boundary).total_seconds())
        if seconds_from_boundary > 60:
            return None

        target_expiry = next_boundary + timedelta(minutes=interval)
        tariff_duration = int((target_expiry - now).total_seconds() // 60)
        if target_expiry > now + timedelta(minutes=tariff_duration):
            tariff_duration += 1
        return max(force_duration, tariff_duration)

    def _as_utc_datetime(self, value: Any) -> datetime | None:
        """Return a timezone-aware UTC datetime for persisted/runtime values."""
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt_util.UTC)
        return parsed.astimezone(dt_util.UTC)

    def _clear_optimizer_force_state(self) -> None:
        """Clear private optimizer-owned force state."""
        state = getattr(self, "_optimizer_force_state", None)
        if not isinstance(state, dict):
            self._optimizer_force_state = {"active": False}
            return
        state.update(
            {
                "active": False,
                "type": None,
                "expires_at": None,
                "hardware_expires_at": None,
                "power_w": 0,
                "started_at": None,
                "source": "optimizer",
                "scope": "optimizer",
            }
        )

    def _set_optimizer_force_state(
        self,
        force_type: str,
        duration_minutes: int,
        power_w: float,
    ) -> None:
        """Record an optimizer-owned hardware force command."""
        now = dt_util.utcnow()
        expires_at = now + timedelta(minutes=max(1, int(duration_minutes)))
        existing_state = getattr(self, "_optimizer_force_state", None)
        started_at = None
        if (
            isinstance(existing_state, dict)
            and existing_state.get("active")
            and existing_state.get("type") == force_type
        ):
            started_at = self._as_utc_datetime(existing_state.get("started_at"))
        self._optimizer_force_state = {
            "active": True,
            "type": force_type,
            "expires_at": expires_at,
            "hardware_expires_at": expires_at,
            "power_w": power_w,
            "started_at": started_at or now,
            "source": "optimizer",
            "scope": "optimizer",
        }

    def _optimizer_force_charge_commitment_remaining(
        self,
        force_state: dict[str, Any],
        action: Any,
    ) -> timedelta | None:
        """Return remaining minimum hold time for optimizer-owned force charge."""
        if (
            force_state.get("scope") != "optimizer"
            or force_state.get("type") != "charge"
        ):
            return None

        started_at = self._as_utc_datetime(force_state.get("started_at"))
        if started_at is None:
            return None

        remaining = (
            OPTIMIZER_FORCE_CHARGE_MIN_COMMITMENT
            - (dt_util.utcnow() - started_at)
        )
        if remaining <= timedelta(0):
            return None

        # Release the anti-thrash hold if the schedule no longer wants to
        # charge anywhere in the remaining window. A price spike flips every
        # remaining slot away from "charge" (e.g. to self_consumption), so
        # without this the battery would keep grid-charging at the spike price
        # for the full 20-minute commitment. Mirrors the discharge variant,
        # which releases when no future export action remains.
        if not self._schedule_has_future_action(action, CHARGE_ACTIONS, remaining):
            return None
        return remaining

    def _optimizer_force_discharge_commitment_remaining(
        self,
        force_state: dict[str, Any],
        action: Any,
    ) -> timedelta | None:
        """Return remaining hold time for optimizer-owned force discharge."""
        if (
            force_state.get("scope") != "optimizer"
            or force_state.get("type") != "discharge"
        ):
            return None

        started_at = self._as_utc_datetime(force_state.get("started_at"))
        if started_at is None:
            return None

        remaining = (
            OPTIMIZER_FORCE_DISCHARGE_MIN_COMMITMENT
            - (dt_util.utcnow() - started_at)
        )
        if remaining <= timedelta(0):
            return None

        if not self._schedule_has_future_action(action, EXPORT_ACTIONS, remaining):
            return None
        return remaining

    def _schedule_has_future_action(
        self,
        action: Any,
        matching_actions: set[str],
        horizon: timedelta,
    ) -> bool:
        """Return true when the active schedule still wants a matching future action."""
        schedule = getattr(self, "_current_schedule", None)
        actions = getattr(schedule, "actions", None)
        if not actions:
            return False

        now = dt_util.utcnow()
        action_ts = self._as_utc_datetime(getattr(action, "timestamp", None))
        start_at = max(now, action_ts) if action_ts is not None else now
        horizon_end = now + horizon

        for scheduled_action in actions:
            scheduled_ts = self._as_utc_datetime(
                getattr(scheduled_action, "timestamp", None)
            )
            if scheduled_ts is None or scheduled_ts < start_at:
                continue
            if scheduled_ts > horizon_end:
                continue
            if getattr(scheduled_action, "action", None) in matching_actions:
                return True
        return False

    def _get_active_force_state(self) -> dict[str, Any]:
        """Return user-visible force state or private optimizer force state."""
        force_state_getter = getattr(self, "_force_state_getter", None)
        if force_state_getter:
            force_state = force_state_getter() or {}
            if force_state.get("active"):
                force_state = dict(force_state)
                force_state.setdefault("scope", "external")
                return force_state

        state = getattr(self, "_optimizer_force_state", None)
        if not isinstance(state, dict) or not state.get("active"):
            return {"active": False}

        expires_at = self._as_utc_datetime(state.get("expires_at"))
        now = dt_util.utcnow()
        if expires_at is not None and expires_at <= now:
            self._clear_optimizer_force_state()
            return {"active": False}

        active = dict(state)
        active.setdefault("source", "optimizer")
        active.setdefault("scope", "optimizer")
        return active

    def get_active_force_state(self) -> dict[str, Any]:
        """Return the active force state, including optimizer-owned hardware force."""
        return self._get_active_force_state()

    def _export_command_power_w(self, action: Any) -> float:
        """Return the hardware export command power for an optimizer action."""
        command_w = float(self._config.max_discharge_w)
        if self._supports_target_export_power():
            try:
                requested_w = float(getattr(action, "power_w", 0.0) or 0.0)
            except (TypeError, ValueError):
                requested_w = 0.0
            if requested_w <= 0 and self.battery_system == "goodwe":
                try:
                    requested_w = float(
                        getattr(action, "battery_discharge_w", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    requested_w = 0.0
            if requested_w > 0:
                command_w = min(command_w, requested_w)
            if self._config.max_grid_export_w is not None:
                command_w = min(command_w, float(self._config.max_grid_export_w))
        return command_w

    @staticmethod
    def _force_command_power_changed(
        previous_power_w: Any,
        target_power_w: float,
        *,
        tolerance_w: float = 50.0,
    ) -> bool:
        """Return True when an active optimizer force command needs a power refresh."""
        if previous_power_w is None:
            return False
        try:
            previous = float(previous_power_w)
            target = float(target_power_w)
        except (TypeError, ValueError):
            return False
        return abs(previous - target) > tolerance_w

    def _force_charge_hardware_needs_refresh(self, target_power_w: float) -> bool:
        """Return True when telemetry shows a stale non-Tesla charge command."""
        if self.battery_system == "tesla":
            return False

        data = self._get_energy_data()
        if not isinstance(data, dict):
            return False

        try:
            target_w = float(target_power_w)
        except (TypeError, ValueError):
            return False
        if target_w <= 0:
            return False

        mode_value = (
            data.get("work_mode_name")
            or data.get("mode")
            or data.get("work_mode")
            or data.get("ems_mode_name")
        )
        mode = str(mode_value or "").strip().lower()
        charge_cmd = data.get("charge_cmd")
        try:
            charge_cmd_int = int(charge_cmd) if charge_cmd is not None else None
        except (TypeError, ValueError):
            charge_cmd_int = None

        if self.battery_system == "sungrow":
            sungrow_force_charge_cmd = 0xAA
            if mode == "forced" and charge_cmd_int == sungrow_force_charge_cmd:
                return False
            if "force charge" in mode or mode == "force_charge":
                return False
        elif "force charge" in mode:
            return False

        try:
            battery_power = float(data.get("battery_power", 0) or 0)
        except (TypeError, ValueError):
            return False
        battery_power_w = battery_power * 1000 if abs(battery_power) < 100 else battery_power
        charge_power_w = max(0.0, -battery_power_w)
        minimum_expected_w = max(500.0, target_w * 0.6)

        if charge_power_w >= minimum_expected_w:
            return False

        _LOGGER.info(
            "Optimizer: force charge hardware appears inactive "
            "(mode=%s, charge_cmd=%s, charging %.0fW below %.0fW target) — refreshing command",
            mode_value,
            charge_cmd,
            charge_power_w,
            target_w,
        )
        return True

    def _force_discharge_hardware_needs_refresh(self, target_power_w: float) -> bool:
        """Return True when telemetry shows a stale non-Tesla discharge command."""
        if self.battery_system == "tesla":
            return False

        data = self._get_energy_data()
        if not isinstance(data, dict):
            return False

        try:
            target_w = float(target_power_w)
        except (TypeError, ValueError):
            return False
        if target_w <= 0:
            return False

        mode_value = (
            data.get("work_mode_name")
            or data.get("mode")
            or data.get("work_mode")
            or data.get("ems_mode_name")
        )
        mode = str(mode_value or "").strip().lower()
        if any(
            token in mode
            for token in (
                "sell",
                "discharge",
                "export",
                "eco_discharge",
                "force_discharge",
            )
        ):
            return False

        try:
            battery_power = float(data.get("battery_power", 0) or 0)
        except (TypeError, ValueError):
            battery_power = 0.0
        battery_power_w = battery_power * 1000 if abs(battery_power) < 100 else battery_power
        discharge_power_w = max(0.0, battery_power_w)

        try:
            grid_power = float(data.get("grid_power", 0) or 0)
        except (TypeError, ValueError):
            grid_power = 0.0
        grid_power_w = grid_power * 1000 if abs(grid_power) < 100 else grid_power
        export_power_w = max(0.0, -grid_power_w)

        observed_power_w = max(discharge_power_w, export_power_w)
        minimum_expected_w = max(500.0, target_w * 0.2)

        if observed_power_w >= minimum_expected_w:
            return False

        _LOGGER.info(
            "Optimizer: force discharge hardware appears inactive "
            "(mode=%s, discharging %.0fW/exporting %.0fW below %.0fW target) — refreshing command",
            mode_value,
            discharge_power_w,
            export_power_w,
            target_w,
        )
        return True

    def _current_import_price_for_action(
        self,
        prices: list[float],
        action: Any | None,
    ) -> float | None:
        """Return the tariff price for an action's scheduled interval."""
        if action is None:
            return None
        action_time = self._as_utc_datetime(getattr(action, "timestamp", None))
        if action_time is None:
            return None
        timestamps = getattr(self, "_last_price_timestamps", None)
        if not timestamps:
            return None

        interval_minutes = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        )
        slot_limit = timedelta(minutes=interval_minutes)
        n = min(len(prices), len(timestamps))
        for idx in range(n):
            slot_start = self._as_utc_datetime(timestamps[idx])
            if slot_start is None:
                continue
            if slot_start <= action_time < slot_start + slot_limit:
                try:
                    return float(prices[idx])
                except (TypeError, ValueError):
                    return None
        return None

    def _current_import_price_is_free(self, action: Any | None = None) -> bool:
        prices = getattr(self, "_last_display_import_prices", None) or getattr(
            self, "_last_import_prices", None
        )
        if not prices:
            return False
        action_price = self._current_import_price_for_action(prices, action)
        if action_price is not None:
            return action_price <= 0.001
        try:
            return float(prices[0]) <= 0.001
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _kw_to_w(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed * 1000.0

    def _live_site_import_charge_limit_w(self) -> float | None:
        """Return live battery charge headroom under the site import cap."""
        max_grid_import_w = self._normalize_optional_power_w(
            self._config.max_grid_import_w
        )
        if max_grid_import_w is None or self.energy_coordinator is None:
            return None

        data = self._get_energy_data()
        if not isinstance(data, dict):
            return None

        max_charge_w = max(0.0, float(self._config.max_charge_w or 0))
        if max_charge_w <= 0:
            return None

        solar_w = self._kw_to_w(data.get("solar_power"))
        load_w = self._kw_to_w(data.get("load_power"))
        if solar_w is not None and load_w is not None:
            return max(0.0, min(max_charge_w, max_grid_import_w + solar_w - load_w))

        grid_w = self._kw_to_w(data.get("grid_power"))
        battery_w = self._kw_to_w(data.get("battery_power"))
        if grid_w is None or battery_w is None:
            return None

        current_charge_w = max(0.0, -battery_w)
        return max(0.0, min(max_charge_w, max_grid_import_w - grid_w + current_charge_w))

    def _charge_command_power_w(self, action: Any) -> float:
        """Return charge command power, using live headroom in free import slots."""
        try:
            scheduled_w = max(0.0, float(getattr(action, "power_w", 0.0) or 0.0))
        except (TypeError, ValueError):
            scheduled_w = 0.0

        if not self._supports_target_charge_power():
            return scheduled_w

        if not self._current_import_price_is_free():
            return scheduled_w

        live_limit_w = self._live_site_import_charge_limit_w()
        if live_limit_w is None:
            return scheduled_w

        if abs(live_limit_w - scheduled_w) >= 250.0:
            _LOGGER.info(
                "Optimizer: Adjusting free-import charge target from %.0fW to %.0fW "
                "using live site-import headroom",
                scheduled_w,
                live_limit_w,
            )
        return live_limit_w

    def _tesla_force_charge_should_yield_to_live_solar(
        self,
        action: Any | None = None,
    ) -> bool:
        """Return True when Tesla force charge should avoid curtailing solar surplus."""
        if self.battery_system != "tesla":
            return False
        if self._supports_target_charge_power():
            return False

        if self._current_import_price_is_free(action):
            _LOGGER.debug(
                "Optimizer: Allowing Tesla force charge with live solar during "
                "free import"
            )
            return False

        data = self._get_energy_data()
        if not isinstance(data, dict):
            return False

        solar_w = self._kw_to_w(data.get("solar_power"))
        if solar_w is None or solar_w < 500.0:
            return False

        try:
            battery_level = float(data.get("battery_level", 0) or 0)
        except (TypeError, ValueError):
            battery_level = 0.0
        if battery_level >= 98.0:
            return False

        load_w = self._kw_to_w(data.get("load_power"))
        battery_w = self._kw_to_w(data.get("battery_power"))
        grid_w = self._kw_to_w(data.get("grid_power"))

        if battery_w is not None and battery_w > 250.0:
            _LOGGER.debug(
                "Optimizer: Allowing Tesla force charge with %.0fW live solar "
                "because the battery is discharging %.0fW into site load",
                solar_w,
                battery_w,
            )
            return False

        if load_w is not None and solar_w - load_w < 500.0:
            _LOGGER.debug(
                "Optimizer: Allowing Tesla force charge with %.0fW live solar "
                "because site load %.0fW leaves no meaningful solar surplus",
                solar_w,
                load_w,
            )
            return False

        if grid_w is not None and grid_w > 250.0:
            _LOGGER.debug(
                "Optimizer: Allowing Tesla force charge with %.0fW live solar "
                "because the site is importing %.0fW",
                solar_w,
                grid_w,
            )
            return False

        target_soc = None
        charge_deadline = None
        if action is not None:
            target_soc = self._tesla_charge_action_target_soc(action)
            charge_deadline = self._tesla_charge_action_deadline(action)

        if (
            target_soc is not None
            and charge_deadline is not None
            and target_soc > 0
        ):
            capacity_wh = float(getattr(self._config, "battery_capacity_wh", 0) or 0)
            if capacity_wh > 0:
                now = dt_util.now()
                if charge_deadline.tzinfo is None:
                    charge_deadline = dt_util.as_local(charge_deadline)
                remaining_h = max(
                    0.0,
                    (charge_deadline - now).total_seconds() / 3600.0,
                )
                live_charge_w = 0.0
                if battery_w is not None and battery_w < -250.0:
                    live_charge_w = -battery_w
                elif load_w is not None:
                    live_charge_w = max(0.0, solar_w - load_w)
                projected_soc = min(
                    1.0,
                    battery_level / 100.0
                    + (live_charge_w * remaining_h / capacity_wh),
                )
                if projected_soc + 0.01 < target_soc:
                    _LOGGER.info(
                        "Optimizer: Allowing Tesla force charge despite %.0fW live "
                        "solar because current solar charging %.0fW projects %.1f%% "
                        "SOC by %s, below planned %.1f%%",
                        solar_w,
                        live_charge_w,
                        projected_soc * 100,
                        charge_deadline.isoformat(),
                        target_soc * 100,
                    )
                    return False

        _LOGGER.info(
            "Optimizer: Blocking Tesla force charge while %.0fW live solar "
            "surplus is available; Tesla TOU force charge cannot target partial charge "
            "power and may curtail AC-coupled solar",
            solar_w,
        )
        return True

    def _tesla_charge_action_target_soc(self, action: Any) -> float | None:
        """Return the target SOC at the end of the contiguous charge block."""
        actions = list(
            getattr(getattr(self, "_current_schedule", None), "actions", []) or []
        )
        if not actions:
            return self._normalise_action_soc(getattr(action, "soc", None))

        action_ts = getattr(action, "timestamp", None)
        start_idx = None
        for idx, candidate in enumerate(actions):
            if candidate is action:
                start_idx = idx
                break
            if (
                action_ts is not None
                and getattr(candidate, "timestamp", None) == action_ts
            ):
                start_idx = idx
                break
        if start_idx is None:
            return self._normalise_action_soc(getattr(action, "soc", None))

        target = None
        for candidate in actions[start_idx:]:
            if getattr(candidate, "action", None) != "charge":
                break
            candidate_soc = self._normalise_action_soc(
                getattr(candidate, "soc", None)
            )
            if candidate_soc is not None:
                target = candidate_soc if target is None else max(target, candidate_soc)
        return target

    def _tesla_charge_action_deadline(self, action: Any) -> datetime | None:
        """Return the end timestamp for the contiguous charge block."""
        actions = list(
            getattr(getattr(self, "_current_schedule", None), "actions", []) or []
        )
        action_ts = getattr(action, "timestamp", None)
        if not actions:
            if isinstance(action_ts, datetime):
                return action_ts + timedelta(minutes=self._config.interval_minutes)
            return None

        start_idx = None
        for idx, candidate in enumerate(actions):
            if candidate is action:
                start_idx = idx
                break
            if (
                action_ts is not None
                and getattr(candidate, "timestamp", None) == action_ts
            ):
                start_idx = idx
                break
        if start_idx is None:
            if isinstance(action_ts, datetime):
                return action_ts + timedelta(minutes=self._config.interval_minutes)
            return None

        end_ts = None
        for candidate in actions[start_idx:]:
            if getattr(candidate, "action", None) != "charge":
                break
            candidate_ts = getattr(candidate, "timestamp", None)
            if isinstance(candidate_ts, datetime):
                end_ts = candidate_ts + timedelta(minutes=self._config.interval_minutes)
        return end_ts

    @staticmethod
    def _normalise_action_soc(raw_soc: Any) -> float | None:
        """Return a schedule SOC as a 0-1 ratio."""
        try:
            soc = float(raw_soc)
        except (TypeError, ValueError):
            return None
        if soc > 1.0:
            soc /= 100.0
        return max(0.0, min(1.0, soc))

    async def _execute_optimizer_action(self, action: Any) -> None:
        """Execute an optimizer action on the battery."""
        # Guard against a solve that was in flight when disable() ran (e.g. an
        # untracked price-triggered re-optimization) completing afterwards and
        # re-commanding the battery. disable() sets _enabled=False before
        # cancelling background tasks, so any execution reaching this point
        # after that must be a no-op. Default to True (enabled) when the
        # attribute is entirely unset — real coordinators always set it
        # explicitly in __init__/enable()/disable(); only lightweight test
        # doubles built via object.__new__() omit it, and they expect this
        # method to behave as if the optimizer is running.
        if not getattr(self, "_enabled", True):
            return
        if not self._executor or not self._executor.battery_controller:
            return

        if (
            getattr(action, "action", None) == "idle"
            and self._should_disable_idle_schedule()
        ):
            action = copy.copy(action)
            action.action = "self_consumption"

        # Monitoring mode — log what would happen but don't execute
        if self._monitoring_mode_active():
            _LOGGER.info(
                "[MONITORING] Optimizer would execute: %s (power=%sW) — blocked by monitoring mode",
                action.action, getattr(action, 'power_w', 'N/A'),
            )
            return

        battery = self._executor.battery_controller

        # Check if force charge/discharge is active.
        # User-triggered force modes own the battery state — don't override.
        # Optimizer-triggered force modes can be overridden if the LP changes
        # its mind (e.g. LP planned 1 export step but now wants self_consumption).
        force_state = self._get_active_force_state()
        if force_state and force_state.get("active"):
                force_type = force_state.get("type", "unknown")
                force_source = force_state.get("source", "user")

                if force_source != "optimizer":
                    # User-triggered — never override
                    _LOGGER.debug(
                        "Optimizer: force %s active (user) — skipping action execution "
                        "(LP wants %s)",
                        force_type, action.action,
                    )
                    return

                # Optimizer-triggered: check if LP still wants the same action.
                # If the current slot no longer matches the active optimizer
                # force mode, restore immediately and let the next 5-minute LP
                # interval issue a fresh command if needed.
                def _action_matches_force(a) -> bool:
                    return (
                        (force_type == "discharge" and a.action in ("discharge", "export"))
                        or (force_type == "charge" and a.action == "charge")
                    )

                preserve_active_for_force = self._scheduled_ev_preserve_active()
                lp_matches_force = _action_matches_force(action)
                if preserve_active_for_force and force_type == "discharge":
                    lp_matches_force = False
                force_window_action = action

                if lp_matches_force:
                    if force_type == "discharge":
                        try:
                            soc_now, _ = await self._get_battery_state()
                            opt_reserve = self._force_discharge_reserve_floor(action)
                            reaches_reserve, projected_soc = (
                                self._force_discharge_reaches_reserve(
                                    action,
                                    soc_now,
                                    opt_reserve,
                                )
                            )
                            if reaches_reserve:
                                soc_text = (
                                    f"{soc_now * 100:.1f}%"
                                    if soc_now is not None
                                    else "unknown"
                                )
                                projected_text = (
                                    f", projected {projected_soc * 100:.1f}%"
                                    if projected_soc is not None
                                    else ""
                                )
                                _LOGGER.warning(
                                    "Optimizer: Canceling active force discharge — "
                                    "SOC %s%s at/below optimizer reserve %.0f%%; "
                                    "restoring self_consumption instead of extending",
                                    soc_text,
                                    projected_text,
                                    opt_reserve * 100,
                                )
                                if force_state.get("scope") == "optimizer":
                                    self._clear_optimizer_force_state()
                                elif self._force_state_clearer:
                                    self._force_state_clearer()
                                if hasattr(battery, "restore_normal"):
                                    await battery.restore_normal()
                                elif hasattr(battery, "set_self_consumption_mode"):
                                    await battery.set_self_consumption_mode()
                                self._last_executed_planned_action = action.action
                                self._last_executed_action = "self_consumption"
                                return
                        except Exception as reserve_err:
                            _LOGGER.debug(
                                "Optimizer: reserve check before extending force "
                                "discharge failed: %s",
                                reserve_err,
                            )

                    if (
                        force_type == "charge"
                        and self._tesla_force_charge_should_yield_to_live_solar(
                            action
                        )
                    ):
                        _LOGGER.info(
                            "Optimizer: Canceling active Tesla force charge — "
                            "live solar is available, restoring self_consumption"
                        )
                        if force_state.get("scope") == "optimizer":
                            self._clear_optimizer_force_state()
                        elif self._force_state_clearer:
                            self._force_state_clearer()
                        if hasattr(battery, "restore_normal"):
                            await battery.restore_normal()
                        elif hasattr(battery, "set_self_consumption_mode"):
                            await battery.set_self_consumption_mode()
                        self._last_executed_planned_action = action.action
                        self._last_executed_action = "self_consumption"
                        return

                    # Extend the expiry timer so the force mode doesn't expire
                    # between optimizer cycles (avoids restore→re-issue gap).
                    from ..const import DOMAIN as _EXT_DOMAIN
                    _ext_data = self.hass.data.get(_EXT_DOMAIN, {}).get(self.entry_id, {})
                    force_scope = force_state.get("scope", "external")
                    if force_scope == "optimizer":
                        _ext_state = self._optimizer_force_state
                    else:
                        _ext_state = _ext_data.get(
                            "force_discharge_state" if force_type == "discharge" else "force_charge_state", {}
                        )
                        if _ext_state.get("cancel_expiry_timer"):
                            _ext_state["cancel_expiry_timer"]()  # Cancel old timer
                    matching_actions = (
                        {"charge"}
                        if force_type == "charge"
                        else {"discharge", "export"}
                    )
                    extend_mins = self._force_duration_for_action_window(
                        force_window_action,
                        matching_actions,
                        allow_boundary_overrun=False,
                        minimum_minutes=self._config.interval_minutes + 5,
                    )
                    tariff_mins = (
                        self._tesla_tariff_duration_for_force_window(extend_mins)
                        if force_type == "discharge"
                        else None
                    )
                    force_power_w = (
                        self._charge_command_power_w(force_window_action)
                        if force_type == "charge"
                        else self._export_command_power_w(force_window_action)
                    )
                    if (
                        force_scope == "optimizer"
                        and force_type == "discharge"
                        and self._should_spread_export_schedule()
                    ):
                        try:
                            previous_power_w = float(
                                _ext_state.get("power_w") or 0.0
                            )
                            target_power_w = float(force_power_w)
                        except (TypeError, ValueError):
                            previous_power_w = 0.0
                            target_power_w = float(force_power_w)
                        if previous_power_w > 0 and target_power_w > previous_power_w:
                            force_power_w = previous_power_w
                    new_expiry = dt_util.utcnow() + timedelta(minutes=extend_mins)
                    hardware_expiry = self._as_utc_datetime(_ext_state.get("hardware_expires_at"))
                    supports_force_power_refresh = (
                        (
                            force_type == "charge"
                            and self._supports_target_charge_power()
                        )
                        or (
                            force_type == "discharge"
                            and self._supports_target_export_power()
                        )
                    )
                    hardware_power_changed = (
                        supports_force_power_refresh
                        and self._force_command_power_changed(
                            _ext_state.get("power_w"),
                            force_power_w,
                        )
                    )
                    if force_scope == "optimizer":
                        now = dt_util.utcnow()
                        refresh_window = timedelta(
                            minutes=max(
                                1,
                                int(getattr(self._config, "interval_minutes", 5) or 5),
                            )
                            + 1
                        )
                        should_refresh_hardware = (
                            hardware_expiry is None
                            or hardware_expiry <= now + refresh_window
                            or hardware_power_changed
                        )
                    else:
                        _ext_state["expires_at"] = new_expiry
                        should_refresh_hardware = (
                            self.battery_system != "tesla"
                            or hardware_power_changed
                        )
                    if self.battery_system == "tesla":
                        # Tesla force modes are implemented as uploaded TOU
                        # tariffs. The software timer can be extended cheaply,
                        # but the already-uploaded tariff only covers its
                        # original 30-minute-aligned window. Refresh when the
                        # desired expiry reaches beyond that hardware window.
                        should_refresh_hardware = (
                            hardware_expiry is None
                            or new_expiry > hardware_expiry - timedelta(minutes=1)
                            or hardware_power_changed
                        )
                    elif force_type == "charge":
                        should_refresh_hardware = (
                            should_refresh_hardware
                            or self._force_charge_hardware_needs_refresh(force_power_w)
                        )
                    elif force_type == "discharge":
                        should_refresh_hardware = (
                            should_refresh_hardware
                            or self._force_discharge_hardware_needs_refresh(force_power_w)
                        )

                    # Re-issue hardware writes when the hardware-side window is
                    # shorter than the extended optimizer-owned force state, or
                    # when the LP changes the target power inside the same mode.
                    if battery and hasattr(battery, "force_charge") and should_refresh_hardware:
                        try:
                            # For Modbus-backed systems, _extend_hardware
                            # re-issues the inverter countdown. For Tesla, the
                            # service falls through to the full tariff uploader
                            # so the TOU force window is rolled forward too.
                            if force_type == "charge":
                                await battery.force_charge(
                                    duration_minutes=extend_mins,
                                    power_w=force_power_w,
                                    _extend_hardware=True,
                                )
                            else:
                                await battery.force_discharge(
                                    duration_minutes=extend_mins,
                                    power_w=force_power_w,
                                    _extend_hardware=True,
                                    _tariff_duration=tariff_mins,
                                )
                            _LOGGER.debug(
                                "Optimizer: re-issued %s command for hardware refresh "
                                "(%dmin, %.0fW)",
                                force_type, extend_mins, force_power_w,
                            )
                            if force_scope == "optimizer":
                                self._set_optimizer_force_state(
                                    force_type,
                                    extend_mins,
                                    force_power_w,
                                )
                            else:
                                _ext_state["power_w"] = force_power_w
                        except Exception as ext_err:
                            _LOGGER.warning("Optimizer: failed to re-issue %s for extension: %s", force_type, ext_err)

                    if force_scope != "optimizer":
                        async def _auto_restore_extended(_now):
                            if _ext_state.get("active"):
                                _LOGGER.info("⏰ Force %s expired (extended timer), auto-restoring", force_type)
                                from ..const import DOMAIN as _SVC_DOMAIN
                                await self.hass.services.async_call(
                                    _SVC_DOMAIN, "restore_normal", {}, blocking=True,
                                )

                        from homeassistant.helpers.event import async_track_point_in_utc_time
                        _ext_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                            self.hass, _auto_restore_extended, new_expiry,
                        )
                    elif not should_refresh_hardware and hardware_expiry is not None:
                        _ext_state["expires_at"] = hardware_expiry
                    logged_expiry = self._as_utc_datetime(
                        _ext_state.get("expires_at")
                    ) or new_expiry
                    _LOGGER.debug(
                        "Optimizer: force %s active (optimizer) — LP still wants %s, "
                        "extended expiry to %s",
                        force_type, action.action,
                        logged_expiry.isoformat(),
                    )
                    return

                # LP changed its mind — cancel the optimizer's force mode.
                if action.action in SELF_USE_ACTIONS or action.action == "idle":
                    if force_type == "charge":
                        commitment_remaining = (
                            self._optimizer_force_charge_commitment_remaining(
                                force_state,
                                action,
                            )
                        )
                    else:
                        commitment_remaining = (
                            self._optimizer_force_discharge_commitment_remaining(
                                force_state,
                                action,
                            )
                        )
                    if commitment_remaining is not None:
                        remaining_minutes = max(
                            1,
                            int((commitment_remaining.total_seconds() + 59) // 60),
                        )
                        _LOGGER.info(
                            "Optimizer: Holding active force %s for %d more min "
                            "despite LP now wanting %s",
                            force_type,
                            remaining_minutes,
                            action.action,
                        )
                        return

                # Clear force state BEFORE calling restore_normal so that
                # TOU sync (triggered inside restore_normal) doesn't skip
                # due to seeing force_charge_state["active"]=True.
                _LOGGER.info(
                    "Optimizer: LP changed mind (%s → %s) — canceling optimizer-triggered "
                    "force %s to execute new action",
                    force_type, action.action, force_type,
                )
                if force_state.get("scope") == "optimizer":
                    self._clear_optimizer_force_state()
                elif self._force_state_clearer:
                    self._force_state_clearer()
                battery = self._executor.battery_controller
                if hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
                await self._restore_pre_idle_backup_reserve(
                    battery,
                    f"after canceling force {force_type}",
                )

        try:
            # During demand charge windows, override IDLE → self_consumption.
            # IDLE holds the battery and lets grid serve load, which increases
            # peak demand — the opposite of what demand charge avoidance wants.
            # Self-consumption lets the battery discharge to cover home load,
            # minimizing grid import during the demand window.
            planned_action = action.action
            effective_action = planned_action

            # --- Off-grid transition handling ---
            # If we're currently off-grid and the new action needs the grid,
            # reconnect FIRST. The contactor takes a few seconds to close.
            if self._last_executed_action == "off_grid" and effective_action != "off_grid":
                _LOGGER.info(
                    "Optimizer: transitioning from OFF_GRID → %s — "
                    "reconnecting grid first",
                    effective_action,
                )
                try:
                    from ..powerwall_local.curtailment_fallback import get_fallback
                    fallback = get_fallback(self.hass, self._entry)
                    reconnected = await fallback.release(
                        trigger_reason="optimizer_reconnect"
                    )
                    if not reconnected:
                        _LOGGER.error(
                            "Optimizer: failed to reconnect grid — "
                            "staying off-grid, skipping %s",
                            effective_action,
                        )
                        return
                except Exception as err:
                    _LOGGER.error(
                        "Optimizer: reconnect error: %s — skipping %s",
                        err, effective_action,
                    )
                    return
                # Brief pause for contactor to close
                import asyncio
                await asyncio.sleep(3)

            # Skip charge/export actions during suspected calibration
            from ..const import DOMAIN as _CAL_DOMAIN
            _cal_ed = self.hass.data.get(_CAL_DOMAIN, {}).get(self.entry_id, {})
            if _cal_ed.get("calibration_suspected") and effective_action in ("charge", "export"):
                _LOGGER.info(
                    "Optimizer: Skipping %s — calibration suspected, using self_consumption",
                    effective_action,
                )
                effective_action = "self_consumption"

            if effective_action == "idle" and self._should_disable_idle_schedule():
                _LOGGER.info(
                    "No Idle mode: overriding optimizer IDLE to self_consumption"
                )
                effective_action = "self_consumption"

            if effective_action == "idle" and self._is_in_demand_window():
                _LOGGER.info(
                    "Optimizer: Overriding IDLE → self_consumption during demand charge window"
                )
                effective_action = "self_consumption"

            # The optimizer reserve is for charge/discharge decisions only.
            # Self-consumption can continue down to the hardware reserve.
            # Only execute IDLE when SOC is well above the optimizer reserve
            # (>5% above = meaningful charge to hold for later export).
            # Otherwise use self-consumption — battery serves load naturally.
            if effective_action == "idle":
                try:
                    soc_now, _ = await self._get_battery_state()
                    opt_reserve = self._config.backup_reserve
                    if opt_reserve + 0.005 < soc_now <= opt_reserve + 0.05:
                        hw_reserve_pct = self._startup_backup_reserve or 0
                        _LOGGER.debug(
                            "Optimizer: Overriding IDLE → self_consumption — "
                            "SOC %.1f%% at optimizer reserve %.0f%%, "
                            "hardware reserve %.0f%% (%.0f%% headroom)",
                            soc_now * 100, opt_reserve * 100,
                            hw_reserve_pct, (opt_reserve * 100 - hw_reserve_pct),
                        )
                        effective_action = "self_consumption"
                except Exception:
                    pass

            if effective_action in ("discharge", "export") and self._should_block_export_for_demand():
                _LOGGER.info(
                    "Optimizer: Overriding EXPORT → self_consumption "
                    "near demand charge window (preserving battery)"
                )
                effective_action = "self_consumption"

            # Block EXPORT when export price is below threshold.
            # Without this, force_discharge can cause the battery to export
            # at a loss during negative/zero prices (e.g. Chip Mode suppression).
            if effective_action in ("discharge", "export"):
                _ep = self._last_export_prices
                if _ep:
                    _current_export = _ep[0] if _ep else 0
                    if _current_export < 0.01:  # < 1c/kWh
                        _LOGGER.info(
                            "Optimizer: Overriding %s → self_consumption — "
                            "export price %.1fc/kWh < 1c threshold",
                            effective_action, _current_export * 100,
                        )
                        effective_action = "self_consumption"

            preserve_active = self._scheduled_ev_preserve_active()
            if preserve_active and effective_action in (
                "discharge",
                "export",
                "consume",
                "self_consumption",
                "idle",
            ):
                if effective_action != "idle":
                    _LOGGER.info(
                        "Scheduled EV preserve: overriding optimizer %s → no_discharge",
                        effective_action,
                    )
                effective_action = "no_discharge"
            elif not preserve_active:
                await self._release_scheduled_ev_no_discharge_mode("preserve inactive")

            # When transitioning from IDLE to another action, immediately undo
            # what IDLE did (restore work mode and backup_reserve) before
            # executing the new LP action.
            prev = self._last_executed_action
            if prev == "idle" and effective_action != "idle":
                if (
                    self.energy_coordinator
                    and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
                ):
                    await self.energy_coordinator.restore_work_mode_from_idle()
                restored = await self._restore_pre_idle_backup_reserve(
                    battery,
                    f"exiting IDLE to {effective_action}",
                )
                if restored:
                    _LOGGER.info(
                        "Optimizer: Exiting IDLE → %s — restored reserve/work mode",
                        effective_action,
                    )
                else:
                    _LOGGER.info(
                        "Optimizer: Exiting IDLE → %s — restored work mode; "
                        "backup reserve restore is pending",
                        effective_action,
                    )
                    return

            # The optimizer backup reserve is a hard software floor for all
            # battery systems.  Once SOC reaches it, stop any forced/max
            # discharge request and return the inverter to self-consumption;
            # do not keep exporting just because the hardware min-SOC would
            # eventually stop the battery.
            if effective_action in ("discharge", "export"):
                try:
                    soc_now, _ = await self._get_battery_state()
                    opt_reserve = self._force_discharge_reserve_floor(action)
                    reaches_reserve, projected_soc = (
                        self._force_discharge_reaches_reserve(
                            action,
                            soc_now,
                            opt_reserve,
                        )
                    )
                    if reaches_reserve:
                        soc_text = (
                            f"{soc_now * 100:.1f}%"
                            if soc_now is not None
                            else "unknown"
                        )
                        projected_text = (
                            f", projected {projected_soc * 100:.1f}%"
                            if projected_soc is not None
                            else ""
                        )
                        _LOGGER.warning(
                            "Optimizer: Blocking %s — SOC %s%s at/below "
                            "optimizer reserve %.0f%%; switching to self_consumption",
                            effective_action,
                            soc_text,
                            projected_text,
                            opt_reserve * 100,
                        )
                        effective_action = "self_consumption"
                except Exception:
                    pass

            if effective_action == "charge":
                if hasattr(battery, "force_charge"):
                    if self._tesla_force_charge_should_yield_to_live_solar(action):
                        effective_action = "self_consumption"
                        if hasattr(battery, "set_self_consumption_mode"):
                            await battery.set_self_consumption_mode()
                        elif hasattr(battery, "restore_normal"):
                            await battery.restore_normal()
                    if effective_action != "charge":
                        self._last_executed_planned_action = action.action
                        self._last_executed_action = effective_action
                        return

                    charge_power_w = self._charge_command_power_w(action)
                    charge_duration = self._force_duration_for_action_window(
                        action,
                        {"charge"},
                        allow_boundary_overrun=False,
                        minimum_minutes=self._config.interval_minutes + 5,
                    )
                    # Near the demand window, shorten charge duration so the
                    # auto-restore fires 1 minute before demand starts.  The
                    # optimizer recalculates every 5 minutes and will upload a
                    # fresh tariff, so the 30-min TOU rounding is irrelevant.
                    # Within 1 minute of demand, override to self_consumption.
                    mins_to_demand = self._minutes_to_demand_start()
                    if mins_to_demand is not None and mins_to_demand <= 1:
                        _LOGGER.info(
                            "Optimizer: Blocking CHARGE — %d min to demand "
                            "window, switching to self_consumption",
                            mins_to_demand,
                        )
                        effective_action = "self_consumption"
                        if hasattr(battery, "set_self_consumption_mode"):
                            await battery.set_self_consumption_mode()
                        elif hasattr(battery, "restore_normal"):
                            await battery.restore_normal()
                    elif mins_to_demand is not None and mins_to_demand <= charge_duration:
                        charge_duration = max(1, mins_to_demand - 1)
                        _LOGGER.info(
                            "Optimizer: Shortening charge to %dmin "
                            "(%d min before demand window)",
                            charge_duration, mins_to_demand,
                        )
                        force_result = await battery.force_charge(
                            duration_minutes=charge_duration,
                            power_w=charge_power_w,
                        )
                        if force_result is not False and self.battery_system != "tesla":
                            self._set_optimizer_force_state(
                                "charge",
                                charge_duration,
                                charge_power_w,
                            )
                        _LOGGER.info(
                            "Optimizer: Charging at %.0fW for %dmin "
                            "(auto-restore before demand)",
                            charge_power_w, charge_duration,
                        )
                    else:
                        force_result = await battery.force_charge(
                            duration_minutes=charge_duration,
                            power_w=charge_power_w,
                        )
                        if force_result is not False and self.battery_system != "tesla":
                            self._set_optimizer_force_state(
                                "charge",
                                charge_duration,
                                charge_power_w,
                            )
                        _LOGGER.info("Optimizer: Charging at %.0fW", charge_power_w)
            elif effective_action in ("discharge", "export"):
                if hasattr(battery, "force_discharge"):
                    discharge_power = self._export_command_power_w(action)
                    discharge_duration = self._force_duration_for_action_window(
                        action,
                        {"discharge", "export"},
                        allow_boundary_overrun=False,
                        minimum_minutes=self._config.interval_minutes + 5,
                    )
                    tariff_duration = self._tesla_tariff_duration_for_force_window(
                        discharge_duration
                    )
                    force_result = await battery.force_discharge(
                        duration_minutes=discharge_duration,
                        power_w=discharge_power,
                        _tariff_duration=tariff_duration,
                    )
                    if force_result is not False and self.battery_system != "tesla":
                        self._set_optimizer_force_state(
                            "discharge",
                            discharge_duration,
                            discharge_power,
                        )
                    _LOGGER.info(
                        "Optimizer: Discharging/exporting at %.0fW for %dmin",
                        discharge_power, discharge_duration,
                    )
            elif effective_action == "no_discharge":
                await self._set_scheduled_ev_no_discharge_mode(
                    battery,
                    getattr(action, "action", "scheduled_ev_preserve"),
                )
            elif effective_action == "idle":
                if await self._set_idle_hold_mode(battery) is False:
                    _LOGGER.warning(
                        "Optimizer: IDLE command failed — keeping previous action "
                        "marker so the next cycle retries"
                    )
                    return
            elif effective_action == "off_grid":
                # Off-grid curtailment: physically disconnect from grid.
                # Delegates to CurtailmentFallback which enforces SOC floor,
                # daily duration cap, and pairing checks.
                #
                # The off-grid overlay only marks pre-validated contiguous
                # runs, so execution can activate immediately here.
                if self._last_executed_action == "off_grid":
                    # Already off-grid — check safety gates are still met
                    try:
                        from ..powerwall_local.curtailment_fallback import get_fallback
                        fallback = get_fallback(self.hass, self._entry)
                        still_safe = await fallback.check_safety()
                        if not still_safe:
                            _LOGGER.info(
                                "Optimizer: OFF_GRID safety check failed — "
                                "reconnected, switching to self_consumption"
                            )
                            effective_action = "self_consumption"
                            if hasattr(battery, "set_self_consumption_mode"):
                                await battery.set_self_consumption_mode()
                        else:
                            _LOGGER.debug("Optimizer: OFF_GRID — holding, safety OK")
                    except Exception as err:
                        _LOGGER.warning("Optimizer: OFF_GRID safety check error: %s", err)
                else:
                    # Go off-grid — no entry holdoff, the overlay already
                    # requires 3 consecutive eligible slots (15 min) before
                    # marking as OFF_GRID so the decision is pre-validated.
                    try:
                        from ..powerwall_local.curtailment_fallback import get_fallback
                        fallback = get_fallback(self.hass, self._entry)
                        ok = await fallback.activate(reason="optimizer_offgrid")
                        if not ok:
                            _LOGGER.info(
                                "Optimizer: OFF_GRID refused by safety gates "
                                "(SOC floor / daily cap) — using self_consumption"
                            )
                            effective_action = "self_consumption"
                            if hasattr(battery, "set_self_consumption_mode"):
                                await battery.set_self_consumption_mode()
                        else:
                            _LOGGER.info(
                                "Optimizer: OFF_GRID — physically disconnected from grid"
                            )
                    except Exception as err:
                        _LOGGER.error("Optimizer: OFF_GRID activation error: %s", err)
                        effective_action = "self_consumption"

            else:
                # self_consumption or consume — let battery operate naturally.
                #
                # For Tesla: keep the hardware backup_reserve aligned with the
                # user's hardware reserve, not the optimizer floor. The LP floor
                # is a software scheduling boundary; temporarily raising Tesla's
                # hardware reserve to that floor can show up in the Tesla app and
                # can trigger grid charging when SOC is below the floor.
                #
                # Off-grid exit is handled by the reconnect transition
                # block at the top of this method — no additional holdoff
                # needed since the overlay already pre-validated run length.

                if effective_action != "off_grid":
                    apply_self_consumption = self._last_executed_action != "self_consumption"
                    reapply_backup_reserve = False
                    sungrow_reapply_reserve_pct: int | None = None
                    sungrow_inferred_restore = False
                    configured_reserve_pct = int(self._config.backup_reserve * 100)
                    reserve_pct: int | None = None
                    if not apply_self_consumption:
                        # Verify the hardware mode has not drifted. On HA restart
                        # Tesla can remain in autonomous while the optimizer's
                        # last action marker is already self_consumption.
                        if hasattr(battery, "get_tesla_operation_mode"):
                            hw_mode = await battery.get_tesla_operation_mode()
                            if hw_mode is not None and hw_mode != "self_consumption":
                                _LOGGER.info(
                                    "Optimizer: Tesla mode is '%s' while LP action is "
                                    "self_consumption — reapplying self-consumption mode",
                                    hw_mode,
                                )
                                apply_self_consumption = True
                        if (
                            self.battery_system == "tesla"
                            and hasattr(battery, "get_backup_reserve")
                        ):
                            soc_now, _ = await self._get_battery_state()
                            soc_pct = max(0, min(100, int(soc_now * 100)))
                            reserve_pct = (
                                self._startup_backup_reserve
                                if self._startup_backup_reserve is not None
                                else configured_reserve_pct
                            )
                            reserve_pct = max(0, min(100, reserve_pct))
                            if 81 <= reserve_pct <= 99:
                                reserve_pct = 80
                            if soc_pct < reserve_pct:
                                reserve_pct = min(reserve_pct, soc_pct)
                                if 81 <= reserve_pct <= 99:
                                    reserve_pct = 80
                            current_reserve_trust = None
                            if hasattr(battery, "read_backup_reserve"):
                                current_reserve_reading = await battery.read_backup_reserve()
                                current_reserve = current_reserve_reading.percent
                                current_reserve_trust = current_reserve_reading.trust
                            else:
                                current_reserve = await battery.get_backup_reserve()
                            if (
                                current_reserve is not None
                                and reserve_pct is not None
                                and current_reserve != reserve_pct
                            ):
                                if current_reserve == 100 and reserve_pct < current_reserve:
                                    _LOGGER.info(
                                        "Optimizer: Tesla backup_reserve=100%% while target "
                                        "self-consumption reserve is %d%% — treating it as "
                                        "stale force-charge state and reapplying",
                                        reserve_pct,
                                    )
                                    reapply_backup_reserve = True
                                elif (
                                    self._pre_idle_backup_reserve is None
                                    and self._idle_hold_reserve is None
                                    and current_reserve > reserve_pct
                                    and current_reserve <= soc_pct
                                    and (
                                        current_reserve_trust is None
                                        or current_reserve_trust in TRUSTED_FOR_PERSIST
                                    )
                                ):
                                    previous_reserve_pct = reserve_pct
                                    self._startup_backup_reserve = current_reserve
                                    if self._optimizer:
                                        self._optimizer.update_hardware_reserve(
                                            current_reserve / 100
                                        )
                                    reserve_pct = current_reserve
                                    _LOGGER.info(
                                        "Optimizer: detected Tesla backup_reserve=%d%% "
                                        "above cached target %d%% while SOC=%d%%; "
                                        "treating it as the current hardware reserve",
                                        current_reserve,
                                        previous_reserve_pct,
                                        soc_pct,
                                    )
                                else:
                                    _LOGGER.info(
                                        "Optimizer: backup_reserve is %d%% while target "
                                        "self-consumption reserve is %d%% — reapplying",
                                        current_reserve,
                                        reserve_pct,
                                    )
                                    reapply_backup_reserve = True
                        if self.battery_system == "goodwe" and self.energy_coordinator:
                            coord_data = getattr(self.energy_coordinator, "data", None) or {}
                            try:
                                grid_kw = float(coord_data.get("grid_power", 0) or 0)
                                battery_kw = float(coord_data.get("battery_power", 0) or 0)
                            except (TypeError, ValueError):
                                grid_kw = 0.0
                                battery_kw = 0.0
                            if grid_kw < -0.5 and battery_kw > 0.5:
                                _LOGGER.info(
                                    "Optimizer: GoodWe is exporting %.2fkW to grid while "
                                    "discharging battery %.2fkW in self_consumption — "
                                    "reapplying self-consumption mode",
                                    abs(grid_kw),
                                    battery_kw,
                                )
                                apply_self_consumption = True
                        if self.battery_system == "sungrow" and self.energy_coordinator:
                            coord_data = getattr(self.energy_coordinator, "data", None) or {}

                            def _coord_float(*keys: str) -> float | None:
                                for key in keys:
                                    try:
                                        value = coord_data.get(key)
                                        if value is None:
                                            continue
                                        return float(value)
                                    except (TypeError, ValueError):
                                        continue
                                return None

                            mode_value = (
                                coord_data.get("ems_mode_name")
                                or coord_data.get("mode")
                                or coord_data.get("work_mode")
                            )
                            mode = str(mode_value or "").strip().lower()
                            charge_cmd = coord_data.get("charge_cmd")
                            try:
                                charge_cmd_int = (
                                    int(charge_cmd)
                                    if charge_cmd is not None
                                    else None
                                )
                            except (TypeError, ValueError):
                                charge_cmd_int = None
                            if mode == "forced" or charge_cmd_int in (0xAA, 0xBB):
                                _LOGGER.info(
                                    "Optimizer: Sungrow still reports forced mode "
                                    "(mode=%s, charge_cmd=%s) while LP action is "
                                    "self_consumption — reapplying restore_normal",
                                    mode_value,
                                    charge_cmd,
                                )
                                apply_self_consumption = True
                            elif (
                                hasattr(
                                    self.energy_coordinator,
                                    "_discharge_appears_blocked_after_restore",
                                )
                                and self.energy_coordinator._discharge_appears_blocked_after_restore()
                            ):
                                last_inferred_restore = getattr(
                                    self,
                                    "_last_sungrow_inferred_restore_at",
                                    None,
                                )
                                now = dt_util.utcnow()
                                if (
                                    last_inferred_restore is None
                                    or now - last_inferred_restore
                                    >= SUNGROW_INFERRED_RESTORE_COOLDOWN
                                ):
                                    _LOGGER.info(
                                        "Optimizer: Sungrow appears discharge-blocked while "
                                        "LP action is self_consumption — reapplying "
                                        "restore_normal"
                                    )
                                    apply_self_consumption = True
                                    sungrow_inferred_restore = True
                                else:
                                    _LOGGER.debug(
                                        "Optimizer: Sungrow inferred restore is in cooldown — "
                                        "skipping redundant restore_normal"
                                    )
                            else:
                                battery_kw = _coord_float("battery_power", "battery_power_kw")
                                grid_kw = _coord_float("grid_power", "grid_power_kw")
                                load_kw = _coord_float("load_power", "home_load")
                                soc_pct_float = _coord_float("battery_level", "battery_soc")
                                current_reserve = _coord_float("backup_reserve", "min_soc")
                                target_reserve = self._startup_backup_reserve
                                grid_serving_load = (
                                    grid_kw is not None
                                    and grid_kw >= 0.15
                                    and (
                                        load_kw is None
                                        or (
                                            load_kw >= 0.15
                                            and grid_kw >= load_kw * 0.6
                                        )
                                    )
                                )
                                if (
                                    target_reserve is not None
                                    and current_reserve is not None
                                    and soc_pct_float is not None
                                    and battery_kw is not None
                                    and abs(battery_kw) <= 0.1
                                    and grid_serving_load
                                    and current_reserve > target_reserve
                                    and soc_pct_float <= current_reserve + 2.0
                                    and soc_pct_float > target_reserve + 2.0
                                ):
                                    sungrow_reapply_reserve_pct = max(
                                        0, min(100, int(target_reserve))
                                    )
                                    _LOGGER.info(
                                        "Optimizer: Sungrow reserve/min-SOC is %.1f%% "
                                        "while cached hardware reserve is %d%% and "
                                        "battery is not discharging; reapplying "
                                        "self-consumption reserve",
                                        current_reserve,
                                        sungrow_reapply_reserve_pct,
                                    )
                                    apply_self_consumption = True
                        if not apply_self_consumption and not reapply_backup_reserve:
                            _LOGGER.debug(
                                "Optimizer: Already in self-consumption mode — "
                                "skipping redundant API call"
                            )
                    mode_apply_failed = False
                    if apply_self_consumption or reapply_backup_reserve:
                        if hasattr(battery, "set_self_consumption_mode"):
                            if apply_self_consumption:
                                if await battery.set_self_consumption_mode() is False:
                                    mode_apply_failed = True
                        elif hasattr(battery, "restore_normal"):
                            if apply_self_consumption:
                                if await battery.restore_normal() is False:
                                    mode_apply_failed = True
                        if sungrow_inferred_restore:
                            self._last_sungrow_inferred_restore_at = dt_util.utcnow()
                        if (
                            sungrow_reapply_reserve_pct is not None
                            and hasattr(battery, "set_backup_reserve")
                        ):
                            await battery.set_backup_reserve(sungrow_reapply_reserve_pct)
                        # Tesla only: reset hardware backup_reserve to prevent
                        # grid charging when the user's hardware reserve
                        # (restored by restore_normal after force_discharge) is
                        # above the current SOC. Modbus batteries such as GoodWe
                        # expose this as a real hardware/DOD setting, so ordinary
                        # self-consumption must not rewrite it to the LP floor.
                        if (
                            self.battery_system == "tesla"
                            and hasattr(battery, "set_backup_reserve")
                        ):
                            if reserve_pct is None:
                                soc_now, _ = await self._get_battery_state()
                                soc_pct = max(0, min(100, int(soc_now * 100)))
                                reserve_pct = (
                                    self._startup_backup_reserve
                                    if self._startup_backup_reserve is not None
                                    else configured_reserve_pct
                                )
                                reserve_pct = max(0, min(100, reserve_pct))
                                if 81 <= reserve_pct <= 99:
                                    reserve_pct = 80
                                if soc_pct < reserve_pct:
                                    reserve_pct = min(reserve_pct, soc_pct)
                                    if 81 <= reserve_pct <= 99:
                                        reserve_pct = 80
                            await battery.set_backup_reserve(reserve_pct)
                            _LOGGER.info(
                                "Optimizer: self_consumption — set backup_reserve=%d%% "
                                "(startup=%s%%, floor=%d%%, current_soc=%d%%)",
                                reserve_pct,
                                (
                                    self._startup_backup_reserve
                                    if self._startup_backup_reserve is not None
                                    else "?"
                                ),
                                configured_reserve_pct,
                                soc_pct,
                            )
                    if mode_apply_failed:
                        # Do not record success: the base BatteryController
                        # returns False instead of raising, and advancing the
                        # marker here masked the failure — the change-detection
                        # above then skipped the command forever, leaving the
                        # inverter in its prior forced mode. Keeping the old
                        # marker makes the next cycle retry.
                        _LOGGER.warning(
                            "Optimizer: self-consumption mode command failed — "
                            "keeping previous action marker so the next cycle retries"
                        )
                        return
                    _LOGGER.debug("Optimizer: Self-consumption mode (action=%s)", effective_action)

            self._last_executed_planned_action = planned_action
            self._last_executed_action = effective_action

        except Exception as e:
            _LOGGER.error("Failed to execute optimizer action: %s", e)

    def _battery_export_allowed_slots(
        self,
        n: int,
        export_prices: list[float] | None = None,
    ) -> bool | list[bool]:
        """Return per-slot permission for intentional battery-to-grid export."""
        if n <= 0:
            return []

        allowed = [False] * n

        slot_sources = [
            self._flow_power_profit_export_slots(n),
            self._export_boost_mask_for_run(n, export_prices),
            self._saving_session_export_slots(n),
        ]
        zerohero_config = self._zerohero_config()
        zerohero_cap = self._last_zerohero_bonus_cap_kwh
        if zerohero_config is not None:
            if zerohero_cap is not None and zerohero_cap > 1e-6:
                slot_sources.append(self._zerohero_window_slots(n))
        else:
            slot_sources.insert(0, self._positive_price_export_slots(n, export_prices))

        for slots in slot_sources:
            for idx, value in enumerate(slots[:n]):
                allowed[idx] = allowed[idx] or value

        allowed_count = sum(allowed)
        if allowed_count:
            _LOGGER.debug(
                "Battery export allowed in %d/%d optimizer intervals",
                allowed_count,
                n,
            )
        return allowed

    def _priority_export_slots_for_run(
        self,
        n: int,
        export_prices: list[float] | None = None,
    ) -> list[bool]:
        """Return explicit export windows that may override self-consumption."""
        if n <= 0:
            return []

        allowed = [False] * n
        slot_sources = [
            self._flow_power_export_window_slots(n),
            self._export_boost_mask_for_run(n, export_prices),
            self._saving_session_export_slots(n),
        ]
        zerohero_config = self._zerohero_config()
        zerohero_cap = self._last_zerohero_bonus_cap_kwh
        if zerohero_config is not None and zerohero_cap is not None and zerohero_cap > 1e-6:
            slot_sources.append(self._zerohero_window_slots(n))

        for slots in slot_sources:
            for idx, value in enumerate(slots[:n]):
                allowed[idx] = allowed[idx] or value

        allowed_count = sum(allowed)
        if allowed_count:
            _LOGGER.debug(
                "Priority export enabled in %d/%d optimizer intervals",
                allowed_count,
                n,
            )
        return allowed

    def _should_spread_export_schedule(self) -> bool:
        """Return True when optimizer export actions should be flattened."""
        return (
            self._config.spread_export_enabled
            and self._supports_target_export_power()
        )

    def _should_spread_import_schedule(self) -> bool:
        """Return True when optimizer charge actions should be flattened."""
        return (
            self._config.spread_import_enabled
            and self._supports_target_charge_power()
        )

    def _spread_import_schedule(
        self,
        schedule: OptimizationSchedule,
        import_prices: list[float] | None,
        blocked_slots: list[bool] | None,
        initial_soc: float,
        *,
        free_only: bool = False,
        solar_forecast: list[float] | None = None,
        load_forecast: list[float] | None = None,
    ) -> OptimizationSchedule:
        """Spread planned grid-charge energy across same-price import windows."""
        actions = list(schedule.actions or [])
        if not actions or not import_prices:
            return schedule

        n = len(actions)
        try:
            prices = [float(price) for price in import_prices[:n]]
        except (TypeError, ValueError):
            return schedule
        if len(prices) < n:
            return schedule

        blocked = [bool(value) for value in (blocked_slots or [])[:n]]
        if len(blocked) < n:
            blocked.extend([False] * (n - len(blocked)))

        interval_hours = max(1, int(self._config.interval_minutes or 5)) / 60.0
        capacity_wh = max(0.0, float(self._config.battery_capacity_wh or 0))
        efficiency = float(getattr(self._optimizer, "efficiency", 0.92) or 0.92)
        max_charge_w = max(0.0, float(self._config.max_charge_w or 0))
        max_grid_import_w = self._normalize_optional_power_w(
            self._config.max_grid_import_w
        )
        cap_by_slot = max_grid_import_w is not None
        new_actions: list[ScheduleAction] = list(actions)
        soc_cursor = max(0.0, min(1.0, float(initial_soc or 0.0)))

        def _forecast_kw(values: list[float] | None, pos: int) -> float:
            if not values or pos >= len(values):
                return 0.0
            try:
                return float(values[pos])
            except (TypeError, ValueError):
                return 0.0

        def _slot_charge_cap_w(pos: int) -> float:
            if max_grid_import_w is None:
                return max_charge_w
            load_w = _forecast_kw(load_forecast, pos) * 1000.0
            solar_w = _forecast_kw(solar_forecast, pos) * 1000.0
            return max(
                0.0,
                min(max_charge_w, max_grid_import_w - load_w + solar_w),
            )

        def _spread_power_by_cap(total_wh: float, caps_w: list[float]) -> list[float]:
            """Spread total Wh evenly while respecting per-slot caps."""
            if not caps_w:
                return []
            remaining = min(total_wh, sum(caps_w) * interval_hours)
            output = [0.0] * len(caps_w)
            open_slots = set(range(len(caps_w)))
            while open_slots and remaining > 1e-6:
                target_w = remaining / (len(open_slots) * interval_hours)
                capped_now = [
                    pos for pos in open_slots if caps_w[pos] <= target_w + 1e-6
                ]
                if not capped_now:
                    for pos in open_slots:
                        output[pos] = target_w
                    break
                for pos in capped_now:
                    output[pos] = caps_w[pos]
                    remaining -= caps_w[pos] * interval_hours
                    open_slots.remove(pos)
            return [round(max(0.0, value), 1) for value in output]

        def _advance_soc(soc: float, action: Any) -> float:
            if capacity_wh <= 0:
                return soc
            try:
                charge_w = max(0.0, float(getattr(action, "battery_charge_w", 0.0) or 0.0))
                discharge_w = max(0.0, float(getattr(action, "battery_discharge_w", 0.0) or 0.0))
            except (TypeError, ValueError):
                return soc
            stored_wh = charge_w * interval_hours * efficiency
            removed_wh = discharge_w * interval_hours / max(efficiency, 0.001)
            return max(0.0, min(1.0, soc + (stored_wh - removed_wh) / capacity_wh))

        idx = 0
        while idx < n:
            if blocked[idx] or getattr(actions[idx], "action", None) in ("discharge", "export"):
                soc_cursor = _advance_soc(soc_cursor, new_actions[idx])
                idx += 1
                continue

            start = idx
            price = prices[idx]
            while (
                idx < n
                and not blocked[idx]
                and getattr(actions[idx], "action", None) not in ("discharge", "export")
                and abs(prices[idx] - price) <= 1e-6
            ):
                idx += 1
            end = idx
            if free_only and not (math.isfinite(price) and price <= 0.001):
                for pos in range(start, end):
                    soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                continue

            window_actions = actions[start:end]
            charge_wh = sum(
                max(0.0, float(getattr(action, "battery_charge_w", 0.0) or 0.0))
                * interval_hours
                for action in window_actions
                if getattr(action, "action", None) == "charge"
            )
            if charge_wh <= 0 or max_charge_w <= 0:
                for pos in range(start, end):
                    soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                continue

            if price <= 0.001 and capacity_wh > 0:
                available_wh = max(0.0, (1.0 - soc_cursor) * capacity_wh / max(efficiency, 0.001))
                charge_wh = min(charge_wh, available_wh)
                if charge_wh <= 0:
                    for pos in range(start, end):
                        soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                    continue

            if cap_by_slot:
                target_by_pos = _spread_power_by_cap(
                    charge_wh,
                    [_slot_charge_cap_w(pos) for pos in range(start, end)],
                )
            else:
                target_w = min(
                    max_charge_w,
                    charge_wh / (len(window_actions) * interval_hours),
                )
                target_w = round(max(0.0, target_w), 1)
                target_by_pos = [target_w] * len(window_actions)

            if not any(target_w > 0 for target_w in target_by_pos):
                for pos in range(start, end):
                    soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                continue

            for pos in range(start, end):
                original = actions[pos]
                target_w = target_by_pos[pos - start]
                if target_w > 0:
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="charge",
                        power_w=target_w,
                        soc=original.soc,
                        battery_charge_w=target_w,
                        battery_discharge_w=0.0,
                    )
                else:
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="self_consumption",
                        power_w=0.0,
                        soc=original.soc,
                        battery_charge_w=0.0,
                        battery_discharge_w=0.0,
                    )
                soc_cursor = _advance_soc(soc_cursor, new_actions[pos])

        return OptimizationSchedule(
            actions=new_actions,
            predicted_cost=schedule.predicted_cost,
            predicted_savings=schedule.predicted_savings,
            last_updated=schedule.last_updated,
        )

    def _spread_export_schedule(
        self,
        schedule: OptimizationSchedule,
        allowed_slots: bool | list[bool],
        export_reserve_floor: float | list[float] | None = None,
    ) -> OptimizationSchedule:
        """Spread planned export energy across each contiguous allowed window."""
        actions = list(schedule.actions or [])
        if not actions:
            return schedule

        n = len(actions)
        if isinstance(allowed_slots, bool):
            allowed = [allowed_slots] * n
        else:
            allowed = [bool(v) for v in allowed_slots[:n]]
            if len(allowed) < n:
                allowed.extend([False] * (n - len(allowed)))

        interval_hours = max(1, int(self._config.interval_minutes or 5)) / 60.0
        capacity_wh = max(0.0, float(self._config.battery_capacity_wh or 0))
        efficiency = float(getattr(self._optimizer, "efficiency", 0.92) or 0.92)
        scoped_export_floors = (
            export_reserve_floor if isinstance(export_reserve_floor, list) else None
        )
        min_export_floor = (
            None
            if scoped_export_floors is not None
            else self._reserve_ratio(export_reserve_floor, None)
        )
        if min_export_floor is None and scoped_export_floors is None:
            min_export_floor = self._force_discharge_reserve_floor()
        new_actions: list[ScheduleAction] = list(actions)
        idx = 0

        def _action_soc(pos: int) -> float | None:
            if pos < 0 or pos >= len(new_actions):
                return None
            return self._reserve_ratio(getattr(new_actions[pos], "soc", None), None)

        def _advance_export_soc(soc: float, export_w: float) -> float:
            if capacity_wh <= 0:
                return soc
            removed_wh = max(0.0, export_w) * interval_hours / max(efficiency, 0.001)
            return max(0.0, min(1.0, soc - removed_wh / capacity_wh))

        def _available_export_w(soc: float, floor: float) -> float:
            if capacity_wh <= 0:
                return 0.0
            available_wh = max(0.0, soc - floor) * capacity_wh
            return available_wh * max(efficiency, 0.001) / interval_hours

        while idx < n:
            if not allowed[idx]:
                idx += 1
                continue

            start = idx
            while idx < n and allowed[idx]:
                idx += 1
            end = idx
            window_floor = min_export_floor
            if scoped_export_floors is not None:
                scoped_window = scoped_export_floors[start:end]
                scoped_floor = max(scoped_window) if scoped_window else 0.0
                window_floor = (
                    scoped_floor
                    if scoped_floor > 0
                    else self._force_discharge_reserve_floor()
                )
            window_actions = actions[start:end]
            export_power_field = (
                "power_w"
                if self._supports_target_export_power()
                else "battery_discharge_w"
            )
            export_wh = sum(
                max(0.0, float(getattr(action, export_power_field, 0.0) or 0.0))
                * interval_hours
                for action in window_actions
                if getattr(action, "action", None) in ("export", "discharge")
            )
            if export_wh <= 0:
                continue

            spread_positions = [
                pos
                for pos in range(start, end)
                if getattr(actions[pos], "action", None) != "charge"
                and not (
                    float(getattr(actions[pos], "battery_charge_w", 0.0) or 0.0) > 0
                )
            ]
            floor = self._reserve_ratio(window_floor, None)
            if floor is not None and any(_action_soc(pos) is not None for pos in spread_positions):
                spread_positions = [
                    pos
                    for pos in spread_positions
                    if (
                        self._reserve_ratio(
                            getattr(actions[pos], "soc", None),
                            None,
                        )
                        or 0.0
                    )
                    > floor + 0.0001
                ]
                if not spread_positions:
                    fallback_soc = _action_soc(start - 1)
                    if fallback_soc is None:
                        fallback_soc = floor
                    for pos in range(start, end):
                        original = actions[pos]
                        if getattr(original, "action", None) in ("export", "discharge"):
                            new_actions[pos] = ScheduleAction(
                                timestamp=original.timestamp,
                                action="self_consumption",
                                power_w=0.0,
                                soc=(
                                    round(fallback_soc, 4)
                                    if fallback_soc is not None
                                    else original.soc
                                ),
                                battery_charge_w=0.0,
                                battery_discharge_w=0.0,
                            )
                    continue

            export_cap_w = (
                self._config.max_grid_export_w
                if self._config.max_grid_export_w is not None
                else self._config.max_discharge_w
            )
            target_w = min(
                float(max(0, export_cap_w)),
                export_wh / (len(spread_positions) * interval_hours),
            )
            target_w = round(max(0.0, target_w), 1)
            if target_w <= 0:
                fallback_soc = _action_soc(start - 1)
                if fallback_soc is None:
                    fallback_soc = _action_soc(start)
                for pos in spread_positions:
                    original = actions[pos]
                    if getattr(original, "action", None) in ("export", "discharge"):
                        new_actions[pos] = ScheduleAction(
                            timestamp=original.timestamp,
                            action="self_consumption",
                            power_w=0.0,
                            soc=(
                                round(fallback_soc, 4)
                                if fallback_soc is not None
                                else original.soc
                            ),
                            battery_charge_w=0.0,
                            battery_discharge_w=0.0,
                        )
                continue

            soc_cursor = _action_soc(start - 1)
            if soc_cursor is None:
                soc_cursor = _action_soc(start)
            for pos in spread_positions:
                original = actions[pos]
                slot_target_w = target_w
                if floor is not None and soc_cursor is not None:
                    slot_target_w = min(
                        slot_target_w,
                        _available_export_w(soc_cursor, floor),
                    )
                    slot_target_w = round(max(0.0, slot_target_w), 1)
                if slot_target_w > 0:
                    soc_after = (
                        _advance_export_soc(soc_cursor, slot_target_w)
                        if soc_cursor is not None
                        else original.soc
                    )
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="export",
                        power_w=slot_target_w,
                        soc=round(soc_after, 4) if soc_cursor is not None else original.soc,
                        battery_charge_w=0.0,
                        battery_discharge_w=slot_target_w,
                    )
                    if soc_cursor is not None:
                        soc_cursor = soc_after
                else:
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="self_consumption",
                        power_w=0.0,
                        soc=(
                            round(soc_cursor, 4)
                            if soc_cursor is not None
                            else original.soc
                        ),
                        battery_charge_w=0.0,
                        battery_discharge_w=0.0,
                    )
            for pos in range(start, end):
                if pos in spread_positions:
                    continue
                original = actions[pos]
                if getattr(original, "action", None) in ("export", "discharge"):
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="self_consumption",
                        power_w=0.0,
                        soc=(
                            round(soc_cursor, 4)
                            if soc_cursor is not None
                            else original.soc
                        ),
                        battery_charge_w=0.0,
                        battery_discharge_w=0.0,
                    )

        return OptimizationSchedule(
            actions=new_actions,
            predicted_cost=schedule.predicted_cost,
            predicted_savings=schedule.predicted_savings,
            last_updated=schedule.last_updated,
        )

    def _battery_charge_blocked_slots(self, n: int) -> list[bool]:
        """Return per-slot blocks where the LP must not charge the battery."""
        if n <= 0:
            return []

        blocked = self._flow_power_export_window_slots(n)
        zerohero_config = self._zerohero_config()
        if zerohero_config is not None and not self._zerohero_credit_lost():
            zerohero_window = self._zerohero_window_slots(n)
            for idx, value in enumerate(zerohero_window[:n]):
                blocked[idx] = blocked[idx] or value

        blocked_count = sum(blocked)
        if blocked_count:
            _LOGGER.debug(
                "Battery charge blocked in %d/%d optimizer intervals",
                blocked_count,
                n,
            )
        return blocked

    def _time_window_slots(
        self,
        n: int,
        start_time: str,
        end_time: str,
        prices: list[float] | None = None,
        threshold: float | None = None,
    ) -> list[bool]:
        """Return slots inside a local time window, optionally price-gated."""
        try:
            sh, sm = map(int, start_time.split(":"))
            eh, em = map(int, end_time.split(":"))
        except (ValueError, IndexError):
            return [False] * n

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        interval = self._config.interval_minutes
        now = dt_util.now()
        result = [False] * n

        for t in range(n):
            if (
                prices is not None
                and threshold is not None
                and (t >= len(prices) or prices[t] < threshold)
            ):
                continue

            ts = now + timedelta(minutes=t * interval)
            minutes_of_day = ts.hour * 60 + ts.minute
            if end_min <= start_min:
                in_window = minutes_of_day >= start_min or minutes_of_day < end_min
            else:
                in_window = start_min <= minutes_of_day < end_min
            result[t] = in_window

        return result

    def _flow_power_profit_export_slots(self, n: int) -> list[bool]:
        """Allow Flow Power profit exports only during Happy Hour."""
        if not self._config.profit_max_enabled or self._provider_key() != "flow_power":
            return [False] * n
        return self._flow_power_export_window_slots(n)

    def _flow_power_export_window_slots(self, n: int) -> list[bool]:
        """Return Flow Power's fixed daily export window slots."""
        if self._provider_key() != "flow_power":
            return [False] * n
        if not self._entry:
            return [False] * n

        from ..const import (
            CONF_FLOW_POWER_EXPORT_RATE,
            CONF_FLOW_POWER_STATE,
            FLOW_POWER_EXPORT_RATES,
        )

        state = self._entry.options.get(
            CONF_FLOW_POWER_STATE,
            self._entry.data.get(CONF_FLOW_POWER_STATE, ""),
        )
        configured_rate = self._entry.options.get(
            CONF_FLOW_POWER_EXPORT_RATE,
            self._entry.data.get(CONF_FLOW_POWER_EXPORT_RATE),
        )
        try:
            happy_rate = (
                float(configured_rate) / 100
                if configured_rate not in (None, "")
                else FLOW_POWER_EXPORT_RATES.get(state, 0.0)
            )
        except (ValueError, TypeError):
            happy_rate = FLOW_POWER_EXPORT_RATES.get(state, 0.0)

        if happy_rate <= 0:
            return [False] * n

        return self._time_window_slots(n, "17:30", "19:30")

    def _positive_price_export_slots(
        self,
        n: int,
        export_prices: list[float] | None,
    ) -> list[bool]:
        """Allow battery exports for any provider with positive sell prices."""
        if not export_prices:
            return [False] * n

        allowed: list[bool] = []
        for price in export_prices[:n]:
            try:
                allowed.append(float(price or 0.0) > 0.0)
            except (TypeError, ValueError):
                allowed.append(False)
        if len(allowed) < n:
            allowed.extend([False] * (n - len(allowed)))
        allowed_count = sum(allowed)

        if allowed_count:
            _LOGGER.debug(
                "Battery export: allowing %d/%d intervals with positive sell price",
                allowed_count,
                n,
            )
        return allowed

    def _export_boost_allowed_slots(
        self,
        n: int,
        export_prices: list[float] | None,
    ) -> list[bool]:
        """Return slots where export boost explicitly allows battery export."""
        if not self._entry:
            return [False] * n

        from ..const import (
            CONF_EXPORT_BOOST_ENABLED,
            CONF_EXPORT_BOOST_START,
            CONF_EXPORT_BOOST_END,
            CONF_EXPORT_BOOST_THRESHOLD,
            DEFAULT_EXPORT_BOOST_START,
            DEFAULT_EXPORT_BOOST_END,
            DEFAULT_EXPORT_BOOST_THRESHOLD,
        )

        opts = getattr(self._entry, "options", {}) or {}
        data = getattr(self._entry, "data", {}) or {}
        if not opts.get(CONF_EXPORT_BOOST_ENABLED, data.get(CONF_EXPORT_BOOST_ENABLED, False)):
            return [False] * n

        boost_start = opts.get(CONF_EXPORT_BOOST_START, DEFAULT_EXPORT_BOOST_START)
        boost_end = opts.get(CONF_EXPORT_BOOST_END, DEFAULT_EXPORT_BOOST_END)
        threshold = (
            opts.get(CONF_EXPORT_BOOST_THRESHOLD, DEFAULT_EXPORT_BOOST_THRESHOLD)
            or 0
        ) / 100

        return self._time_window_slots(
            n,
            boost_start,
            boost_end,
            export_prices,
            threshold,
        )

    def _export_boost_mask_for_run(
        self,
        n: int,
        export_prices: list[float] | None,
    ) -> list[bool]:
        """Return the export boost mask produced during price preparation."""
        last_mask = getattr(self, "_last_export_boost_allowed_slots", [])
        if len(last_mask) == n:
            return list(last_mask)
        return self._export_boost_allowed_slots(n, export_prices)

    def _saving_session_export_slots(self, n: int) -> list[bool]:
        """Allow battery export only for joined Octopus saving sessions."""
        allowed = [False] * n
        data = getattr(self._saving_session_coordinator, "data", None)
        sessions = data.get("sessions", []) if isinstance(data, dict) else []
        if not sessions:
            return allowed

        interval = self._config.interval_minutes
        now = dt_util.now()
        for session in sessions:
            if (
                not getattr(session, "joined", False)
                or getattr(session, "session_type", None) != "saving"
            ):
                continue

            start = getattr(session, "start", None)
            end = getattr(session, "end", None)
            if start is None or end is None:
                continue
            if getattr(start, "tzinfo", None) is None:
                start = start.replace(tzinfo=dt_util.UTC)
            if getattr(end, "tzinfo", None) is None:
                end = end.replace(tzinfo=dt_util.UTC)

            for t in range(n):
                ts = now + timedelta(minutes=t * interval)
                ts_utc = (
                    ts.astimezone(dt_util.UTC)
                    if getattr(ts, "tzinfo", None) is not None
                    else ts.replace(tzinfo=dt_util.UTC)
                )
                if start <= ts_utc < end:
                    allowed[t] = True

        return allowed

    def _apply_export_boost(
        self,
        export_prices: list[float],
        import_prices: list[float] | None = None,
    ) -> tuple[list[float], list[bool]]:
        """Apply export boost to LP export prices during configured window.

        Increases export prices by offset and applies a minimum floor so the LP
        is more willing to discharge during the boost window. Mirrors the Tesla
        tariff pipeline logic but operates on flat 5-min price arrays.

        Anti-arbitrage guard: caps boosted prices so the LP never sees profitable
        grid→battery→grid arbitrage that doesn't exist at real export prices.
        Without this, the LP may import from grid to charge the battery for later
        export at the inflated boosted price — a net loss at real prices.
        """
        allowed_slots = [False] * len(export_prices)
        if not self._entry:
            self._last_export_boost_allowed_slots = allowed_slots
            return export_prices, allowed_slots

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
            self._last_export_boost_allowed_slots = allowed_slots
            return export_prices, allowed_slots

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
            self._last_export_boost_allowed_slots = allowed_slots
            return export_prices, allowed_slots

        # Anti-arbitrage cap: the boosted export price must not create phantom
        # arbitrage where the LP charges from grid to export at inflated prices.
        # Cap = max(real_export, cheapest_import / round_trip_efficiency²)
        # This allows discharge of existing/solar charge at boosted prices
        # but prevents grid-charge-then-export from appearing profitable.
        eff = 0.92  # round-trip efficiency (matches optimizer default)
        arbitrage_cap = None
        if import_prices:
            min_import = min(p for p in import_prices if p > 0.001) if any(p > 0.001 for p in import_prices) else 0
            if min_import > 0:
                arbitrage_cap = min_import / (eff * eff)

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        interval = self._config.interval_minutes
        now = dt_util.now()
        boosted = 0
        capped = 0

        result = list(export_prices)
        allowed_slots = self._export_boost_allowed_slots(len(result), export_prices)
        self._last_export_boost_allowed_slots = allowed_slots
        for t in range(len(result)):
            ts = now + timedelta(minutes=t * interval)
            minutes_of_day = ts.hour * 60 + ts.minute

            # Check if in boost window (handles overnight wrap)
            if end_min <= start_min:
                in_window = minutes_of_day >= start_min or minutes_of_day < end_min
            else:
                in_window = start_min <= minutes_of_day < end_min

            if in_window and allowed_slots[t]:
                real_price = result[t]
                boosted_price = max(real_price + offset, min_price)

                # Anti-arbitrage cap: only restrict the boost when it would
                # create PHANTOM arbitrage that doesn't exist at real prices.
                # If real_price >= arb_cap, real arbitrage is already profitable
                # so the full boost is safe (no phantom incentive to grid-charge).
                if (arbitrage_cap is not None
                        and real_price < arbitrage_cap
                        and boosted_price > arbitrage_cap):
                    boosted_price = arbitrage_cap
                    capped += 1

                result[t] = boosted_price
                boosted += 1

        if boosted:
            cap_msg = f", {capped} capped by anti-arbitrage" if capped else ""
            _LOGGER.debug(
                "Export boost: boosted %d intervals (offset=%.1fc, min=%.1fc, "
                "window=%s-%s, arb_cap=%.1fc%s)",
                boosted, offset * 100, min_price * 100, boost_start, boost_end,
                (arbitrage_cap or 0) * 100, cap_msg,
            )

        return result, allowed_slots

    def _apply_saving_session_prices(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> tuple[list[float], list[float]]:
        """Overlay saving session rates onto LP prices.

        Saving sessions: massive export boost (octopoints rate >> normal export).
        Free electricity: import price -> 0 (free grid power).
        """
        if not self._saving_session_coordinator or not self._saving_session_coordinator.data:
            return import_prices, export_prices

        sessions = self._saving_session_coordinator.data.get("sessions", [])
        if not sessions:
            return import_prices, export_prices

        try:
            octopoints_per_penny = float(
                getattr(self._saving_session_coordinator, "_octopoints_per_penny", 8)
                or 8
            )
        except (TypeError, ValueError):
            octopoints_per_penny = 8.0
        if octopoints_per_penny <= 0:
            octopoints_per_penny = 8.0

        interval = self._config.interval_minutes
        now = dt_util.now()
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=dt_util.UTC)
        else:
            now = now.astimezone(dt_util.UTC)
        import_result = list(import_prices)
        export_result = list(export_prices)
        boosted = 0

        for session in sessions:
            if not session.joined:
                continue
            start = getattr(session, "start", None)
            end = getattr(session, "end", None)
            if start is None or end is None:
                continue
            if getattr(start, "tzinfo", None) is None:
                start = start.replace(tzinfo=dt_util.UTC)
            else:
                start = start.astimezone(dt_util.UTC)
            if getattr(end, "tzinfo", None) is None:
                end = end.replace(tzinfo=dt_util.UTC)
            else:
                end = end.astimezone(dt_util.UTC)

            # Convert octopoints to GBP/kWh:
            # octopoints_per_kwh / octopoints_per_penny = pence/kWh
            # pence/kWh / 100 = GBP/kWh (same unit as our price arrays)
            try:
                octopoints_per_kwh = float(
                    getattr(session, "octopoints_per_kwh", 0) or 0
                )
            except (TypeError, ValueError):
                octopoints_per_kwh = 0.0
            if octopoints_per_kwh > 0:
                session_rate = (octopoints_per_kwh / octopoints_per_penny) / 100
            else:
                session_rate = 0.0

            for t in range(len(export_result)):
                ts = now + timedelta(minutes=t * interval)
                if start <= ts < end:
                    if session.session_type == "saving":
                        # Add session rate ON TOP of existing export price
                        export_result[t] += session_rate
                        # Also bump import price to discourage grid charging
                        import_result[t] = max(import_result[t], session_rate * 2)
                    elif session.session_type == "free_electricity":
                        # Free power - set import price to 0
                        import_result[t] = 0.0
                    boosted += 1

        if boosted:
            joined_count = len([s for s in sessions if s.joined])
            _LOGGER.info(
                "Saving sessions: overlaid %d intervals from %d session(s)",
                boosted, joined_count,
            )

        return import_result, export_result

    def _apply_chip_mode(
        self,
        export_prices: list[float],
        reference_export_prices: list[float] | None = None,
    ) -> list[float]:
        """Apply chip mode to LP export prices — suppress exports unless price exceeds threshold.

        During the configured window, sets export prices to 0 so the LP won't plan
        exports. Preserves price for spikes above threshold. If export prices have
        already been adjusted by Export Boost, reference_export_prices keeps the
        Chip threshold tied to the real export price.
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
        threshold_prices = (
            reference_export_prices
            if reference_export_prices is not None
            and len(reference_export_prices) == len(result)
            else result
        )
        for t in range(len(result)):
            ts = now + timedelta(minutes=t * interval)
            minutes_of_day = ts.hour * 60 + ts.minute

            # Check if in chip window (handles overnight wrap)
            if end_min <= start_min:
                in_window = minutes_of_day >= start_min or minutes_of_day < end_min
            else:
                in_window = start_min <= minutes_of_day < end_min

            if in_window:
                if threshold_prices[t] >= threshold:
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

    def _next_charge_by_time_target_slot(self) -> int | None:
        """Slot index of the next Charge By Time SOC target in the LP horizon."""
        if not self._config.charge_by_time_enabled:
            return None

        from ..const import (
            CONF_CHARGE_BY_TIME_TARGET_TIME,
            CONF_PROFIT_MAX_TARGET_TIME,
            DEFAULT_CHARGE_BY_TIME_TARGET_TIME,
        )
        target_time = getattr(
            self._config,
            "charge_by_time_target_time",
            DEFAULT_CHARGE_BY_TIME_TARGET_TIME,
        )
        if self._entry:
            target_time = self._entry.options.get(
                CONF_CHARGE_BY_TIME_TARGET_TIME,
                self._entry.data.get(
                    CONF_CHARGE_BY_TIME_TARGET_TIME,
                    self._entry.options.get(
                        CONF_PROFIT_MAX_TARGET_TIME,
                        self._entry.data.get(
                            CONF_PROFIT_MAX_TARGET_TIME,
                            target_time,
                        ),
                    ),
                ),
            )
        target_min = _hhmm_to_minutes(
            target_time,
            DEFAULT_CHARGE_BY_TIME_TARGET_TIME,
        )
        interval = self._config.interval_minutes
        target_slot_min = (target_min // interval) * interval
        n_steps = int(self._config.horizon_hours * 60) // interval
        raw_now = dt_util.now()
        now = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0, microsecond=0,
        )
        for t in range(n_steps):
            slot = now + timedelta(minutes=t * interval)
            slot_min = slot.hour * 60 + slot.minute
            if slot_min == target_slot_min:
                # Skip t=0: the target is now, so there are no pre-window slots
                # to charge in. The next matching target will be tomorrow.
                if t == 0:
                    continue
                return t
        return None

    def _charge_by_time_target_soc(self) -> float:
        """Return the configured Charge By Time target SOC as a 0-1 ratio."""
        if not self._entry:
            return self._soc_ratio(self._config.charge_by_time_target_soc, 1.0)

        from ..const import (
            CONF_CHARGE_BY_TIME_TARGET_SOC,
            CONF_PROFIT_MAX_TARGET_SOC,
            DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
        )

        return self._soc_ratio(
            self._entry.options.get(
                CONF_CHARGE_BY_TIME_TARGET_SOC,
                self._entry.data.get(
                    CONF_CHARGE_BY_TIME_TARGET_SOC,
                    self._entry.options.get(
                        CONF_PROFIT_MAX_TARGET_SOC,
                        self._entry.data.get(
                            CONF_PROFIT_MAX_TARGET_SOC,
                            DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
                        ),
                    ),
                ),
            ),
            DEFAULT_CHARGE_BY_TIME_TARGET_SOC,
        )

    def _apply_flow_power_export(
        self, export_prices: list[float]
    ) -> list[float]:
        """Replace export prices with Flow Power Happy Hour schedule.

        Flow Power: 0c export except Happy Hour (17:30-19:30) at 45c/35c.
        """
        if not self._entry:
            return export_prices

        from ..const import (
            CONF_ELECTRICITY_PROVIDER,
            CONF_FLOW_POWER_EXPORT_RATE,
            CONF_FLOW_POWER_STATE,
            FLOW_POWER_EXPORT_RATES,
        )

        provider = self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        if provider != "flow_power":
            return export_prices

        state = self._entry.options.get(
            CONF_FLOW_POWER_STATE,
            self._entry.data.get(CONF_FLOW_POWER_STATE, ""),
        )
        if not state:
            return export_prices

        configured_rate = self._entry.options.get(
            CONF_FLOW_POWER_EXPORT_RATE,
            self._entry.data.get(CONF_FLOW_POWER_EXPORT_RATE),
        )
        try:
            happy_rate = (
                float(configured_rate) / 100
                if configured_rate not in (None, "")
                else FLOW_POWER_EXPORT_RATES.get(state, 0.0)
            )
        except (ValueError, TypeError):
            happy_rate = FLOW_POWER_EXPORT_RATES.get(state, 0.0)
        happy_start = 17 * 60 + 30  # 17:30
        happy_end = 19 * 60 + 30    # 19:30
        interval = self._config.interval_minutes
        now = dt_util.now()

        result = []
        for i in range(len(export_prices)):
            slot = now + timedelta(minutes=i * interval)
            mins = slot.hour * 60 + slot.minute
            result.append(happy_rate if happy_start <= mins < happy_end else 0.0)

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

    def _is_in_demand_window(self) -> bool:
        """Check if the current time is within a demand charge window."""
        if not self._entry:
            return False

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return False

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

        try:
            s_parts = start_str.split(":")
            start_min = int(s_parts[0]) * 60 + int(s_parts[1])
            e_parts = end_str.split(":")
            end_min = int(e_parts[0]) * 60 + int(e_parts[1])
        except (ValueError, IndexError):
            return False

        now = dt_util.now()
        weekday = now.weekday()

        if days == "Weekdays Only" and weekday >= 5:
            return False
        if days == "Weekends Only" and weekday < 5:
            return False

        current_min = now.hour * 60 + now.minute

        if end_min <= start_min:
            return current_min >= start_min or current_min < end_min
        return start_min <= current_min < end_min

    def _is_near_demand_window(self, lead_minutes: int = 30) -> bool:
        """Check if current time is within lead_minutes before or inside a demand charge window."""
        if not self._entry:
            return False

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return False

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

        try:
            s_parts = start_str.split(":")
            start_min = int(s_parts[0]) * 60 + int(s_parts[1])
            e_parts = end_str.split(":")
            end_min = int(e_parts[0]) * 60 + int(e_parts[1])
        except (ValueError, IndexError):
            return False

        now = dt_util.now()
        weekday = now.weekday()

        if days == "Weekdays Only" and weekday >= 5:
            return False
        if days == "Weekends Only" and weekday < 5:
            return False

        current_min = now.hour * 60 + now.minute
        buffered_start = start_min - lead_minutes

        if end_min <= start_min:
            # Overnight window (e.g. 22:00-06:00)
            return current_min >= buffered_start or current_min < end_min
        # Normal window — buffer may wrap to previous day
        if buffered_start < 0:
            return current_min >= (buffered_start + 1440) or current_min < end_min
        return buffered_start <= current_min < end_min

    def _minutes_to_demand_start(self) -> int | None:
        """Return minutes until the demand charge window starts today.

        Returns:
            Positive int if before the window (minutes until start).
            0 if currently inside the window.
            None if demand charge is disabled or doesn't apply today.
        """
        if not self._entry:
            return None

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return None

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

        try:
            s_parts = start_str.split(":")
            start_min = int(s_parts[0]) * 60 + int(s_parts[1])
            e_parts = end_str.split(":")
            end_min = int(e_parts[0]) * 60 + int(e_parts[1])
        except (ValueError, IndexError):
            return None

        now = dt_util.now()
        weekday = now.weekday()

        if days == "Weekdays Only" and weekday >= 5:
            return None
        if days == "Weekends Only" and weekday < 5:
            return None

        current_min = now.hour * 60 + now.minute

        # Check if inside the window
        if end_min > start_min:
            if start_min <= current_min < end_min:
                return 0
        else:
            if current_min >= start_min or current_min < end_min:
                return 0

        # Before the window — return minutes until start
        diff = start_min - current_min
        if diff < 0:
            diff += 1440
        return diff

    def _should_block_export_for_demand(self) -> bool:
        """Check if exports should be blocked for demand charge reasons.

        The LP re-optimizes every 5 minutes and already factors demand
        penalties into its cost function, so no lead-up guard is needed —
        it won't schedule exports that leave the battery too depleted.

        Only blocks exports when demand_charge_apply_to includes sell
        ("Sell Only" or "Both"), since exporting itself would increase
        export peak demand. "Buy Only" never blocks exports.
        """
        if not self._entry:
            return False

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_APPLY_TO,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return False

        apply_to = self._entry.options.get(
            CONF_DEMAND_CHARGE_APPLY_TO,
            self._entry.data.get(CONF_DEMAND_CHARGE_APPLY_TO, "Buy Only"),
        )
        if apply_to == "Buy Only":
            return False

        # "Sell Only" or "Both": exporting during the window increases
        # export peak demand, so block exports inside the window only.
        return self._is_in_demand_window()

    def _is_in_demand_window_at(self, ts: datetime) -> bool:
        """Check if a given timestamp falls within a demand charge window."""
        if not self._entry:
            return False

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return False

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

        try:
            s_parts = start_str.split(":")
            start_min = int(s_parts[0]) * 60 + int(s_parts[1])
            e_parts = end_str.split(":")
            end_min = int(e_parts[0]) * 60 + int(e_parts[1])
        except (ValueError, IndexError):
            return False

        weekday = ts.weekday()

        if days == "Weekdays Only" and weekday >= 5:
            return False
        if days == "Weekends Only" and weekday < 5:
            return False

        current_min = ts.hour * 60 + ts.minute

        if end_min <= start_min:
            return current_min >= start_min or current_min < end_min
        return start_min <= current_min < end_min

    def _get_demand_window_config(self) -> dict[str, Any] | None:
        """Get demand window configuration for API response, or None if disabled."""
        if not self._entry:
            return None

        from ..const import (
            CONF_DEMAND_CHARGE_ENABLED,
            CONF_DEMAND_CHARGE_START_TIME,
            CONF_DEMAND_CHARGE_END_TIME,
            CONF_DEMAND_CHARGE_DAYS,
            CONF_DEMAND_ARTIFICIAL_PRICE,
        )

        enabled = self._entry.options.get(
            CONF_DEMAND_CHARGE_ENABLED,
            self._entry.data.get(CONF_DEMAND_CHARGE_ENABLED, False),
        )
        if not enabled:
            return None

        # The artificial price uplift baked into TOU prices ($/kWh).
        # Currently hardcoded at $2/kWh in tariff_converter.py.
        artificial_enabled = self._entry.options.get(
            CONF_DEMAND_ARTIFICIAL_PRICE,
            self._entry.data.get(CONF_DEMAND_ARTIFICIAL_PRICE, False),
        )
        uplift_kwh = 2.0 if artificial_enabled else 0.0

        return {
            "start_time": self._entry.options.get(
                CONF_DEMAND_CHARGE_START_TIME,
                self._entry.data.get(CONF_DEMAND_CHARGE_START_TIME, "14:00"),
            ),
            "end_time": self._entry.options.get(
                CONF_DEMAND_CHARGE_END_TIME,
                self._entry.data.get(CONF_DEMAND_CHARGE_END_TIME, "20:00"),
            ),
            "days": self._entry.options.get(
                CONF_DEMAND_CHARGE_DAYS,
                self._entry.data.get(CONF_DEMAND_CHARGE_DAYS, "All Days"),
            ),
            "artificial_uplift_kwh": uplift_kwh,
        }

    def _apply_confidence_decay(
        self,
        import_prices: list[float],
        export_prices: list[float],
        confidence_horizon_hours: float = 6.0,
        decay_rate: float = 0.15,
    ) -> tuple[list[float], list[float]]:
        """Pull far-future prices toward median to reflect forecast uncertainty.

        Prices within confidence_horizon_hours are unchanged. Beyond that,
        each price decays toward the median at exp(-decay_rate * excess_hours).

        6h horizon ensures evening peaks are visible from early afternoon,
        so the LP pre-charges rather than leaving the battery empty through
        the peak. Far-future spikes (12h+) still decay heavily.

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

    def _apply_solar_nowcast_derate(
        self,
        solar_forecast: list[float],
        soc: float,
        fade_hours: float = 6.0,
    ) -> list[float]:
        """Reduce near-term solar forecast when live production is under forecast.

        The LP is deterministic: if the solar forecast says energy is coming, it
        will rationally wait for that energy instead of grid-charging earlier.
        Prices can be treated as firm over the near horizon, but solar needs a
        live reality check. When current production is materially below the
        first forecast slots, derate the next few hours and fade back to the raw
        Solcast forecast.
        """
        if not solar_forecast:
            return solar_forecast
        if soc >= 0.98:
            # Near-full batteries and curtailment can make measured solar lower
            # than potential production. Don't learn a false cloud signal there.
            return solar_forecast
        data = self._get_energy_data()
        if not data:
            return solar_forecast

        try:
            actual_kw = max(0.0, float(data.get("solar_power", 0) or 0))
        except (TypeError, ValueError):
            return solar_forecast

        window = [max(0.0, v) for v in solar_forecast[:3] if v is not None]
        if not window:
            return solar_forecast
        forecast_now_kw = sum(window) / len(window)
        if forecast_now_kw < 0.5:
            # Dawn/dusk and very low production are too noisy to learn from.
            return solar_forecast

        ratio = actual_kw / forecast_now_kw if forecast_now_kw > 0 else 1.0
        ratio = max(0.0, min(1.5, ratio))
        self._last_solar_nowcast_ratio = ratio

        if ratio < 0.75:
            target = max(0.35, min(1.0, ratio + 0.10))
            self._solar_nowcast_derate = min(
                self._solar_nowcast_derate,
                (self._solar_nowcast_derate * 0.35) + (target * 0.65),
            )
        elif ratio >= 0.9:
            self._solar_nowcast_derate = min(1.0, self._solar_nowcast_derate + 0.08)

        if self._solar_nowcast_derate >= 0.98:
            return solar_forecast

        interval = self._config.interval_minutes
        adjusted: list[float] = []
        for t, value in enumerate(solar_forecast):
            hours_ahead = (t * interval) / 60.0
            weight = max(0.0, 1.0 - (hours_ahead / fade_hours))
            factor = 1.0 - ((1.0 - self._solar_nowcast_derate) * weight)
            adjusted.append(value * factor)

        if (
            self._last_logged_solar_nowcast_derate is None
            or abs(self._last_logged_solar_nowcast_derate - self._solar_nowcast_derate) >= 0.05
        ):
            _LOGGER.info(
                "Solar forecast nowcast derate: live %.1fkW vs forecast %.1fkW "
                "(%.0f%%), applying %.0f%% factor now fading to 100%% over %.0fh",
                actual_kw,
                forecast_now_kw,
                ratio * 100,
                self._solar_nowcast_derate * 100,
                fade_hours,
            )
            self._last_logged_solar_nowcast_derate = self._solar_nowcast_derate
        return adjusted

    @staticmethod
    def _get_entry_start_time(e: dict) -> str:
        """Get the start time of a price entry across all provider formats.

        Octopus entries have valid_from. Amber/AEMO entries have nemTime
        (interval end) and duration (minutes) — start = nemTime - duration.

        Returns:
            ISO format start time string, or "" if indeterminate
        """
        # Octopus format
        vf = e.get("valid_from")
        if vf:
            return vf

        # Amber/AEMO format: nemTime is the interval END
        nem = e.get("nemTime")
        dur = e.get("duration")
        if nem and dur:
            try:
                end = datetime.fromisoformat(nem.replace("Z", "+00:00"))
                start = end - timedelta(minutes=int(dur))
                return start.isoformat()
            except (ValueError, TypeError):
                pass

        return ""

    @staticmethod
    def _get_entry_end_time(e: dict) -> str:
        """Get the end time of a price entry across all provider formats.

        Octopus entries have valid_to. Amber/AEMO entries have nemTime
        which is itself the interval END.

        Returns:
            ISO format end time string, or "" if indeterminate
        """
        vt = e.get("valid_to")
        if vt:
            return vt
        nem = e.get("nemTime")
        if nem:
            return nem
        return ""

    @classmethod
    def _get_entry_start_datetime(
        cls,
        e: dict,
        fallback: datetime,
    ) -> datetime:
        """Return a parsed entry start datetime, falling back to the LP window."""
        start_str = cls._get_entry_start_time(e)
        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    return start_dt.replace(tzinfo=fallback.tzinfo)
                return start_dt
            except (ValueError, TypeError):
                pass
        return fallback

    @classmethod
    def _entry_remaining_minutes(
        cls,
        e: dict,
        current_window: datetime,
        fallback_dur: int,
    ) -> int:
        """Minutes of this entry that lie at or after current_window.

        Used for first-slot expansion: the active 30-min interval may have
        only N minutes of validity remaining after current_window. Returns
        fallback_dur if start/end can't be parsed.
        """
        start_str = cls._get_entry_start_time(e)
        end_str = cls._get_entry_end_time(e)
        if not start_str or not end_str:
            return max(0, int(fallback_dur))
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return max(0, int(fallback_dur))
        effective_start = max(start_dt, current_window)
        remaining = int((end_dt - effective_start).total_seconds() // 60)
        return max(0, remaining)

    @classmethod
    def _entry_slot_bounds(
        cls,
        e: dict,
        current_window: datetime,
        interval_minutes: int,
        n_steps: int,
    ) -> tuple[int, int] | None:
        """Return optimizer slot bounds for a timestamped price entry."""
        start_str = cls._get_entry_start_time(e)
        end_str = cls._get_entry_end_time(e)
        if not start_str or not end_str:
            return None

        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=current_window.tzinfo)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=current_window.tzinfo)
        if current_window.tzinfo is not None:
            start_dt = start_dt.astimezone(current_window.tzinfo)
            end_dt = end_dt.astimezone(current_window.tzinfo)

        interval_seconds = max(1, interval_minutes) * 60
        start_offset = (start_dt - current_window).total_seconds()
        end_offset = (end_dt - current_window).total_seconds()
        start_idx = max(0, int(math.floor(start_offset / interval_seconds)))
        end_idx = min(n_steps, int(math.ceil(end_offset / interval_seconds)))
        if end_idx <= start_idx:
            return None
        return start_idx, end_idx

    @staticmethod
    def _fill_price_gaps(
        values: list[float | None],
        default: float | None = None,
    ) -> list[float]:
        """Fill timestamp gaps without shifting later price boundaries."""
        first = next((value for value in values if value is not None), default)
        if first is None:
            return []

        filled: list[float] = []
        last = float(first)
        for value in values:
            if value is not None:
                last = float(value)
            filled.append(last)
        return filled

    @staticmethod
    def _dynamic_import_price_dollar(
        entry: dict,
        provider: str,
        amber_forecast_type: str = "predicted",
    ) -> float | None:
        """Resolve the retail import price for a dynamic pricing entry."""
        if provider != "amber":
            return entry.get("perKwh", 0) / 100

        interval_type = entry.get("type")
        if interval_type == "ActualInterval":
            return entry.get("perKwh", 0) / 100

        if interval_type not in ("CurrentInterval", "ForecastInterval"):
            return entry.get("perKwh", 0) / 100

        advanced_price = entry.get("advancedPrice")
        if isinstance(advanced_price, dict):
            if interval_type == "CurrentInterval":
                price_cents = advanced_price.get(
                    amber_forecast_type,
                    advanced_price.get("predicted"),
                )
            else:
                price_cents = advanced_price.get(amber_forecast_type)
        elif isinstance(advanced_price, (int, float)):
            price_cents = advanced_price
        else:
            price_cents = None

        if price_cents is None:
            return None
        return price_cents / 100

    @staticmethod
    def _dynamic_export_price_dollar(
        entry: dict,
        provider: str,
        amber_forecast_type: str = "predicted",
    ) -> float | None:
        """Resolve the retail feed-in price for a dynamic pricing entry."""
        if provider != "amber":
            return entry.get("perKwh", 0) / 100

        interval_type = entry.get("type")
        if interval_type == "ActualInterval":
            return entry.get("perKwh", 0) / 100

        if interval_type not in ("CurrentInterval", "ForecastInterval"):
            return entry.get("perKwh", 0) / 100

        advanced_price = entry.get("advancedPrice")
        if isinstance(advanced_price, dict):
            if interval_type == "CurrentInterval":
                price_cents = advanced_price.get(
                    amber_forecast_type,
                    advanced_price.get("predicted"),
                )
            else:
                price_cents = advanced_price.get(amber_forecast_type)
        elif isinstance(advanced_price, (int, float)):
            price_cents = advanced_price
        else:
            price_cents = None

        if price_cents is None:
            return None
        return price_cents / 100

    def _epex_price_entity_id(self, conf_key: str) -> str | None:
        """Return a configured EPEX price valuation sensor, if any."""
        if not self._entry:
            return None

        from ..const import CONF_ELECTRICITY_PROVIDER

        provider = self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        if provider != "epex":
            return None

        entity_id = self._entry.options.get(
            conf_key,
            self._entry.data.get(conf_key),
        )
        if isinstance(entity_id, str):
            entity_id = entity_id.strip()
        return entity_id or None

    def _epex_import_price_entity_id(self) -> str | None:
        """Return the configured EPEX import valuation sensor, if any."""
        from ..const import CONF_EPEX_IMPORT_PRICE_ENTITY

        return self._epex_price_entity_id(CONF_EPEX_IMPORT_PRICE_ENTITY)

    def _epex_export_price_entity_id(self) -> str | None:
        """Return the configured EPEX export valuation sensor, if any."""
        from ..const import CONF_EPEX_EXPORT_PRICE_ENTITY

        return self._epex_price_entity_id(CONF_EPEX_EXPORT_PRICE_ENTITY)

    @staticmethod
    def _epex_sensor_value_to_major(value: Any, unit: str | None) -> float | None:
        """Convert an EPEX price sensor value to EUR/kWh."""
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None

        label = (unit or "").strip().lower()
        if not label:
            return numeric / 100.0
        if "ct" in label or "cent" in label:
            return numeric / 100.0
        return numeric

    def _epex_sensor_unit(self, attrs: dict[str, Any]) -> str | None:
        """Pick the unit label for an EPEX price sensor."""
        for key in ("unit_of_measurement", "price_unit", "minor_price_unit"):
            unit = attrs.get(key)
            if isinstance(unit, str) and unit.strip():
                return unit
        return "ct/kWh"

    @staticmethod
    def _parse_price_timestamp(value: Any) -> datetime | None:
        """Parse an ISO timestamp key from a price sensor attribute."""
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _timestamped_price_values_to_slots(
        self,
        raw_values: dict[Any, Any],
        unit: str | None,
        n_steps: int,
    ) -> list[float]:
        """Convert timestamp-keyed sensor values into optimizer price slots."""
        interval = max(1, self._config.interval_minutes)
        now = dt_util.now()
        current_window = now.replace(
            minute=(now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        entries: list[tuple[datetime, float]] = []
        for key, raw_price in raw_values.items():
            start_dt = self._parse_price_timestamp(key)
            if start_dt is None:
                continue
            price = self._epex_sensor_value_to_major(raw_price, unit)
            if price is None:
                continue
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=current_window.tzinfo)
            if current_window.tzinfo is not None:
                start_dt = start_dt.astimezone(current_window.tzinfo)
            entries.append((start_dt, price))

        if not entries:
            return []

        entries.sort(key=lambda item: item[0])
        slots: list[float | None] = [None] * n_steps
        last_delta = timedelta(minutes=interval)
        for idx, (start_dt, price) in enumerate(entries):
            next_start = entries[idx + 1][0] if idx + 1 < len(entries) else None
            if next_start is not None:
                delta = next_start - start_dt
                if delta.total_seconds() > 0:
                    last_delta = delta
                end_dt = next_start
            else:
                end_dt = start_dt + last_delta

            slot_bounds = self._entry_slot_bounds(
                {
                    "valid_from": start_dt.isoformat(),
                    "valid_to": end_dt.isoformat(),
                },
                current_window,
                interval,
                n_steps,
            )
            if slot_bounds is None:
                continue
            start_idx, end_idx = slot_bounds
            for pos in range(start_idx, end_idx):
                slots[pos] = price

        return self._fill_price_gaps(slots)

    def _timestamp_attribute_price_values(
        self,
        attrs: dict[str, Any],
    ) -> dict[str, Any]:
        """Return direct timestamp attributes from HA price sensors."""
        return {
            key: value
            for key, value in attrs.items()
            if self._parse_price_timestamp(key) is not None
        }

    def _read_epex_price_entity(
        self,
        n_steps: int,
        entity_id: str | None,
        price_kind: str,
    ) -> list[float] | None:
        """Read an optional EPEX price override sensor."""
        if not entity_id:
            return None

        state_getter = getattr(
            getattr(self.hass, "states", None),
            "get",
            lambda _eid: None,
        )
        state = state_getter(entity_id)
        if state is None:
            _LOGGER.warning(
                "EPEX %s price override sensor %s not found; using EPEX %s prices",
                price_kind,
                entity_id,
                price_kind,
            )
            return None

        state_value = getattr(state, "state", None)
        if str(state_value).lower() in ("unknown", "unavailable", "none", ""):
            _LOGGER.debug(
                "EPEX %s price override sensor %s is %s; using EPEX %s prices",
                price_kind,
                entity_id,
                state_value,
                price_kind,
            )
            return None

        attrs = getattr(state, "attributes", {}) or {}
        unit = self._epex_sensor_unit(attrs)
        raw_values = attrs.get("price_values")

        values: list[float | None] = []
        if isinstance(raw_values, list) and raw_values:
            values = [
                self._epex_sensor_value_to_major(value, unit)
                for value in raw_values
            ]
            display_prices = self._fill_price_gaps(values)
        elif isinstance(raw_values, dict) and raw_values:
            display_prices = self._timestamped_price_values_to_slots(
                raw_values,
                unit,
                n_steps,
            )
        else:
            timestamp_values = self._timestamp_attribute_price_values(attrs)
            if timestamp_values:
                display_prices = self._timestamped_price_values_to_slots(
                    timestamp_values,
                    unit,
                    n_steps,
                )
            else:
                value = self._epex_sensor_value_to_major(state_value, unit)
                display_prices = [value] if value is not None else []

        if not display_prices:
            _LOGGER.warning(
                "EPEX %s price override sensor %s has no numeric price values; "
                "using EPEX %s prices",
                price_kind,
                entity_id,
                price_kind,
            )
            return None

        if len(display_prices) < n_steps:
            display_prices.extend(
                [display_prices[-1]] * (n_steps - len(display_prices))
            )
        display_prices = display_prices[:n_steps]

        _LOGGER.info(
            "EPEX %s price override: using %s (%d steps, %.2f-%.2f ct/kWh)",
            price_kind,
            entity_id,
            len(display_prices),
            min(display_prices) * 100,
            max(display_prices) * 100,
        )
        return display_prices

    def _read_epex_import_price_entity(self, n_steps: int) -> list[float] | None:
        """Read the optional EPEX import price override sensor."""
        return self._read_epex_price_entity(
            n_steps,
            self._epex_import_price_entity_id(),
            "import",
        )

    def _read_epex_export_price_entity(
        self,
        n_steps: int,
    ) -> tuple[list[float], list[float]] | None:
        """Read the optional EPEX export price override sensor.

        Returns display prices and LP prices in EUR/kWh. Display prices preserve
        signed export earnings; LP prices are clamped so negative export value
        cannot become profitable revenue.
        """
        display_prices = self._read_epex_price_entity(
            n_steps,
            self._epex_export_price_entity_id(),
            "export",
        )
        if display_prices is None:
            return None

        lp_prices = [max(0.0, price) for price in display_prices]
        return display_prices, lp_prices

    async def _get_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Get price forecasts for optimizer.

        For dynamic providers (Amber, Flow Power): reads from price_coordinator.
        For static TOU providers (GloBird, etc.): generates from tariff_schedule.
        """
        if self._prefers_static_tou_pricing():
            tou_prices = self._get_tou_price_forecast_if_available()
            if tou_prices is not None:
                if self.price_coordinator and self.price_coordinator.data:
                    _LOGGER.debug(
                        "Using TOU tariff prices for static provider %s; ignoring %s data",
                        self._electricity_provider(),
                        type(self.price_coordinator).__name__,
                    )
                return tou_prices

            # No tariff schedule cached yet - never fall through to the
            # dynamic-pricing path for tariff-backed providers. A leftover
            # AEMOPriceCoordinator (e.g. set up before a provider switch)
            # could still hold stale data and silently feed it to the LP.
            _LOGGER.debug(
                "Tariff-backed provider %s but tariff_schedule not yet cached; "
                "skipping dynamic-pricing fallback",
                self._electricity_provider(),
            )
            return None

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
                    is_flow_power_provider = self._electricity_provider() == "flow_power"

                    # Sort by start time (works for Octopus, Amber, and AEMO)
                    for lst in (general, feed_in):
                        lst.sort(key=lambda e: self._get_entry_start_time(e))

                    # Filter out fully-past entries — providers return
                    # historical entries, but the LP needs prices starting
                    # from the current interval. Use END time so an
                    # interval that started before current_window but is
                    # still active (e.g. 30-min Octopus slot at minute 20)
                    # is preserved; its remaining-minutes are computed
                    # during expansion.
                    now = dt_util.now()
                    current_window = now.replace(
                        minute=(now.minute // 5) * 5,
                        second=0, microsecond=0,
                    )
                    fp_current_general = None
                    fp_current_period_start = None
                    fp_current_period_end = None
                    if is_flow_power_provider:
                        current_general = [
                            e
                            for e in data.get("current", []) or []
                            if e.get("channelType") == "general"
                        ]
                        current_feedin = [
                            e
                            for e in data.get("current", []) or []
                            if e.get("channelType") == "feedIn"
                        ]
                        current_general.sort(key=lambda e: self._get_entry_end_time(e))
                        current_feedin.sort(key=lambda e: self._get_entry_end_time(e))
                        if current_general:
                            fp_current_general = current_general[-1]
                            current_nem_start = self._get_entry_start_datetime(
                                fp_current_general,
                                current_window,
                            ).astimezone(FLOW_POWER_NEM_TZ)
                            fp_current_period_start = current_nem_start.replace(
                                minute=0 if current_nem_start.minute < 30 else 30,
                                second=0,
                                microsecond=0,
                            )
                            fp_current_period_end = fp_current_period_start + timedelta(
                                minutes=30
                            )

                            def _flow_power_current_period_entry(source: dict) -> dict:
                                entry = dict(source)
                                entry["nemTime"] = fp_current_period_end.isoformat()
                                entry["duration"] = 30
                                entry["type"] = "CurrentInterval"
                                return entry

                            general.append(
                                _flow_power_current_period_entry(fp_current_general)
                            )
                            if current_feedin:
                                feed_in.append(
                                    _flow_power_current_period_entry(current_feedin[-1])
                                )

                    for lst in (general, feed_in):
                        original_len = len(lst)
                        filtered = []
                        for e in lst:
                            end_str = self._get_entry_end_time(e)
                            if end_str:
                                try:
                                    entry_end = datetime.fromisoformat(
                                        end_str.replace("Z", "+00:00")
                                    )
                                    if entry_end <= current_window:
                                        continue
                                except (ValueError, TypeError):
                                    pass
                            filtered.append(e)
                        lst[:] = filtered
                        if len(lst) < original_len:
                            _LOGGER.debug(
                                "Filtered %d past price entries (ended <= %s), "
                                "%d remaining",
                                original_len - len(lst),
                                current_window.isoformat(),
                                len(lst),
                            )

                    # Build 5-min price arrays with per-entry expansion.
                    # Mixed feeds (e.g. Amber 5-min + 30-min) expand each entry
                    # by its own duration: 5-min→1x, 30-min→6x.
                    interval = self._config.interval_minutes  # 5
                    n_steps = int(self._config.horizon_hours * 60) // interval  # 576

                    # Detect Flow Power for price adjustment
                    is_flow_power = False
                    fp_base_rate = 34.0
                    fp_pea_enabled = True
                    fp_custom_pea = None
                    fp_pricing_context: FlowPowerPricingContext = (
                        resolve_flow_power_pricing_context({}, {}, {})
                    )
                    fp_avg_daily_tariff = None
                    fp_network = None
                    fp_tariff_code = None
                    fp_tariff_rates: dict[int, float] = {}
                    _provider = self._electricity_provider()
                    amber_forecast_type = "predicted"
                    if self._entry:
                        from ..const import (
                            CONF_AMBER_FORECAST_TYPE,
                            CONF_FP_NETWORK,
                            CONF_FP_TARIFF_CODE,
                            CONF_PEA_ENABLED,
                            CONF_FLOW_POWER_BASE_RATE,
                            CONF_PEA_CUSTOM_VALUE,
                            FLOW_POWER_DEFAULT_BASE_RATE,
                            NETWORK_API_NAME,
                            DOMAIN as _DOMAIN,
                        )
                        amber_forecast_type = self._entry.options.get(
                            CONF_AMBER_FORECAST_TYPE,
                            self._entry.data.get(
                                CONF_AMBER_FORECAST_TYPE, "predicted"
                            ),
                        )
                        is_flow_power = _provider == "flow_power"
                        if is_flow_power:
                            def _flow_power_option(key: str, default=None):
                                return self._entry.options.get(
                                    key,
                                    self._entry.data.get(key, default),
                                )

                            fp_pea_enabled = _flow_power_option(
                                CONF_PEA_ENABLED, True
                            )
                            fp_base_rate = _flow_power_option(
                                CONF_FLOW_POWER_BASE_RATE,
                                FLOW_POWER_DEFAULT_BASE_RATE,
                            )
                            fp_custom_pea = _flow_power_option(CONF_PEA_CUSTOM_VALUE)
                            domain_data = self.hass.data.get(
                                _DOMAIN, {}
                            ).get(self._entry.entry_id, {})
                            fp_pricing_context = resolve_flow_power_pricing_context(
                                self._entry.options,
                                self._entry.data,
                                domain_data,
                            )
                            fp_avg_daily_tariff = domain_data.get(
                                "fp_avg_daily_tariff"
                            )
                            fp_network_name = self._entry.options.get(
                                CONF_FP_NETWORK,
                                self._entry.data.get(CONF_FP_NETWORK),
                            )
                            fp_tariff_code = self._entry.options.get(
                                CONF_FP_TARIFF_CODE,
                                self._entry.data.get(CONF_FP_TARIFF_CODE),
                            )
                            if fp_network_name:
                                fp_network = NETWORK_API_NAME.get(
                                    fp_network_name,
                                    str(fp_network_name).lower(),
                                )

                    if (
                        is_flow_power
                        and fp_network
                        and fp_tariff_code
                        and fp_avg_daily_tariff is not None
                    ):
                        tariff_datetimes: dict[int, datetime] = {}
                        for entry in general:
                            start_dt = self._get_entry_start_datetime(
                                entry,
                                current_window,
                            ).astimezone(FLOW_POWER_NEM_TZ)
                            tariff_datetimes[id(entry)] = start_dt

                        def _lookup_flow_power_tariff_rates() -> dict[int, float]:
                            rates: dict[int, float] = {}
                            cache: dict[datetime, float | None] = {}
                            for entry_id, start_dt in tariff_datetimes.items():
                                cached = cache.get(start_dt)
                                if start_dt not in cache:
                                    cached = _flow_power_network_tariff_rate(
                                        start_dt,
                                        fp_network,
                                        fp_tariff_code,
                                    )
                                    cache[start_dt] = cached
                                if cached is not None:
                                    rates[entry_id] = cached
                            return rates

                        try:
                            if hasattr(self.hass, "async_add_executor_job"):
                                fp_tariff_rates = await self.hass.async_add_executor_job(
                                    _lookup_flow_power_tariff_rates
                                )
                            else:
                                fp_tariff_rates = _lookup_flow_power_tariff_rates()
                        except Exception as err:
                            _LOGGER.warning(
                                "Flow Power v2 tariff lookup failed for %s/%s; "
                                "falling back to legacy PEA formula: %s",
                                fp_network,
                                fp_tariff_code,
                                err,
                            )

                    import_slots: list[float | None] = [None] * n_steps
                    entry_positions = []  # start index for each general entry
                    entry_expands_general = []  # parallel: actual expand count per entry
                    write_cursor = 0
                    last_import_slot = 0
                    for e in general:
                        dur = e.get("duration", 30)
                        slot_bounds = self._entry_slot_bounds(
                            e, current_window, interval, n_steps
                        )
                        if slot_bounds is None:
                            # Fallback for legacy/test data with no timestamps:
                            # preserve the previous append-based behavior.
                            effective_min = self._entry_remaining_minutes(
                                e, current_window, dur,
                            )
                            entry_expand = (
                                max(1, effective_min // interval)
                                if effective_min > 0
                                else 0
                            )
                            start_idx = write_cursor
                            end_idx = min(n_steps, start_idx + entry_expand)
                            write_cursor = end_idx
                        else:
                            start_idx, end_idx = slot_bounds
                            entry_expand = end_idx - start_idx
                        entry_positions.append(start_idx)
                        entry_expands_general.append(entry_expand)
                        if entry_expand == 0:
                            continue
                        if is_flow_power:
                            if fp_custom_pea is not None:
                                price_dollar = max(
                                    0, (fp_base_rate + fp_custom_pea) / 100
                                )
                            elif fp_pea_enabled:
                                wholesale_cents = e.get("wholesaleKWHPrice")
                                if wholesale_cents is None:
                                    wholesale_cents = e.get("perKwh", 0)
                                if (
                                    fp_current_general
                                    and fp_current_period_start is not None
                                ):
                                    entry_period_start = self._get_entry_start_datetime(
                                        e,
                                        current_window,
                                    ).astimezone(FLOW_POWER_NEM_TZ)
                                    entry_period_start = entry_period_start.replace(
                                        minute=(
                                            0
                                            if entry_period_start.minute < 30
                                            else 30
                                        ),
                                        second=0,
                                        microsecond=0,
                                    )
                                    if entry_period_start == fp_current_period_start:
                                        current_wholesale_cents = (
                                            fp_current_general.get("wholesaleKWHPrice")
                                        )
                                        if current_wholesale_cents is None:
                                            current_wholesale_cents = (
                                                fp_current_general.get("perKwh")
                                            )
                                        if current_wholesale_cents is not None:
                                            wholesale_cents = current_wholesale_cents
                                tariff_rate = fp_tariff_rates.get(id(e))
                                if (
                                    tariff_rate is not None
                                    and fp_avg_daily_tariff is not None
                                ):
                                    pea = calculate_flow_power_pea(
                                        wholesale_cents,
                                        fp_pricing_context,
                                        tariff_rate=tariff_rate,
                                        avg_daily_tariff=fp_avg_daily_tariff,
                                    )
                                else:
                                    pea = calculate_flow_power_pea(
                                        wholesale_cents,
                                        fp_pricing_context,
                                    )
                                price_dollar = max(
                                    0, (fp_base_rate + pea) / 100
                                )
                            else:
                                price_dollar = max(0, fp_base_rate / 100)
                        else:
                            price_dollar = self._dynamic_import_price_dollar(
                                e,
                                _provider,
                                amber_forecast_type,
                            )
                            if price_dollar is None:
                                last_import_slot = max(last_import_slot, end_idx)
                                continue
                        for pos in range(start_idx, end_idx):
                            import_slots[pos] = price_dollar
                        last_import_slot = max(last_import_slot, end_idx)

                    import_prices = self._fill_price_gaps(import_slots)

                    export_slots: list[float | None] = [None] * n_steps
                    display_export_slots: list[float | None] = [None] * n_steps
                    export_write_cursor = 0
                    for e in feed_in:
                        dur = e.get("duration", 30)
                        slot_bounds = self._entry_slot_bounds(
                            e, current_window, interval, n_steps
                        )
                        if slot_bounds is None:
                            effective_min = self._entry_remaining_minutes(
                                e, current_window, dur,
                            )
                            entry_expand = (
                                max(1, effective_min // interval)
                                if effective_min > 0
                                else 0
                            )
                            start_idx = export_write_cursor
                            end_idx = min(n_steps, start_idx + entry_expand)
                            export_write_cursor = end_idx
                        else:
                            start_idx, end_idx = slot_bounds
                        if end_idx <= start_idx:
                            continue
                        # feedIn perKwh: negative = you get paid, positive = you pay to export.
                        # display_price keeps the signed value so the UI chart can show
                        # negative dips during oversupply (when you'd pay to export).
                        # lp_price clamps to 0 so the LP doesn't see paying-to-export
                        # as profitable revenue.
                        raw_export_dollar = self._dynamic_export_price_dollar(
                            e,
                            _provider,
                            amber_forecast_type,
                        )
                        if raw_export_dollar is None:
                            continue
                        display_price = -raw_export_dollar
                        lp_price = max(0.0, display_price)
                        for pos in range(start_idx, end_idx):
                            export_slots[pos] = lp_price
                            display_export_slots[pos] = display_price

                    export_prices = self._fill_price_gaps(export_slots)
                    display_export_raw = self._fill_price_gaps(
                        display_export_slots,
                        export_prices[0] if export_prices else None,
                    )

                    # Track actual forecast length before padding
                    actual_price_intervals = last_import_slot

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

                    if display_export_raw:
                        if len(display_export_raw) < n_steps:
                            last = display_export_raw[-1]
                            display_export_raw.extend(
                                [last] * (n_steps - len(display_export_raw))
                            )
                        display_export_raw = display_export_raw[:n_steps]

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
                                    base_idx = entry_positions[idx]
                                    entry_expand = (
                                        entry_expands_general[idx]
                                        if idx < len(entry_expands_general)
                                        else max(1, e.get("duration", 30) // interval)
                                    )
                                    if entry_expand == 0:
                                        continue
                                    original_price = e.get("perKwh", 0)
                                    capped_count = 0
                                    for j in range(entry_expand):
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
                        epex_import_override = self._read_epex_import_price_entity(
                            n_steps
                        )
                        if epex_import_override is not None:
                            import_prices = epex_import_override

                        epex_override = self._read_epex_export_price_entity(n_steps)
                        if epex_override is not None:
                            display_export_raw, export_prices = epex_override

                        # Apply Flow Power export schedule before display storage.
                        # For Flow Power, the synthetic Happy Hour schedule IS the
                        # contractual truth, so it overrides the Amber-derived
                        # signed values for both the LP and the display chart.
                        # For other providers this is a no-op.
                        export_prices = self._apply_flow_power_export(export_prices)
                        if is_flow_power:
                            display_export_raw = list(export_prices)

                        # Store prices for UI display BEFORE LP adjustments.
                        # Clip to actual forecast length so the app chart doesn't
                        # show flat-line padding where the forecast ran out.
                        # display_export_raw keeps the signed export rate so the
                        # chart shows negative dips when wholesale is oversupplied
                        # (Amber feedIn perKwh > 0 → you pay to export).
                        self._last_display_import_prices = list(import_prices[:actual_price_intervals])
                        self._last_display_export_prices = list(display_export_raw[:actual_price_intervals])
                        self._last_grid_charge_cap_import_prices = list(import_prices)

                        # Apply export boost, saving session overlay, and chip mode to LP prices.
                        # Chip mode uses the real export price as its threshold reference so
                        # Export Boost cannot make a below-threshold export slot look allowed.
                        chip_reference_export_prices = list(export_prices)
                        export_prices, _ = self._apply_export_boost(export_prices, import_prices)
                        import_prices, export_prices = self._apply_saving_session_prices(import_prices, export_prices)
                        export_prices = self._apply_chip_mode(
                            export_prices,
                            chip_reference_export_prices,
                        )

                        # Apply demand charge penalty to LP import prices
                        import_prices = self._apply_demand_charge_penalty(import_prices)

                        # Apply confidence decay for LP input.
                        decay_horizon = 12.0 if self._config.profit_max_enabled else 6.0
                        if is_flow_power:
                            # Flow Power Happy Hour export is contractual, so keep
                            # the export schedule fixed. Import PEA forecasts still
                            # come from speculative wholesale forecasts and should
                            # not let far-future spikes dominate the LP unchanged.
                            import_prices, _ = self._apply_confidence_decay(
                                import_prices,
                                export_prices,
                                confidence_horizon_hours=decay_horizon,
                            )
                        else:
                            import_prices, export_prices = self._apply_confidence_decay(
                                import_prices, export_prices,
                                confidence_horizon_hours=decay_horizon,
                            )

                        _price_label = "Flow Power" if is_flow_power else "Dynamic"
                        _LOGGER.debug(
                            "%s prices: %d steps, display %.1fc-%.1fc, "
                            "LP %s %.1fc-%.1fc",
                            _price_label,
                            len(import_prices),
                            min(self._last_display_import_prices) * 100,
                            max(self._last_display_import_prices) * 100,
                            "(import-decayed)" if is_flow_power else "(decayed)",
                            min(import_prices) * 100,
                            max(import_prices) * 100,
                        )
                        return (import_prices, export_prices)

        # Static TOU pricing fallback (GloBird, custom tariff, etc.)
        # Generate 576-point price forecast from tariff schedule.
        tou_prices = self._get_tou_price_forecast_if_available()
        if tou_prices is not None:
            return tou_prices

        _LOGGER.warning(
            "No price data available! price_coordinator=%s, tariff=%s. "
            "Optimizer will use default flat rates.",
            self.price_coordinator is not None,
            self._get_tou_tariff_schedule() is not None,
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
        timestamps: list[datetime] = []

        # Log TOU period windows for debugging day-of-week matching
        dow_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        for pname in tou_periods:
            for pw in period_entries(tou_periods[pname]):
                fd, td = pw.get("fromDayOfWeek", 0), pw.get("toDayOfWeek", 6)
                fh, th = pw.get("fromHour", 0), pw.get("toHour", 24)
                _LOGGER.debug(
                    "TOU period %s: %s-%s %02d:00-%02d:00 (sell=%s)",
                    pname, dow_names[fd], dow_names[td], fh, th,
                    sell_rates.get(pname, "?"),
                )

        for t in range(n_steps):
            ts = now + timedelta(minutes=t * interval)
            timestamps.append(ts)

            matched_period = find_matching_tou_period(
                tou_periods,
                ts,
                default="OFF_PEAK",
                buy_rates=buy_rates,
                sell_rates=sell_rates,
            )

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
                # Global FiT (ALL key) is the correct fallback for unmatched periods
                sell = sell_rates.get("ALL")
            if sell is None:
                for fallback in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                    if fallback in sell_rates:
                        sell = sell_rates[fallback]
                        break
            if sell is None:
                sell = 0.0  # No sell rate configured — default to 0 (no export value)

            # Store actual tariff rates for display before LP adjustment
            display_import.append(buy)
            display_export.append(sell)

            # When price is exactly zero the LP has zero marginal cost,
            # so HiGHS may assign imports/exports arbitrarily (LP
            # degeneracy).  Use a tiny positive epsilon to break ties
            # while keeping the cost economically irrelevant.
            #
            # The epsilon must be much smaller than the terminal-price
            # floor (0.001) so that free-import tariffs (e.g. GloBird
            # FOUR4FREE super-off-peak at 0c) still show a clear net
            # benefit for grid charging after efficiency losses.
            # At 0.001 the import cost exceeded the terminal benefit
            # (0.001 * eff / cap), causing the LP to avoid charging
            # during genuinely free windows.
            # Only apply epsilon to BUY prices (free charging windows need
            # non-zero cost to avoid LP degeneracy). SELL prices at 0 must
            # stay 0 so the LP's zero-export guard (0.01 cost) activates.
            # Setting sell to 1e-6 bypasses the guard and causes the LP to
            # export at negligible revenue — a net loss for the user.
            if buy < 1e-6:
                buy = 1e-6

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
        self._last_grid_charge_cap_import_prices = list(import_prices)
        self._last_price_timestamps = timestamps

        # Apply saving session overlay to TOU prices
        import_prices, export_prices = self._apply_saving_session_prices(import_prices, export_prices)

        # Apply demand charge penalty to LP import prices
        import_prices = self._apply_demand_charge_penalty(import_prices)

        return (import_prices, export_prices)

    def _get_warnings(self) -> list[dict[str, str]]:
        """Get active warnings for the optimizer."""
        warnings = []
        if getattr(self, "_has_solar_forecast", None) is False:
            warnings.append({
                "type": "no_solar_forecast",
                "title": "No Solar Forecast",
                "message": "No supported solar forecast provider is configured. The optimizer is making decisions based on price only, without knowing when solar will be available. Install Solcast Solar or Open-Meteo Solar Forecast for optimal scheduling.",
            })
        return warnings

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
            if not self._load_estimator.load_entity_id:
                load_entity = self._get_load_entity_id()
                if load_entity:
                    _LOGGER.info("Load sensor became available: %s", load_entity)
                    self._load_estimator.load_entity_id = load_entity
                    self._load_estimator._history_cache.clear()
                    self._load_estimator._cache_time = None
                else:
                    data = self._get_energy_data() or {}
                    try:
                        current_load_kw = float(data.get("load_power"))
                    except (TypeError, ValueError):
                        current_load_kw = 0.0
                    if current_load_kw > 0:
                        n_intervals = (
                            self._config.horizon_hours
                            * 60
                            // self._config.interval_minutes
                        )
                        return self._load_estimator._simple_forecast(
                            current_load_kw * 1000.0,
                            dt_util.now(),
                            n_intervals,
                        )
            # Feed the estimator the EV charger power sensors to subtract from
            # load history (removes recurring EV charging that would otherwise
            # be double-counted against the planned-EV overlay).
            self._load_estimator.ev_power_entity_ids = (
                self._ev_load_subtraction_entities()
            )
            return await self._load_estimator.get_forecast(
                horizon_hours=self._config.horizon_hours
            )
        return None

    def _ev_load_subtraction_entities(self) -> list[str]:
        """EV charger power sensors to subtract from load history.

        Only returned for battery brands whose home-load sensor does NOT
        already exclude EV charging (Tesla and Sigenergy subtract it upstream,
        so subtracting again would under-forecast household load), and only when
        the user has configured a generic charger power entity. Returning an
        empty list leaves the forecast unchanged — zero regression for setups
        without a configured charger power sensor.
        """
        if not getattr(self, "_ev_integration_enabled", False):
            return []
        if getattr(self, "battery_system", None) in ("tesla", "sigenergy"):
            return []
        try:
            from ..automations.ev_charging_planner import get_auto_schedule_executor

            executor = get_auto_schedule_executor()
        except Exception:
            executor = None
        entities: list[str] = []
        if executor:
            settings = getattr(executor, "_settings", {}) or {}
            for cfg in settings.values():
                entity = getattr(cfg, "charger_power_entity", None)
                if entity and entity not in entities:
                    entities.append(entity)
        if self._entry:
            entry_entity = self._entry.options.get(
                CONF_GENERIC_CHARGER_POWER_ENTITY,
                self._entry.data.get(CONF_GENERIC_CHARGER_POWER_ENTITY),
            )
            if entry_entity and entry_entity not in entities:
                entities.append(entry_entity)
        return entities

    async def _refresh_ev_forecast_inputs(self) -> None:
        """Refresh EV schedule inputs before an LP solve without charger commands."""
        try:
            from ..automations.ev_charging_planner import get_auto_schedule_executor

            executor = get_auto_schedule_executor()
            refresh = getattr(executor, "refresh_optimizer_forecast_plans", None)
            if refresh is not None:
                await refresh()
        except Exception as err:
            _LOGGER.debug("Optimizer: EV forecast refresh skipped: %s", err)

    def _get_planned_ev_load_forecast(self, n_intervals: int) -> list[float] | None:
        """Read an optional forecast-only EV load overlay from a HA sensor."""
        entity_id = (self._planned_ev_load_entity_id or "").strip()
        if not entity_id or n_intervals <= 0:
            return None

        state_getter = getattr(
            getattr(self.hass, "states", None),
            "get",
            lambda _eid: None,
        )
        state = state_getter(entity_id)
        if state is None:
            _LOGGER.warning(
                "Planned EV load forecast sensor %s not found; skipping overlay",
                entity_id,
            )
            return None

        state_value = getattr(state, "state", None)
        if str(state_value).lower() in ("unknown", "unavailable", "none", ""):
            _LOGGER.debug(
                "Planned EV load forecast sensor %s is %s; skipping overlay",
                entity_id,
                state_value,
            )
            return None

        attrs = getattr(state, "attributes", {}) or {}
        planned_load = attrs.get("planned_load")
        if not planned_load:
            return None

        interval = max(1, self._config.interval_minutes)
        now = dt_util.now()
        current_window = now.replace(
            minute=(now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        ev_load = [0.0] * n_intervals

        if isinstance(planned_load, list):
            self._apply_planned_ev_load_windows(
                ev_load,
                planned_load,
                current_window,
                interval,
            )
        elif isinstance(planned_load, dict):
            self._apply_timestamped_planned_ev_load(
                ev_load,
                planned_load,
                attrs,
                current_window,
                interval,
            )

        if not any(value > 0 for value in ev_load):
            return None

        peak_kw = max(ev_load) / 1000.0
        total_kwh = sum(ev_load) / 1000.0 * (interval / 60)
        _LOGGER.debug(
            "Planned EV load overlay: peak %.1fkW, total %.1fkWh from %s",
            peak_kw,
            total_kwh,
            entity_id,
        )
        return ev_load

    def _apply_planned_ev_load_windows(
        self,
        ev_load: list[float],
        windows: list[Any],
        current_window: datetime,
        interval: int,
    ) -> None:
        """Apply explicit planned EV load windows into a watts slot array."""
        for window in windows:
            if not isinstance(window, dict):
                continue
            start = window.get("start") or window.get("valid_from")
            end = window.get("end") or window.get("valid_to")
            if not start or not end:
                continue
            power_w = self._planned_ev_window_power_to_w(window)
            if power_w <= 0:
                continue
            bounds = self._entry_slot_bounds(
                {
                    "valid_from": str(start),
                    "valid_to": str(end),
                },
                current_window,
                interval,
                len(ev_load),
            )
            if bounds is None:
                continue
            start_idx, end_idx = bounds
            for idx in range(start_idx, end_idx):
                ev_load[idx] += power_w

    def _apply_timestamped_planned_ev_load(
        self,
        ev_load: list[float],
        raw_values: dict[Any, Any],
        attrs: dict[str, Any],
        current_window: datetime,
        interval: int,
    ) -> None:
        """Apply timestamp-keyed planned EV load values into a watts slot array."""
        entries: list[tuple[datetime, float]] = []
        unit = self._planned_ev_load_unit(attrs)
        for key, raw_power in raw_values.items():
            start_dt = self._parse_price_timestamp(key)
            if start_dt is None:
                continue
            power_w = self._planned_ev_scalar_to_w(raw_power, unit)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=current_window.tzinfo)
            if current_window.tzinfo is not None:
                start_dt = start_dt.astimezone(current_window.tzinfo)
            entries.append((start_dt, power_w))

        if not entries:
            return

        entries.sort(key=lambda item: item[0])
        last_delta = timedelta(minutes=interval)
        for idx, (start_dt, power_w) in enumerate(entries):
            next_start = entries[idx + 1][0] if idx + 1 < len(entries) else None
            if next_start is not None:
                delta = next_start - start_dt
                if delta.total_seconds() > 0:
                    last_delta = delta
                end_dt = next_start
            else:
                end_dt = start_dt + last_delta
            if power_w <= 0:
                continue
            bounds = self._entry_slot_bounds(
                {
                    "valid_from": start_dt.isoformat(),
                    "valid_to": end_dt.isoformat(),
                },
                current_window,
                interval,
                len(ev_load),
            )
            if bounds is None:
                continue
            start_idx, end_idx = bounds
            for pos in range(start_idx, end_idx):
                ev_load[pos] += power_w

    @staticmethod
    def _planned_ev_load_unit(attrs: dict[str, Any]) -> str:
        unit = attrs.get("unit_of_measurement")
        return str(unit).strip() if unit else "kW"

    def _planned_ev_window_power_to_w(self, window: dict[str, Any]) -> float:
        if "power_w" in window:
            return self._planned_ev_scalar_to_w(window.get("power_w"), "W")
        if "power_kw" in window:
            return self._planned_ev_scalar_to_w(window.get("power_kw"), "kW")
        if "power" in window:
            return self._planned_ev_scalar_to_w(window.get("power"), "kW")
        return 0.0

    @staticmethod
    def _planned_ev_scalar_to_w(value: Any, unit: str | None) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(numeric) or numeric <= 0:
            return 0.0
        label = (unit or "kW").strip().lower()
        if label in ("w", "watt", "watts") or label.endswith(" w"):
            return numeric
        return numeric * 1000.0

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
            configured_power_w = None
            try:
                settings = getattr(executor, "_settings", {}).get(vehicle_id)
                if settings is not None:
                    executor._sync_charger_params_from_vehicle_configs(
                        vehicle_id,
                        settings,
                    )
                    configured_power_w = (
                        float(settings.max_charge_amps)
                        * float(settings.voltage)
                        * float(settings.phases)
                    )
            except Exception:
                configured_power_w = None

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
                if configured_power_w and configured_power_w > 0:
                    if power_w > configured_power_w:
                        _LOGGER.debug(
                            "EV load overlay: clamping %s planned power %.1fkW "
                            "to configured charger limit %.1fkW",
                            vehicle_id,
                            power_w / 1000,
                            configured_power_w / 1000,
                        )
                    power_w = min(power_w, configured_power_w)

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

            # AlphaESS auto-detection: the coordinator exposes BMS-reported
            # max charge/discharge power (watts) and rated capacity (kWh) directly
            # — no voltage assumption needed.
            ae_max_charge_w = data.get("battery_max_charge_power_w")
            ae_max_discharge_w = data.get("battery_max_discharge_power_w")
            ae_capacity_kwh = data.get("battery_capacity_kwh")

            if ae_max_charge_w and ae_max_charge_w > 0:
                self._config.max_charge_w = int(ae_max_charge_w)
                self._config.max_discharge_w = int(ae_max_discharge_w or ae_max_charge_w)
                if ae_capacity_kwh and ae_capacity_kwh > 0:
                    self._config.battery_capacity_wh = int(ae_capacity_kwh * 1000)
                self._battery_specs_source = "auto"

                _LOGGER.info(
                    "Auto-detected AlphaESS battery specs from Modbus: "
                    "capacity %.1f kWh, charge %.1f kW, discharge %.1f kW",
                    (ae_capacity_kwh or self._config.battery_capacity_wh / 1000),
                    self._config.max_charge_w / 1000,
                    self._config.max_discharge_w / 1000,
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
            # nameplate_power is total site power in watts
            discharge_w = int(nameplate_power)
            # Tesla firmware now allows charging at the full inverter rate
            # (up to 10kW per battery unit)
            charge_w = discharge_w
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
            charge_w = int(battery_count * 5000)
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

        data = self._get_energy_data()
        if data:
            soc_value = data.get("battery_level")
            if soc_value is not None:
                # battery_level is always 0-100 percentage from all coordinators
                # (Tesla, Sigenergy, FoxESS, Sungrow). Previous heuristic
                # (>1 means %, <=1 means fraction) broke when SOC was genuinely
                # below 1% — e.g. 0.6% was misread as 60%.
                soc = max(0.0, min(1.0, soc_value / 100))

        return soc, capacity

    def _get_actual_battery_power_w(self) -> float:
        """Get actual battery power from energy coordinator."""
        data = self._get_energy_data()
        if data:
            power = data.get("battery_power", 0)
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
            self._actual_grid_charge_kwh_today = float(data.get("grid_charge_kwh", 0.0))
            self._actual_grid_charge_cost_today = float(data.get("grid_charge_cost", 0.0))
            zerohero = data.get("zerohero", {}) or {}
            self._actual_zerohero_import_kwh_today = float(zerohero.get("import_window_kwh", 0.0))
            self._actual_zerohero_export_kwh_today = float(zerohero.get("export_window_kwh", 0.0))
            self._actual_zerohero_bonus_export_kwh_today = float(zerohero.get("bonus_export_kwh", 0.0))
            self._actual_zerohero_base_export_earnings_today = float(zerohero.get("base_export_earnings", 0.0))
            self._actual_zerohero_bonus_export_earnings_today = float(zerohero.get("bonus_export_earnings", 0.0))
            self._actual_zerohero_credit_value_today = float(zerohero.get("credit_value", 0.0))
            self._actual_zerocharge_import_kwh_today = float(zerohero.get("zerocharge_import_kwh", 0.0))
            self._actual_zerocharge_credit_value_today = float(zerohero.get("zerocharge_credit_value", 0.0))
            self._baseline_zerohero_import_kwh_today = float(zerohero.get("baseline_import_window_kwh", 0.0))
            self._baseline_zerohero_bonus_export_kwh_today = float(zerohero.get("baseline_bonus_export_kwh", 0.0))
            self._baseline_zerohero_credit_value_today = float(zerohero.get("baseline_credit_value", 0.0))
            self._baseline_zerocharge_import_kwh_today = float(zerohero.get("baseline_zerocharge_import_kwh", 0.0))
            self._baseline_zerocharge_credit_value_today = float(zerohero.get("baseline_zerocharge_credit_value", 0.0))
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
            "grid_charge_kwh": round(self._actual_grid_charge_kwh_today, 4),
            "grid_charge_cost": round(self._actual_grid_charge_cost_today, 4),
            "zerohero": {
                "import_window_kwh": round(self._actual_zerohero_import_kwh_today, 4),
                "export_window_kwh": round(self._actual_zerohero_export_kwh_today, 4),
                "bonus_export_kwh": round(self._actual_zerohero_bonus_export_kwh_today, 4),
                "base_export_earnings": round(self._actual_zerohero_base_export_earnings_today, 4),
                "bonus_export_earnings": round(self._actual_zerohero_bonus_export_earnings_today, 4),
                "credit_value": round(self._actual_zerohero_credit_value_today, 4),
                "zerocharge_import_kwh": round(self._actual_zerocharge_import_kwh_today, 4),
                "zerocharge_credit_value": round(self._actual_zerocharge_credit_value_today, 4),
                "baseline_import_window_kwh": round(self._baseline_zerohero_import_kwh_today, 4),
                "baseline_bonus_export_kwh": round(self._baseline_zerohero_bonus_export_kwh_today, 4),
                "baseline_credit_value": round(self._baseline_zerohero_credit_value_today, 4),
                "baseline_zerocharge_import_kwh": round(self._baseline_zerocharge_import_kwh_today, 4),
                "baseline_zerocharge_credit_value": round(self._baseline_zerocharge_credit_value_today, 4),
            },
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

    # ------------------------------------------------------------------
    # Off-grid curtailment overlay
    # ------------------------------------------------------------------

    # Minimum consecutive eligible slots (5 min each) before going off-grid.
    # 3 slots = 15 minutes — prevents short contactor cycles.
    _OFFGRID_MIN_CONSECUTIVE = 3
    # Export price threshold ($/kWh). Below this, export has negative or
    # negligible value and off-grid curtailment is beneficial.
    _OFFGRID_EXPORT_THRESHOLD = 0.01  # 1c/kWh
    # SOC threshold for automated off-grid curtailment. Only trigger when
    # the battery is essentially full — below this, we should CHARGE the
    # battery from solar instead of wasting it by islanding.
    _OFFGRID_FULL_SOC_THRESHOLD = 98.0  # %

    def _should_apply_offgrid_overlay(self) -> bool:
        """Check if off-grid curtailment overlay should be applied."""
        from ..const import (
            CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
            CONF_POWERWALL_LOCAL_PAIRED,
            DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
        )
        if not self._entry:
            return False
        entry = self._entry
        enabled = entry.options.get(
            CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
            entry.data.get(
                CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
                DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
            ),
        )
        paired = entry.data.get(CONF_POWERWALL_LOCAL_PAIRED, False)
        battery_type = entry.data.get("battery_system", "")
        return bool(enabled and paired and battery_type == "tesla")

    def _apply_offgrid_overlay(
        self,
        schedule: "OptimizationSchedule",
        export_prices: list[float],
    ) -> "OptimizationSchedule":
        """Post-LP overlay: mark eligible slots as OFF_GRID.

        A slot is eligible when:
          - export_price < threshold (negative/zero value export)
          - LP action is self_consumption or idle (grid not actively needed)
          - projected SOC is at or above FULL threshold (battery can't
            absorb more — otherwise we should charge instead of curtail)

        Only marks contiguous runs of >= _OFFGRID_MIN_CONSECUTIVE slots.
        Inserts a reconnect buffer (self_consumption) before any CHARGE
        slot that follows an off-grid run.
        """
        actions = getattr(schedule, "actions", None)
        if not actions or not export_prices:
            return schedule

        # ScheduleAction.soc is a 0-1 fraction; the threshold constant is a
        # percentage, so compare against the fractional equivalent.
        soc_floor = self._OFFGRID_FULL_SOC_THRESHOLD / 100.0
        n = min(len(actions), len(export_prices))

        # Step 1: flag each slot as eligible
        eligible = []
        for t in range(n):
            action = actions[t]
            act = action.action
            price = export_prices[t] if t < len(export_prices) else 1.0
            soc = action.soc

            is_eligible = (
                price < self._OFFGRID_EXPORT_THRESHOLD
                and act in ("self_consumption", "idle")
                and soc is not None
                and soc >= soc_floor
            )
            eligible.append(is_eligible)

        # Step 2: find contiguous runs of eligible slots
        # and mark them as off_grid if long enough
        result = list(actions)
        t = 0
        while t < n:
            if not eligible[t]:
                t += 1
                continue
            # Find the end of this eligible run
            run_start = t
            while t < n and eligible[t]:
                t += 1
            run_end = t  # exclusive
            run_length = run_end - run_start

            if run_length < self._OFFGRID_MIN_CONSECUTIVE:
                continue  # Too short — skip

            # Check if a CHARGE slot follows — need reconnect buffer
            next_action = ""
            if run_end < len(actions):
                next_action = actions[run_end].action

            # Mark slots as off_grid
            mark_end = run_end
            if next_action == "charge" and run_length > 1:
                # Leave last slot as self_consumption (reconnect buffer)
                mark_end = run_end - 1

            for i in range(run_start, mark_end):
                slot = result[i]
                # ScheduleAction dataclass — create a copy with new action
                from .schedule_reader import ScheduleAction
                result[i] = ScheduleAction(
                    timestamp=slot.timestamp,
                    action="off_grid",
                    power_w=slot.power_w,
                    soc=slot.soc,
                    battery_charge_w=slot.battery_charge_w,
                    battery_discharge_w=slot.battery_discharge_w,
                )

        offgrid_count = sum(1 for s in result if s.action == "off_grid")
        if offgrid_count > 0:
            _LOGGER.info(
                "Off-grid overlay: marked %d/%d slots as OFF_GRID "
                "(export threshold=%.1fc, SOC floor=%d%%)",
                offgrid_count, n, self._OFFGRID_EXPORT_THRESHOLD * 100,
                self._OFFGRID_FULL_SOC_THRESHOLD,
            )

        schedule.actions = result
        return schedule

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
                # Record baseline to Amber usage coordinator for savings tracking
                try:
                    from ..const import DOMAIN
                    usage_coord = self.hass.data.get(DOMAIN, {}).get(
                        self.entry_id, {}
                    ).get("amber_usage_coordinator")
                    if usage_coord:
                        usage_coord.record_baseline(
                            date_str=self._last_cost_date,
                            baseline_cost=self._actual_baseline_today,
                        )
                except Exception as e:
                    _LOGGER.debug("Could not record baseline to usage coordinator: %s", e)
            self._actual_cost_today = 0.0
            self._actual_baseline_today = 0.0
            self._actual_import_kwh_today = 0.0
            self._actual_export_kwh_today = 0.0
            self._actual_charge_kwh_today = 0.0
            self._actual_discharge_kwh_today = 0.0
            self._actual_import_cost_today = 0.0
            self._actual_export_earnings_today = 0.0
            self._actual_grid_charge_kwh_today = 0.0
            self._actual_grid_charge_cost_today = 0.0
            self._actual_zerohero_import_kwh_today = 0.0
            self._actual_zerohero_export_kwh_today = 0.0
            self._actual_zerohero_bonus_export_kwh_today = 0.0
            self._actual_zerohero_base_export_earnings_today = 0.0
            self._actual_zerohero_bonus_export_earnings_today = 0.0
            self._actual_zerohero_credit_value_today = 0.0
            self._actual_zerocharge_import_kwh_today = 0.0
            self._actual_zerocharge_credit_value_today = 0.0
            self._baseline_zerohero_import_kwh_today = 0.0
            self._baseline_zerohero_bonus_export_kwh_today = 0.0
            self._baseline_zerohero_credit_value_today = 0.0
            self._baseline_zerocharge_import_kwh_today = 0.0
            self._baseline_zerocharge_credit_value_today = 0.0
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
        data = self._get_energy_data()
        if not data:
            _LOGGER.debug("Cost tracking skipped: no energy coordinator data")
            return
        if not self._last_import_prices or not self._last_export_prices:
            _LOGGER.debug("Cost tracking skipped: no cached prices yet")
            return

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
        grid_import_kwh = grid_import_kw * dt_hours
        grid_export_kwh = grid_export_kw * dt_hours
        actual_import_cost = grid_import_kwh * import_price
        actual_export_earnings = grid_export_kwh * export_price

        zerohero_config = self._zerohero_config()
        if zerohero_config is not None:
            settlement = settle_zerohero_series(
                zerohero_config,
                [now],
                [grid_import_kwh],
                [grid_export_kwh],
                [export_price],
                initial_bonus_kwh=self._actual_zerohero_bonus_export_kwh_today,
                initial_import_window_kwh=self._actual_zerohero_import_kwh_today,
                credit_already_applied=self._actual_zerohero_credit_value_today > 0,
            )
            actual_export_earnings = settlement.export_earnings
            self._actual_zerohero_import_kwh_today = settlement.import_window_kwh
            if zerohero_is_in_window(now, zerohero_config):
                self._actual_zerohero_export_kwh_today += grid_export_kwh
            self._actual_zerohero_bonus_export_kwh_today += settlement.bonus_export_kwh
            self._actual_zerohero_base_export_earnings_today += settlement.base_export_earnings
            self._actual_zerohero_bonus_export_earnings_today += settlement.bonus_export_earnings
            zerocharge_import, zerocharge_credit = settle_zerocharge_imports(
                zerohero_config,
                [now],
                [grid_import_kwh],
                [import_price],
                initial_import_kwh=self._actual_zerocharge_import_kwh_today,
            )
            self._actual_zerocharge_import_kwh_today = zerocharge_import
            self._actual_zerocharge_credit_value_today += zerocharge_credit
            actual_import_cost -= zerocharge_credit

        actual_cost = actual_import_cost - actual_export_earnings

        # Accumulate actual energy measurements
        self._actual_import_kwh_today += grid_import_kwh
        self._actual_export_kwh_today += grid_export_kwh
        self._actual_import_cost_today += actual_import_cost
        self._actual_export_earnings_today += actual_export_earnings

        # Track battery charge/discharge energy
        battery_charge_kw = max(0.0, -battery_power_kw)   # negative = charging
        battery_discharge_kw = max(0.0, battery_power_kw)  # positive = discharging
        self._actual_charge_kwh_today += battery_charge_kw * dt_hours
        self._actual_discharge_kwh_today += battery_discharge_kw * dt_hours

        # Grid-sourced portion of battery charging. With solar serving load and
        # battery first, the grid-charged power equals min(battery_charge,
        # grid_import): when solar covers the charge, grid_import is ~0; when it
        # does not, the shortfall is exactly the grid contribution. Costing only
        # this energy (not all household import, and not solar charging) gives
        # the true acquisition cost of stored grid energy.
        grid_charge_kw = min(battery_charge_kw, grid_import_kw)
        grid_charge_kwh = grid_charge_kw * dt_hours
        self._actual_grid_charge_kwh_today += grid_charge_kwh
        self._actual_grid_charge_cost_today += grid_charge_kwh * import_price

        # Baseline cost: what would happen without a battery
        # Power balance: load = solar + grid + battery (Tesla sign convention)
        # Without battery, net_grid = load - solar = grid_power + battery_power
        baseline_grid_kw = grid_power_kw + battery_power_kw
        baseline_import_kw = max(0.0, baseline_grid_kw)
        baseline_export_kw = max(0.0, -baseline_grid_kw)
        baseline_import_kwh = baseline_import_kw * dt_hours
        baseline_export_kwh = baseline_export_kw * dt_hours
        baseline_import_cost = baseline_import_kwh * import_price
        baseline_export_earnings = baseline_export_kwh * export_price
        if zerohero_config is not None:
            baseline_settlement = settle_zerohero_series(
                zerohero_config,
                [now],
                [baseline_import_kwh],
                [baseline_export_kwh],
                [export_price],
                initial_bonus_kwh=self._baseline_zerohero_bonus_export_kwh_today,
                initial_import_window_kwh=self._baseline_zerohero_import_kwh_today,
                credit_already_applied=self._baseline_zerohero_credit_value_today > 0,
            )
            baseline_export_earnings = baseline_settlement.export_earnings
            self._baseline_zerohero_import_kwh_today = baseline_settlement.import_window_kwh
            self._baseline_zerohero_bonus_export_kwh_today += baseline_settlement.bonus_export_kwh
            baseline_zerocharge_import, baseline_zerocharge_credit = (
                settle_zerocharge_imports(
                    zerohero_config,
                    [now],
                    [baseline_import_kwh],
                    [import_price],
                    initial_import_kwh=self._baseline_zerocharge_import_kwh_today,
                )
            )
            self._baseline_zerocharge_import_kwh_today = baseline_zerocharge_import
            self._baseline_zerocharge_credit_value_today += baseline_zerocharge_credit
            baseline_import_cost -= baseline_zerocharge_credit

        baseline_cost = baseline_import_cost - baseline_export_earnings

        if zerohero_config is not None:
            window_end = zerohero_window_end_for(now, zerohero_config)
            if (
                now >= window_end
                and self._actual_zerohero_credit_value_today <= 0
                and self._actual_zerohero_import_kwh_today
                <= zerohero_config.import_allowance_kwh + 1e-6
            ):
                self._actual_zerohero_credit_value_today = zerohero_config.credit_amount
                actual_cost -= zerohero_config.credit_amount
            if (
                now >= window_end
                and self._baseline_zerohero_credit_value_today <= 0
                and self._baseline_zerohero_import_kwh_today
                <= zerohero_config.import_allowance_kwh + 1e-6
            ):
                self._baseline_zerohero_credit_value_today = zerohero_config.credit_amount
                baseline_cost -= zerohero_config.credit_amount

        self._actual_cost_today += actual_cost
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

        zerohero_config = self._zerohero_config()
        if zerohero_config is not None:
            timestamps = self._price_timestamps(len(prices_import))
            predicted_import_kwh: list[float] = []
            predicted_export_kwh: list[float] = []
            predicted_export_prices: list[float] = []
            baseline_import_kwh: list[float] = []
            baseline_export_kwh: list[float] = []
            baseline_export_prices: list[float] = []
            future_timestamps: list[datetime] = []
            predicted_import_cost = 0.0
            baseline_import_cost = 0.0

            for step in range(1, steps_to_midnight + 1):
                idx = offset + step
                if (
                    idx >= len(grid_import_w)
                    or idx >= len(grid_export_w)
                    or idx >= len(prices_import)
                ):
                    break

                import_p = prices_import[idx]
                export_p = prices_export[idx] if idx < len(prices_export) else 0.05
                ts = timestamps[idx] if idx < len(timestamps) else now + timedelta(
                    minutes=step * self._config.interval_minutes
                )

                import_kwh = (grid_import_w[idx] / 1000) * dt_hours
                export_kwh = (grid_export_w[idx] / 1000) * dt_hours
                predicted_import_cost += import_p * import_kwh
                predicted_import_kwh.append(import_kwh)
                predicted_export_kwh.append(export_kwh)
                predicted_export_prices.append(export_p)
                future_timestamps.append(ts)

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
                base_import = max(0.0, net_load) * dt_hours
                base_export = max(0.0, -net_load) * dt_hours
                baseline_import_cost += import_p * base_import
                baseline_import_kwh.append(base_import)
                baseline_export_kwh.append(base_export)
                baseline_export_prices.append(export_p)

            predicted_settlement = settle_zerohero_series(
                zerohero_config,
                future_timestamps,
                predicted_import_kwh,
                predicted_export_kwh,
                predicted_export_prices,
                initial_bonus_kwh=self._actual_zerohero_bonus_export_kwh_today,
                initial_import_window_kwh=self._actual_zerohero_import_kwh_today,
                credit_already_applied=self._actual_zerohero_credit_value_today > 0,
                include_credit=True,
            )
            baseline_settlement = settle_zerohero_series(
                zerohero_config,
                future_timestamps,
                baseline_import_kwh,
                baseline_export_kwh,
                baseline_export_prices,
                initial_bonus_kwh=self._baseline_zerohero_bonus_export_kwh_today,
                initial_import_window_kwh=self._baseline_zerohero_import_kwh_today,
                credit_already_applied=self._baseline_zerohero_credit_value_today > 0,
                include_credit=True,
            )
            predicted_zerocharge_import, predicted_zerocharge_credit = (
                settle_zerocharge_imports(
                    zerohero_config,
                    future_timestamps,
                    predicted_import_kwh,
                    [
                        prices_import[
                            min(offset + idx + 1, len(prices_import) - 1)
                        ]
                        for idx in range(len(predicted_import_kwh))
                    ],
                    initial_import_kwh=self._actual_zerocharge_import_kwh_today,
                )
            )
            baseline_zerocharge_import, baseline_zerocharge_credit = (
                settle_zerocharge_imports(
                    zerohero_config,
                    future_timestamps,
                    baseline_import_kwh,
                    [
                        prices_import[
                            min(offset + idx + 1, len(prices_import) - 1)
                        ]
                        for idx in range(len(baseline_import_kwh))
                    ],
                    initial_import_kwh=self._baseline_zerocharge_import_kwh_today,
                )
            )
            predicted_cost = (
                predicted_import_cost
                - predicted_zerocharge_credit
                - predicted_settlement.export_earnings
                - predicted_settlement.credit_value
            )
            baseline_cost = (
                baseline_import_cost
                - baseline_zerocharge_credit
                - baseline_settlement.export_earnings
                - baseline_settlement.credit_value
            )
            return (predicted_cost, baseline_cost)

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

    def _display_grid_arrays_from_schedule(
        self,
        api_response: dict[str, list[Any]],
        raw_grid_import_w: list[float] | None,
        raw_grid_export_w: list[float] | None,
    ) -> tuple[list[float], list[float]]:
        """Build display grid arrays from the post-processed schedule."""
        timestamps = api_response.get("timestamps", [])
        n = len(timestamps)
        charge_w = api_response.get("charge_w", [])
        consume_w = api_response.get("battery_consume_w", [])
        export_w = api_response.get("battery_export_w", [])
        display_import: list[float] = []
        display_export: list[float] = []

        for idx in range(n):
            raw_import = (
                float(raw_grid_import_w[idx])
                if raw_grid_import_w is not None and idx < len(raw_grid_import_w)
                else 0.0
            )
            raw_export = (
                float(raw_grid_export_w[idx])
                if raw_grid_export_w is not None and idx < len(raw_grid_export_w)
                else 0.0
            )
            battery_charge = (
                float(charge_w[idx]) if idx < len(charge_w) and charge_w[idx] else 0.0
            )
            battery_consume = (
                float(consume_w[idx]) if idx < len(consume_w) and consume_w[idx] else 0.0
            )
            battery_export = (
                float(export_w[idx]) if idx < len(export_w) and export_w[idx] else 0.0
            )

            if (
                idx < len(getattr(self, "_last_solar_forecast", []) or [])
                and idx < len(getattr(self, "_last_load_forecast", []) or [])
            ):
                solar_w = max(
                    0.0,
                    float(self._last_solar_forecast[idx] or 0.0) * 1000.0,
                )
                load_w = max(
                    0.0,
                    float(self._last_load_forecast[idx] or 0.0) * 1000.0,
                )
                display_export.append(
                    round(
                        max(0.0, solar_w + battery_export - load_w - battery_charge),
                        1,
                    )
                )
                display_import.append(
                    round(max(0.0, load_w + battery_charge - solar_w - battery_consume), 1)
                )
                continue

            if battery_export <= 0:
                display_export.append(0.0)
            else:
                display_export.append(round(max(0.0, raw_export), 1))
            display_import.append(round(max(0.0, raw_import), 1))

        return display_import, display_export

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
            if key == "interval_minutes":
                value = FIXED_OPTIMIZATION_INTERVAL_MINUTES
            if key == "max_grid_import_w":
                value = self._normalize_optional_power_w(value)
            if key == "max_grid_export_w":
                value = self._normalize_optional_export_power_w(value)
            if key == "max_grid_charge_price":
                # Already normalized to $/kWh by the caller (set_settings /
                # config-flow / startup restore). Do NOT re-apply the cents->
                # dollars heuristic here: it is non-idempotent, so a valid cap
                # above $1/kWh (>100 c/kWh) would be divided by 100 a second
                # time (e.g. 150c -> $1.50 -> $0.015), silently disabling grid
                # charging. Just guard the type.
                value = self._coerce_optional_price(value)
            if key == "grid_charge_soc_cap":
                value = self._soc_ratio(value, 1.0)
            if hasattr(self._config, key):
                setattr(self._config, key, value)
        self._config.interval_minutes = FIXED_OPTIMIZATION_INTERVAL_MINUTES

        # Sync config to optimizer
        if self._optimizer:
            self._optimizer.update_config(
                capacity_wh=self._config.battery_capacity_wh,
                max_charge_w=self._config.max_charge_w,
                max_discharge_w=self._config.max_discharge_w,
                max_grid_import_w=self._config.max_grid_import_w,
                max_grid_export_w=self._config.max_grid_export_w,
                backup_reserve=self._config.backup_reserve,
                grid_charge_soc_cap=self._config.grid_charge_soc_cap,
                horizon_hours=self._config.horizon_hours,
            )
            self._optimizer.terminal_weight = self._profit_max_terminal_weight()
        if (
            "backup_reserve" in kwargs
            and self.energy_coordinator
            and hasattr(self.energy_coordinator, "set_min_soc_pct")
        ):
            self.energy_coordinator.set_min_soc_pct(
                self._config.backup_reserve * 100
            )

    @staticmethod
    def _normalize_optional_power_w(value: Any) -> int | None:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _normalize_optional_export_power_w(value: Any) -> int | None:
        if value in (None, "", []):
            return None
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _normalize_optional_price(value: Any) -> float | None:
        if value in (None, "", []):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        # Mobile/config flows expose cents/kWh. Internal prices are dollars/kWh.
        if parsed > 1:
            parsed = parsed / 100.0
        return parsed

    @staticmethod
    def _coerce_optional_price(value: Any) -> float | None:
        """Validate an already-normalized $/kWh price without unit conversion.

        Unlike _normalize_optional_price this applies NO cents->dollars
        heuristic, so it is safe to call on values that are already in dollars
        (idempotent) — used for stored config values that must not be scaled
        down a second time.
        """
        if value in (None, "", []):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _grid_charge_cap_import_prices(
        self,
        import_prices: list[float],
    ) -> list[float]:
        """Return the user-facing import prices used for hard grid-charge caps."""
        reference = getattr(self, "_last_grid_charge_cap_import_prices", None)
        if not reference:
            return import_prices

        cap_prices = list(reference[:len(import_prices)])
        if len(cap_prices) < len(import_prices):
            cap_prices.extend(import_prices[len(cap_prices):])
        return cap_prices

    def _grid_charge_allowed_slots(
        self,
        import_prices: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        current_soc: float,
    ) -> list[bool]:
        """Return per-slot permission for forced grid battery charging."""
        allowed = [True] * len(import_prices)
        # The stored config value is already $/kWh — coerce, do not re-normalize
        # (re-applying the cents heuristic would divide a >$1/kWh cap by 100).
        price_cap = self._coerce_optional_price(
            getattr(self._config, "max_grid_charge_price", None)
        )
        if price_cap is not None:
            for idx, price in enumerate(import_prices):
                try:
                    if float(price) > price_cap + 1e-9:
                        allowed[idx] = False
                except (TypeError, ValueError):
                    continue

        zerohero_config = self._zerohero_config()
        if zerohero_config is not None and zerohero_config.zerocharge_enabled:
            remaining_zerocharge_kwh = max(
                0.0,
                zerohero_config.zerocharge_import_cap_kwh
                - self._actual_zerocharge_import_kwh_today,
            )
            zerocharge_slots = (
                self._zerocharge_window_slots(len(import_prices))
                if remaining_zerocharge_kwh > 1e-6
                else [False] * len(import_prices)
            )
            allowed = [
                bool(is_allowed) and bool(is_zerocharge)
                for is_allowed, is_zerocharge in zip(
                    allowed,
                    zerocharge_slots,
                    strict=False,
                )
            ]

        return allowed

    async def force_reoptimize(self) -> Any:
        """Force immediate re-optimization."""
        await self._run_optimization(force=True)
        return self._current_schedule

    @staticmethod
    def _settings_groups() -> dict[str, Any]:
        """Return non-breaking mobile metadata for grouped optimizer settings."""
        return {
            "optimizer": {
                "title": "Smart Optimization",
                "collapsed": False,
                "fields": [
                    "enabled",
                    "backup_reserve",
                    "hardware_backup_reserve",
                    "profit_max_enabled",
                    "charge_by_time_enabled",
                    "charge_by_time_target_time",
                    "charge_by_time_target_soc",
                    "load_entity",
                    "planned_ev_load_entity",
                    "battery_capacity_wh",
                    "max_charge_w",
                    "max_discharge_w",
                ],
            },
            "advanced_optimizer": {
                "title": "Advanced optimizer controls",
                "collapsed": True,
                "fields": [
                    "allow_grid_charge",
                    "max_grid_charge_price",
                    "grid_charge_soc_cap",
                    "max_grid_import_w",
                    "max_grid_export_w",
                    "spread_import_enabled",
                    "spread_export_enabled",
                    "disable_idle_enabled",
                    "auto_apply_reserve_enabled",
                ],
            },
        }

    def get_forecast_data(self) -> dict[str, Any]:
        """Get forecast data for LP forecast sensors.

        Returns summary values (for sensor state) and full arrays (for attributes).
        """
        data: dict[str, Any] = {
            "available": self._last_solar_forecast is not None,
            "solar_nowcast_derate": round(self._solar_nowcast_derate, 3),
        }
        if self._last_solar_nowcast_ratio is not None:
            data["solar_nowcast_ratio"] = round(self._last_solar_nowcast_ratio, 3)
        dt_h = self._config.interval_minutes / 60

        if self._last_solar_forecast:
            data["solar_forecast_kwh"] = sum(self._last_solar_forecast) * dt_h
            data["solar_peak_kw"] = max(self._last_solar_forecast)
            data["solar_forecast"] = self._last_solar_forecast

        if self._last_load_forecast:
            data["load_forecast_kwh"] = sum(self._last_load_forecast) * dt_h
            data["load_peak_kw"] = max(self._last_load_forecast)
            data["load_forecast"] = self._last_load_forecast
            load_summary = self._summarise_load_forecast()
            if load_summary:
                data["load_today_remaining_kwh"] = load_summary["today_remaining_kwh"]
                data["load_tomorrow_kwh"] = load_summary["tomorrow_kwh"]
                data["load_hourly_today_remaining"] = load_summary["hourly_today_remaining"]
                data["load_hourly_tomorrow"] = load_summary["hourly_tomorrow"]
                data["load_temperature_adjusted"] = load_summary["temperature_adjusted"]
                data["load_away_mode"] = load_summary["away_mode"]
                data["load_away_in_recovery"] = load_summary.get("away_in_recovery", False)
                data["load_away_enabled_at"] = load_summary.get("away_enabled_at")
                data["load_away_disabled_at"] = load_summary.get("away_disabled_at")
                data["load_away_recovery_remaining_hours"] = load_summary.get("away_recovery_remaining_hours")
                data["profit_max_mode"] = load_summary.get("profit_max_mode", False)

        if self._last_planned_ev_load_forecast_w:
            planned_kw = [
                round(value / 1000.0, 3)
                for value in self._last_planned_ev_load_forecast_w
            ]
            data["planned_ev_load_forecast_w"] = self._last_planned_ev_load_forecast_w
            data["planned_ev_load_peak_kw"] = max(planned_kw) if planned_kw else 0.0
            data["planned_ev_load_kwh"] = sum(planned_kw) * dt_h

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

        if self._current_schedule and self._current_schedule.actions:
            charge_kw = [
                -round((action.battery_charge_w or 0.0) / 1000.0, 3)
                for action in self._current_schedule.actions
            ]
            discharge_kw = [
                round((action.battery_discharge_w or 0.0) / 1000.0, 3)
                for action in self._current_schedule.actions
            ]
            net_kw = [
                round(discharge_kw[i] + charge_kw[i], 3)
                for i in range(len(charge_kw))
            ]
            data["battery_power_now_kw"] = net_kw[0] if net_kw else 0.0
            data["battery_charge_peak_kw"] = abs(min(charge_kw)) if charge_kw else 0.0
            data["battery_discharge_peak_kw"] = max(discharge_kw) if discharge_kw else 0.0
            data["battery_schedule_available"] = True
            data["battery_charge_forecast"] = charge_kw
            data["battery_discharge_forecast"] = discharge_kw
            data["battery_power_forecast"] = net_kw

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
        actual_battery_power_w = self._get_actual_battery_power_w()
        current_power_w = actual_battery_power_w
        planned_current_action = current_action
        planned_current_power_w = current_power_w
        effective_current_action = current_action
        current_action_end_time = None  # When the current scheduled action segment ends
        next_action = "idle"
        next_action_time = None
        next_action_power_w = 0

        if self._current_schedule and self._current_schedule.actions:
            ca = self._get_current_action()
            if ca:
                current_action = ca.action
                current_power_w = ca.power_w
                planned_current_action = current_action
                planned_current_power_w = current_power_w
                runtime_current_action = self._effective_runtime_action(
                    planned_current_action,
                    ca.timestamp,
                )
                force_state = self._get_active_force_state()
                force_type = force_state.get("type") if force_state.get("active") else None
                last_executed_action = getattr(self, "_last_executed_action", None)
                last_executed_planned_action = getattr(
                    self,
                    "_last_executed_planned_action",
                    None,
                )
                if force_type in ("charge", "discharge"):
                    effective_current_action = (
                        "charge" if force_type == "charge" else "discharge"
                    )
                    current_action = effective_current_action
                    try:
                        force_power_w = float(force_state.get("power_w") or 0)
                    except (TypeError, ValueError):
                        force_power_w = 0
                    if force_power_w > 0:
                        current_power_w = force_power_w
                elif (
                    last_executed_action
                    and last_executed_planned_action == planned_current_action
                ):
                    effective_current_action = last_executed_action
                    current_action = effective_current_action
                    if current_action in ("charge", "discharge", "export"):
                        force_type = (
                            "charge"
                            if current_action == "charge"
                            else "discharge"
                        )
                        force_state = self._optimizer_force_state or {}
                        if (
                            force_state.get("active")
                            and force_state.get("type") == force_type
                        ):
                            try:
                                force_power_w = float(force_state.get("power_w") or 0)
                            except (TypeError, ValueError):
                                force_power_w = 0
                            if force_power_w > 0:
                                current_power_w = force_power_w
                    if current_action in ("idle", "no_discharge", "self_consumption"):
                        current_power_w = actual_battery_power_w
                else:
                    effective_current_action = runtime_current_action or current_action
                    current_action = effective_current_action
                    if current_action in ("idle", "no_discharge", "self_consumption"):
                        current_power_w = actual_battery_power_w

            now = dt_util.now()

            # First future action of any type tells us when the current segment ends.
            # That's a separate concern from "next different action" — the existing
            # next_action field skips ahead past long self_consumption stretches,
            # which is useful but reads as misleading without an "until" timestamp.
            for a in self._current_schedule.actions:
                if a.timestamp > now:
                    current_action_end_time = a.timestamp.isoformat()
                    break

            # Find next different action (used by the Next Scheduled Change sensor)
            for a in self._current_schedule.actions:
                runtime_next_action = self._effective_runtime_action(
                    a.action,
                    a.timestamp,
                )
                if a.timestamp > now and a.action != planned_current_action:
                    next_action = runtime_next_action
                    next_action_time = a.timestamp.isoformat()
                    next_action_power_w = (
                        actual_battery_power_w
                        if runtime_next_action in (
                            "idle",
                            "no_discharge",
                            "self_consumption",
                        )
                        else a.power_w
                    )
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
            lp_stats.update(getattr(self._last_optimizer_result, "lp_stats", {}) or {})

        reserve_recommendation = (
            getattr(self._last_optimizer_result, "reserve_recommendation", {}) or {}
            if self._last_optimizer_result
            else {}
        )
        if reserve_recommendation:
            reserve_recommendation = dict(reserve_recommendation)
            reserve_recommendation["auto_apply_enabled"] = self.auto_apply_reserve_enabled
            manual_reserve = self.manual_backup_reserve
            if manual_reserve is not None:
                reserve_recommendation["manual_optimizer_reserve_percent"] = int(
                    round(manual_reserve * 100)
                )
            reserve_recommendation.setdefault(
                "applied_optimizer_reserve_percent",
                int(round(self._config.backup_reserve * 100)),
            )

        # Read monitoring mode from config entry
        from ..const import CONF_MONITORING_MODE
        monitoring_mode = False
        if self._entry:
            monitoring_mode = self._entry.options.get(
                CONF_MONITORING_MODE, self._entry.data.get(CONF_MONITORING_MODE, False)
            )

        data = {
            "success": True,
            "enabled": self._enabled,
            "monitoring_mode": monitoring_mode,
            "optimizer_available": optimizer_available,
            "engine_available": optimizer_available,
            "engine": "built-in",
            "status_message": status_message,
            "cost_function": self._cost_function.value,
            "spread_export_enabled": self._config.spread_export_enabled,
            "spread_import_enabled": self._config.spread_import_enabled,
            "disable_idle_enabled": self.disable_idle_enabled,
            "profit_max_enabled": self.profit_max_mode,
            "profit_max_mode": self.profit_max_mode,
            "charge_by_time_enabled": self.charge_by_time_enabled,
            "auto_apply_reserve_enabled": self.auto_apply_reserve_enabled,
            "manual_backup_reserve": self.manual_backup_reserve,
            "backup_reserve": self._config.backup_reserve,
            "settings_groups": self._settings_groups(),
            "idle_hold_active": (
                self._last_executed_action == "idle"
                and self._idle_hold_reserve is not None
            ),
            "idle_hold_reserve": (
                self._idle_hold_reserve / 100
                if self._idle_hold_reserve is not None
                else None
            ),
            "idle_hold_reserve_percent": (
                self._idle_hold_reserve
                if self._idle_hold_reserve is not None
                else None
            ),
            "status": "active" if self._enabled and optimizer_available else "disabled",
            "optimization_status": "active" if optimizer_available else "not_available",
            "current_action": current_action,
            "current_power_w": current_power_w,
            "planned_current_action": planned_current_action,
            "planned_current_power_w": planned_current_power_w,
            "effective_current_action": effective_current_action,
            "actual_battery_power_w": actual_battery_power_w,
            "current_action_end_time": current_action_end_time,
            "next_action": next_action,
            "next_action_time": next_action_time,
            "next_action_power_w": next_action_power_w,
            "last_optimization": self._last_update_time.isoformat() if self._last_update_time else None,
            "predicted_cost": self._get_daily_cost(),
            "predicted_savings": self._get_daily_savings(),
            "lp_stats": lp_stats,
            "reserve_recommendation": reserve_recommendation,
            "config": {
                "battery_capacity_wh": self._config.battery_capacity_wh,
                "max_charge_w": self._config.max_charge_w,
                "max_discharge_w": self._config.max_discharge_w,
                "max_grid_import_w": self._config.max_grid_import_w,
                "max_grid_export_w": self._config.max_grid_export_w,
                "max_grid_charge_price": (
                    round(self._config.max_grid_charge_price * 100, 3)
                    if self._config.max_grid_charge_price is not None
                    else 0
                ),
                "grid_charge_soc_cap": int(
                    round(self._soc_ratio(self._config.grid_charge_soc_cap, 1.0) * 100)
                ),
                "allow_grid_charge": self._config.allow_grid_charge,
                "spread_export_enabled": self._config.spread_export_enabled,
                "spread_import_enabled": self._config.spread_import_enabled,
                "disable_idle_enabled": self.disable_idle_enabled,
                "profit_max_enabled": self.profit_max_mode,
                "charge_by_time_enabled": self.charge_by_time_enabled,
                "charge_by_time_target_time": self._config.charge_by_time_target_time,
                "charge_by_time_target_soc": int(
                    round(self._charge_by_time_target_soc() * 100)
                ),
                "profit_max_target_time": self._config.charge_by_time_target_time,
                "profit_max_target_soc": int(
                    round(self._charge_by_time_target_soc() * 100)
                ),
                "auto_apply_reserve_enabled": self.auto_apply_reserve_enabled,
                "manual_backup_reserve": self.manual_backup_reserve,
                "battery_specs_source": self._battery_specs_source,
                "backup_reserve": self._config.backup_reserve,
                "hardware_backup_reserve": (self._startup_backup_reserve if self._startup_backup_reserve is not None else 0) / 100,
                "idle_hold_active": (
                    self._last_executed_action == "idle"
                    and self._idle_hold_reserve is not None
                ),
                "idle_hold_reserve": (
                    self._idle_hold_reserve / 100
                    if self._idle_hold_reserve is not None
                    else None
                ),
                "idle_hold_reserve_percent": (
                    self._idle_hold_reserve
                    if self._idle_hold_reserve is not None
                    else None
                ),
                "interval_minutes": self._config.interval_minutes,
                "horizon_hours": self._config.horizon_hours,
                "planned_ev_load_entity": self._planned_ev_load_entity_id,
            },
            "features": {
                "ev_integration": self._ev_integration_enabled or len(self._ev_configs) > 0,
                "planned_ev_load": bool(self._planned_ev_load_entity_id),
                "spread_export": self._should_spread_export_schedule(),
                "spread_import": self._should_spread_import_schedule(),
                "vpp_enabled": False,
                "built_in_optimizer": True,
            },
            "warnings": self._get_warnings(),
        }

        # Add load forecast summary for mobile app
        load_summary = self._summarise_load_forecast()
        if load_summary:
            data["forecast_summary"] = {
                "load_today_remaining_kwh": load_summary["today_remaining_kwh"],
                "load_tomorrow_kwh": load_summary["tomorrow_kwh"],
                "load_peak_kw": load_summary["peak_kw"],
                "temperature_adjusted": load_summary["temperature_adjusted"],
                "away_mode": load_summary["away_mode"],
                "profit_max_mode": load_summary.get("profit_max_mode", False),
                "charge_by_time_enabled": load_summary.get("charge_by_time_enabled", False),
            }

        if self._last_planned_ev_load_forecast_w:
            dt_h = self._config.interval_minutes / 60
            data["planned_ev_load_forecast_w"] = self._last_planned_ev_load_forecast_w
            data["planned_ev_load_peak_kw"] = round(
                max(self._last_planned_ev_load_forecast_w) / 1000.0,
                3,
            )
            data["planned_ev_load_kwh"] = round(
                sum(self._last_planned_ev_load_forecast_w) / 1000.0 * dt_h,
                3,
            )

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
            "zerohero": self._zerohero_cost_breakdown(),
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
                grid_import_w, grid_export_w = self._display_grid_arrays_from_schedule(
                    api_response,
                    self._last_optimizer_result.grid_import_w,
                    self._last_optimizer_result.grid_export_w,
                )
                api_response["grid_import_w"] = grid_import_w
                api_response["grid_export_w"] = grid_export_w
            # Add price arrays for pricing overlay (use actual tariff rates, not LP-adjusted)
            n_sched = len(api_response["timestamps"])
            display_import = self._last_display_import_prices or self._last_import_prices
            display_export = self._last_display_export_prices or self._last_export_prices
            if display_import:
                api_response["import_price"] = display_import[:n_sched]
            if display_export:
                api_response["export_price"] = display_export[:n_sched]
            if self._last_planned_ev_load_forecast_w:
                api_response["planned_ev_load_w"] = (
                    self._last_planned_ev_load_forecast_w[:n_sched]
                )
            # Debug: log SOC range for API response
            soc_vals = api_response.get("soc", [])
            if soc_vals:
                _DECISION_LOGGER.debug(
                    "Schedule API: %d points, SOC range %.2f-%.2f (first=%.4f, last=%.4f)",
                    len(soc_vals), min(soc_vals), max(soc_vals),
                    soc_vals[0], soc_vals[-1],
                )

            data["schedule"] = api_response

            # Add EV charging power overlay from the same source the LP uses
            if self._ev_integration_enabled:
                n_sched_pts = len(api_response["timestamps"])
                ev_load_w = self._get_ev_planned_load(n_sched_pts)
                if ev_load_w:
                    api_response["ev_charging_w"] = ev_load_w
                elif self._ev_coordinator and data.get("ev"):
                    # Fallback: use EVCoordinator's real-time charging plan
                    ev_power = [0.0] * n_sched_pts
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
                    if any(v > 0 for v in ev_power):
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

            # Add Amber usage data (actual metered costs) if available
            try:
                from ..const import DOMAIN as _DOMAIN
                usage_coord = self.hass.data.get(_DOMAIN, {}).get(
                    self.entry_id, {}
                ).get("amber_usage_coordinator")
                if usage_coord:
                    data["amber_usage"] = {
                        "yesterday": usage_coord.get_savings_summary("yesterday"),
                        "week": usage_coord.get_savings_summary("week"),
                        "month": usage_coord.get_savings_summary("month"),
                        "last_fetch": usage_coord.last_fetch_iso,
                    }
            except Exception:
                pass  # Non-critical — don't break API response

            # Add demand window config for chart overlay
            demand_window = self._get_demand_window_config()
            if demand_window:
                data["demand_window"] = demand_window

            # Consolidate schedule into action ranges for the next 24h
            # e.g. [self_consumption 16:00-17:00, export 17:00-21:00, ...]
            intervals_24h = min(
                int(24 * 60 / self._config.interval_minutes),
                len(self._current_schedule.actions),
            )
            action_ranges: list[dict[str, Any]] = []
            interval_delta = timedelta(minutes=self._config.interval_minutes)
            for a in self._current_schedule.actions[:intervals_24h]:
                ad = a.to_dict()
                runtime_action = self._effective_runtime_action(
                    ad.get("action"),
                    a.timestamp,
                )
                if runtime_action != ad.get("action"):
                    ad["planned_action"] = ad.get("action")
                    ad["action"] = runtime_action
                # end_time = end of this interval (start + duration).
                # Use the raw datetime (a.timestamp) since ad["timestamp"]
                # is already an ISO string from to_dict().
                interval_end = (a.timestamp + interval_delta).isoformat()
                if (
                    action_ranges
                    and action_ranges[-1]["action"] == ad["action"]
                ):
                    # Extend the current range — update end SOC
                    action_ranges[-1]["end_time"] = interval_end
                    action_ranges[-1]["soc"] = ad["soc"]
                    if ad["power_w"]:
                        power_vals = action_ranges[-1].setdefault("_powers", [])
                        power_vals.append(ad["power_w"])
                        action_ranges[-1]["power_w"] = max(power_vals)
                else:
                    # Start a new range — soc is the START of this period
                    # (previous range's end SOC, or current battery SOC for first)
                    start_soc = ad["soc"]
                    if action_ranges:
                        # Use previous range's end SOC as this range's start
                        start_soc = action_ranges[-1]["soc"]
                    action_ranges.append({
                        "action": ad["action"],
                        **(
                            {"planned_action": ad["planned_action"]}
                            if ad.get("planned_action")
                            else {}
                        ),
                        "timestamp": ad["timestamp"],
                        "end_time": interval_end,
                        "power_w": ad["power_w"],
                        "soc": start_soc,
                        "_powers": [ad["power_w"]] if ad["power_w"] else [],
                    })
            # Clean up internal _powers list before sending
            for ar in action_ranges:
                ar.pop("_powers", None)
            data["next_actions"] = action_ranges

        # Add calibration status
        from ..const import DOMAIN as _CAL_DOMAIN
        _cal_entry_data = self.hass.data.get(_CAL_DOMAIN, {}).get(self.entry_id, {})
        data["calibration_suspected"] = _cal_entry_data.get("calibration_suspected", False)
        _cal_detected_at = _cal_entry_data.get("calibration_detected_at")
        data["calibration_detected_at"] = _cal_detected_at.isoformat() if _cal_detected_at else None

        return data

    async def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update optimization settings from API."""
        response = {"success": True, "changes": []}
        rerun_after_settings = False

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
                persisted_changed = new_options.get(CONF_OPTIMIZATION_ENABLED) != enabled
                new_options[CONF_OPTIMIZATION_ENABLED] = enabled
                # Prevent reload from API-driven options update — only when this
                # write actually changes persisted state, otherwise HA never
                # fires the update listener to consume the flag and it is left
                # stuck for the next (unrelated) structural options change.
                from ..const import DOMAIN as _SKIP_DOM
                if persisted_changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)

        if "auto_apply_reserve_enabled" in settings:
            changed = await self.set_auto_apply_reserve_enabled(
                bool(settings["auto_apply_reserve_enabled"]),
                rerun=False,
            )
            response["changes"].append(
                f"auto_apply_reserve_enabled: {settings['auto_apply_reserve_enabled']}"
            )
            if changed:
                rerun_after_settings = True

        if "manual_backup_reserve" in settings:
            manual_reserve = self._reserve_ratio(settings["manual_backup_reserve"])
            if manual_reserve is not None:
                self._manual_backup_reserve = manual_reserve
                self._config.manual_backup_reserve = manual_reserve
                self._persist_optimizer_reserve_settings(
                    manual_reserve=manual_reserve
                )
                response["changes"].append(
                    f"manual_backup_reserve: {int(round(manual_reserve * 100))}%"
                )

        # Handle cost function
        if "cost_function" in settings:
            try:
                self.set_cost_function(settings["cost_function"])
                response["changes"].append(f"cost_function: {settings['cost_function']}")

                if self._entry:
                    from ..const import CONF_OPTIMIZATION_COST_FUNCTION
                    new_data = dict(self._entry.data)
                    persisted_changed = (
                        new_data.get(CONF_OPTIMIZATION_COST_FUNCTION)
                        != settings["cost_function"]
                    )
                    new_data[CONF_OPTIMIZATION_COST_FUNCTION] = settings["cost_function"]
                    # Prevent reload from API-driven options update — only when
                    # this write actually changes persisted state (see the
                    # "enabled" toggle above for why an unconditional set is a
                    # bug: HA never fires the listener for a no-op write, so a
                    # stale flag would swallow the next real structural reload).
                    from ..const import DOMAIN as _SKIP_DOM
                    if persisted_changed:
                        self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                    self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            except ValueError as e:
                response["success"] = False
                response["error"] = f"Invalid cost function: {e}"
                return response

        # Handle config updates
        config_keys = [
            "battery_capacity_wh", "max_charge_w", "max_discharge_w",
            "max_grid_import_w", "max_grid_export_w",
            "max_grid_charge_price", "grid_charge_soc_cap",
            "allow_grid_charge", "backup_reserve", "horizon_hours",
        ]
        config_updates = {k: v for k, v in settings.items() if k in config_keys}
        if "interval_minutes" in settings:
            self._config.interval_minutes = FIXED_OPTIMIZATION_INTERVAL_MINUTES
        if config_updates:
            # Convert backup_reserve from percentage (0-100) to decimal (0-1)
            if "backup_reserve" in config_updates:
                reserve = config_updates["backup_reserve"]
                if reserve > 1:
                    config_updates["backup_reserve"] = reserve / 100
            if "max_grid_charge_price" in config_updates:
                config_updates["max_grid_charge_price"] = (
                    self._normalize_optional_price(
                        config_updates["max_grid_charge_price"]
                    )
                )
            if "grid_charge_soc_cap" in config_updates:
                config_updates["grid_charge_soc_cap"] = self._soc_ratio(
                    config_updates["grid_charge_soc_cap"],
                    1.0,
                )
            if "horizon_hours" in config_updates:
                try:
                    horizon_hours = int(float(config_updates["horizon_hours"]))
                except (TypeError, ValueError):
                    config_updates.pop("horizon_hours", None)
                else:
                    if horizon_hours > 0:
                        config_updates["horizon_hours"] = horizon_hours
                    else:
                        config_updates.pop("horizon_hours", None)

            self.update_config(**config_updates)
            response["changes"].append(f"config: {list(config_updates.keys())}")
            rerun_after_settings = True

            # Persist settings to config entry
            if self._entry:
                from ..const import (
                    CONF_OPTIMIZATION_BACKUP_RESERVE,
                    CONF_OPTIMIZATION_MANUAL_RESERVE,
                    CONF_OPTIMIZATION_HORIZON,
                    CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
                    CONF_OPTIMIZATION_ALLOW_GRID_CHARGE,
                    CONF_OPTIMIZATION_MAX_CHARGE_W,
                    CONF_OPTIMIZATION_MAX_DISCHARGE_W,
                    CONF_OPTIMIZATION_MAX_GRID_IMPORT_W,
                    CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
                    CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE,
                    CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP,
                )
                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                _persisted_before = (dict(new_data), dict(new_options))
                if "backup_reserve" in settings:
                    reserve_value = settings["backup_reserve"]
                    if reserve_value > 1:
                        reserve_value = reserve_value / 100
                    new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = reserve_value
                    new_options[CONF_OPTIMIZATION_BACKUP_RESERVE] = reserve_value
                    self._manual_backup_reserve = reserve_value
                    self._config.manual_backup_reserve = reserve_value
                    new_data[CONF_OPTIMIZATION_MANUAL_RESERVE] = reserve_value
                    new_options[CONF_OPTIMIZATION_MANUAL_RESERVE] = reserve_value
                    rerun_after_settings = True
                if "horizon_hours" in settings:
                    try:
                        horizon_hours = int(float(settings["horizon_hours"]))
                    except (TypeError, ValueError):
                        horizon_hours = None
                    if horizon_hours is not None and horizon_hours > 0:
                        new_data[CONF_OPTIMIZATION_HORIZON] = horizon_hours
                        new_options[CONF_OPTIMIZATION_HORIZON] = horizon_hours
                if "battery_capacity_wh" in settings:
                    new_options[CONF_OPTIMIZATION_BATTERY_CAPACITY_WH] = int(settings["battery_capacity_wh"])
                if "max_charge_w" in settings:
                    new_options[CONF_OPTIMIZATION_MAX_CHARGE_W] = int(settings["max_charge_w"])
                if "max_discharge_w" in settings:
                    new_options[CONF_OPTIMIZATION_MAX_DISCHARGE_W] = int(settings["max_discharge_w"])
                if "max_grid_import_w" in settings:
                    grid_import_w = self._normalize_optional_power_w(
                        settings["max_grid_import_w"]
                    )
                    if grid_import_w is None:
                        new_options.pop(CONF_OPTIMIZATION_MAX_GRID_IMPORT_W, None)
                        new_data.pop(CONF_OPTIMIZATION_MAX_GRID_IMPORT_W, None)
                    else:
                        new_options[CONF_OPTIMIZATION_MAX_GRID_IMPORT_W] = grid_import_w
                if "max_grid_export_w" in settings:
                    grid_export_w = self._normalize_optional_export_power_w(
                        settings["max_grid_export_w"]
                    )
                    if grid_export_w is None:
                        new_options.pop(CONF_OPTIMIZATION_MAX_GRID_EXPORT_W, None)
                        new_data.pop(CONF_OPTIMIZATION_MAX_GRID_EXPORT_W, None)
                    else:
                        new_options[CONF_OPTIMIZATION_MAX_GRID_EXPORT_W] = grid_export_w
                if "max_grid_charge_price" in settings:
                    price_cap = self._normalize_optional_price(
                        settings["max_grid_charge_price"]
                    )
                    if price_cap is None:
                        new_options.pop(CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE, None)
                        new_data.pop(CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE, None)
                    else:
                        new_options[CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE] = price_cap
                        new_data[CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE] = price_cap
                if "grid_charge_soc_cap" in settings:
                    soc_cap = self._soc_ratio(settings["grid_charge_soc_cap"], 1.0)
                    new_options[CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP] = soc_cap
                    new_data[CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP] = soc_cap
                if "allow_grid_charge" in settings:
                    new_options[CONF_OPTIMIZATION_ALLOW_GRID_CHARGE] = bool(settings["allow_grid_charge"])
                # Prevent reload from API-driven options update — only when
                # this write actually changes persisted state (see the
                # "enabled" toggle above for why an unconditional set is a bug).
                from ..const import DOMAIN as _SKIP_DOM
                if (new_data, new_options) != _persisted_before:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )

            # Mark as manual when user explicitly sets battery specs
            if any(k in settings for k in ("battery_capacity_wh", "max_charge_w", "max_discharge_w")):
                self._battery_specs_source = "manual"

        # Handle hardware backup reserve
        if "hardware_backup_reserve" in settings:
            hw_reserve = settings["hardware_backup_reserve"]
            if hw_reserve > 1:
                hw_reserve = hw_reserve / 100.0
            hw_int = int(hw_reserve * 100)
            self._startup_backup_reserve = hw_int
            if self._optimizer:
                self._optimizer.update_hardware_reserve(hw_reserve)
            # Persist to config entry
            if self._entry:
                from ..const import CONF_HARDWARE_BACKUP_RESERVE
                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                _persisted_before = (dict(new_data), dict(new_options))
                new_data[CONF_HARDWARE_BACKUP_RESERVE] = hw_reserve
                new_options[CONF_HARDWARE_BACKUP_RESERVE] = hw_reserve
                new_options.pop("_user_backup_reserve", None)
                # Prevent reload from API-driven options update — only when
                # this write actually changes persisted state (see the
                # "enabled" toggle above for why an unconditional set is a bug).
                from ..const import DOMAIN as _SKIP_DOM
                if (new_data, new_options) != _persisted_before:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )
            response["changes"].append(f"hardware_backup_reserve: {hw_int}%")

        # Handle profit maximisation mode toggle
        if "profit_max_enabled" in settings:
            new_val = bool(settings["profit_max_enabled"])
            changed = self.set_profit_max_mode(new_val)
            if changed:
                response["changes"].append(f"profit_max_enabled: {settings['profit_max_enabled']}")
                rerun_after_settings = True

        if "charge_by_time_enabled" in settings:
            new_val = bool(settings["charge_by_time_enabled"])
            changed = self.set_charge_by_time_enabled(new_val)
            if changed:
                response["changes"].append(
                    f"charge_by_time_enabled: {settings['charge_by_time_enabled']}"
                )
                rerun_after_settings = True

        if "spread_export_enabled" in settings:
            new_val = bool(settings["spread_export_enabled"])
            changed = self.set_spread_export_enabled(new_val)
            if changed:
                response["changes"].append(f"spread_export_enabled: {settings['spread_export_enabled']}")
                rerun_after_settings = True

        if "spread_import_enabled" in settings:
            new_val = bool(settings["spread_import_enabled"])
            changed = self.set_spread_import_enabled(new_val)
            if changed:
                response["changes"].append(f"spread_import_enabled: {settings['spread_import_enabled']}")
                rerun_after_settings = True

        if "disable_idle_enabled" in settings:
            new_val = bool(settings["disable_idle_enabled"])
            changed = self.set_disable_idle_enabled(new_val)
            if changed:
                response["changes"].append(
                    f"disable_idle_enabled: {self.disable_idle_enabled}"
                )
                rerun_after_settings = True

        if "planned_ev_load_entity" in settings:
            raw_entity = settings.get("planned_ev_load_entity")
            entity_id = raw_entity.strip() if isinstance(raw_entity, str) else None
            entity_id = entity_id or None
            changed = entity_id != self._planned_ev_load_entity_id
            self._planned_ev_load_entity_id = entity_id
            if self._entry:
                from ..const import CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY
                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                new_data[CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY] = entity_id
                new_options[CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY] = entity_id
                from ..const import DOMAIN as _SKIP_DOM
                # Only when this write actually changes persisted state (see
                # the "enabled" toggle above for why an unconditional set is a
                # bug).
                if changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )
            response["changes"].append(
                f"planned_ev_load_entity: {entity_id or 'cleared'}"
            )
            if changed:
                rerun_after_settings = True

        if "load_entity" in settings:
            raw_entity = settings.get("load_entity")
            entity_id = raw_entity.strip() if isinstance(raw_entity, str) else None
            entity_id = entity_id or None
            changed = entity_id != self._configured_load_entity_id
            self._configured_load_entity_id = entity_id
            if self._entry:
                from ..const import CONF_OPTIMIZATION_LOAD_ENTITY
                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                new_data[CONF_OPTIMIZATION_LOAD_ENTITY] = entity_id
                new_options[CONF_OPTIMIZATION_LOAD_ENTITY] = entity_id
                from ..const import DOMAIN as _SKIP_DOM
                # Only when this write actually changes persisted state (see
                # the "enabled" toggle above for why an unconditional set is a
                # bug).
                if changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )
            if changed and self._load_estimator:
                self._load_estimator.load_entity_id = self._get_load_entity_id()
                self._load_estimator._history_cache.clear()
                self._load_estimator._cache_time = None
                rerun_after_settings = True
            response["changes"].append(
                f"load_entity: {entity_id or 'auto-discovery'}"
            )

        target_time_key = (
            "charge_by_time_target_time"
            if "charge_by_time_target_time" in settings
            else "profit_max_target_time"
            if "profit_max_target_time" in settings
            else None
        )
        if target_time_key and self._entry:
            from ..const import (
                CONF_CHARGE_BY_TIME_TARGET_TIME,
                CONF_PROFIT_MAX_TARGET_TIME,
            )
            target_time = str(settings[target_time_key])
            changed = target_time != getattr(
                self._config,
                "charge_by_time_target_time",
                target_time,
            )
            self._config.charge_by_time_target_time = target_time
            new_data = dict(self._entry.data)
            new_options = dict(self._entry.options)
            new_data[CONF_CHARGE_BY_TIME_TARGET_TIME] = target_time
            new_options[CONF_CHARGE_BY_TIME_TARGET_TIME] = target_time
            new_data[CONF_PROFIT_MAX_TARGET_TIME] = target_time
            new_options[CONF_PROFIT_MAX_TARGET_TIME] = target_time
            from ..const import DOMAIN as _SKIP_DOM
            # Only when this write actually changes persisted state (see the
            # "enabled" toggle above for why an unconditional set is a bug).
            if changed:
                self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(
                self._entry,
                data=new_data,
                options=new_options,
            )
            response["changes"].append(f"{target_time_key}: {target_time}")
            if changed:
                rerun_after_settings = True

        target_soc_key = (
            "charge_by_time_target_soc"
            if "charge_by_time_target_soc" in settings
            else "profit_max_target_soc"
            if "profit_max_target_soc" in settings
            else None
        )
        if target_soc_key:
            target_soc = self._soc_ratio(settings[target_soc_key], 1.0)
            changed = not math.isclose(
                self._config.charge_by_time_target_soc,
                target_soc,
                abs_tol=0.0001,
            )
            self._config.charge_by_time_target_soc = target_soc
            if self._entry:
                from ..const import (
                    CONF_CHARGE_BY_TIME_TARGET_SOC,
                    CONF_PROFIT_MAX_TARGET_SOC,
                )
                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                new_data[CONF_CHARGE_BY_TIME_TARGET_SOC] = target_soc
                new_options[CONF_CHARGE_BY_TIME_TARGET_SOC] = target_soc
                new_data[CONF_PROFIT_MAX_TARGET_SOC] = target_soc
                new_options[CONF_PROFIT_MAX_TARGET_SOC] = target_soc
                from ..const import DOMAIN as _SKIP_DOM
                # Only when this write actually changes persisted state (see
                # the "enabled" toggle above for why an unconditional set is a
                # bug).
                if changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )
            response["changes"].append(
                f"{target_soc_key}: {int(round(target_soc * 100))}%"
            )
            if changed:
                rerun_after_settings = True

        # Handle EV integration toggle
        if "ev_integration" in settings:
            ev_enabled = settings["ev_integration"]
            self._ev_integration_enabled = ev_enabled
            if self._entry:
                from ..const import CONF_OPTIMIZATION_EV_INTEGRATION
                new_options = dict(self._entry.options)
                persisted_changed = new_options.get(CONF_OPTIMIZATION_EV_INTEGRATION) != ev_enabled
                new_options[CONF_OPTIMIZATION_EV_INTEGRATION] = ev_enabled
                # Prevent reload from API-driven options update — only when
                # this write actually changes persisted state (see the
                # "enabled" toggle above for why an unconditional set is a bug).
                from ..const import DOMAIN as _SKIP_DOM
                if persisted_changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(self.entry_id, {})["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)
                response["changes"].append(f"ev_integration: {ev_enabled}")

        if rerun_after_settings and getattr(self, "_enabled", False):
            self._schedule_settings_reoptimization()

        return response

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic data update — return cached API data.

        LP optimization is driven exclusively by _schedule_polling_loop and
        _initial_opt_task; running it here as well caused duplicate Modbus
        writes when both fired at the same 5-min boundary.
        """
        await self._execute_cached_current_action_if_changed()
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
        min_power_w: int = 1400,
    ) -> bool:
        """Add an EV charger to smart charging coordination.

        Args:
            entity_id: HA entity ID of the EV charger
            name: Friendly name for the charger
            max_power_w: Maximum charging power in watts
            target_soc: Target state of charge (0-1)
            departure_time: Time when car needs to be ready (HH:MM)
            price_threshold: Max $/kWh for smart charging
            min_power_w: Minimum charging power in watts (vehicle-specific)

        Returns:
            True if added successfully
        """
        if min_power_w <= 0 or min_power_w > max_power_w:
            _LOGGER.error(
                "Invalid EV power bounds for %s: min_power_w=%s, max_power_w=%s",
                entity_id, min_power_w, max_power_w,
            )
            return False

        config = EVConfig(
            entity_id=entity_id,
            name=name or entity_id.split(".")[-1],
            max_charging_power_w=max_power_w,
            min_charging_power_w=min_power_w,
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
